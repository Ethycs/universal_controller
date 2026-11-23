"""Windows: one browser process on one profile, owned by one thread.

``WindowManager`` reads the config and holds the ``Window`` objects.
``Window`` owns, per launch:

* an **owner thread** with its own sync Playwright that launches the browser,
  applies per-context setup (API shim, ``state_file`` cookies, stealth), runs
  short owner jobs (create/close pages, cookies), pumps events and detects a
  dead browser, then tears everything down;
* two **lanes** of worker threads (short controls; long controls such as
  ``chat`` and ``scroll_feed``), each worker with its own Playwright and CDP
  attachment, grown on demand up to ``control_workers`` per lane.

Firefox and WebKit serve no CDP, so there is nothing for a worker to attach
to: ``Window.work`` runs on the owner thread instead. Every operation except
*attach* still works, but a long operation occupies the owner and delays
other owner jobs, ``Window.info`` (which then reports its last pages) and
shutdown of that window until it ends or the browser is closed.

Playwright objects never leave the thread that created them; ``Runtime``
enforces it (see ``workers.py``).

Environment
-----------
``UC_WINDOWS_FILE``     config path (default ``data/windows.json``)
``UC_SERVICE_HEADLESS`` ``1``/``true``: launch headless (not possible with the
                        ``extension`` engine, which needs a headed Chromium)
``UC_WINDOW_KEEPASSXC`` ``0``: do not stage/load KeePassXC for ``chrome`` windows
"""

from __future__ import annotations

import concurrent.futures as cf
import inspect
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from uc_browser import _paths

from . import profiles
from .errors import ServiceError
from .workers import (
    OWNER_HEARTBEAT_S,
    Deadlines,
    Job,
    JobQueue,
    Lane,
    Runtime,
    engine_bundle_path,
    is_closed_error,
    no_such_target,
    stopped,
)

log = logging.getLogger("uc_browser.service.windows")

BROWSERS = ("chromium", "chrome", "firefox", "webkit")
ENGINES = ("extension", "inject", "none")
CDP_BROWSERS = ("chromium", "chrome")
LONG_CONTROLS = frozenset({"chat", "scroll_feed"})
MAX_CONTROL_WORKERS = 16
LAUNCH_TIMEOUT_S = 120.0
INFO_WAIT_S = 5.0
CONTROL_QUEUE_TIMEOUT_S = 30.0
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}")
_SPEC_KEYS = frozenset(
    {"browser", "engine", "profile", "state_file", "stealth", "control_workers", "lazy", "queue_timeout_s"}
)
_UNSET = object()


class ConfigError(ValueError):
    """The windows config is invalid. Raised at load, never swallowed."""


