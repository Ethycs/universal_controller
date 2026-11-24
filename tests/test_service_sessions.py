"""Site sessions: cap/LRU/busy, persistence, independent stop, sweep protection,
streaming, errors and shutdown. No browser: a fake window, runtime, page and
adapter, real threads and the real ``LeaseStore``/``SessionManager``."""
import asyncio
import concurrent.futures as cf
import itertools
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from uc_browser.service.errors import ServiceError
from uc_browser.service.leases import LeaseStore
from uc_browser.service.sessions import (
    SessionManager,
    SessionStore,
    SiteSpec,
    builtin_sites,
    grok_conversation_key,
    parse_sites,
)
from uc_browser.service import site_api
from uc_browser.service.site_api import StreamSink, _ClientGone, run_unstreamed
from uc_browser.service.windows import ConfigError

PROJECT = "https://grok.com/project/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def cid(n):
    return f"00000000-0000-4000-8000-{n:012x}"


def conv(n):
    return f"{PROJECT}?chat={cid(n)}"


def classic(n):
    return f"https://grok.com/c/{cid(n)}"


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Gate:
    """Hold an operation inside the adapter until released (or its page closes)."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, adapter, result=None):
        self.entered.set()
        end = time.monotonic() + 10
        while not self.release.is_set():
            if adapter.page.is_closed():
                raise RuntimeError("Target closed")
            if time.monotonic() > end:
                raise AssertionError("gate was never released")
            time.sleep(0.005)
        return {} if result is None else result


# ── fakes ─────────────────────────────────────────────────────────────────


class FakePage:
    def __init__(self, window, target):
        self.window, self.target = window, target
        self.url = "about:blank"
        self.closed = threading.Event()

    def is_closed(self):
        return self.closed.is_set()

    def close(self):
        self.window.events.append(("page-closed", self.target))
        self.closed.set()
        self.window.pages.pop(self.target, None)


class FakeWindow:
    """Window and, for its owner/worker jobs, the runtime those jobs receive."""

    def __init__(self, name="grok"):
        self.spec = SimpleNamespace(
            name=name, browser="chromium", engine="extension", queue_timeout_s=5.0,
            offers=["attach", "control", "fetch", "cookies", "snapshot", "mhtml"],
        )
        self.running = False
        self.generation = 0
        self.pages = {}
        self.events = []
        self.work_threads = []
        self._seq = 0
        self._lock = threading.RLock()
        self.pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="fake-work")

    @property
    def cdp(self):
        return "ws://127.0.0.1:1/devtools/browser/x" if self.running else None

    # runtime interface (rt is the window itself)
    @property
    def context(self):
        return self

    def new_page(self):
        with self._lock:
            self._seq += 1
            page = FakePage(self, f"t{self._seq}")
            self.pages[page.target] = page
            return page

    def target_id(self, page):
        return page.target

    def resolve(self, target):
        page = self.pages.get(target)
        if page is None:
            raise ServiceError("no_such_target", "closed", 404)
        return page

    def close_target(self, target):
        self.resolve(target).close()

    def pages_info(self):
        with self._lock:
            return [{"target_id": p.target, "url": p.url, "age_s": 0} for p in list(self.pages.values())]

    # window interface
    def start(self):
        with self._lock:
            if not self.running:
                self.running = True
                self.generation += 1
        return self.info()

    def owner(self, fn, *, launch=True):
        future = cf.Future()
        try:
            if launch:
                self.start()
            elif not self.running:
                raise ServiceError("window_stopped", "not running", 503)
            future.set_result(fn(self))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    def work(self, fn, **kwargs):
        return self.pool.submit(self._work, fn)

    def _work(self, fn):
        self.work_threads.append(threading.current_thread().name)
        return fn(self)

    def info(self):
        return {
            "browser": "chromium", "engine": "extension", "running": self.running,
            "offers": self.spec.offers, "cdp": self.cdp, "queue_timeout_s": self.spec.queue_timeout_s,
            "pages": self.pages_info(),
        }

    def crash(self):
        with self._lock:
            self.running = False
            for page in list(self.pages.values()):
                page.closed.set()
            self.pages.clear()


class FakeWindows:
    def __init__(self, window, config=None):
        self.windows = {window.spec.name: window}
        self.config = config or {}
        self.closed = 0

    def get(self, name):
        if name not in self.windows:
            raise ServiceError("no_such_window", "unknown", 404)
        return self.windows[name]

    def missing_engine_files(self):
        return []

    def start_eager(self):
        return {}

    def close(self):
        self.closed += 1


class SessionRuntime:
    """What the session thread owns; refuses use from any other thread."""

    def __init__(self, window):
        self.window = window
        self.generation = window.generation
        self.thread = threading.current_thread()
        self.target = None
        self.stopped_on = None

    def _own(self):
        assert threading.current_thread() is self.thread, "Playwright object used off its own thread"

    @property
    def alive(self):
        return self.window.running and self.window.generation == self.generation

    def resolve(self, target):
        self._own()
        self.target = target
        return self.window.resolve(target)

    def stop_playwright(self):
        self._own()
        self.stopped_on = threading.current_thread()
        self.window.events.append(("runtime-stopped", self.target))


class FakeAdapter:
    def __init__(self, script, runtime, page, **options):
        if script.adapter_error is not None:
            raise script.adapter_error
        self.script, self.runtime, self.page, self.options = script, runtime, page, options
        self.bound = None
        script.adapters.append(self)

    @property
    def url(self):
        return self.bound

    def execute(self, op, kwargs, on_delta=None):
        self.runtime._own()
        self.script.calls.append((op, dict(kwargs), threading.current_thread().name, self.page.target))
        handler = self.script.handlers.get(op)
        if handler is not None:
            return handler(self, kwargs, on_delta)
        if op == "send":
            url = kwargs.get("conversation_url") or conv(next(self.script.counter))
            return {"response": "ok", "url": url, "conversation_id": None}
        if op in ("delete", "archive"):
            return True
        return {"op": op}


class Script:
    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.adapters = []
        self.runtimes = []
        self.stop_calls = []
        self.counter = itertools.count(1)
        self.adapter_error = None

    def stop_page(self, page):
        self.stop_calls.append((page.target, threading.current_thread().name))
        return True

    def sends(self):
        return [c for c in self.calls if c[0] == "send"]


OPS = frozenset({"send", "read", "list_conversations", "delete", "archive", "new_chat", "regenerate",
                 "stop", "cleanup_threads"})


def make_spec(script):
    return SiteSpec(
        name="grok",
        adapter=lambda runtime, page, **options: FakeAdapter(script, runtime, page, **options),
        operations=OPS,
        generating=frozenset({"send", "regenerate"}),
        session_url_ops=frozenset({"send"}),
        bind_ops={"send": "url"},
        reset_ops=frozenset({"new_chat"}),
        end_ops=frozenset({"delete", "archive"}),
        bulk_end_ops={"cleanup_threads": "deleted_ids"},
        stop_op="stop",
        stop_page=script.stop_page,
        conversation_key=grok_conversation_key,
        options=frozenset({"folder", "folder_url"}),
    )


class Env:
    def __init__(self, tmp_path):
        self.window = FakeWindow("grok")
        self.windows = FakeWindows(self.window)
        self.leases = LeaseStore(self.windows, identity=lambda pid: None)
        self.script = Script()
        self.clock = Clock()
        self.path = tmp_path / "state" / "sessions.json"
        self.sites = {"grok": {"window": "grok", "max_sessions": 2, "max_concurrent_sends": 1,
                               "session_idle_s": 60}}
        self.runtime_error = None
        self.runtime_hold = None  # an Event: new session threads wait on it before attaching
        self.managers = []

    def _runtime(self, window):
        if self.runtime_hold is not None:
            assert self.runtime_hold.wait(10), "runtime_hold was never released"
        if self.runtime_error is not None:
            raise self.runtime_error
        runtime = SessionRuntime(window)
        self.script.runtimes.append(runtime)
        return runtime

    def manager(self, **overrides):
        kwargs = dict(
            sites=json.loads(json.dumps(self.sites)), specs={"grok": make_spec(self.script)},
            store_path=self.path, runtime_factory=self._runtime, clock=self.clock, wall=self.clock,
        )
        kwargs.update(overrides)
        manager = SessionManager(self.windows, self.leases, **kwargs)
        self.managers.append(manager)
        return manager

    def target(self, manager, sid):
        return manager._live[("grok", sid)].target

    def holders(self):
        return {p["holder"] for p in self.leases.window_info("grok")["pages"]}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    for manager in e.managers:
        manager.close()
    e.window.pool.shutdown(wait=False)


# ── configuration ─────────────────────────────────────────────────────────


def test_site_config_is_validated_when_the_manager_is_built(env):
    bad = [
        ["grok"],
        {"nope": {"window": "grok"}},
        {"grok": []},
        {"grok": {}},
        {"grok": {"window": "missing"}},
        {"grok": {"window": "grok", "max_sessions": 0}},
        {"grok": {"window": "grok", "max_sessions": True}},
        {"grok": {"window": "grok", "max_concurrent_sends": 1.5}},
        {"grok": {"window": "grok", "session_idle_s": 0}},
        {"grok": {"window": "grok", "session_idle_s": float("inf")}},
        {"grok": {"window": "grok", "typo": 1}},
        {"grok": {"window": "grok", "folder": ""}},
        {"grok": {"window": "grok", "folder_url": "ftp://grok.com/project/x"}},
    ]
    for sites in bad:
        with pytest.raises(ConfigError):
            env.manager(sites=sites)
    env.window.spec.offers = ["fetch", "cookies", "snapshot"]  # no attach, no control
    with pytest.raises(ConfigError):
        env.manager()


def test_invalid_grok_folder_configuration_is_rejected(env):

    specs = builtin_sites()
    for options in ({"folder": "NSFW"}, {"folder": "NSFW", "folder_url": "http://grok.com/project/x"},
                    {"folder_url": "https://grok.com/"}):
        with pytest.raises(ConfigError):
            parse_sites({"grok": {"window": "grok", **options}}, env.windows, specs)


def test_unknown_site_and_operation_and_bad_arguments_are_refused(env):
    manager = env.manager()
    for call, status, code in [
        (lambda: manager.submit("nope", "send", {}), 404, "no_such_site"),
        (lambda: manager.submit("grok", "frobnicate", {}), 404, "no_such_op"),
        (lambda: manager.submit("grok", "_op_send", {}), 404, "no_such_op"),
        (lambda: manager.submit("grok", "send", {"x": float("nan")}), 400, "bad_arguments"),
        (lambda: manager.submit("grok", "send", {"x": object()}), 400, "bad_arguments"),
        (lambda: manager.submit("grok", "send", ["not", "an", "object"]), 400, "bad_arguments"),
        (lambda: manager.submit("grok", "send", {}, session_id=""), 400, "bad_arguments"),
        (lambda: manager.submit("grok", "send", {}, session_id=7), 400, "bad_arguments"),
        (lambda: manager.submit("grok", "send", {}, wait="yes"), 400, "bad_arguments"),
        (lambda: manager.list_sessions("nope"), 404, "no_such_site"),
        (lambda: manager.forget("grok", "ghost"), 404, "no_such_session"),
    ]:
        with pytest.raises(ServiceError) as exc:
            call()
        assert (exc.value.status, exc.value.code) == (status, code)
    assert env.script.calls == [] and not env.window.pages


def test_default_map_path_and_environment_override(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.delenv("UC_SESSIONS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert env.manager(store_path=None)._store.path == Path("data/sessions.json")
    monkeypatch.setenv("UC_SESSIONS_FILE", str(tmp_path / "elsewhere.json"))
    assert env.manager(store_path=None)._store.path == tmp_path / "elsewhere.json"
    # No configured site: nothing is read or created.
    assert env.manager(sites={}, store_path=None)._store is None


# ── persistence ───────────────────────────────────────────────────────────


def test_map_holds_only_url_and_last_use_and_rebuilds_after_a_restart(env):
    manager = env.manager()
    result = manager.submit("grok", "send", {"message": "SECRET-PROMPT"}, "chat-a").result(5)
    url = result["url"]
    text = env.path.read_text()
    assert json.loads(text) == {
        "version": 1, "sessions": {"grok": {"chat-a": {"url": url, "last_used": env.clock.now}}}}
    assert "SECRET-PROMPT" not in text and "response" not in text
    assert [p.name for p in env.path.parent.iterdir()] == ["sessions.json"]  # no temp file left behind
    manager.close()

    restarted = env.manager()
    assert restarted.list_sessions("grok")["chat-a"] == {"url": url, "live": False, "idle_s": 0.0}
    env.script.calls.clear()
    restarted.submit("grok", "send", {"message": "again"}, "chat-a").result(5)
    assert env.script.sends()[0][1] == {"message": "again", "conversation_url": url}


def test_explicit_conversation_url_wins_and_null_asks_for_a_new_conversation(env):
    manager = env.manager()
    first = manager.submit("grok", "send", {"message": "1"}, "s").result(5)["url"]
    manager.submit("grok", "send", {"message": "2", "conversation_url": conv(90)}, "s").result(5)
    assert env.script.sends()[-1][1]["conversation_url"] == conv(90)
    assert manager.list_sessions("grok")["s"]["url"] == conv(90) != first
    result = manager.submit("grok", "send", {"message": "3", "conversation_url": None}, "s").result(5)
    assert env.script.sends()[-1][1]["conversation_url"] is None
    assert manager.list_sessions("grok")["s"]["url"] == result["url"] != conv(90)


def test_adapter_url_is_the_fallback_when_the_result_has_no_url(env):
    def send(adapter, kwargs, on_delta):
        adapter.bound = conv(5)
        return {"response": "ok"}

    env.script.handlers["send"] = send
    manager = env.manager()
    manager.submit("grok", "send", {"message": "m"}, "s").result(5)
    assert manager.list_sessions("grok")["s"]["url"] == conv(5)


def test_only_success_or_a_partial_timeout_updates_the_map(env):
    manager = env.manager()

    def failing(adapter, kwargs, on_delta):
        adapter.bound = "https://grok.com/sign-in"  # whatever the page shows is not a conversation
        raise ServiceError("auth_required", "signed out", 401)

    env.script.handlers["send"] = failing
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "m"}, "s").result(5)
    assert (exc.value.status, exc.value.code) == (401, "auth_required")
    assert not env.path.exists()

    def partial(adapter, kwargs, on_delta):
        err = ServiceError("timeout", "half a reply", 504)
        err.data = {"partial": "half a reply", "result": {"response": "half a reply", "url": conv(3)}}
        raise err

    env.script.handlers["send"] = partial
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "m"}, "s").result(5)
    assert (exc.value.status, exc.value.code, exc.value.detail) == (504, "timeout", "half a reply")
    assert manager.list_sessions("grok")["s"]["url"] == conv(3)
    assert json.loads(env.path.read_text())["sessions"]["grok"]["s"]["url"] == conv(3)


def test_deleting_or_archiving_clears_every_matching_entry_and_a_new_chat_resets(env):
    env.sites["grok"]["max_sessions"] = 3
    manager = env.manager()
    for sid in ("s1", "s2"):
        manager.submit("grok", "send", {"message": sid}, sid).result(5)
    assert [manager.list_sessions("grok")[s]["url"] for s in ("s1", "s2")] == [conv(1), conv(2)]

    # A failed delete changes nothing; a successful one clears the entry whichever URL form named it.
    env.script.handlers["delete"] = lambda a, k, d: False
    manager.submit("grok", "delete", {"conversation_url": conv(2)}, "s2").result(5)
    assert manager.list_sessions("grok")["s2"]["url"] == conv(2)
    env.script.handlers.pop("delete")
    manager.submit("grok", "delete", {"conversation_url": classic(1)}, "s2").result(5)
    entries = manager.list_sessions("grok")
    assert entries["s1"]["url"] is None and entries["s2"]["url"] == conv(2)
    manager.submit("grok", "archive", {"conversation_url": cid(2)}, "s1").result(5)  # a bare id also names it
    assert manager.list_sessions("grok")["s2"]["url"] is None

    # Bulk cleanup lists the conversation ids it removed.
    manager.submit("grok", "send", {"message": "a"}, "s3").result(5)
    manager.submit("grok", "send", {"message": "b"}, "s1").result(5)
    s3_url, s1_url = (manager.list_sessions("grok")[s]["url"] for s in ("s3", "s1"))
    env.script.handlers["cleanup_threads"] = lambda a, k, d: {
        "summary": "ok", "deleted_ids": [cid(int(s3_url[-12:], 16))]}
    manager.submit("grok", "cleanup_threads", {"keep_recent": 0}, "s2").result(5)
    entries = manager.list_sessions("grok")
    assert entries["s3"]["url"] is None and entries["s1"]["url"] == s1_url

    # A new chat starts the session over: the next send is not given the old URL.
    manager.submit("grok", "new_chat", {}, "s1").result(5)
    assert manager.list_sessions("grok")["s1"]["url"] is None
    manager.submit("grok", "send", {"message": "fresh"}, "s1").result(5)
    assert "conversation_url" not in env.script.sends()[-1][1]


def test_store_write_is_atomic_and_a_corrupt_file_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    store = SessionStore(path, wall=lambda: 1.0)
    store.touch("grok", "a", url=conv(1))
    before = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(ServiceError) as exc:
        store.touch("grok", "a", url=conv(2))
    assert exc.value.code == "session_store_failed"
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["sessions.json"]

    # A failed write leaves memory ahead of the file (the conversation exists); the
    # next successful write catches the file up. URLs that are not plain http(s)
    # are never persisted.
    store.touch("grok", "a", url="javascript:alert(1)")
    store.touch("grok", "b", url="https://user:pw@grok.com/c/x")
    assert SessionStore(path).entries("grok") == {"a": (conv(2), 1.0)}

    path.write_text("{not json")
    with pytest.raises(ConfigError):
        SessionStore(path)
    path.write_text(json.dumps({"version": 1, "sessions": {"grok": {"a": {"url": 5, "last_used": 1}}}}))
    with pytest.raises(ConfigError):
        SessionStore(path)


def test_conversation_key_reads_every_grok_url_form():
    assert grok_conversation_key(classic(4)) == cid(4)
    assert grok_conversation_key(conv(4)) == cid(4)
    assert grok_conversation_key(cid(4).upper()) == cid(4)
    assert grok_conversation_key("https://grok.com/") is None
    assert grok_conversation_key(PROJECT) is None


# ── cap, LRU, busy ────────────────────────────────────────────────────────


def test_cap_evicts_the_least_recently_used_idle_session_and_keeps_its_map_entry(env):
    manager = env.manager()
    for sid in ("a", "b"):
        env.clock.advance(1)
        manager.submit("grok", "send", {"message": sid}, sid).result(5)
    env.clock.advance(1)
    manager.submit("grok", "send", {"message": "a again"}, "a").result(5)  # b is now the LRU
    env.clock.advance(1)
    manager.submit("grok", "send", {"message": "c"}, "c").result(5)

    entries = manager.list_sessions("grok")
    assert {sid: e["live"] for sid, e in entries.items()} == {"a": True, "b": False, "c": True}
    assert entries["b"]["url"] is not None  # a normal close keeps the map
    assert env.holders() == {"session:grok/a", "session:grok/c"}
    stopped = [r for r in env.script.runtimes if r.stopped_on is not None]
    assert len(stopped) == 1 and stopped[0].stopped_on is stopped[0].thread  # on its own thread

    env.script.calls.clear()
    manager.submit("grok", "send", {"message": "b again"}, "b").result(5)  # rebuilt from the map
    assert env.script.sends()[0][1]["conversation_url"] == entries["b"]["url"]


def test_busy_sessions_are_never_evicted_and_admission_fails_fast_or_waits_for_a_slot(env):
    gates = {"a": Gate(), "b": Gate()}
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gates[kwargs["tag"]].run(
        adapter, {"tag": kwargs["tag"]})
    manager = env.manager()
    a = manager.submit("grok", "read", {"tag": "a"}, "a")
    b = manager.submit("grok", "read", {"tag": "b"}, "b")
    assert gates["a"].entered.wait(5) and gates["b"].entered.wait(5)

    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "list_conversations", {}, "c", wait=False)
    assert (exc.value.status, exc.value.code) == (503, "busy")
    env.window.spec.queue_timeout_s = 0.2
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "list_conversations", {}, "c")  # waits, then the admission deadline passes
    assert exc.value.code == "busy"
    assert {e["live"] for e in manager.list_sessions("grok").values()} == {True}

    env.window.spec.queue_timeout_s = 5.0
    admitted = {}
    thread = threading.Thread(
        target=lambda: admitted.update(call=manager.submit("grok", "list_conversations", {}, "c")))
    thread.start()
    wait_until(lambda: manager._slot_waiters == 1)
    gates["a"].release.set()  # a finishes, becomes idle, is evicted for c
    assert a.result(5) == {"tag": "a"}
    thread.join(5)
    assert not thread.is_alive()
    assert admitted["call"].result(5) == {"op": "list_conversations"}
    live = {sid: e["live"] for sid, e in manager.list_sessions("grok").items()}
    assert live == {"b": True, "c": True} or live == {"a": False, "b": True, "c": True}
    gates["b"].release.set()
    assert b.result(5) == {"tag": "b"}


def test_operations_on_one_session_run_in_order_on_one_thread_and_wait_false_is_busy(env):
    order, gate = [], Gate()

    def read(adapter, kwargs, on_delta):
        order.append(("start", kwargs["tag"]))
        if kwargs["tag"] == "1":
            gate.run(adapter)
        order.append(("end", kwargs["tag"]))
        return {}

    env.script.handlers["read"] = read
    manager = env.manager()
    first = manager.submit("grok", "read", {"tag": "1"}, "s")
    assert gate.entered.wait(5)
    second = manager.submit("grok", "read", {"tag": "2"}, "s")
    assert len(manager._live[("grok", "s")].tasks) == 1  # queued, not running
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "read", {"tag": "3"}, "s", wait=False)
    assert (exc.value.status, exc.value.code) == (503, "busy")
    gate.release.set()
    first.result(5)
    second.result(5)
    assert order == [("start", "1"), ("end", "1"), ("start", "2"), ("end", "2")]
    assert len({c[2] for c in env.script.calls}) == 1  # one session thread


def test_one_account_send_at_a_time_fifo_for_waiters_and_busy_for_wait_false(env):
    env.sites["grok"]["max_sessions"] = 3
    lock, state, starts, first = threading.Lock(), {"now": 0, "max": 0}, [], Gate()

    def send(adapter, kwargs, on_delta):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            starts.append(kwargs["message"])
        try:
            if kwargs["message"] == "first":
                first.run(adapter)
            return {"response": kwargs["message"], "url": conv(len(starts))}
        finally:
            with lock:
                state["now"] -= 1

    env.script.handlers["send"] = send
    manager = env.manager()
    permits = manager._permits["grok"]
    c1 = manager.submit("grok", "send", {"message": "first"}, "s1")
    assert first.entered.wait(5)

    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "refused"}, "s2", wait=False)
    assert (exc.value.status, exc.value.code) == (503, "busy")
    assert "s2" not in manager.list_sessions("grok")  # refused before a session or page was made

    c2 = manager.submit("grok", "send", {"message": "second"}, "s2")
    wait_until(lambda: len(permits._waiting) == 1)
    c3 = manager.submit("grok", "send", {"message": "third"}, "s3")
    wait_until(lambda: len(permits._waiting) == 2)
    assert starts == ["first"]
    first.release.set()
    for call in (c1, c2, c3):
        call.result(5)
    assert starts == ["first", "second", "third"] and state["max"] == 1
    wait_until(lambda: permits._free == 1)

    # With a free permit a wait=False send is admitted, takes it, and returns it.
    manager.submit("grok", "send", {"message": "now"}, "s1", wait=False).result(5)
    wait_until(lambda: permits._free == 1)


def test_caller_leaving_cancels_queued_and_permit_waiting_work_but_not_running_work(env):
    gate = Gate()
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gate.run(adapter, {"tag": kwargs["tag"]})
    manager = env.manager()
    running = manager.submit("grok", "read", {"tag": "1"}, "s")
    assert gate.entered.wait(5)
    queued = manager.submit("grok", "read", {"tag": "2"}, "s")
    queued.abandon()
    running.abandon()
    assert queued.future.cancelled() and not running.future.cancelled()
    gate.release.set()
    assert running.result(5) == {"tag": "1"}
    wait_until(lambda: not manager._live[("grok", "s")].tasks)
    assert [c[1]["tag"] for c in env.script.calls if c[0] == "read"] == ["1"]

    # A send waiting for the account permit is cancelled the same way and never starts.
    holder = Gate()
    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: (
        holder.run(adapter, {"response": "x", "url": conv(1)}) if kwargs["message"] == "holder"
        else {"response": "late", "url": conv(2)})
    first = manager.submit("grok", "send", {"message": "holder"}, "p1")
    assert holder.entered.wait(5)
    waiting = manager.submit("grok", "send", {"message": "abandoned"}, "p2")
    permits = manager._permits["grok"]
    wait_until(lambda: len(permits._waiting) == 1)
    waiting.abandon()
    with pytest.raises(ServiceError) as exc:
        waiting.result(5)
    assert exc.value.code == "cancelled"
    holder.release.set()
    first.result(5)
    assert [c[1]["message"] for c in env.script.sends()] == ["holder"]
    wait_until(lambda: permits._free == 1)


# ── independent stop ──────────────────────────────────────────────────────


def test_stop_runs_on_a_work_worker_and_never_queues_behind_the_send(env):
    gate = Gate()
    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: gate.run(
        adapter, {"response": "x", "url": conv(7)})
    manager = env.manager()
    send = manager.submit("grok", "send", {"message": "long", "conversation_url": classic(7)}, "s1")
    assert gate.entered.wait(5)
    other = manager.submit("grok", "list_conversations", {}, "s2")  # an idle neighbour
    other.result(5)
    pages = set(env.window.pages)

    assert manager.submit("grok", "stop", {}, "s1").result(5) is True  # by session id
    # By conversation URL alone, in another URL form: the send is still in flight, the map empty.
    assert manager.submit("grok", "stop", {"conversation_url": conv(7)}).result(5) is True
    assert not send.future.done()
    assert [c[0] for c in env.script.calls if c[2] == env.script.calls[0][2]] == ["send"]
    assert "stop" not in [c[0] for c in env.script.calls]  # never reached an adapter
    s1_page = env.target(manager, "s1")
    assert [target for target, _ in env.script.stop_calls] == [s1_page] * 2
    assert all(thread.startswith("fake-work") for _, thread in env.script.stop_calls)
    assert set(env.window.pages) == pages  # no page created, none navigated or closed

    # Nothing is stopped for conversations or sessions that are not running here.
    for call in (
        manager.submit("grok", "stop", {"conversation_url": conv(8)}),
        manager.submit("grok", "stop", {"conversation_url": conv(8)}, "s1"),  # s1 is known to hold conv(7)
        manager.submit("grok", "stop", {}, "ghost"),
        manager.submit("grok", "stop", {}),  # no "default" session exists
    ):
        assert call.result(5) is False
    assert len(env.script.stop_calls) == 2
    gate.release.set()
    send.result(5)


def test_stop_by_url_matches_the_page_url_before_the_map_knows_it(env):
    gate = Gate()
    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: gate.run(
        adapter, {"response": "x", "url": conv(9)})
    manager = env.manager()
    send = manager.submit("grok", "send", {"message": "new conversation"}, "s1")
    assert gate.entered.wait(5)
    assert manager.list_sessions("grok")["s1"]["url"] is None
    assert manager.submit("grok", "stop", {"conversation_url": conv(9)}).result(5) is False
    env.window.pages[env.target(manager, "s1")].url = classic(9)  # Grok redirected the page
    assert manager.submit("grok", "stop", {"conversation_url": conv(9)}).result(5) is True
    assert manager.submit("grok", "stop", {"conversation_url": conv(10)}).result(5) is False
    assert len(env.script.stop_calls) == 1
    gate.release.set()
    send.result(5)


def test_stop_with_an_unreadable_page_is_not_an_error_and_only_touches_the_named_session(env):
    gate = Gate()
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gate.run(adapter)
    manager = env.manager()
    busy = manager.submit("grok", "read", {}, "s1")
    assert gate.entered.wait(5)
    manager.submit("grok", "list_conversations", {}, "s2").result(5)
    target = env.target(manager, "s1")
    assert manager.submit("grok", "stop", {}, "s1").result(5) is True
    assert [t for t, _ in env.script.stop_calls] == [target]  # s2's page was left alone
    env.window.pages[target].close()  # the tab is closed out from under the session
    assert manager.submit("grok", "stop", {}, "s1").result(5) is False
    with pytest.raises(ServiceError):
        busy.result(5)


# ── leases and the sweep ──────────────────────────────────────────────────


def test_session_pages_survive_the_zero_client_sweep_and_closing_removes_the_lease(env):
    manager = env.manager()
    manager.submit("grok", "send", {"message": "m"}, "s1").result(5)
    target = env.target(manager, "s1")
    raw = env.window.new_page()
    raw.url = "https://other.test/"

    assert env.leases.internal == {("grok", target): "session:grok/s1"}
    env.leases.reap()  # no registered client: unheld raw pages are swept
    assert raw.target not in env.window.pages
    assert target in env.window.pages and not env.window.pages[target].is_closed()
    listed = {p["target_id"]: p["holder"] for p in env.leases.window_info("grok")["pages"]}
    assert listed[target] == "session:grok/s1"

    manager.forget("grok", "s1")
    assert target not in env.window.pages
    assert env.leases.internal == {}
    assert ("grok", target) not in env.leases.leases


def test_fetch_style_internal_pages_still_have_no_holder(env):
    target = env.leases.internal_page("grok")
    assert env.leases.internal[("grok", target)] is None
    assert {p["target_id"]: p["holder"] for p in env.leases.window_info("grok")["pages"]}[target] is None


def test_idle_sweep_closes_old_idle_sessions_keeps_the_map_and_rebuilds(env):
    manager = env.manager()
    url = manager.submit("grok", "send", {"message": "m"}, "s1").result(5)["url"]
    env.clock.advance(59)
    assert manager.sweep() == 0
    env.clock.advance(2)
    assert manager.sweep() == 1
    assert env.window.pages == {} or all(p.is_closed() for p in env.window.pages.values())
    entry = manager.list_sessions("grok")["s1"]
    assert entry["live"] is False and entry["url"] == url and entry["idle_s"] == 61.0
    assert env.script.runtimes[0].stopped_on is not None

    env.script.calls.clear()
    manager.submit("grok", "send", {"message": "again"}, "s1").result(5)
    assert env.script.sends()[0][1]["conversation_url"] == url
    assert len(env.script.runtimes) == 2  # a fresh thread and attachment

    gate = Gate()
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gate.run(adapter)
    busy = manager.submit("grok", "read", {}, "s2")
    assert gate.entered.wait(5)
    env.clock.advance(10_000)
    assert manager.sweep() == 1  # the idle s1 only; the busy session is never swept
    assert manager.list_sessions("grok")["s2"]["live"] is True
    gate.release.set()
    busy.result(5)


def test_sweep_closes_idle_sessions_of_a_relaunched_browser(env):
    manager = env.manager()
    manager.submit("grok", "send", {"message": "m"}, "s1").result(5)
    env.window.crash()
    env.window.start()  # a new generation: the old attachment is dead
    assert manager.list_sessions("grok")["s1"]["live"] is False
    assert manager.sweep() == 1
    assert env.script.runtimes[0].stopped_on is not None
    manager.submit("grok", "send", {"message": "again"}, "s1").result(5)
    assert len(env.script.runtimes) == 2 and env.script.runtimes[1].generation == 2


# ── recovery ──────────────────────────────────────────────────────────────


def test_a_page_closed_between_operations_is_rebuilt_without_running_anything_twice(env):
    manager = env.manager()
    url = manager.submit("grok", "send", {"message": "one"}, "s1").result(5)["url"]
    old = env.target(manager, "s1")
    env.window.pages[old].close()
    env.script.calls.clear()
    manager.submit("grok", "send", {"message": "two"}, "s1").result(5)
    assert [c[1]["message"] for c in env.script.sends()] == ["two"]  # once
    assert env.script.sends()[0][1]["conversation_url"] == url
    new = env.target(manager, "s1")
    assert new != old and env.leases.internal == {("grok", new): "session:grok/s1"}
    assert len(env.script.runtimes) == 2 and env.script.runtimes[0].stopped_on is not None


def test_a_page_closed_during_a_send_is_reported_and_never_resent(env):
    attempts = []

    def send(adapter, kwargs, on_delta):
        attempts.append(kwargs["message"])
        adapter.page.close()
        raise RuntimeError("Target closed")

    env.script.handlers["send"] = send
    manager = env.manager()
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "once"}, "s1").result(5)
    assert (exc.value.status, exc.value.code) == (503, "session_lost")
    assert attempts == ["once"]
    env.script.handlers.pop("send")
    manager.submit("grok", "send", {"message": "next"}, "s1").result(5)  # the session heals on next use
    assert attempts == ["once"]


def test_browser_relaunch_rebuilds_the_session_before_the_next_operation(env):
    manager = env.manager()
    manager.submit("grok", "send", {"message": "one"}, "s1").result(5)
    env.window.crash()
    env.window.start()
    env.script.calls.clear()
    manager.submit("grok", "send", {"message": "two"}, "s1").result(5)
    assert len(env.script.sends()) == 1
    assert env.script.runtimes[-1].generation == 2 and env.script.runtimes[0].stopped_on is not None
    assert list(env.leases.internal.values()) == ["session:grok/s1"]


def test_a_session_that_cannot_keep_a_page_open_fails_instead_of_looping(env):
    manager = env.manager()
    manager.submit("grok", "send", {"message": "one"}, "s1").result(5)
    env.window.pages[env.target(manager, "s1")].close()  # the tab is gone: the next use rebuilds
    original = env.window.new_page

    def doomed():
        page = original()
        page.closed.set()  # closed the moment it exists, though the browser still lists it
        return page

    env.window.new_page = doomed
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "list_conversations", {}, "s1").result(10)
    assert (exc.value.status, exc.value.code) == (503, "session_unavailable")
    wait_until(lambda: ("grok", "s1") not in manager._live and not manager._closing)
    assert [c[0] for c in env.script.calls] == ["send"]  # the operation itself never ran
    assert env.leases.internal == {}

    env.window.new_page = original
    manager.submit("grok", "send", {"message": "two"}, "s1").result(5)  # and the session recovers
    assert len(env.script.sends()) == 2


def test_bind_failures_reach_the_caller_and_leave_no_session_or_page(env):
    manager = env.manager()
    env.runtime_error = ServiceError("launch_failed", "no browser", 500)
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "m"}, "s1").result(5)
    assert (exc.value.status, exc.value.code) == (500, "launch_failed")
    assert env.window.pages == {} and manager._live == {}

    env.runtime_error = None
    env.script.adapter_error = ValueError("a folder needs a folder_url")
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "m"}, "s1").result(5)
    assert exc.value.code == "session_unavailable" and "folder_url" in exc.value.detail
    wait_until(lambda: env.window.pages == {} and env.leases.internal == {})
    assert env.script.runtimes[-1].stopped_on is not None and manager._live == {}

    env.script.adapter_error = None
    manager.submit("grok", "send", {"message": "m"}, "s1").result(5)


def test_adapter_failures_keep_their_codes_and_unexpected_ones_become_site_failed(env):
    manager = env.manager()
    for err, status, code, detail in [
        (ServiceError("auth_required", "signed out", 401), 401, "auth_required", "signed out"),
        (ServiceError("page_deviation", "composer missing", 502), 502, "page_deviation", "composer missing"),
        (ServiceError("timeout", "partial text", 504), 504, "timeout", "partial text"),
    ]:
        env.script.handlers["read"] = lambda adapter, kwargs, on_delta, err=err: (_ for _ in ()).throw(err)
        with pytest.raises(ServiceError) as exc:
            manager.submit("grok", "read", {}, "s1").result(5)
        assert (exc.value.status, exc.value.code, exc.value.detail) == (status, code, detail)
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "read", {}, "s1").result(5)
    assert (exc.value.status, exc.value.code) == (500, "site_failed") and "RuntimeError" in exc.value.detail
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: {"bad": object()}
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "read", {}, "s1").result(5)
    assert (exc.value.status, exc.value.code) == (500, "site_failed")
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: ["a", "list", "is", "json"]
    assert manager.submit("grok", "read", {}, "s1").result(5) == ["a", "list", "is", "json"]


# ── ephemeral sessions ────────────────────────────────────────────────────


def test_ephemeral_sessions_are_never_persisted_and_their_lifetime_is_never_converted(env):
    manager = env.manager()
    first = manager.submit("grok", "send", {"message": "1"}, "_uc-tmp-1", ephemeral=True).result(5)["url"]
    assert not env.path.exists()
    entry = manager.list_sessions("grok")["_uc-tmp-1"]
    assert entry == {"url": first, "live": True, "idle_s": 0.0, "ephemeral": True}
    # Follow-on operations on the same session continue its in-memory conversation.
    manager.submit("grok", "send", {"message": "2"}, "_uc-tmp-1", ephemeral=True).result(5)
    assert env.script.sends()[-1][1]["conversation_url"] == first
    assert not env.path.exists() and all("ephemeral" not in c[1] for c in env.script.calls)

    manager.submit("grok", "send", {"message": "keep"}, "keep").result(5)
    for sid, ephemeral in (("keep", True), ("_uc-tmp-1", False)):
        with pytest.raises(ServiceError) as exc:
            manager.submit("grok", "send", {"message": "x"}, sid, ephemeral=ephemeral)
        assert (exc.value.status, exc.value.code) == (409, "session_conflict")

    # Idle expiry discards the ephemeral session without a trace; a mapped id stays persistent.
    env.clock.advance(61)
    assert manager.sweep() == 2
    assert set(manager.list_sessions("grok")) == {"keep"}
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "x"}, "keep", ephemeral=True)
    assert exc.value.code == "session_conflict"
    manager.submit("grok", "send", {"message": "x"}, "_uc-tmp-1").result(5)  # the id is free again
    assert "_uc-tmp-1" in json.loads(env.path.read_text())["sessions"]["grok"]


def test_a_forgotten_ephemeral_session_closes_its_page_and_leaves_nothing(env):
    manager = env.manager()
    manager.submit("grok", "send", {"message": "1"}, "_uc-tmp-2", ephemeral=True).result(5)
    target = env.target(manager, "_uc-tmp-2")
    assert manager.forget("grok", "_uc-tmp-2") is True
    assert target not in env.window.pages and manager.list_sessions("grok") == {}
    assert not env.path.exists()


# ── forget and shutdown ───────────────────────────────────────────────────


def test_forget_closes_even_a_busy_page_and_a_late_result_does_not_resurrect_the_entry(env):
    manager = env.manager()
    manager.submit("grok", "send", {"message": "1"}, "s1").result(5)
    assert "s1" in json.loads(env.path.read_text())["sessions"]["grok"]
    release = threading.Event()

    def late(adapter, kwargs, on_delta):
        release.set()
        assert adapter.page.closed.wait(10)  # held until forget closes the page
        return {"response": "late", "url": conv(42)}

    env.script.handlers["send"] = late
    busy = manager.submit("grok", "send", {"message": "2"}, "s1")
    assert release.wait(5)
    target = env.target(manager, "s1")
    assert manager.forget("grok", "s1") is True
    assert target not in env.window.pages and env.leases.internal == {}
    assert busy.result(5)["url"] == conv(42)  # the caller still gets its answer
    assert "s1" not in manager.list_sessions("grok")
    assert "s1" not in json.loads(env.path.read_text()).get("sessions", {}).get("grok", {})
    with pytest.raises(ServiceError) as exc:
        manager.forget("grok", "s1")
    assert exc.value.code == "no_such_session"


def test_close_interrupts_busy_pages_before_joining_workers_and_keeps_the_map(env):
    manager = env.manager()
    for sid in ("s1", "s2"):
        manager.submit("grok", "send", {"message": sid}, sid).result(5)
    gate = Gate()
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gate.run(adapter)
    busy = manager.submit("grok", "read", {}, "s2")
    assert gate.entered.wait(5)
    queued = manager.submit("grok", "read", {}, "s2")  # behind the busy one
    target = env.target(manager, "s2")
    worker = manager._live[("grok", "s2")].worker
    before = json.loads(env.path.read_text())

    manager.close()
    with pytest.raises(ServiceError) as exc:
        queued.result(5)
    assert (exc.value.status, exc.value.code) == (503, "window_stopped")
    with pytest.raises(ServiceError):
        busy.result(5)  # its page was closed under it
    events = env.window.events
    assert events.index(("page-closed", target)) < events.index(("runtime-stopped", target))
    assert not worker.is_alive()
    assert env.window.pages == {} and env.leases.internal == {}
    assert all(r.stopped_on is r.thread for r in env.script.runtimes)
    assert json.loads(env.path.read_text())["sessions"]["grok"].keys() == before["sessions"]["grok"].keys()
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "x"}, "s1")
    assert exc.value.code == "window_stopped"
    manager.close()  # idempotent


# ── streaming ─────────────────────────────────────────────────────────────


def test_abandoned_stream_drops_deltas_but_the_operation_completes_and_saves_the_map(env):
    gate = Gate()

    def send(adapter, kwargs, on_delta):
        on_delta("one")
        gate.run(adapter)
        on_delta("two")
        return {"response": "onetwo", "url": conv(11)}

    env.script.handlers["send"] = send
    manager = env.manager()

    async def scenario():
        sink = StreamSink(asyncio.get_running_loop())
        call = await asyncio.to_thread(
            manager.submit, "grok", "send", {"message": "m"}, "s1", on_delta=sink.on_delta)
        sink.attach(call)
        lines = sink.lines()
        assert json.loads(await lines.__anext__()) == {"delta": "one"}
        await lines.aclose()  # the consumer disconnects mid-reply
        assert not call.future.cancelled() and not call.future.done()
        gate.release.set()
        result = await asyncio.wrap_future(call.future)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        leftovers = []
        while not sink._queue.empty():
            leftovers.append(sink._queue.get_nowait())
        return result, leftovers

    result, leftovers = asyncio.run(scenario())
    assert result["url"] == conv(11)
    assert {"delta": "two"} not in leftovers  # nothing enqueued for the departed consumer
    assert manager.list_sessions("grok")["s1"]["url"] == conv(11)
    assert json.loads(env.path.read_text())["sessions"]["grok"]["s1"]["url"] == conv(11)
    assert len(env.script.sends()) == 1  # never resent


# ── HTTP ──────────────────────────────────────────────────────────────────


@pytest.fixture
def api(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from uc_browser.health import HealthMonitor, HealthStore
    from uc_browser.registry import SiteRegistry
    from uc_browser.status_server import create_app

    # create_app makes its own LeaseStore: keep its reaper off the fake window,
    # which would sweep session pages that only the manager's lease store owns.
    monkeypatch.setattr(LeaseStore, "start", lambda self: None)
    manager = env.manager()
    app = create_app(
        registry=SiteRegistry(path=tmp_path / "sites.json"),
        monitor=HealthMonitor(store=HealthStore(base_dir=tmp_path / "health")),
        run_scheduler=False, with_mcp=False, window_manager=env.windows, session_manager=manager,
    )
    with TestClient(app) as client:
        yield client, manager


def ndjson(client, path, body):
    with client.stream("POST", path, json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.iter_lines() if line]


def test_http_unstreamed_results_default_session_and_metadata_stay_out_of_the_adapter(api, env):
    client, manager = api
    reply = client.post("/sites/grok/send", json={"message": "hi"})
    assert reply.status_code == 200
    assert reply.json() == {"result": {"response": "ok", "url": conv(1), "conversation_id": None}}
    reply = client.post("/sites/grok/send", json={"message": "hi", "session_id": "chat", "stream": False,
                                                  "wait": True, "ephemeral": False})
    assert reply.status_code == 200
    # Metadata never reaches the adapter; kwargs are exactly the operation's own.
    assert [c[1] for c in env.script.sends()] == [{"message": "hi"}, {"message": "hi"}]
    sessions = client.get("/sites/grok/sessions").json()
    assert set(sessions) == {"default", "chat"}
    assert sessions["default"] == {"url": conv(1), "live": True, "idle_s": 0.0}
    env.clock.advance(5)
    assert client.get("/sites/grok/sessions").json()["chat"]["idle_s"] == 5.0
    assert client.post("/sites/grok/list_conversations").json() == {"result": {"op": "list_conversations"}}


@pytest.mark.parametrize("path,body,status,code", [
    ("/sites/nope/send", {}, 404, "no_such_site"),
    ("/sites/grok/frobnicate", {}, 404, "no_such_op"),
    ("/sites/grok/sessions", {}, 404, "no_such_op"),
    ("/sites/grok/send", [1], 400, "bad_arguments"),
    ("/sites/grok/send", {"stream": "yes"}, 400, "bad_arguments"),
    ("/sites/grok/send", {"wait": None}, 400, "bad_arguments"),
    ("/sites/grok/send", {"ephemeral": 1}, 400, "bad_arguments"),
    ("/sites/grok/send", {"session_id": 5}, 400, "bad_arguments"),
])
def test_http_refusals_use_the_wire_error_shape(api, path, body, status, code):
    client, _ = api
    reply = client.post(path, json=body)
    assert (reply.status_code, reply.json()["error"]) == (status, code)
    assert set(reply.json()) == {"error", "detail"}


def test_http_body_must_be_json(api):
    client, _ = api
    reply = client.post("/sites/grok/send", content=b"{not json", headers={"content-type": "application/json"})
    assert (reply.status_code, reply.json()["error"]) == (400, "bad_arguments")
    assert client.post("/sites/grok/list_conversations", content=b"").status_code == 200  # no body: no arguments


def test_http_adapter_errors_keep_status_and_code_with_partial_text(api, env):
    client, _ = api
    for err, status in [
        (ServiceError("auth_required", "signed out", 401), 401),
        (ServiceError("page_deviation", "composer missing", 502), 502),
        (ServiceError("timeout", "partial text so far", 504), 504),
    ]:
        env.script.handlers["send"] = lambda adapter, kwargs, on_delta, err=err: (_ for _ in ()).throw(err)
        reply = client.post("/sites/grok/send", json={"message": "m"})
        assert reply.status_code == status
        assert reply.json() == {"error": err.code, "detail": err.detail}
    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: (_ for _ in ()).throw(RuntimeError("boom"))
    reply = client.post("/sites/grok/send", json={"message": "m"})
    assert (reply.status_code, reply.json()["error"]) == (500, "site_failed")


def test_http_wait_false_is_busy_while_the_session_or_the_account_is_occupied(api, env):
    client, manager = api
    gate = Gate()
    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: gate.run(
        adapter, {"response": "x", "url": conv(1)})
    held = manager.submit("grok", "send", {"message": "long"}, "s1")
    assert gate.entered.wait(5)
    same_session = client.post("/sites/grok/send", json={"message": "m", "session_id": "s1", "wait": False})
    other_session = client.post("/sites/grok/send", json={"message": "m", "session_id": "s2", "wait": False})
    streamed = client.post("/sites/grok/send", json={"message": "m", "session_id": "s2", "wait": False,
                                                     "stream": True})
    for reply in (same_session, other_session, streamed):
        assert (reply.status_code, reply.json()["error"]) == (503, "busy")  # plain HTTP errors, even streamed
    gate.release.set()
    held.result(5)


def test_http_streamed_send_yields_deltas_then_one_result(api, env):
    client, _ = api

    def send(adapter, kwargs, on_delta):
        on_delta("Hel")
        on_delta("lo")
        return {"response": "Hello", "url": conv(1), "conversation_id": None}

    env.script.handlers["send"] = send
    lines = ndjson(client, "/sites/grok/send", {"message": "m", "stream": True, "session_id": "s"})
    assert lines == [{"delta": "Hel"}, {"delta": "lo"},
                     {"result": {"response": "Hello", "url": conv(1), "conversation_id": None}}]
    assert client.get("/sites/grok/sessions").json()["s"]["url"] == conv(1)


def test_http_streamed_failure_ends_with_one_error_line_after_the_deltas(api, env):
    client, _ = api

    def send(adapter, kwargs, on_delta):
        on_delta("par")
        raise ServiceError("timeout", "par", 504)

    env.script.handlers["send"] = send
    lines = ndjson(client, "/sites/grok/send", {"message": "m", "stream": True})
    assert lines == [{"delta": "par"}, {"error": "timeout", "detail": "par"}]

    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: (_ for _ in ()).throw(
        ServiceError("auth_required", "signed out", 401))
    lines = ndjson(client, "/sites/grok/send", {"message": "m", "stream": True})
    assert lines == [{"error": "auth_required", "detail": "signed out"}]


def test_http_sessions_listing_and_forgetting(api, env):
    client, manager = api
    client.post("/sites/grok/send", json={"message": "m", "session_id": "chat"})
    assert client.get("/sites/grok/sessions").json() == {"chat": {"url": conv(1), "live": True, "idle_s": 0.0}}
    target = env.target(manager, "chat")
    assert client.delete("/sites/grok/sessions/chat").json() == {"forgotten": True}
    assert target not in env.window.pages
    assert client.get("/sites/grok/sessions").json() == {}
    reply = client.delete("/sites/grok/sessions/chat")
    assert (reply.status_code, reply.json()["error"]) == (404, "no_such_session")
    assert client.get("/sites/nope/sessions").status_code == 404
    assert client.delete("/sites/nope/sessions/chat").status_code == 404


def test_http_ephemeral_flag_and_lifetime_conflict(api, env):
    client, _ = api
    reply = client.post("/sites/grok/send", json={"message": "m", "session_id": "_uc-tmp-9", "ephemeral": True})
    assert reply.status_code == 200
    assert not env.path.exists()
    assert client.get("/sites/grok/sessions").json()["_uc-tmp-9"]["ephemeral"] is True
    reply = client.post("/sites/grok/send", json={"message": "m", "session_id": "_uc-tmp-9"})
    assert (reply.status_code, reply.json()["error"]) == (409, "session_conflict")
    assert client.delete("/sites/grok/sessions/_uc-tmp-9").json() == {"forgotten": True}


def test_http_stop_returns_a_bool_while_the_send_is_still_waiting(api, env):
    client, manager = api
    gate = Gate()
    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: gate.run(
        adapter, {"response": "x", "url": conv(3)})
    send = manager.submit("grok", "send", {"message": "long", "conversation_url": conv(3)}, "s1")
    assert gate.entered.wait(5)
    assert client.post("/sites/grok/stop", json={"conversation_url": classic(3)}).json() == {"result": True}
    assert client.post("/sites/grok/stop", json={"session_id": "s1"}).json() == {"result": True}
    assert client.post("/sites/grok/stop", json={"conversation_url": conv(4)}).json() == {"result": False}
    reply = client.post("/sites/grok/stop", json={"conversation_url": 5})
    assert (reply.status_code, reply.json()["error"]) == (400, "bad_arguments")
    reply = client.post("/sites/grok/stop", json={"message": "extra"})
    assert (reply.status_code, reply.json()["error"]) == (400, "bad_arguments")
    assert not send.future.done()
    gate.release.set()
    send.result(5)
    assert ndjson(client, "/sites/grok/stop", {"stream": True, "session_id": "s1"}) == [{"result": True}]


# ── application configuration ────────────────────────────────────────────




def test_create_app_builds_sessions_from_the_windows_config_and_rejects_bad_sites_early(tmp_path, monkeypatch):
    from uc_browser.health import HealthMonitor, HealthStore
    from uc_browser.registry import SiteRegistry
    from uc_browser.status_server import create_app

    monkeypatch.setenv("UC_SESSIONS_FILE", str(tmp_path / "map.json"))

    def build(sites):
        windows = FakeWindows(FakeWindow("grok"), config={"windows": {}, "sites": sites})
        return create_app(
            registry=SiteRegistry(path=tmp_path / "sites.json"),
            monitor=HealthMonitor(store=HealthStore(base_dir=tmp_path / "health")),
            run_scheduler=False, with_mcp=False, window_manager=windows,
        )

    app = build({"grok": {"window": "grok", "max_sessions": 4, "max_concurrent_sends": 1, "session_idle_s": 1800,
                          "folder": "NSFW", "folder_url": PROJECT}})
    config = app.state.sessions.configs["grok"]
    assert (config.window, config.max_sessions, config.session_idle_s) == ("grok", 4, 1800.0)
    assert config.options == {"folder": "NSFW", "folder_url": PROJECT}
    assert app.state.sessions._store.path == tmp_path / "map.json"
    assert build({}).state.sessions.configs == {}  # no sites: nothing is served, nothing is created
    for bad in ({"grok": {"window": "elsewhere"}}, {"chatgpt": {"window": "grok"}},
                {"grok": {"window": "grok", "folder": "NSFW"}}):
        with pytest.raises(ConfigError):
            build(bad)


# ── map durability, teardown accounting, client disconnects ──────────────


def test_a_lost_map_write_is_surfaced_after_the_operation_ran_and_the_session_stays_usable(env, monkeypatch):
    manager = env.manager()
    manager.submit("grok", "send", {"message": "1"}, "s1").result(5)
    permits = manager._permits["grok"]
    real = os.replace

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "send", {"message": "2"}, "s1").result(5)
    assert (exc.value.status, exc.value.code) == (500, "session_store_failed")
    assert "do not retry" in exc.value.detail
    assert len(env.script.sends()) == 2  # each send ran exactly once
    assert not manager._live[("grok", "s1")].busy  # running state was released regardless
    monkeypatch.setattr(os, "replace", real)
    wait_until(lambda: permits._free == 1)
    manager.submit("grok", "send", {"message": "3"}, "s1").result(5)
    assert len(env.script.sends()) == 3


def test_a_session_whose_worker_outlives_the_join_keeps_its_slot_until_it_really_ends(env, monkeypatch):
    monkeypatch.setattr("uc_browser.service.sessions.JOIN_S", 0.2)
    env.sites["grok"]["max_sessions"] = 1
    entered, stuck = threading.Event(), threading.Event()

    def read(adapter, kwargs, on_delta):  # ignores its page being closed
        entered.set()
        assert stuck.wait(10)
        return {}

    env.script.handlers["read"] = read
    manager = env.manager()
    held = manager.submit("grok", "read", {}, "a")
    assert entered.wait(5)
    assert manager.forget("grok", "a") is True  # the join times out with the worker still in the adapter
    with pytest.raises(ServiceError) as exc:
        held.result(5)
    assert exc.value.code == "session_closed"
    assert len(manager._closing) == 1 and manager._live == {}
    with pytest.raises(ServiceError) as exc:
        manager.submit("grok", "list_conversations", {}, "b", wait=False)
    assert (exc.value.status, exc.value.code) == (503, "busy")  # the slot is still taken
    stuck.set()
    wait_until(lambda: not manager._closing)
    manager.submit("grok", "list_conversations", {}, "b").result(5)


class GoneRequest:
    """The slice of ``starlette.requests.Request`` the disconnect watcher uses."""

    def __init__(self):
        self.gone = False

    async def is_disconnected(self):
        return self.gone


def test_a_caller_that_disconnects_cancels_what_has_not_started_but_never_a_running_operation(env, monkeypatch):
    monkeypatch.setattr(site_api, "DISCONNECT_POLL_S", 0.01)
    gate = Gate()
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gate.run(adapter, {"tag": kwargs["tag"]})
    manager = env.manager()
    running = manager.submit("grok", "read", {"tag": "1"}, "s")
    assert gate.entered.wait(5)
    request = GoneRequest()

    async def queued_caller():
        task = asyncio.ensure_future(
            run_unstreamed(request, manager, "grok", "read", {"tag": "2"}, "s", True, False))
        while not manager._live[("grok", "s")].tasks:  # admitted: queued behind the running operation
            await asyncio.sleep(0.005)
        request.gone = True
        with pytest.raises(_ClientGone):
            await task

    asyncio.run(queued_caller())
    gate.release.set()
    assert running.result(5) == {"tag": "1"}
    wait_until(lambda: not manager._live[("grok", "s")].tasks)
    assert [c[1]["tag"] for c in env.script.calls if c[0] == "read"] == ["1"]  # the queued one never ran


def test_a_caller_that_disconnects_mid_operation_does_not_cancel_or_repeat_it(env, monkeypatch):
    monkeypatch.setattr(site_api, "DISCONNECT_POLL_S", 0.01)
    gate = Gate()
    env.script.handlers["send"] = lambda adapter, kwargs, on_delta: gate.run(
        adapter, {"response": "x", "url": conv(5)})
    manager = env.manager()
    request = GoneRequest()

    async def caller():
        task = asyncio.ensure_future(
            run_unstreamed(request, manager, "grok", "send", {"message": "m"}, "s2", True, False))
        while not gate.entered.is_set():
            await asyncio.sleep(0.005)
        request.gone = True
        with pytest.raises(_ClientGone):
            await task

    asyncio.run(caller())
    gate.release.set()
    wait_until(lambda: manager.list_sessions("grok")["s2"]["url"] == conv(5))  # completed and saved
    assert len(env.script.sends()) == 1


def test_a_caller_that_leaves_during_admission_leaves_nothing_queued(env, monkeypatch):
    monkeypatch.setattr(site_api, "DISCONNECT_POLL_S", 0.01)
    env.sites["grok"]["max_sessions"] = 1
    gate = Gate()
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gate.run(adapter)
    manager = env.manager()
    held = manager.submit("grok", "read", {}, "a")
    assert gate.entered.wait(5)
    request = GoneRequest()
    env.runtime_hold = threading.Event()  # keep the new session's thread from starting the task

    async def caller():
        task = asyncio.ensure_future(
            run_unstreamed(request, manager, "grok", "list_conversations", {}, "b", True, False))
        while manager._slot_waiters == 0:  # blocked waiting for a session slot
            await asyncio.sleep(0.005)
        request.gone = True
        with pytest.raises(_ClientGone):
            await task
        gate.release.set()  # a finishes and is evicted: the pending admission now completes for nobody
        deadline = time.monotonic() + 5
        while True:
            session = manager._live.get(("grok", "b"))
            if session is not None and session.tasks and session.tasks[0].future.cancelled():
                break
            assert time.monotonic() < deadline, "the abandoned admission was never cancelled"
            await asyncio.sleep(0.005)

    asyncio.run(caller())
    env.runtime_hold.set()
    held.result(5)
    wait_until(lambda: not manager._live[("grok", "b")].tasks)
    assert "list_conversations" not in [c[0] for c in env.script.calls]


def test_disconnect_before_stream_headers_cancels_the_queued_send(env):
    from starlette.requests import ClientDisconnect

    gate = Gate()
    env.script.handlers["read"] = lambda adapter, kwargs, on_delta: gate.run(adapter)
    manager = env.manager()
    running = manager.submit("grok", "read", {}, "s")
    assert gate.entered.wait(5)

    async def scenario():
        sink = StreamSink(asyncio.get_running_loop())
        queued = manager.submit("grok", "send", {"message": "never send"}, "s")
        sink.attach(queued)
        response = site_api._SessionStreamResponse(sink)

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise OSError("connection closed before headers")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert queued.future.cancelled()

    try:
        asyncio.run(scenario())
    finally:
        gate.release.set()
    running.result(5)
    assert env.script.sends() == []
