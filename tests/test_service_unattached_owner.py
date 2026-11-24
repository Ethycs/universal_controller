"""Unattached windows: native launch, HTTP-only owner, temporary attachments, quiet reservation.

No browser and no Playwright: launches are fakes (a fake Chrome process, a fake
DevTools HTTP server on a real loopback socket), and a guard fixture fails any
test that starts Playwright. They pin the owner/idle rule (no attachment, ever),
the reservation races, and the HTTP encoding the owner depends on.
"""

import http.server
import itertools
import json
import os
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from test_service_windows_core import (
    NativeEnv,
    _stderr_readers,
    foreign_holds_port_chrome_logs_bind_failure,
    foreign_holds_v4_chrome_fell_back_to_v6,
    foreign_serves_other_path,
    wait_until,
    write_config,
)

from uc_browser.service import profiles, unattached, windows
from uc_browser.service.errors import ServiceError
from uc_browser.service.unattached import HttpRuntime, TransientLane
from uc_browser.service.windows import ConfigError, Window, WindowManager, WindowSpec
from uc_browser.service.workers import Deadlines

needs_proc = pytest.mark.skipif(profiles._proc_established(0) is None, reason="needs /proc/net/tcp")


@pytest.fixture(autouse=True)
def no_playwright(monkeypatch):
    import playwright.sync_api as sync_api

    def boom(*args, **kwargs):
        raise AssertionError("an unattached owner path started Playwright")

    monkeypatch.setattr(sync_api, "sync_playwright", boom)


@pytest.fixture(autouse=True)
def fast_heartbeat(monkeypatch):
    monkeypatch.setattr(windows, "OWNER_HEARTBEAT_S", 0.05)


def raised(call, *args, **kwargs) -> ServiceError:
    with pytest.raises(ServiceError) as err:
        call(*args, **kwargs)
    return err.value


# ── spec / config ─────────────────────────────────────────────────────────


def test_unattached_config_roundtrip_offers_and_info(tmp_path):
    mgr = WindowManager(write_config(tmp_path, {
        "q": {"browser": "chrome", "engine": "inject", "profile": str(tmp_path / "q"), "unattached": True},
        "n": {"browser": "chrome", "engine": "inject", "profile": str(tmp_path / "n")},
        "z": {"browser": "chrome", "engine": "none", "profile": str(tmp_path / "z"), "unattached": True},
    }))
    q, n, z = (mgr.get(k).spec for k in "qnz")
    assert q.unattached is True and q.stealth is False and q.state_file is None
    assert q.offers == ["attach", "control", "fetch", "cookies", "snapshot", "mhtml", "unattached"]
    assert z.offers == ["attach", "fetch", "cookies", "snapshot", "mhtml", "unattached"]
    assert n.unattached is False
    assert n.offers == ["attach", "control", "fetch", "cookies", "snapshot", "mhtml"]  # unchanged
    assert mgr.get("q").info()["unattached"] is True
    assert mgr.get("n").info()["unattached"] is False


@pytest.mark.parametrize("raw", [
    {"browser": "chromium", "engine": "inject"},
    {"browser": "firefox", "engine": "inject"},
    {"browser": "chrome", "engine": "inject", "stealth": True},
    {"browser": "chrome", "engine": "inject", "state_file": "state.json"},
    {"browser": "chrome", "engine": "inject", "unattached": "yes"},
    {"browser": "chrome", "engine": "inject", "unattached": 1},
], ids=["chromium", "firefox", "stealth", "state_file", "string", "int"])
def test_bad_unattached_config_rejected(tmp_path, raw):
    raw = {"profile": str(tmp_path / "p"), "unattached": True, **raw}
    with pytest.raises(ConfigError):
        WindowManager(write_config(tmp_path, {"w": raw}))


def test_unattached_lanes_need_their_window(tmp_path):
    spec = WindowSpec(name="w", browser="chrome", engine="inject", profile=tmp_path / "p", unattached=True)
    with pytest.raises(ValueError):
        windows._Lanes(QuietLaunch(), spec)


# ── native process launch without Playwright ──────────────────────────────


@pytest.fixture
def native(monkeypatch):
    env = NativeEnv()
    monkeypatch.setattr(profiles.subprocess, "Popen", env.popen)

    def terminate(proc, timeout=8.0):
        env.terminated.append(proc)
        proc.exit(-15)

    monkeypatch.setattr(profiles, "terminate_process", terminate)
    monkeypatch.setattr(profiles, "port_owned_by", lambda port, pids: None)
    yield env
    for chrome in env.chromes:
        chrome.exit(0)
    for srv in env.servers:
        srv.shutdown()
        srv.server_close()
    assert wait_until(lambda: not _stderr_readers()), "stderr reader thread leaked"


def launch_process(tmp_path, **kw):
    return profiles.launch_native_process(tmp_path / "prof", chrome_path="fake-chrome", **kw)


