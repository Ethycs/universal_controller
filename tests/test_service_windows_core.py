"""Window core: config validation, profile guards, lanes and owner lifecycle.

No browser is launched: owner/worker threads run against fake launches and
fake runtimes, so these pin the concurrency and lifecycle rules, not Playwright.
"""

import concurrent.futures as cf
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from uc_browser.service import profiles, windows, workers
from uc_browser.service.errors import ServiceError
from uc_browser.service.windows import ConfigError, Window, WindowManager, WindowSpec


def write_config(tmp_path, windows_cfg, name="windows.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"windows": windows_cfg}))
    return path


def code_of(exc_info):
    return exc_info.value.code


# ── config ────────────────────────────────────────────────────────────────


def test_default_window_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UC_WINDOWS_FILE", raising=False)
    mgr = WindowManager()
    assert list(mgr.windows) == ["default"]
    spec = mgr.get("default").spec
    assert (spec.browser, spec.engine, spec.stealth, spec.lazy) == ("chromium", "extension", True, True)
    assert spec.profile.name == ".uc_chromium_profile"


def test_explicit_missing_config_is_an_error(tmp_path, monkeypatch):
    with pytest.raises(ConfigError):
        WindowManager(tmp_path / "nope.json")
    monkeypatch.setenv("UC_WINDOWS_FILE", str(tmp_path / "also-nope.json"))
    with pytest.raises(ConfigError):
        WindowManager()


def test_repeated_and_nested_resolved_profiles_rejected(tmp_path):
    p = tmp_path / "p"
    with pytest.raises(ConfigError, match="share profile"):
        WindowManager(write_config(tmp_path, {
            "a": {"profile": str(p)},
            "b": {"profile": str(tmp_path / "x" / ".." / "p")},
        }))
    with pytest.raises(ConfigError, match="nested"):
        WindowManager(write_config(tmp_path, {
            "a": {"profile": str(p)},
            "b": {"profile": str(p / "inner")},
        }))


@pytest.mark.parametrize("raw", [
    {"browser": "chrome", "engine": "extension"},
    {"browser": "firefox", "engine": "extension"},
    {"browser": "opera"},
    {"engine": "magic"},
    {"control_workers": 0},
    {"control_workers": True},
    {"control_workers": 99},
    {"lazy": "yes"},
    {"stealth": 1},
    {"browser": "firefox", "engine": "inject", "stealth": True},
    {"bogus": 1},
    {"queue_timeout_s": 0},
    {"queue_timeout_s": -1},
    {"queue_timeout_s": True},
    {"queue_timeout_s": "5"},
    {"queue_timeout_s": None},
    {"queue_timeout_s": float("nan")},
    {"queue_timeout_s": float("inf")},
])
def test_bad_window_config_rejected(tmp_path, raw):
    with pytest.raises(ConfigError):
        WindowManager(write_config(tmp_path, {"w": {"profile": str(tmp_path / "p"), **raw}}))


def test_bad_names_missing_profile_duplicate_keys(tmp_path):
    with pytest.raises(ConfigError):
        WindowManager(write_config(tmp_path, {"bad name": {"profile": str(tmp_path / "p")}}))
    with pytest.raises(ConfigError):
        WindowManager(write_config(tmp_path, {"w": {"browser": "chromium"}}))
    dup = tmp_path / "dup.json"
    dup.write_text('{"windows": {"w": {"profile": "a"}, "w": {"profile": "b"}}}')
    with pytest.raises(ConfigError, match="duplicate"):
        WindowManager(dup)
    empty = write_config(tmp_path, {}, "empty.json")
    with pytest.raises(ConfigError):
        WindowManager(empty)


def test_defaults_offers_and_lookup(tmp_path):
    mgr = WindowManager(write_config(tmp_path, {
        "c": {"browser": "chrome", "engine": "inject", "profile": str(tmp_path / "c")},
        "f": {"browser": "firefox", "engine": "none", "profile": str(tmp_path / "f"), "lazy": False},
    }))
    c, f = mgr.get("c").spec, mgr.get("f").spec
    assert c.stealth is False and c.control_workers == 2 and c.lazy is True
    assert c.offers == ["attach", "control", "fetch", "cookies", "snapshot", "mhtml"]
    assert f.offers == ["fetch", "cookies", "snapshot"]  # no CDP, no engine
    with pytest.raises(ServiceError) as err:
        mgr.get("missing")
    assert (err.value.code, err.value.status) == ("no_such_window", 404)


# ── profile guards ────────────────────────────────────────────────────────


def test_held_profile_refused_stale_lock_ignored(tmp_path, monkeypatch):
    profile = tmp_path / "prof"
    profile.mkdir()
    lock = profile / "SingletonLock"
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", lock)
    monkeypatch.setattr(profiles, "_cmdline_names_profile", lambda pid, prof: True)
    with pytest.raises(ServiceError) as err:
        profiles.assert_profile_available(profile)
    assert (err.value.code, err.value.status) == ("profile_held", 409)
    # pid alive but its command line is another profile: pid reuse, not held
    monkeypatch.setattr(profiles, "_cmdline_names_profile", lambda pid, prof: False)
    profiles.assert_profile_available(profile)
    # dead pid: stale
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock.unlink()
    os.symlink(f"{socket.gethostname()}-{dead.pid}", lock)
    monkeypatch.setattr(profiles, "_cmdline_names_profile", lambda pid, prof: True)
    profiles.assert_profile_available(profile)
    assert os.path.islink(lock)  # a lock is never deleted by the guard


def test_lock_from_another_host_counts_as_held(tmp_path):
    profile = tmp_path / "prof"
    profile.mkdir()
    os.symlink("some-other-host-1234", profile / "SingletonLock")
    with pytest.raises(ServiceError) as err:
        profiles.assert_profile_available(profile)
    assert err.value.code == "profile_held"


def test_stale_devtools_port_is_rejected(tmp_path):
    f = tmp_path / "DevToolsActivePort"
    f.write_text("9\n/devtools/browser/old\n")
    old = time.time() - 3600
    os.utime(f, (old, old))
    with pytest.raises(ServiceError) as err:
        profiles.wait_for_endpoint(tmp_path, started=time.time(), timeout=0.3)
    assert err.value.code == "launch_failed"
    profiles.remove_stale_endpoint(tmp_path)
    assert not f.exists()


@pytest.mark.skipif(profiles.fcntl is None, reason="POSIX flock only")
def test_launch_lock_serialises_launches(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with profiles.launch_lock(tmp_path):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(5)
    with pytest.raises(ServiceError) as err:
        with profiles.launch_lock(tmp_path, timeout=0.2):
            pass
    assert err.value.code == "profile_held"
    release.set()
    t.join(5)
    with profiles.launch_lock(tmp_path, timeout=1):
        pass


# ── runtime (fake context, no CDP) ────────────────────────────────────────


class FakePage:
    def __init__(self, url="about:blank", ready=False):
        self.url = url
        self.closed = False
        self.ready = ready
        self.evaluated = []
        self.waited = 0

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        self.evaluated.append(js)
        if "typeof window.__UC" in js:
            return False
        if js == "BUNDLE":
            self.ready = True
            return None
        return self.ready

    def wait_for_function(self, js, timeout=None):
        self.waited += 1


class FakeContext:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.handlers = {}

    def on(self, name, fn):
        self.handlers[name] = fn

    def cookies(self, *_):
        return []


def make_runtime(engine="none", pages=()):
    ctx = FakeContext(pages)
    return workers.Runtime(window="w", engine=engine, kind="firefox", pw=None, browser=None,
                           context=ctx, cdp=None, role="owner"), ctx


def test_non_cdp_ids_are_stable_and_missing_target_is_distinct_404():
    p1, p2 = FakePage(), FakePage()
    rt, ctx = make_runtime(pages=[p1, p2])
    ids = rt.scan()
    assert len(ids) == 2 and rt.scan().keys() == ids.keys()
    tid1 = rt.target_id(p1)
    assert rt.resolve(tid1) is p1
    p1.closed = True
    started = time.monotonic()
    with pytest.raises(ServiceError) as err:
        rt.resolve(tid1)
    assert (err.value.code, err.value.status) == ("no_such_target", 404)
    assert time.monotonic() - started < 2  # no propagation wait for a target the browser lacks
    with pytest.raises(ServiceError):
        rt.target_id(p1)


def test_runtime_refuses_foreign_threads():
    rt, _ = make_runtime(pages=[FakePage()])
    errors = []

    def other():
        for use in (lambda: rt.context, lambda: rt.scan(), lambda: rt.resolve("x")):
            try:
                use()
            except RuntimeError as exc:
                errors.append(exc)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert len(errors) == 3


def test_extension_engine_never_injects_or_waits_on_blank_page():
    rt, _ = make_runtime(engine="extension")
    page = FakePage("about:blank")
    assert rt.ensure_engine(page) is False
    assert page.waited == 0
    assert not any(js == "BUNDLE" for js in page.evaluated)
    ready = FakePage("https://example.com/", ready=True)
    assert rt.ensure_engine(ready) is True and ready.waited == 0
    pending = FakePage("https://example.com/")
    assert rt.ensure_engine(pending) is True and pending.waited == 1  # one wait; the method's own returns at once


def test_inject_engine_evaluates_bundle_when_absent(monkeypatch):
    monkeypatch.setattr(workers, "engine_bundle", lambda: "BUNDLE")
    rt, _ = make_runtime(engine="inject")
    page = FakePage("https://example.com/")
    assert rt.ensure_engine(page) is True
    assert "BUNDLE" in page.evaluated


# ── lanes ─────────────────────────────────────────────────────────────────


class FakeWorkerRuntime:
    def __init__(self):
        self.tid = threading.get_ident()
        self.alive = True
        self.stopped = False

    def heartbeat(self):
        return self.alive

    def stop_playwright(self):
        assert threading.get_ident() == self.tid  # stopped on its own thread
        self.stopped = True


def make_lane(size=2, factory=FakeWorkerRuntime, **kw):
    deadlines = workers.Deadlines("test")
    lane = workers.Lane("test", size, factory, deadlines, heartbeat_s=0.05, **kw)
    return lane, deadlines


def test_lane_is_bounded_and_times_out_with_busy():
    lane, deadlines = make_lane(size=2)
    gate = threading.Event()
    try:
        running = [lane.submit(lambda rt: (gate.wait(5), rt.tid)[1], 5) for _ in range(2)]
        time.sleep(0.2)
        waiting = lane.submit(lambda rt: "late", 0.2)
        with pytest.raises(ServiceError) as err:
            waiting.result(3)
        assert (err.value.code, err.value.status) == ("busy", 503)
        assert lane.thread_count <= 2
        gate.set()
        tids = {f.result(5) for f in running}
        assert len(tids) == 2 and threading.get_ident() not in tids
        assert lane.submit(lambda rt: "ok", 5).result(5) == "ok"
    finally:
        gate.set()
        lane.close(5)
        deadlines.close()
    assert lane.thread_count == 0


def test_lane_startup_failure_fails_queued_jobs_then_recovers():
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("no browser")
        return FakeWorkerRuntime()

    lane, deadlines = make_lane(size=1, factory=factory)
    try:
        with pytest.raises(ServiceError) as err:
            lane.submit(lambda rt: 1, 5).result(5)
        assert err.value.code == "window_stopped"
        assert lane.submit(lambda rt: 2, 5).result(5) == 2
    finally:
        lane.close(5)
        deadlines.close()


def test_lane_close_fails_queue_stops_playwright_on_owning_thread():
    made = []

    def factory():
        rt = FakeWorkerRuntime()
        made.append(rt)
        return rt

    lane, deadlines = make_lane(size=1, factory=factory)
    gate = threading.Event()
    first = lane.submit(lambda rt: gate.wait(5), None)
    time.sleep(0.1)
    queued = lane.submit(lambda rt: "never", None)
    closer = threading.Thread(target=lambda: lane.close(5))
    closer.start()
    with pytest.raises(ServiceError):
        queued.result(3)
    gate.set()
    closer.join(5)
    assert first.result(1) is True
    assert made and all(rt.stopped for rt in made)
    with pytest.raises(ServiceError):
        lane.submit(lambda rt: 1, 1).result(1)
    deadlines.close()


def test_cancelled_queued_job_never_runs():
    lane, deadlines = make_lane(size=1)
    gate = threading.Event()
    ran = []
    try:
        lane.submit(lambda rt: gate.wait(5), None)
        time.sleep(0.1)
        fut = lane.submit(lambda rt: ran.append(1), None)
        assert fut.cancel()
        gate.set()
        time.sleep(0.3)
        assert not ran
    finally:
        gate.set()
        lane.close(5)
        deadlines.close()


# ── owner lifecycle (fake launches) ───────────────────────────────────────


class FakeRuntime:
    def __init__(self):
        self.alive = True
        self.stopped = False
        self.tid = None

    def heartbeat(self):
        return self.alive

    def stop_playwright(self):
        self.stopped = True

    def pages_info(self):
        return [{"target_id": "t1", "url": "about:blank", "age_s": 0.0}]


class FakeLaunch:
    def __init__(self):
        self.runtime = FakeRuntime()
        self.cdp = None
        self.proc = None
        self.make_worker_runtime = None
        self.closed = False
        self.killed = False

    def close_browser(self):
        self.closed = True

    def kill(self):
        self.killed = True


def make_window(tmp_path, launcher, **spec_kw):
    spec = WindowSpec(name="w", browser=spec_kw.pop("browser", "firefox"), engine=spec_kw.pop("engine", "inject"),
                      profile=tmp_path / "p", **spec_kw)
    return Window(spec, launcher=launcher)


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def fast_heartbeat(monkeypatch):
    monkeypatch.setattr(windows, "OWNER_HEARTBEAT_S", 0.05)


def test_owner_jobs_run_on_one_thread_and_listing_never_launches(tmp_path):
    launches = []

    def launcher(spec):
        launches.append(FakeLaunch())
        return launches[-1]

    w = make_window(tmp_path, launcher)
    info = w.info()
    assert (info["running"], info["pages"], info["cdp"], launches) == (False, [], None, [])
    tids = {w.owner(lambda rt: threading.get_ident()).result(5) for _ in range(5)}
    assert len(tids) == 1 and threading.get_ident() not in tids
    assert w.running and w.generation == 1 and len(launches) == 1
    assert w.info()["pages"][0]["target_id"] == "t1"
    w.close()
    assert launches[0].closed and launches[0].runtime.stopped
    assert not w.running


def test_launch_failure_reaches_pending_jobs_and_next_use_relaunches(tmp_path):
    calls = []

    def launcher(spec):
        calls.append(1)
        if len(calls) == 1:
            raise ServiceError("profile_held", "held", 409)
        return FakeLaunch()

    w = make_window(tmp_path, launcher)
    with pytest.raises(ServiceError) as err:
        w.owner(lambda rt: 1).result(5)
    assert (err.value.code, err.value.status) == ("profile_held", 409)
    assert not w.running and w.generation == 0
    assert w.owner(lambda rt: 2).result(5) == 2
    assert w.generation == 1
    w.close()


def test_crash_fails_nothing_silently_and_relaunches_lazily(tmp_path):
    launches = []

    def launcher(spec):
        launches.append(FakeLaunch())
        return launches[-1]

    w = make_window(tmp_path, launcher)
    w.start()
    assert w.generation == 1
    launches[0].runtime.alive = False  # heartbeat reports a dead browser
    assert wait_until(lambda: not w.running)
    assert wait_until(lambda: launches[0].closed and launches[0].runtime.stopped)
    assert len(launches) == 1  # no automatic relaunch
    assert w.info()["running"] is False and len(launches) == 1
    assert w.owner(lambda rt: "again").result(5) == "again"
    assert w.generation == 2 and len(launches) == 2
    w.close()


def test_close_is_final_and_idempotent(tmp_path):
    w = make_window(tmp_path, lambda spec: FakeLaunch())
    w.start()
    w.close()
    w.close()
    with pytest.raises(ServiceError) as err:
        w.owner(lambda rt: 1).result(2)
    assert err.value.code == "window_stopped"


def test_non_cdp_work_runs_on_owner(tmp_path):
    w = make_window(tmp_path, lambda spec: FakeLaunch())
    owner_tid = w.owner(lambda rt: threading.get_ident()).result(5)
    assert w.work(lambda rt: threading.get_ident(), long=True).result(5) == owner_tid
    w.close()


def test_work_uses_configured_queue_timeout_and_keeps_explicit_values(tmp_path):
    launch = FakeLaunch()
    launch.make_worker_runtime = FakeWorkerRuntime
    mgr = WindowManager(write_config(tmp_path, {"w": {
        "profile": str(tmp_path / "p"), "browser": "chromium", "engine": "inject",
        "control_workers": 1, "queue_timeout_s": 0.3,
    }}))
    w = mgr.get("w")
    lanes = windows._Lanes(launch, w.spec)
    w._lanes = lambda: lanes
    gate, entered = threading.Event(), threading.Event()

    def block(rt):
        entered.set()
        return gate.wait(10)

    try:
        running = w.work(block, queue_timeout=None)
        assert entered.wait(5)
        waiting = w.work(lambda rt: "late")  # omitted: the configured 0.3s deadline
        with pytest.raises(ServiceError) as err:
            waiting.result(3)
        assert (err.value.code, err.value.status) == ("busy", 503)
        # explicit None disables the deadline: still queued well past 0.3s
        patient = w.work(lambda rt: "patient", queue_timeout=None)
        with pytest.raises(cf.TimeoutError):
            patient.result(0.6)
        gate.set()
        assert running.result(5) is True
        assert patient.result(5) == "patient"
    finally:
        gate.set()
        lanes.close(5)


def test_manager_start_eager_reports_failures_without_raising(tmp_path):
    mgr = WindowManager(write_config(tmp_path, {
        "eager": {"browser": "firefox", "engine": "none", "profile": str(tmp_path / "e"), "lazy": False},
        "lazy": {"browser": "firefox", "engine": "none", "profile": str(tmp_path / "l")},
    }))

    def boom(spec):
        raise ServiceError("launch_failed", "no browser here")

    for window in mgr.windows.values():
        window._launcher = boom
    failures = mgr.start_eager()
    assert list(failures) == ["eager"] and failures["eager"].code == "launch_failed"
    mgr.close()


# ── control validation (never reaches a worker) ───────────────────────────


def test_control_refusals_precede_workers_and_launch(tmp_path):
    launches = []

    def launcher(spec):
        launches.append(1)
        return FakeLaunch()

    none_window = make_window(tmp_path, launcher, engine="none")
    with pytest.raises(ServiceError) as err:
        none_window.control("t", "detect_all", [], {}).result(2)
    assert (err.value.code, err.value.status) == ("no_controls", 409)

    w = make_window(tmp_path, launcher)
    cases = [
        (("t", "_wait_ready", [], {}), "no_such_control", 404),
        (("t", "start", [], {}), "no_such_control", 404),  # public, but not page-first
        (("t", "nope", [], {}), "no_such_control", 404),
        (("t", "detect", [], {"unknown": 1}), "bad_arguments", 400),
        (("t", "scan_action", ["x"], {}), "bad_arguments", 400),  # callable argument
        (("t", "scroll_feed", [], {"on_item": "x"}), "bad_arguments", 400),
        (("t", "detect", ["search"], {}), "no_such_target", 404),  # valid, but window not running
    ]
    for call, code, status in cases:
        with pytest.raises(ServiceError) as err:
            w.control(*call).result(2)
        assert (err.value.code, err.value.status) == (code, status), call
    assert launches == []  # none of it launched a browser


def test_control_allowlist_is_page_first_public_methods():
    names = windows.control_names()
    assert "detect_all" in names and "scan_action" in names and "chat" in names
    assert all(not n.startswith("_") for n in names)
    assert not {"start", "close", "open", "new_page", "is_alive"} & set(names)
    assert windows.LONG_CONTROLS <= set(names)


def test_chain_propagates_result_and_cancellation():
    src, dst = cf.Future(), cf.Future()
    windows._chain(src, dst)
    src.set_result(3)
    assert dst.result(1) == 3
    src2, dst2 = cf.Future(), cf.Future()
    windows._chain(src2, dst2)
    assert dst2.cancel()
    assert src2.cancelled()