# ── spec ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WindowSpec:
    name: str
    browser: str = "chromium"
    engine: str = "extension"
    profile: Path = None  # required; keeps the contract's field order
    state_file: Path | None = None
    stealth: bool | None = None  # None: True for chromium, else False
    control_workers: int = 2
    lazy: bool = True
    queue_timeout_s: float = CONTROL_QUEUE_TIMEOUT_S

    def __post_init__(self):
        n = self.name
        if not isinstance(n, str) or not _NAME_RE.fullmatch(n):
            raise ConfigError(f"window name {n!r} must match {_NAME_RE.pattern}")
        if self.browser not in BROWSERS:
            raise ConfigError(f"window {n}: browser {self.browser!r} not in {BROWSERS}")
        if self.engine not in ENGINES:
            raise ConfigError(f"window {n}: engine {self.engine!r} not in {ENGINES}")
        if self.engine == "extension" and self.browser != "chromium":
            raise ConfigError(
                f"window {n}: engine 'extension' needs browser 'chromium' (branded Chrome, "
                f"Firefox and WebKit cannot load it); use 'inject' or 'none'"
            )
        if self.profile is None or not str(self.profile):
            raise ConfigError(f"window {n}: profile is required")
        if not isinstance(self.lazy, bool):
            raise ConfigError(f"window {n}: lazy must be a boolean")
        qt = self.queue_timeout_s
        if isinstance(qt, bool) or not isinstance(qt, (int, float)) or not math.isfinite(qt) or qt <= 0:
            raise ConfigError(f"window {n}: queue_timeout_s must be a finite number > 0")
        object.__setattr__(self, "queue_timeout_s", float(qt))
        cw = self.control_workers
        if isinstance(cw, bool) or not isinstance(cw, int) or not 1 <= cw <= MAX_CONTROL_WORKERS:
            raise ConfigError(f"window {n}: control_workers must be an integer in 1..{MAX_CONTROL_WORKERS}")
        stealth = self.stealth
        if stealth is None:
            stealth = self.browser == "chromium"
        elif not isinstance(stealth, bool):
            raise ConfigError(f"window {n}: stealth must be a boolean")
        elif stealth and self.browser in ("firefox", "webkit"):
            raise ConfigError(f"window {n}: stealth applies to Chromium-family browsers only")
        object.__setattr__(self, "stealth", stealth)
        object.__setattr__(self, "profile", Path(self.profile).expanduser().resolve())
        if self.state_file is not None:
            object.__setattr__(self, "state_file", Path(self.state_file).expanduser().resolve())

    @property
    def has_cdp(self) -> bool:
        return self.browser in CDP_BROWSERS

    @property
    def offers(self) -> list[str]:
        out = []
        if self.has_cdp:
            out.append("attach")
        if self.engine != "none":
            out.append("control")
        out += ["fetch", "cookies", "snapshot"]
        if self.has_cdp:
            out.append("mhtml")
        return out


def _no_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ConfigError(f"duplicate key {key!r} in windows config")
        out[key] = value
    return out


def _parse_spec(name: str, raw) -> WindowSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"window {name!r} must be an object")
    unknown = set(raw) - _SPEC_KEYS
    if unknown:
        raise ConfigError(f"window {name!r}: unknown keys {sorted(unknown)}")
    profile = raw.get("profile")
    if not isinstance(profile, str) or not profile.strip():
        raise ConfigError(f"window {name!r}: profile must be a non-empty path string")
    state_file = raw.get("state_file")
    if state_file is not None and (not isinstance(state_file, str) or not state_file.strip()):
        raise ConfigError(f"window {name!r}: state_file must be a path string")
    for key in ("browser", "engine"):
        if key in raw and not isinstance(raw[key], str):
            raise ConfigError(f"window {name!r}: {key} must be a string")
    kwargs = {
        k: raw[k]
        for k in ("browser", "engine", "stealth", "control_workers", "lazy", "queue_timeout_s")
        if k in raw
    }
    return WindowSpec(
        name=name,
        profile=Path(profile),
        state_file=Path(state_file) if state_file else None,
        **kwargs,
    )


def _check_profiles(specs: list[WindowSpec]) -> None:
    for i, a in enumerate(specs):
        for b in specs[i + 1:]:
            if a.profile == b.profile:
                raise ConfigError(f"windows {a.name!r} and {b.name!r} share profile {a.profile}")
            if a.profile in b.profile.parents or b.profile in a.profile.parents:
                raise ConfigError(f"windows {a.name!r} and {b.name!r} have nested profiles")


def default_spec() -> WindowSpec:
    """The single window used when there is no config: the profile and state
    file ``BrowserMode.CHROMIUM_EXT`` uses today."""
    from uc_browser import browser as _b

    return WindowSpec(
        name="default",
        browser="chromium",
        engine="extension",
        profile=_b._CHROME_PROFILE.parent / ".uc_chromium_profile",
        state_file=Path(_b._STATE_FILE),
    )