def test_native_process_launch_attaches_nothing_and_adds_no_flags(native, tmp_path):
    profile = tmp_path / "prof"
    profile.mkdir()
    stale = profile / "DevToolsActivePort"
    stale.write_text("9\n/devtools/browser/stale\n")
    proc, cdp = launch_process(tmp_path, extra_args=("--headless=new",))
    chrome = native.chromes[0]
    assert chrome.cmd == [
        "fake-chrome",
        f"--remote-debugging-port={chrome.port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--headless=new",
        "about:blank",
    ]
    assert chrome.port > 0 and proc is chrome
    assert cdp == f"ws://127.0.0.1:{chrome.port}/devtools/browser/ours"
    assert native.pw.attached == [] and native.terminated == []
    assert not stale.exists()


@pytest.mark.parametrize("script", [
    foreign_serves_other_path, foreign_holds_v4_chrome_fell_back_to_v6, foreign_holds_port_chrome_logs_bind_failure,
])
def test_native_process_launch_never_adopts_a_foreign_listener(native, tmp_path, script):
    native.script = script
    assert raised(launch_process, tmp_path).code == "launch_failed"
    assert len(native.chromes) == 1 and native.terminated == native.chromes


def test_native_process_launch_refuses_a_port_the_child_does_not_own(native, tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "port_owned_by", lambda port, pids: False)
    assert raised(launch_process, tmp_path).code == "launch_failed"
    assert native.terminated == native.chromes and len(native.chromes) == 1


@pytest.mark.parametrize("code, expected", [(0, "profile_held"), (1, "launch_failed")])
def test_native_process_launch_reports_early_exit(native, tmp_path, code, expected):
    native.script = lambda env, chrome: chrome.exit(code)
    assert raised(launch_process, tmp_path).code == expected
    assert native.terminated == native.chromes and len(native.chromes) == 1


def test_native_process_launch_times_out_and_cleans_up(native, tmp_path):
    native.script = lambda env, chrome: None
    assert raised(launch_process, tmp_path, timeout=0.3).code == "launch_failed"
    assert native.terminated == native.chromes and len(native.chromes) == 1


def test_native_process_launch_stops_the_browser_on_an_unexpected_error(native, tmp_path, monkeypatch):
    def boom(profile, port, *, launched=None):
        raise OSError("proc vanished")

    monkeypatch.setattr(profiles, "verify_endpoint_owner", boom)
    err = raised(launch_process, tmp_path)
    assert err.code == "launch_failed" and "launch failed" in err.detail and "proc vanished" in err.detail
    assert native.terminated == native.chromes and len(native.chromes) == 1


def test_native_process_launch_refuses_a_held_profile_before_starting_chrome(native, tmp_path, monkeypatch):
    profile = tmp_path / "prof"
    profile.mkdir()
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", profile / "SingletonLock")
    monkeypatch.setattr(profiles, "_cmdline_names_profile", lambda pid, prof: True)
    err = raised(launch_process, tmp_path)
    assert (err.code, err.status) == ("profile_held", 409)
    assert native.chromes == []


class FakeProc:
    pid = 424242

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.mark.parametrize("headless, args", [(False, []), (True, ["--headless=new"])])
def test_unattached_launch_browser_creates_no_playwright_and_no_context_setup(tmp_path, monkeypatch, headless, args):
    spec = WindowSpec(name="w", browser="chrome", engine="inject", profile=tmp_path / "p", unattached=True)
    proc = FakeProc()
    calls, terminated = [], []

    def launch_process_(profile, *, extra_args=(), **kw):
        calls.append((profile, list(extra_args)))
        return proc, "ws://127.0.0.1:4321/devtools/browser/abc"

    def forbidden(*a, **kw):
        raise AssertionError("not for an unattached launch")

    monkeypatch.setattr(profiles, "launch_native_process", launch_process_)
    monkeypatch.setattr(profiles, "launch_native", forbidden)
    monkeypatch.setattr(profiles, "terminate_process", lambda p, timeout=8.0: terminated.append(p))
    monkeypatch.setattr(windows, "_setup_context", forbidden)
    monkeypatch.setattr(windows, "_native_extra_args", forbidden)  # no KeePassXC, no extras
    monkeypatch.setattr(windows, "_require_engine_files", lambda s: None)
    monkeypatch.setenv("UC_SERVICE_HEADLESS", "1" if headless else "0")

    launch = windows.launch_browser(spec)

    assert calls == [(spec.profile, args)]
    assert launch.pw is None and launch.context is None and launch.browser is None
    assert launch.proc is proc and launch.browser_pid == proc.pid
    assert launch.cdp == "ws://127.0.0.1:4321/devtools/browser/abc"
    rt = launch.runtime
    assert isinstance(rt, HttpRuntime)
    assert (rt.port, rt.browser_path, rt.alive) == (4321, "/devtools/browser/abc", True)
    assert not hasattr(rt, "context") and not hasattr(rt, "pw") and not hasattr(rt, "browser")
    launch.abort()  # owns the process: terminated, and there is no Playwright to stop
    assert terminated == [proc]


