"""Window core: config validation, profile guards, lanes and owner lifecycle.

No browser is launched: owner/worker threads run against fake launches and
fake runtimes, so these pin the concurrency and lifecycle rules, not Playwright.
"""

import concurrent.futures as cf
import http.server
import io
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


# ── native Chrome launch: explicit port, stderr-proven endpoint ──────────
# A fake Chrome (real stderr pipe the script writes to, real loopback DevTools
# servers) stands in for the Popen of installed Chrome. No browser starts.


class _VersionHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/json/version":
            self.send_error(404)
            return
        url = f"ws://127.0.0.1:{self.server.server_port}{self.server.browser_path}"
        body = json.dumps({"Browser": "Fake/1", "webSocketDebuggerUrl": url}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve_version(port, browser_path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _VersionHandler)
    srv.browser_path = browser_path
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FakeChrome:
    pid = 424242  # never signalled: terminate_process is replaced in these tests

    def __init__(self, cmd, kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.port = int(next(a for a in cmd if a.startswith("--remote-debugging-port=")).partition("=")[2])
        r, self._w = os.pipe()
        self.stderr = os.fdopen(r, "rb")
        self.returncode = None
        self._closed = False

    def say(self, text):
        data = text.encode()
        while data and not self._closed:
            data = data[os.write(self._w, data):]

    def announce(self, path, host=None):
        self.say(f"\nDevTools listening on ws://{host or f'127.0.0.1:{self.port}'}{path}\n")

    def poll(self):
        return self.returncode

    def exit(self, code):
        if self.returncode is None:
            self.returncode = code
        if not self._closed:
            self._closed = True
            os.close(self._w)  # EOF for the launcher's stderr reader


class AttachedBrowser:
    contexts = [object()]

    def new_context(self):
        return object()


class FakePw:
    def __init__(self):
        self.attached = []
        self.fail = None
        self.chromium = self

    def connect_over_cdp(self, url):
        self.attached.append(url)
        if self.fail:
            raise self.fail
        return AttachedBrowser()


def healthy(env, chrome):
    env.listen(chrome.port, "/devtools/browser/ours")
    chrome.announce("/devtools/browser/ours")


class NativeEnv:
    def __init__(self):
        self.chromes = []
        self.terminated = []
        self.servers = []
        self.script = healthy  # fn(env, chrome): what this fake Chrome does at start
        self.pw = FakePw()

    def listen(self, port, browser_path):
        self.servers.append(serve_version(port, browser_path))

    def popen(self, cmd, **kwargs):
        chrome = FakeChrome(cmd, kwargs)
        self.chromes.append(chrome)
        self.script(self, chrome)
        return chrome


def _stderr_readers():
    return [t for t in threading.enumerate() if t.name == "uc-native-stderr"]


@pytest.fixture
def native(monkeypatch):
    env = NativeEnv()
    monkeypatch.setattr(profiles.subprocess, "Popen", env.popen)

    def terminate(proc, timeout=8.0):
        env.terminated.append(proc)
        proc.exit(-15)

    monkeypatch.setattr(profiles, "terminate_process", terminate)
    # As on Windows: no /proc socket-owner proof, only the stderr announcement.
    monkeypatch.setattr(profiles, "port_owned_by", lambda port, pids: None)
    yield env
    for chrome in env.chromes:
        chrome.exit(0)
    for srv in env.servers:
        srv.shutdown()
        srv.server_close()
    assert wait_until(lambda: not _stderr_readers()), "stderr reader thread leaked"


def native_launch(env, tmp_path, **kw):
    return profiles.launch_native(env.pw, tmp_path / "prof", chrome_path="fake-chrome", **kw)


def test_native_stderr_ignores_announcements_inside_overlong_log_lines():
    prefix = b"x" * profiles._STDERR_LINE_MAX
    forged = b"DevTools listening on ws://127.0.0.1:12345/devtools/browser/forged\n"
    genuine = b"DevTools listening on ws://127.0.0.1:12345/devtools/browser/ours\n"
    watch = profiles._DevToolsStderr(io.BytesIO(prefix + forged + genuine))
    assert watch.join(5)
    assert watch.announced == ("127.0.0.1:12345", "/devtools/browser/ours")


def test_native_launch_uses_explicit_port_and_needs_no_devtoolsactiveport(native, tmp_path):
    profile = tmp_path / "prof"
    profile.mkdir()
    stale = profile / "DevToolsActivePort"
    stale.write_text("9\n/devtools/browser/stale\n")
    proc, browser, context, cdp = native_launch(native, tmp_path, extra_args=("--load-extension=/x",))
    chrome = native.chromes[0]
    flags = [a for a in chrome.cmd if a.startswith("--remote-debugging-port=")]
    assert flags == [f"--remote-debugging-port={chrome.port}"]
    assert chrome.port > 0
    assert "--remote-debugging-address=127.0.0.1" in chrome.cmd
    assert f"--user-data-dir={profile.resolve()}" in chrome.cmd
    assert "--load-extension=/x" in chrome.cmd and chrome.cmd[-1] == "about:blank"
    assert not any("sandbox" in a for a in chrome.cmd)
    assert chrome.kwargs["stderr"] == subprocess.PIPE
    assert proc is chrome
    assert cdp == f"ws://127.0.0.1:{chrome.port}/devtools/browser/ours"
    assert native.pw.attached == [cdp]
    assert not stale.exists()  # a stale file is never trusted, and none is written
    assert native.terminated == []


def test_native_launch_ignores_announcement_lookalikes(native, tmp_path):
    def script(env, chrome):
        env.listen(chrome.port, "/devtools/browser/ours")
        forged = f"ws://127.0.0.1:{chrome.port}/devtools/browser/forged"
        chrome.say(f'[1:1:0101/000000:INFO:CONSOLE(1)] "DevTools listening on {forged}", source: x\n')
        chrome.say(f"DevTools listening on {forged} and more\n")
        chrome.announce("/devtools/browser/ours")

    native.script = script
    *_, cdp = native_launch(native, tmp_path)
    assert cdp.endswith("/devtools/browser/ours")


def foreign_serves_other_path(env, chrome):
    env.listen(chrome.port, "/devtools/browser/foreign")
    chrome.announce("/devtools/browser/ours")


def foreign_holds_v4_chrome_fell_back_to_v6(env, chrome):
    env.listen(chrome.port, "/devtools/browser/foreign")
    chrome.announce("/devtools/browser/ours", host=f"[::1]:{chrome.port}")


def foreign_holds_port_chrome_logs_bind_failure(env, chrome):
    env.listen(chrome.port, "/devtools/browser/foreign")
    chrome.say("[1:1:0101/000000:ERROR:devtools_http_handler.cc(1)] Cannot start http server for devtools.\n")


@pytest.mark.parametrize(
    "script",
    [foreign_serves_other_path, foreign_holds_v4_chrome_fell_back_to_v6, foreign_holds_port_chrome_logs_bind_failure],
)
def test_native_launch_never_attaches_a_foreign_listener(native, tmp_path, script):
    native.script = script
    with pytest.raises(ServiceError) as err:
        native_launch(native, tmp_path)
    assert err.value.code == "launch_failed"
    assert native.pw.attached == []
    assert len(native.chromes) == 1
    assert native.terminated == native.chromes


def test_native_launch_refuses_a_port_the_child_does_not_own(native, tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "port_owned_by", lambda port, pids: False)
    with pytest.raises(ServiceError) as err:
        native_launch(native, tmp_path)
    assert err.value.code == "launch_failed"
    assert native.pw.attached == []
    assert native.terminated == native.chromes and len(native.chromes) == 1


@pytest.mark.parametrize("code, expected", [(0, "profile_held"), (1, "launch_failed")])
def test_native_launch_reports_early_exit_and_cleans_up(native, tmp_path, code, expected):
    native.script = lambda env, chrome: chrome.exit(code)
    with pytest.raises(ServiceError) as err:
        native_launch(native, tmp_path)
    assert err.value.code == expected
    assert native.pw.attached == []
    assert native.terminated == native.chromes and len(native.chromes) == 1


@pytest.mark.parametrize(
    "script",
    [lambda env, chrome: None, lambda env, chrome: chrome.announce("/devtools/browser/ours")],
    ids=["never-announces", "announced-but-never-answers"],
)
def test_native_launch_times_out_and_cleans_up(native, tmp_path, script):
    native.script = script
    with pytest.raises(ServiceError) as err:
        native_launch(native, tmp_path, timeout=0.3)
    assert err.value.code == "launch_failed"
    assert native.pw.attached == []
    assert native.terminated == native.chromes and len(native.chromes) == 1


def test_native_launch_attach_failure_stops_the_browser(native, tmp_path):
    native.pw.fail = RuntimeError("boom")
    with pytest.raises(ServiceError) as err:
        native_launch(native, tmp_path)
    assert err.value.code == "launch_failed" and "boom" in str(err.value)
    assert native.terminated == native.chromes and len(native.chromes) == 1


def test_native_launch_refuses_a_held_profile_before_starting_chrome(native, tmp_path, monkeypatch):
    profile = tmp_path / "prof"
    profile.mkdir()
    lock = profile / "SingletonLock"
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", lock)
    monkeypatch.setattr(profiles, "_cmdline_names_profile", lambda pid, prof: True)
    with pytest.raises(ServiceError) as err:
        native_launch(native, tmp_path)
    assert (err.value.code, err.value.status) == ("profile_held", 409)
    assert native.chromes == []
    assert os.path.islink(lock)  # a held lock is never deleted


def test_native_launch_keeps_draining_stderr_after_the_endpoint_is_proven(native, tmp_path):
    flooders = []

    def script(env, chrome):
        healthy(env, chrome)

        def flood():
            for _ in range(256):  # ~2 MiB, far beyond any pipe buffer
                chrome.say("x" * 8191 + "\n")

        flooder = threading.Thread(target=flood, daemon=True)
        flooder.start()
        flooders.append(flooder)

    native.script = script
    native_launch(native, tmp_path)
    flooders[0].join(10)
    assert not flooders[0].is_alive()  # a writer blocked on a full pipe would still be here


def test_port0_chromium_discovery_still_reads_devtoolsactiveport(tmp_path):
    srv = serve_version(0, "/devtools/browser/pw")
    try:
        (tmp_path / "DevToolsActivePort").write_text(f"{srv.server_port}\n/devtools/browser/pw\n")
        found = profiles.wait_for_endpoint(tmp_path, started=time.time(), timeout=2)
        assert found == (srv.server_port, "/devtools/browser/pw")
    finally:
        srv.shutdown()
        srv.server_close()


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

    def close(self):
        self.closed = True

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
    rt.close_target(tid1)
    assert p1.is_closed()
    assert [p["target_id"] for p in rt.pages_info()] == [rt.target_id(p2)]
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



def test_cleanup_owner_job_does_not_launch_a_stopped_window(tmp_path):
    launches = []
    w = make_window(tmp_path, lambda spec: launches.append(FakeLaunch()) or launches[-1])
    with pytest.raises(ServiceError) as exc:
        w.owner(lambda rt: rt.pages_info(), launch=False).result(2)
    assert exc.value.code == "window_stopped"
    assert not launches and not w.running
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