# ── control allowlist ─────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _control_table() -> dict:
    """Public ``UCBrowser`` methods whose first parameter after ``self`` is
    ``page``: ``{name: (original function, signature)}``. Wrappers installed
    by the client library are looked through (``functools.wraps``) so the
    service always runs the original body."""
    from uc_browser.browser import UCBrowser

    table = {}
    for name, attr in inspect.getmembers(UCBrowser, inspect.isfunction):
        if name.startswith("_"):
            continue
        fn = inspect.unwrap(attr)
        sig = inspect.signature(fn)
        if list(sig.parameters)[1:2] == ["page"]:
            table[name] = (fn, sig)
    return table


def control_names() -> list[str]:
    return sorted(_control_table())


def _closed_future(exc: BaseException) -> cf.Future:
    fut: cf.Future = cf.Future()
    fut.set_exception(exc)
    return fut


def _run_control(rt, target_id: str, name: str, args: tuple, kwargs: dict):
    fn, _ = _control_table()[name]
    page = rt.resolve(target_id)
    try:
        rt.ensure_engine(page)
        result = fn(rt.receiver(), page, *args, **kwargs)
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_closed_error(exc) or page.is_closed():
            raise no_such_target(f"page {target_id} was closed during {name}") from exc
        raise ServiceError("control_failed", f"{type(exc).__name__}: {exc}"[:2000]) from exc
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ServiceError("control_failed", f"{name} returned a non-JSON result") from exc
    return result


# ── launching (runs on the owner thread) ──────────────────────────────────


def _headless() -> bool:
    return os.environ.get("UC_SERVICE_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on")


class _Launch:
    """The browser one owner thread launched, and how to stop it."""

    def __init__(self, spec: WindowSpec, pw):
        self.spec = spec
        self.pw = pw
        self.context = None
        self.browser = None
        self.proc = None
        self.cdp: str | None = None
        self.browser_pid: int | None = None
        self.runtime: Runtime | None = None

    def make_worker_runtime(self) -> Runtime:
        """Runs on a worker thread: its own Playwright, its own attachment."""
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(self.cdp)
            if not browser.contexts:
                raise ServiceError("launch_failed", "attached browser has no default context")
            return Runtime(
                window=self.spec.name, engine=self.spec.engine, kind=self.spec.browser, pw=pw,
                browser=browser, context=browser.contexts[0], cdp=self.cdp, role="worker",
            )
        except BaseException:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass
            raise

    def close_browser(self) -> None:
        """Owner thread: stop the browser, interrupting every attachment."""
        spec = self.spec
        try:
            if spec.browser == "chrome":
                if self.proc is not None:
                    profiles.terminate_process(self.proc)
            elif self.context is not None:
                self.context.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("closing browser of %s: %s", spec.name, exc)
        pid = self.browser_pid
        if pid is not None and spec.browser != "chrome":
            deadline = time.monotonic() + 3.0
            while profiles.pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if profiles.pid_alive(pid):
                profiles.kill_profile_browser(pid, spec.profile)

    def kill(self) -> None:
        """Any thread: process-level stop (no Playwright objects touched)."""
        try:
            if self.proc is not None:
                profiles.terminate_process(self.proc, timeout=3.0)
            elif self.browser_pid is not None:
                profiles.kill_profile_browser(self.browser_pid, self.spec.profile, timeout=3.0)
        except Exception as exc:  # noqa: BLE001
            log.debug("killing browser of %s: %s", self.spec.name, exc)

    def abort(self) -> None:
        """Owner thread: undo a failed launch."""
        self.close_browser()
        try:
            self.pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _require_engine_files(spec: WindowSpec) -> None:
    if spec.engine == "none":
        return
    bundle = engine_bundle_path()
    missing = [str(p) for p in (bundle,) if not p.is_file()]
    if spec.engine == "extension" and not (_paths.ext_dir() / "manifest.json").is_file():
        missing.append(str(_paths.ext_dir() / "manifest.json"))
    if missing:
        raise ServiceError(
            "launch_failed",
            f"window {spec.name}: engine {spec.engine!r} needs {', '.join(missing)} "
            f"(the extension bundle is gitignored; build it with npm in extension/)",
        )