def test_unattached_launch_failures_surface_unchanged_and_start_nothing(tmp_path, monkeypatch):
    spec = WindowSpec(name="w", browser="chrome", engine="none", profile=tmp_path / "p", unattached=True)
    monkeypatch.setattr(windows, "_require_engine_files", lambda s: None)
    held = ServiceError("profile_held", "held", 409)

    def refuse(profile, **kw):
        raise held

    monkeypatch.setattr(profiles, "launch_native_process", refuse)
    assert raised(windows.launch_browser, spec) is held

    def crash(profile, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(profiles, "launch_native_process", crash)
    err = raised(windows.launch_browser, spec)
    assert err.code == "launch_failed" and "kaboom" in err.detail


# ── HTTP owner runtime against a fake DevTools server ─────────────────────


class FakeDevTools:
    """Loopback DevTools HTTP: records every request, speaks Chrome's dialect
    (/json/new needs PUT and takes the percent-encoded URL after ``?``;
    /json/close answers plain text; unknown id is 404)."""

    def __init__(self, browser_path="/devtools/browser/ours"):
        self.browser_path = browser_path
        self.requests: list[tuple[str, str]] = []
        self.targets: dict[str, dict] = {}
        self.override: dict[str, tuple[int, bytes]] = {}
        self.ids = itertools.count(1)
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, body, ctype="application/json; charset=UTF-8"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_request(self, method):
                fake.requests.append((method, self.path))
                for prefix, (status, body) in list(fake.override.items()):
                    if self.path.startswith(prefix):
                        return self.reply(status, body)
                path, _, query = self.path.partition("?")
                if method == "GET" and path == "/json/version":
                    url = f"ws://127.0.0.1:{fake.port}{fake.browser_path}"
                    return self.reply(200, {"Browser": "Fake/1", "webSocketDebuggerUrl": url})
                if method == "GET" and path == "/json/list":
                    return self.reply(200, [{"id": k, **v} for k, v in fake.targets.items()])
                if path == "/json/new":
                    if method != "PUT":
                        return self.reply(405, b"Using unsafe HTTP verb GET to invoke /json/new.", "text/plain")
                    tid = f"T{next(fake.ids)}"
                    fake.targets[tid] = {"type": "page", "url": unquote(query), "title": ""}
                    return self.reply(200, {"id": tid, "type": "page", "url": fake.targets[tid]["url"]})
                if method == "GET" and path.startswith("/json/close/"):
                    tid = unquote(path.rsplit("/", 1)[1])
                    if fake.targets.pop(tid, None) is None:
                        return self.reply(404, f"No such target id: {tid}".encode(), "text/plain")
                    return self.reply(200, b"Target is closing", "application/json; charset=UTF-8")
                return self.reply(404, b"unknown", "text/plain")

            def do_GET(self):
                self.handle_request("GET")

            def do_PUT(self):
                self.handle_request("PUT")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def devtools():
    dev = FakeDevTools()
    yield dev
    dev.close()


def http_runtime(dev, **kw):
    return HttpRuntime(window="w", port=dev.port, browser_path=dev.browser_path, **kw)


@pytest.mark.parametrize("url", [
    "https://example.com/a?x=1&y=2#frag",
    "https://example.com/p%2Fq?x=%26&z=100%25",
    "https://example.com/ü?q=日本#é",
    "https://example.com/s?q=a+b%20c&r=;%3B/?:@=$,#a&b",
    "about:blank",
])
def test_new_page_uses_put_and_the_whole_url_survives(devtools, url):
    rt = http_runtime(devtools)
    tid = rt.new_page(url)
    assert devtools.targets[tid]["url"] == url  # Chrome decodes the query once
    ((method, raw),) = devtools.requests
    assert method == "PUT" and raw.startswith("/json/new?")
    query = raw.partition("?")[2]
    assert query.isascii() and not any(c in query for c in "&#?/ ")  # nothing the request line could cut at
    assert rt.new_page() != tid and devtools.targets["T2"]["url"] == "about:blank"


def test_new_page_rejects_bad_urls_and_endpoint_errors(devtools):
    rt = http_runtime(devtools)
    assert raised(rt.new_page, "").code == "bad_arguments"
    assert raised(rt.new_page, None).code == "bad_arguments"
    assert raised(rt.new_page, "x\ud800").code == "bad_arguments"  # a lone surrogate cannot be sent
    assert devtools.requests == []
    for status, body in [(500, b"boom"), (200, b"not json"), (200, b"[]"), (200, b'{"id": ""}')]:
        devtools.override["/json/new"] = (status, body)
        err = raised(rt.new_page, "https://example.com/")
        assert (err.code, err.status) == ("devtools_failed", 502), (status, body)


def test_pages_info_shape_filters_non_pages_and_tracks_age(devtools):
    devtools.targets.update({
        "A": {"type": "page", "url": "https://a/", "title": "Title A"},
        "SW": {"type": "service_worker", "url": "https://a/sw.js", "title": ""},
        "B": {"type": "page", "url": "about:blank", "title": "about:blank"},
    })
    rt = http_runtime(devtools)
    first = rt.pages_info()
    assert first == [
        {"target_id": "A", "url": "https://a/", "title": "Title A", "age_s": 0.0},
        {"target_id": "B", "url": "about:blank", "title": "about:blank", "age_s": 0.0},
    ]
    time.sleep(0.12)
    assert all(p["age_s"] >= 0.1 for p in rt.pages_info())
    del devtools.targets["A"]
    assert [p["target_id"] for p in rt.pages_info()] == ["B"]
    assert "A" not in rt._seen


@pytest.mark.parametrize("override", [
    (500, b"boom"),
    (200, b"<html>"),
    (200, b'{"not": "a list"}'),
    (200, b'["not an object"]'),
    (200, b'[{"type": "page", "url": "x"}]'),
    (200, b'[{"type": "page", "id": "T", "url": 5}]'),
    (200, b'[{"type": "page", "id": "T", "url": "x", "title": 5}]'),
])
def test_pages_info_failures_are_errors_never_an_empty_list(devtools, override):
    devtools.override["/json/list"] = override
    err = raised(http_runtime(devtools).pages_info)
    assert (err.code, err.status) == ("devtools_failed", 502)


def test_pages_info_unreachable_endpoint_is_an_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens
    rt = HttpRuntime(window="w", port=port, browser_path="/devtools/browser/x", timeout=1.0)
    err = raised(rt.pages_info)
    assert (err.code, err.status) == ("devtools_failed", 502)


def test_close_target_accepts_plain_text_and_maps_404(devtools):
    devtools.targets["A"] = {"type": "page", "url": "about:blank", "title": ""}
    rt = http_runtime(devtools)
    rt.pages_info()
    assert rt.close_target("A") is None  # "Target is closing" is not JSON and not needed
    assert "A" not in devtools.targets and "A" not in rt._seen
    err = raised(rt.close_target, "A")
    assert (err.code, err.status) == ("no_such_target", 404)
    assert raised(rt.close_target, "").code == "no_such_target"
    devtools.override["/json/close/"] = (500, b"boom")
    assert raised(rt.close_target, "A").code == "close_failed"


def test_close_target_cannot_be_redirected_by_a_hostile_id(devtools):
    rt = http_runtime(devtools)
    raised(rt.close_target, "../protocol?x=1#y")
    ((method, raw),) = devtools.requests
    assert method == "GET" and raw.startswith("/json/close/") and raw.count("/") == 3
    assert "?" not in raw and "#" not in raw


def test_heartbeat_tolerates_slow_beats_but_not_a_silent_or_foreign_endpoint(devtools):
    rt = http_runtime(devtools)
    assert rt.heartbeat() is True
    devtools.override["/json/version"] = (500, b"busy")
    assert [rt.heartbeat() for _ in range(2)] == [True, True]  # a busy browser is not a dead one
    del devtools.override["/json/version"]
    assert rt.heartbeat() is True and rt._misses == 0
    devtools.override["/json/version"] = (500, b"gone")
    assert [rt.heartbeat() for _ in range(3)] == [True, True, False]
    del devtools.override["/json/version"]
    watcher = http_runtime(devtools)  # expects the browser it launched
    assert watcher.heartbeat() is True
    devtools.browser_path = "/devtools/browser/someone-else"
    assert watcher.heartbeat() is False  # another browser took the port


def test_dead_process_is_gone_without_any_request(devtools):
    proc = FakeProc()
    rt = http_runtime(devtools, proc=proc)
    assert rt.alive and rt.heartbeat()
    proc.returncode = 0
    assert not rt.alive and rt.heartbeat() is False
    devtools.requests.clear()
    assert raised(rt.pages_info).code == "window_stopped"
    assert raised(rt.new_page, "https://x/").code == "window_stopped"
    assert devtools.requests == []
    rt2 = http_runtime(devtools)
    rt2.stop_playwright()  # there is no driver: it only marks the runtime finished
    assert not rt2.alive


# ── established connections: what "attached" looks like from the port ─────


@needs_proc
def test_established_clients_sees_a_held_connection_and_names_its_pid(devtools):
    assert profiles.established_clients(devtools.port) == []
    raw = socket.create_connection(("127.0.0.1", devtools.port))
    try:
        assert wait_until(lambda: profiles.established_clients(devtools.port) == [os.getpid()])
        http_runtime(devtools).pages_info()  # a finished HTTP request leaves nothing behind
        assert wait_until(lambda: profiles.established_clients(devtools.port) == [os.getpid()])
    finally:
        raw.close()
    assert wait_until(lambda: profiles.established_clients(devtools.port) == [])


def test_established_clients_falls_back_to_psutil_and_unknown_is_none(monkeypatch):
    import psutil

    def conn(lport, rport, pid, status=psutil.CONN_ESTABLISHED):
        return SimpleNamespace(laddr=SimpleNamespace(port=lport), raddr=SimpleNamespace(port=rport),
                               status=status, pid=pid)

    table = [conn(9222, 40000, 11), conn(40000, 9222, 4321), conn(9222, 40001, 12),
             conn(9222, 40002, 13, psutil.CONN_TIME_WAIT), conn(5000, 6000, 99)]
    monkeypatch.setattr(profiles, "_proc_established", lambda port: None)
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": table)
    assert profiles.established_clients(9222) == [4321, None]  # the second peer's owner is not visible

    def denied(kind="inet"):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "net_connections", denied)
    assert profiles.established_clients(9222) is None