def _native_extra_args(spec: WindowSpec) -> list[str]:
    args = ["--headless=new"] if _headless() else []
    if os.environ.get("UC_WINDOW_KEEPASSXC", "1") != "0":
        try:
            from uc_browser.browser import _ensure_keepassxc_extension

            path = _ensure_keepassxc_extension()
        except Exception as exc:  # noqa: BLE001 - autofill is optional
            log.info("KeePassXC extension unavailable: %s", exc)
            path = None
        if path is not None:
            args += [f"--load-extension={path}", f"--disable-extensions-except={path}"]
    return args


def _launch_chromium(launch: _Launch) -> None:
    spec = launch.spec
    headless = _headless()
    if headless and spec.engine == "extension":
        raise ServiceError("launch_failed", "UC_SERVICE_HEADLESS cannot be used with the extension engine")
    args = [
        "--disable-blink-features=AutomationControlled",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
    ]
    if spec.engine == "extension":
        ext = str(_paths.ext_dir().resolve())
        args += [f"--load-extension={ext}", f"--disable-extensions-except={ext}"]
    spec.profile.mkdir(parents=True, exist_ok=True)
    with profiles.launch_lock(spec.profile):
        profiles.assert_profile_available(spec.profile)
        profiles.remove_stale_endpoint(spec.profile)
        started = time.time()
        launch.context = launch.pw.chromium.launch_persistent_context(
            str(spec.profile), headless=headless, channel=None, args=args
        )
        launch.browser = launch.context.browser
        port, path = profiles.wait_for_endpoint(spec.profile, None, started=started, timeout=30.0)
        launch.browser_pid = profiles.verify_endpoint_owner(spec.profile, port)
        launch.cdp = f"ws://127.0.0.1:{port}{path}"


def _launch_chrome(launch: _Launch) -> None:
    spec = launch.spec
    proc, browser, context, cdp = profiles.launch_native(
        launch.pw, spec.profile, extra_args=_native_extra_args(spec)
    )
    launch.proc, launch.browser, launch.context, launch.cdp = proc, browser, context, cdp
    launch.browser_pid = proc.pid


def _launch_other(launch: _Launch) -> None:
    spec = launch.spec
    browser_type = getattr(launch.pw, spec.browser)
    spec.profile.mkdir(parents=True, exist_ok=True)
    with profiles.launch_lock(spec.profile):
        profiles.assert_profile_available(spec.profile)
        launch.context = browser_type.launch_persistent_context(str(spec.profile), headless=_headless())
        launch.browser = launch.context.browser


def _setup_context(spec: WindowSpec, context) -> None:
    """Once per launch, on the context, in this order: API shim, saved
    cookies, stealth. Init scripts here reach pages created by any client."""
    from uc_browser.browser import _UC_API_SHIM_JS

    context.add_init_script(_UC_API_SHIM_JS)
    state = spec.state_file
    if state is not None:
        if state.is_file():
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
                cookies = data.get("cookies", []) if isinstance(data, dict) else None
                if not isinstance(cookies, list):
                    raise ValueError("expected an object with a 'cookies' list")
            except (OSError, ValueError) as exc:
                raise ServiceError("launch_failed", f"window {spec.name}: unreadable state_file {state}: {exc}") from exc
            if cookies:
                context.add_cookies(cookies)
                log.info("window %s: added %d cookies from %s", spec.name, len(cookies), state)
        else:
            log.info("window %s: state_file %s does not exist yet; starting without cookies", spec.name, state)
    if spec.stealth:
        try:
            from playwright_stealth import Stealth
        except ImportError as exc:
            raise ServiceError("launch_failed", f"window {spec.name}: stealth requested but playwright-stealth is missing") from exc
        Stealth().apply_stealth_sync(context)