def test_assert_no_clients_names_pid_or_unknown_and_refuses_when_unreadable(devtools, monkeypatch):
    rt = http_runtime(devtools)
    monkeypatch.setattr(profiles, "established_clients", lambda port: [])
    rt.assert_no_clients()
    monkeypatch.setattr(profiles, "established_clients", lambda port: [4242, None])
    err = raised(rt.assert_no_clients, 0)
    assert (err.code, err.status) == ("busy", 409) and "2 external connection" in err.detail and "4242" in err.detail
    monkeypatch.setattr(profiles, "established_clients", lambda port: [None])
    assert "unidentified process" in raised(rt.assert_no_clients, 0).detail
    monkeypatch.setattr(profiles, "established_clients", lambda port: None)
    err = raised(rt.assert_no_clients, 0)
    assert err.code == "busy" and "cannot prove" in err.detail


@needs_proc
def test_assert_no_clients_gives_a_leaving_connection_time_to_go(devtools, monkeypatch):
    monkeypatch.setattr(unattached, "CLIENT_SETTLE_S", 5.0)
    raw = socket.create_connection(("127.0.0.1", devtools.port))
    assert wait_until(lambda: profiles.established_clients(devtools.port) == [os.getpid()])
    threading.Timer(0.3, raw.close).start()
    started = time.monotonic()
    http_runtime(devtools).assert_no_clients()
    assert 0.2 < time.monotonic() - started < 4