def launch_browser(spec: WindowSpec) -> _Launch:
    """Launch ``spec``'s browser on the calling thread (which becomes the
    owner of its Playwright). Cleans up after itself on failure."""
    from playwright.sync_api import sync_playwright

    _require_engine_files(spec)
    pw = sync_playwright().start()
    launch = _Launch(spec, pw)
    try:
        {"chromium": _launch_chromium, "chrome": _launch_chrome}.get(spec.browser, _launch_other)(launch)
        _setup_context(spec, launch.context)
        launch.runtime = Runtime(
            window=spec.name, engine=spec.engine, kind=spec.browser, pw=pw,
            browser=launch.browser, context=launch.context, cdp=launch.cdp, role="owner",
        )
        return launch
    except BaseException as exc:
        launch.abort()
        if isinstance(exc, (ServiceError, KeyboardInterrupt, SystemExit)):
            raise
        holder = profiles.profile_holder(spec.profile)
        if holder is not None:
            raise ServiceError("profile_held", f"profile {spec.profile} is held by process {holder}: {exc}", 409) from exc
        raise ServiceError("launch_failed", f"window {spec.name}: {type(exc).__name__}: {exc}") from exc


# ── owner thread ──────────────────────────────────────────────────────────

_STARTING, _RUNNING, _STOPPING, _STOPPED = "starting", "running", "stopping", "stopped"


class _Lanes:
    def __init__(self, launch: _Launch, spec: WindowSpec):
        self.deadlines = Deadlines(spec.name)
        make = launch.make_worker_runtime
        self.short = Lane(f"{spec.name}-ctl", spec.control_workers, make, self.deadlines, keep=1)
        self.long = Lane(f"{spec.name}-long", spec.control_workers, make, self.deadlines, keep=0)

    def submit(self, fn, long: bool, queue_timeout: float | None) -> cf.Future:
        return (self.long if long else self.short).submit(fn, queue_timeout)

    def close(self, join_timeout: float = 10.0) -> None:
        stuck = self.short.close(join_timeout) + self.long.close(join_timeout)
        self.deadlines.close()
        if stuck:
            log.warning("%d worker thread(s) did not stop: %s", len(stuck), [t.name for t in stuck])


class _Owner(threading.Thread):
    """One launch of one window: launch, serve owner jobs, tear down."""

    def __init__(self, window: "Window", prev: "_Owner | None"):
        super().__init__(name=f"uc-owner-{window.spec.name}", daemon=True)
        self.window = window
        self.prev = prev
        self.queue = JobQueue()
        self.phase = _STARTING
        self.accepting = True  # guarded by window._lock
        self.closing = threading.Event()
        self.ready = threading.Event()
        self.launch: _Launch | None = None
        self.lanes: _Lanes | None = None
        self.cdp: str | None = None

    def request_stop(self) -> None:
        self.closing.set()
        self.queue.wake()

    def force_kill(self) -> None:
        launch = self.launch
        if launch is not None:
            launch.kill()

    def run(self) -> None:
        w = self.window
        try:
            self._launch()
        except BaseException as exc:  # noqa: BLE001
            err = exc if isinstance(exc, ServiceError) else ServiceError("launch_failed", f"{type(exc).__name__}: {exc}")
            log.warning("window %s launch failed: %s", w.spec.name, err)
            self._finish(err)
            return
        try:
            self._serve()
        finally:
            self._finish(stopped(f"window {w.spec.name} stopped"))

    def _launch(self) -> None:
        w = self.window
        if self.prev is not None and self.prev is not self:
            self.prev.join(60)
            if self.prev.is_alive():
                raise ServiceError("launch_failed", f"previous browser of window {w.spec.name} is still shutting down")
        launch = w._launcher(w.spec)
        with w._lock:
            w._generation += 1
            self.launch = launch
            self.cdp = launch.cdp
            if launch.cdp is not None:
                self.lanes = _Lanes(launch, w.spec)
            self.phase = _RUNNING
        log.info("window %s running (generation %d, cdp %s)", w.spec.name, w._generation, self.cdp)
        self.ready.set()

    def _serve(self) -> None:
        rt = self.launch.runtime
        last_beat = time.monotonic()
        while not self.closing.is_set():
            job = self.queue.get(OWNER_HEARTBEAT_S)
            failed = False
            if job is not None and job.claim():
                failed = job.run(rt) is not None
            now = time.monotonic()
            if job is None or failed or now - last_beat >= OWNER_HEARTBEAT_S:
                last_beat = now
                proc = self.launch.proc
                if not rt.heartbeat() or (proc is not None and proc.poll() is not None):
                    log.warning("window %s: browser is gone", self.window.spec.name)
                    return

    def _finish(self, err: ServiceError) -> None:
        w = self.window
        with w._lock:
            self.accepting = False
            self.phase = _STOPPING
        launch = self.launch
        if launch is not None:
            launch.close_browser()  # interrupts every attachment's pending calls
        if self.lanes is not None:
            self.lanes.close()
        for job in self.queue.drain():
            job.fail(err)
        if launch is not None and launch.runtime is not None:
            launch.runtime.stop_playwright()
        with w._lock:
            self.phase = _STOPPED
        self.ready.set()