# ── fake launches for Window ──────────────────────────────────────────────


class QuietRuntime:
    """Owner runtime fake: no Playwright attributes at all."""

    role = "owner"

    def __init__(self):
        self.alive = True
        self.beats = 0
        self.stopped = False
        self.refuse: ServiceError | None = None
        self.checks: list[tuple[int, str | None]] = []
        self.window = None

    def heartbeat(self):
        self.beats += 1
        return self.alive

    def stop_playwright(self):
        self.stopped = True

    def pages_info(self):
        return [{"target_id": "t1", "url": "about:blank", "title": "", "age_s": 0.0}]

    def assert_no_clients(self, settle_s=None):
        self.checks.append((threading.get_ident(), self.window._quiet if self.window is not None else None))
        if self.refuse is not None:
            raise self.refuse


class AttachedRuntime:
    def __init__(self, launch):
        self.launch = launch
        self.tid = threading.get_ident()
        self.stopped = False
        self.alive = True
        launch.events.append("attach")

    def heartbeat(self):
        return True

    def stop_playwright(self):
        assert threading.get_ident() == self.tid  # stopped on its own thread
        self.stopped = True
        self.launch.events.append("stop")


class QuietLaunch:
    def __init__(self, runtime=None):
        self.runtime = runtime or QuietRuntime()
        self.cdp = "ws://127.0.0.1:1/devtools/browser/x"
        self.proc = None
        self.events: list[str] = []
        self.made: list[AttachedRuntime] = []
        self.closed = False
        self.killed = False
        self.attach_error: Exception | None = None

    def make_worker_runtime(self):
        if self.attach_error is not None:
            raise self.attach_error
        rt = AttachedRuntime(self)
        self.made.append(rt)
        return rt

    @property
    def live(self):
        return sum(not rt.stopped for rt in self.made)

    def close_browser(self):
        self.closed = True

    def kill(self):
        self.killed = True


def quiet_window(tmp_path, **kw):
    launches: list[QuietLaunch] = []

    def launcher(spec):
        launch = QuietLaunch()
        launch.runtime.window = window
        launches.append(launch)
        return launch

    spec = WindowSpec(name="w", browser="chrome", engine=kw.pop("engine", "inject"), profile=tmp_path / "p",
                      unattached=True, **kw)
    window = Window(spec, launcher=launcher)
    return window, launches


def blocking_job(gate, entered, result="blocking"):
    def job(rt):
        entered.set()
        gate.wait(10)
        return result

    return job


# ── owner never attaches ──────────────────────────────────────────────────


def test_idle_unattached_owner_makes_no_attachment(tmp_path):
    w, launches = quiet_window(tmp_path)
    info = w.start()
    launch = launches[0]
    assert info["unattached"] is True and "unattached" in info["offers"] and info["cdp"] == launch.cdp
    assert wait_until(lambda: launch.runtime.beats >= 3)  # heartbeats ran, over HTTP only
    assert w.owner(lambda rt: rt.pages_info()).result(5)[0]["target_id"] == "t1"
    assert w.info()["pages"][0]["title"] == ""
    assert launch.made == [] and launch.events == [] and w._workers == 0
    w.close()
    assert launch.closed and launch.runtime.stopped and not w.running