# ── window ────────────────────────────────────────────────────────────────


def _noop(_rt) -> None:
    return None


def _chain(src: cf.Future, dst: cf.Future) -> None:
    def done(f: cf.Future):
        if f.cancelled():
            dst.cancel()
            return
        exc = f.exception()
        try:
            if exc is not None:
                dst.set_exception(exc)
            else:
                dst.set_result(f.result())
        except cf.InvalidStateError:
            pass

    src.add_done_callback(done)
    dst.add_done_callback(lambda d: d.cancelled() and src.cancel())


class Window:
    """One browser window. Thread-safe; all browser work happens on threads
    this object owns."""

    def __init__(self, spec: WindowSpec, launcher=None):
        self.spec = spec
        self._launcher = launcher or launch_browser
        self._lock = threading.RLock()
        self._owner: _Owner | None = None
        self._generation = 0
        self._closed = False
        self._pages_cache: list[dict] = []

    def __repr__(self) -> str:
        return f"<Window {self.spec.name} {'running' if self.running else 'stopped'} gen={self._generation}>"

    # state
    @property
    def running(self) -> bool:
        o = self._owner
        return o is not None and o.phase == _RUNNING

    @property
    def cdp(self) -> str | None:
        o = self._owner
        return o.cdp if o is not None and o.phase == _RUNNING else None

    @property
    def generation(self) -> int:
        """Incremented on every successful launch; leases are void across it."""
        return self._generation

    # owner jobs
    def _submit_owner(self, fn, *, launch: bool) -> cf.Future:
        with self._lock:
            if self._closed:
                return _closed_future(stopped(f"window {self.spec.name} is closed"))
            owner = self._owner
            if owner is None or not owner.accepting:
                if not launch:
                    return _closed_future(stopped(f"window {self.spec.name} is not running"))
                owner = _Owner(self, owner)
                self._owner = owner
                owner.start()
            elif not launch and owner.phase != _RUNNING:
                return _closed_future(stopped(f"window {self.spec.name} is not running"))
            job = Job(fn)
            try:
                owner.queue.put(job)
            except ServiceError as exc:
                return _closed_future(exc)
            return job.future

    def owner(self, fn) -> cf.Future:
        """Run ``fn(runtime)`` on the window's single owner thread, launching
        the browser first if it is not running (lazy relaunch). Keep ``fn``
        short. Never blocks: launch happens on the owner thread."""
        return self._submit_owner(fn, launch=True)

    def start(self, timeout: float = LAUNCH_TIMEOUT_S) -> dict:
        """Launch if needed and return ``info()``. Blocks; call from a plain thread."""
        fut = self._submit_owner(_noop, launch=True)
        try:
            fut.result(timeout=timeout)
        except cf.TimeoutError:
            fut.cancel()
            raise ServiceError("launch_failed", f"window {self.spec.name} not ready after {timeout:.0f}s") from None
        return self.info()

    def info(self) -> dict:
        """Wire base entry. Never launches. ``pages`` is freshly pumped."""
        pages: list[dict] = []
        if self.running:
            fut = self._submit_owner(lambda rt: rt.pages_info(), launch=False)
            try:
                pages = fut.result(timeout=INFO_WAIT_S)
                self._pages_cache = pages
            except cf.TimeoutError:
                fut.cancel()
                pages = list(self._pages_cache)  # owner busy (non-CDP long job)
            except ServiceError:
                pages = []
        running = self.running
        if not running:
            self._pages_cache = []
        return {
            "browser": self.spec.browser,
            "engine": self.spec.engine,
            "running": running,
            "offers": self.spec.offers,
            "cdp": self.cdp if running else None,
            "pages": pages if running else [],
            "queue_timeout_s": self.spec.queue_timeout_s,
        }

    # worker jobs
    def _lanes(self) -> _Lanes | None:
        o = self._owner
        return o.lanes if o is not None and o.phase == _RUNNING and o.accepting else None

    def work(self, fn, *, long: bool = False, queue_timeout=_UNSET) -> cf.Future:
        """Run ``fn(runtime)`` on a worker with its own Playwright and CDP
        attachment (``long=True``: the separate lane for long operations).
        ``queue_timeout`` bounds the wait for a free worker (``busy``, 503);
        omitted, it is the window's configured ``queue_timeout_s``; an explicit
        value, including ``None`` (no deadline), is passed through unchanged.
        Windows without CDP run it on the owner thread instead."""
        if queue_timeout is _UNSET:
            queue_timeout = self.spec.queue_timeout_s
        if not self.spec.has_cdp:
            return self.owner(fn)
        lanes = self._lanes()
        if lanes is not None:
            return lanes.submit(fn, long, queue_timeout)
        outer: cf.Future = cf.Future()
        boot = self._submit_owner(_noop, launch=True)

        def dispatch(f: cf.Future) -> None:
            try:
                f.result()
            except BaseException as exc:  # noqa: BLE001
                try:
                    outer.set_exception(exc)
                except cf.InvalidStateError:
                    pass
                return
            ready = self._lanes()
            if ready is None:
                try:
                    outer.set_exception(stopped(f"window {self.spec.name} stopped before the job could start"))
                except cf.InvalidStateError:
                    pass
                return
            _chain(ready.submit(fn, long, queue_timeout), outer)

        boot.add_done_callback(dispatch)
        return outer

    def control(self, target_id: str, name: str, args=(), kwargs=None) -> cf.Future:
        """Run the original ``UCBrowser.<name>(page, *args, **kwargs)`` on the
        page with this target id. Errors, including refusals, arrive through
        the returned future: ``no_such_control`` 404, ``no_controls`` 409
        (``engine: none``, before any worker), ``bad_arguments`` 400,
        ``no_such_target`` 404 (also when the window is not running; this
        never launches a browser), ``busy`` 503, ``control_failed`` 500."""
        args = tuple(args or ())
        kwargs = dict(kwargs or {})
        try:
            entry = _control_table().get(name) if isinstance(name, str) else None
            if entry is None:
                raise ServiceError("no_such_control", f"unknown control {name!r}", 404)
            if self.spec.engine == "none":
                raise ServiceError("no_controls", f"window {self.spec.name} has engine none", 409)
            _, sig = entry
            try:
                bound = sig.bind(None, None, *args, **kwargs)
                json.dumps([args, kwargs], allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ServiceError("bad_arguments", f"arguments do not fit {name}{sig}: {exc}", 400) from exc
            for pname, param in sig.parameters.items():
                if param.annotation is callable or param.annotation == "callable":
                    if param.default is param.empty or bound.arguments.get(pname) is not None:
                        raise ServiceError(
                            "bad_arguments", f"{name}: callable argument {pname!r} cannot cross the wire", 400
                        )
        except ServiceError as exc:
            return _closed_future(exc)
        if not self.running:
            return _closed_future(no_such_target(f"window {self.spec.name} is not running"))
        return self.work(
            lambda rt: _run_control(rt, target_id, name, args, kwargs),
            long=name in LONG_CONTROLS,
        )

    # shutdown
    def close(self, timeout: float = 30.0) -> None:
        """Close the browser from its owner (interrupting running work), stop
        every Playwright on its own thread, join with a bound. Idempotent; the
        window cannot be relaunched afterwards."""
        with self._lock:
            self._closed = True
            owner = self._owner
            if owner is not None and owner.accepting:
                owner.request_stop()
        if owner is None:
            return
        owner.join(timeout)
        if owner.is_alive():
            log.error("window %s owner did not stop within %.0fs; killing its browser", self.spec.name, timeout)
            owner.force_kill()
            owner.join(5)


# ── manager ───────────────────────────────────────────────────────────────


class WindowManager:
    """Reads the windows config and holds the windows.

    ``config_path`` (else ``UC_WINDOWS_FILE``, else ``data/windows.json``):
    when named explicitly it must exist; when only the default is missing,
    one ``default`` window on today's ``CHROMIUM_EXT`` profile is used.
    ``self.config`` is the parsed JSON (``{}`` for the default), so the owner
    of other sections (``sites``) reads the same file once.
    """

    def __init__(self, config_path=None):
        env = os.environ.get("UC_WINDOWS_FILE", "").strip()
        explicit = config_path is not None or bool(env)
        path = Path(config_path or env or "data/windows.json").expanduser()
        self.config_path: Path | None = None
        self.config: dict = {}
        if path.is_file():
            self.config_path = path.resolve()
            specs = self._load(path)
        elif explicit:
            raise ConfigError(f"windows config {path} does not exist")
        else:
            specs = [default_spec()]
        _check_profiles(specs)
        self.windows: dict[str, Window] = {s.name: Window(s) for s in specs}

    def _load(self, path: Path) -> list[WindowSpec]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_dupes)
        except OSError as exc:
            raise ConfigError(f"cannot read windows config {path}: {exc}") from exc
        except ValueError as exc:
            if isinstance(exc, ConfigError):
                raise ConfigError(f"{path}: {exc}") from exc
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("windows"), dict) or not data["windows"]:
            raise ConfigError(f"{path}: expected an object with a non-empty 'windows' object")
        self.config = data
        return [_parse_spec(name, raw) for name, raw in data["windows"].items()]

    def get(self, name: str) -> Window:
        window = self.windows.get(name) if isinstance(name, str) else None
        if window is None:
            raise ServiceError("no_such_window", f"no window named {name!r}", 404)
        return window

    def missing_engine_files(self) -> list[str]:
        """Names of windows that need the (gitignored) extension bundle and
        cannot launch without it. Cheap check for service start."""
        out = []
        for name, window in self.windows.items():
            try:
                _require_engine_files(window.spec)
            except ServiceError:
                out.append(name)
        return out

    def start_eager(self) -> dict[str, BaseException]:
        """Launch every ``lazy: false`` window concurrently. Returns the
        failures by window name (empty when all launched); never raises for a
        launch failure, so the caller decides whether the service starts."""
        pending = {n: w.owner(_noop) for n, w in self.windows.items() if not w.spec.lazy}
        failures: dict[str, BaseException] = {}
        for name, fut in pending.items():
            try:
                fut.result(timeout=LAUNCH_TIMEOUT_S)
            except BaseException as exc:  # noqa: BLE001
                failures[name] = exc
                log.error("eager launch of window %s failed: %s", name, exc)
        return failures

    def close(self) -> None:
        """Close every window in parallel (bounded). Idempotent."""
        threads = [
            threading.Thread(target=w.close, name=f"uc-close-{n}", daemon=True)
            for n, w in self.windows.items()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