def test_owner_jobs_that_need_playwright_cannot_run_on_the_http_owner(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.start()
    with pytest.raises(AttributeError):
        w.owner(lambda rt: rt.context.new_page()).result(5)
    assert launches[0].made == []
    w.close()


# ── temporary attachments ─────────────────────────────────────────────────


def test_each_job_attaches_temporarily_and_detaches_before_its_future_completes(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.start()
    launch = launches[0]
    owner_tid = w.owner(lambda rt: threading.get_ident()).result(5)
    counts: list[int] = []

    def job(rt):
        launch.events.append("run")
        assert w._workers == 1 and launch.live == 1
        return threading.get_ident()

    fut = w.work(job)
    fut.add_done_callback(lambda f: (launch.events.append("done"), counts.append(w._workers)))
    tid = fut.result(5)
    assert wait_until(lambda: launch.events[-1:] == ["done"])
    assert launch.events == ["attach", "run", "stop", "done"]  # stopped before the future completed
    assert counts == [0] and launch.live == 0  # and released before it too
    assert tid not in (owner_tid, threading.get_ident())

    assert w.work(lambda rt: "again", long=True).result(5) == "again"
    assert len(launch.made) == 2 and launch.made[0] is not launch.made[1]  # never retained
    assert all(rt.stopped for rt in launch.made) and launch.live == 0

    def failing(rt):
        raise RuntimeError("job failed")

    with pytest.raises(RuntimeError, match="job failed"):
        w.work(failing).result(5)
    assert launch.live == 0 and w._workers == 0  # an error still detaches
    w.close()


def test_attach_failure_is_window_stopped_and_releases_its_slot(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.start()
    launches[0].attach_error = RuntimeError("no websocket")
    err = raised(w.work(lambda rt: 1).result, 5)
    assert err.code == "window_stopped" and "no websocket" in err.detail
    refusal = ServiceError("launch_failed", "no default context")
    launches[0].attach_error = refusal
    assert raised(w.work(lambda rt: 1).result, 5) is refusal
    assert w._workers == 0
    launches[0].attach_error = None
    assert w.work(lambda rt: "ok").result(5) == "ok"
    w.close()


def test_work_before_launch_boots_the_window_then_attaches_per_job(tmp_path):
    w, launches = quiet_window(tmp_path)
    assert w.work(lambda rt: "first").result(10) == "first"
    assert len(launches) == 1 and launches[0].events == ["attach", "stop"] and w._workers == 0
    w.close()


# ── quiet reservation ─────────────────────────────────────────────────────


def test_quiet_reservation_refuses_work_controls_and_ordinary_attachment(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.start()
    w.begin_unattached("c1")
    for fut in (w.work(lambda rt: 1), w.work(lambda rt: 1, long=True), w.control("t", "detect", ["search"], {})):
        err = raised(fut.result, 2)
        assert (err.code, err.status) == ("busy", 409)
    err = raised(w.assert_attach_allowed)
    assert (err.code, err.status) == ("busy", 409)
    assert launches[0].made == []  # refused before any attachment
    assert raised(w.begin_unattached, "c2").code == "busy"  # one preparer
    w.end_unattached("c2")  # not the holder: nothing changes
    assert raised(w.assert_attach_allowed).code == "busy"
    assert w.owner(lambda rt: rt.pages_info()).result(5)  # HTTP metadata stays available
    w.end_unattached("c1")
    w.end_unattached("c1")  # idempotent
    w.assert_attach_allowed()
    assert w.work(lambda rt: "ok").result(5) == "ok"
    w.close()


def test_begin_needs_an_unattached_window_and_ordinary_windows_stay_open(tmp_path):
    ordinary = Window(WindowSpec(name="n", browser="chrome", engine="inject", profile=tmp_path / "n"),
                      launcher=lambda spec: QuietLaunch())
    err = raised(ordinary.begin_unattached, "c1")
    assert (err.code, err.status) == ("unsupported_offer", 409)
    ordinary.assert_attach_allowed()
    assert ordinary.info()["unattached"] is False


def test_begin_is_refused_while_a_service_worker_is_attached_and_says_so(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.start()
    gate, entered = threading.Event(), threading.Event()
    fut = w.work(blocking_job(gate, entered))
    assert entered.wait(5)
    err = raised(w.begin_unattached, "c1")
    assert (err.code, err.status) == ("busy", 409) and "service" in err.detail and "1 active" in err.detail
    assert w._quiet is None and launches[0].runtime.checks == []
    gate.set()
    assert fut.result(5) == "blocking"
    assert wait_until(lambda: w._workers == 0)
    w.begin_unattached("c1")
    w.close()


def test_queued_job_rechecks_the_reservation_before_it_attaches(tmp_path):
    w, launches = quiet_window(tmp_path, control_workers=1)
    w.start()
    launch = launches[0]
    gate, entered = threading.Event(), threading.Event()
    first = w.work(blocking_job(gate, entered, "first"))
    assert entered.wait(5)
    ran: list[int] = []
    queued = w.work(lambda rt: ran.append(1), queue_timeout=None)
    begun: list[None] = []

    # Runs on the first job's thread after its attachment is stopped and before
    # the lane starts the queued job: the instant a quiet reservation can win.
    first.add_done_callback(lambda f: begun.append(w.begin_unattached("c1")))
    gate.set()
    assert first.result(5) == "first"
    err = raised(queued.result, 5)
    assert (err.code, err.status) == ("busy", 409)
    assert begun == [None] and ran == [] and len(launch.made) == 1
    w.close()


def test_cancelling_the_outer_future_does_not_release_a_live_attachment(tmp_path):
    w, launches = quiet_window(tmp_path)
    gate, entered = threading.Event(), threading.Event()
    outer = w.work(blocking_job(gate, entered))  # not launched yet: boot, dispatch, chained future
    assert entered.wait(10)
    assert outer.cancel()  # the caller walks away; the worker is still attached
    err = raised(w.begin_unattached, "c1")
    assert err.code == "busy" and "active service worker" in err.detail
    assert w._quiet is None and w._workers == 1
    gate.set()
    assert wait_until(lambda: w._workers == 0)
    w.begin_unattached("c1")
    w.close()


def test_cancelled_queued_job_never_attaches(tmp_path):
    w, launches = quiet_window(tmp_path, control_workers=1)
    w.start()
    gate, entered = threading.Event(), threading.Event()
    first = w.work(blocking_job(gate, entered))
    assert entered.wait(5)
    ran: list[int] = []
    queued = w.work(lambda rt: ran.append(1), queue_timeout=None)
    assert queued.cancel()
    gate.set()
    first.result(5)
    time.sleep(0.2)
    assert ran == [] and len(launches[0].made) == 1
    w.close()


def test_close_fails_queued_jobs_without_attaching_them(tmp_path):
    w, launches = quiet_window(tmp_path, control_workers=1)
    w.start()
    launch = launches[0]
    gate, entered = threading.Event(), threading.Event()
    first = w.work(blocking_job(gate, entered))
    assert entered.wait(5)
    queued = w.work(lambda rt: "never", queue_timeout=None)
    closer = threading.Thread(target=w.close)
    closer.start()
    assert raised(queued.result, 5).code == "window_stopped"
    gate.set()
    closer.join(10)
    assert not closer.is_alive()
    assert first.result(5) == "blocking"
    assert len(launch.made) == 1 and launch.live == 0 and w._workers == 0
    assert raised(w.work(lambda rt: 1).result, 2).code == "window_stopped"


def test_reservation_survives_a_crash_and_a_stuck_worker_keeps_the_count(tmp_path):
    w, launches = quiet_window(tmp_path, control_workers=1)
    w.start()
    gate, entered = threading.Event(), threading.Event()
    stuck = w.work(blocking_job(gate, entered))
    assert entered.wait(5)
    launches[0].runtime.alive = False  # the browser dies under the attached worker
    assert wait_until(lambda: not w.running)
    err = raised(w.begin_unattached, "c1")  # the old generation's worker has not returned
    assert err.code == "busy" and "active service worker" in err.detail
    gate.set()
    assert stuck.result(5) == "blocking"
    assert wait_until(lambda: w._workers == 0 and launches[0].closed)

    w.begin_unattached("c1")  # not running: no browser, no clients, nothing launched
    assert len(launches) == 1
    assert raised(w.work(lambda rt: 1).result, 2).code == "busy"  # refused before any relaunch
    assert len(launches) == 1
    assert raised(w.assert_attach_allowed).code == "busy"
    w.end_unattached("c1")
    assert w.work(lambda rt: 7).result(10) == 7  # relaunches lazily
    assert w.generation == 2 and len(launches) == 2
    w.close()


# ── the client check on the HTTP owner ────────────────────────────────────


def test_begin_checks_clients_on_the_owner_after_marking_the_reservation(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.start()
    owner_tid = w.owner(lambda rt: threading.get_ident()).result(5)
    w.begin_unattached("c1")
    ((tid, quiet),) = launches[0].runtime.checks
    assert tid == owner_tid  # serialised with the owner's own HTTP requests
    assert quiet == "c1"  # no worker could attach while the check ran
    w.close()


def test_failed_client_check_unwinds_the_reservation(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.start()
    busy = ServiceError("busy", "1 external connection(s) are still attached (process 4242)", 409)
    launches[0].runtime.refuse = busy
    assert raised(w.begin_unattached, "c1") is busy
    assert w._quiet is None
    w.assert_attach_allowed()
    assert w.work(lambda rt: "ok").result(5) == "ok"
    launches[0].runtime.refuse = None
    w.begin_unattached("c1")
    w.close()


def test_a_window_that_is_not_running_has_no_clients_and_is_not_launched(tmp_path):
    w, launches = quiet_window(tmp_path)
    w.begin_unattached("c1")
    assert w._quiet == "c1" and launches == []
    w.close()


def test_begin_waits_for_a_launching_browser_before_checking(tmp_path):
    release, started = threading.Event(), threading.Event()
    launches: list[QuietLaunch] = []

    def slow_launcher(spec):
        started.set()
        release.wait(10)
        launch = QuietLaunch()
        launch.runtime.window = w
        launches.append(launch)
        return launch

    spec = WindowSpec(name="w", browser="chrome", engine="inject", profile=tmp_path / "p", unattached=True)
    w = Window(spec, launcher=slow_launcher)
    booting = w.owner(lambda rt: None)
    assert started.wait(5)
    outcome: list = []
    t = threading.Thread(target=lambda: outcome.append(w.begin_unattached("c1")))
    t.start()
    time.sleep(0.2)
    assert t.is_alive() and w._quiet == "c1"  # reserved, but waiting for the launch to finish
    release.set()
    t.join(10)
    booting.result(5)
    assert outcome == [None] and len(launches[0].runtime.checks) == 1
    w.close()


@needs_proc
def test_unregistered_or_revoked_attachment_blocks_a_quiet_reservation_until_it_detaches(tmp_path, devtools, monkeypatch):
    monkeypatch.setattr(unattached, "CLIENT_SETTLE_S", 0.2)
    runtime = http_runtime(devtools)
    spec = WindowSpec(name="w", browser="chrome", engine="inject", profile=tmp_path / "p", unattached=True)
    launch = QuietLaunch(runtime)
    w = Window(spec, launcher=lambda s: launch)
    w.start()
    # A live CDP-style connection nobody registered (or whose lease was revoked).
    raw = socket.create_connection(("127.0.0.1", devtools.port))
    try:
        assert wait_until(lambda: profiles.established_clients(devtools.port) == [os.getpid()])
        err = raised(w.begin_unattached, "c1")
        assert (err.code, err.status) == ("busy", 409) and str(os.getpid()) in err.detail
        assert w._quiet is None
        w.assert_attach_allowed()  # the refused reservation left nothing behind
        assert raised(w.begin_unattached, "c2").code == "busy"
    finally:
        raw.close()
    assert wait_until(lambda: profiles.established_clients(devtools.port) == [])
    w.begin_unattached("c1")
    assert w._quiet == "c1"
    assert runtime.pages_info() == []  # its own HTTP use never counts as a client
    w.close()


# ── TransientLane ─────────────────────────────────────────────────────────


class LaneHarness:
    def __init__(self, size=2, refuse=None, make_error=None):
        self.events: list[str] = []
        self.made: list[AttachedRuntime] = []
        self.refuse = refuse
        self.make_error = make_error
        self.deadlines = Deadlines("lane-test")
        launch = SimpleNamespace(events=self.events)

        def make():
            if self.make_error is not None:
                raise self.make_error
            rt = AttachedRuntime(launch)
            self.made.append(rt)
            return rt

        self.lane = TransientLane("lane-test", size, make, self.deadlines, self.enter, self.leave)

    def enter(self):
        if self.refuse is not None:
            raise self.refuse
        self.events.append("enter")

    def leave(self):
        self.events.append("leave")

    def close(self):
        self.lane.close(5)
        self.deadlines.close()


def test_lane_orders_gate_attachment_and_completion():
    h = LaneHarness()
    try:
        fut = h.lane.submit(lambda rt: h.events.append("run") or "r", 5)
        fut.add_done_callback(lambda f: h.events.append("done"))
        assert fut.result(5) == "r"
        assert wait_until(lambda: h.events[-1:] == ["done"])
        assert h.events == ["enter", "attach", "run", "stop", "leave", "done"]
        assert wait_until(lambda: h.lane.thread_count == 0)  # nothing is retained while idle
    finally:
        h.close()


def test_lane_refused_job_never_attaches_or_leaves():
    refusal = ServiceError("busy", "reserved", 409)
    h = LaneHarness(refuse=refusal)
    try:
        assert raised(h.lane.submit(lambda rt: 1, 5).result, 5) is refusal
        assert h.made == [] and h.events == []
    finally:
        h.close()


def test_lane_attach_failure_maps_and_still_leaves():
    h = LaneHarness(make_error=RuntimeError("nope"))
    try:
        err = raised(h.lane.submit(lambda rt: 1, 5).result, 5)
        assert err.code == "window_stopped" and "nope" in err.detail
        assert h.events == ["enter", "leave"]
        refusal = ServiceError("launch_failed", "no context")
        h.make_error = refusal
        assert raised(h.lane.submit(lambda rt: 1, 5).result, 5) is refusal
    finally:
        h.close()


def test_lane_is_bounded_and_times_out_with_busy():
    h = LaneHarness(size=1)
    gate, entered = threading.Event(), threading.Event()
    try:
        running = h.lane.submit(blocking_job(gate, entered), 5)
        assert entered.wait(5)
        err = raised(h.lane.submit(lambda rt: "late", 0.2).result, 3)
        assert (err.code, err.status) == ("busy", 503)
        assert h.lane.thread_count == 1
        gate.set()
        assert running.result(5) == "blocking"
        assert h.lane.submit(lambda rt: "ok", 5).result(5) == "ok"
    finally:
        gate.set()
        h.close()


def test_lane_close_fails_queue_returns_stuck_workers_and_refuses_new_work():
    h = LaneHarness(size=1)
    gate, entered = threading.Event(), threading.Event()
    first = h.lane.submit(blocking_job(gate, entered), None)
    assert entered.wait(5)
    queued = h.lane.submit(lambda rt: "never", None)
    stuck = h.lane.close(0.2)
    assert len(stuck) == 1  # still attached: the caller learns it, the slot is not forgotten
    assert raised(queued.result, 3).code == "window_stopped"
    assert raised(h.lane.submit(lambda rt: 1, 1).result, 1).code == "window_stopped"
    gate.set()
    assert first.result(5) == "blocking"
    assert wait_until(lambda: h.events[-2:] == ["stop", "leave"])
    assert len(h.made) == 1
    h.deadlines.close()


def test_lane_future_is_not_complete_while_the_attachment_is_alive():
    h = LaneHarness()
    stopping, release = threading.Event(), threading.Event()

    class SlowStop(AttachedRuntime):
        def stop_playwright(self):
            stopping.set()
            release.wait(5)
            super().stop_playwright()

    launch = SimpleNamespace(events=h.events)
    lane = TransientLane("slow", 1, lambda: SlowStop(launch), h.deadlines, h.enter, h.leave)
    try:
        fut = lane.submit(lambda rt: "r", 5)
        assert stopping.wait(5)
        time.sleep(0.1)
        assert not fut.done() and "leave" not in h.events  # still attached: nothing released
        release.set()
        assert fut.result(5) == "r"
        assert h.events[-2:] == ["stop", "leave"]
    finally:
        release.set()
        lane.close(5)
        h.close()
