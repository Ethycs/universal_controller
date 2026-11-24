"""Service prototype probes B and C, rebuilt from
``docs/08 - Analysis/00 - Service Prototype Measurements.md`` (rows 1-24).

Two modes
---------

**Baseline (default).** Self-contained, needs no UC service. The script starts

* a loopback HTTP *fixture* server (the pages the probes load),
* a loopback HTTP *prototype service* that speaks the subset of wire contract
  v0 the probes need (``GET /windows``, ``POST /windows/{w}/start``,
  ``POST|DELETE /clients``, ``POST|DELETE /windows/{w}/pages``,
  ``POST /windows/{w}/pages/{t}/controls/{name}``). It is defined in this file
  and imports nothing from a future ``uc_browser.service``; it owns windows
  exactly as Build Spec §5 describes (owner thread per window, a control
  worker with its own Playwright instance and attachment, an unstarted
  ``UCBrowser`` as the receiver of page-taking controls),
* windows: three Playwright-launched Chromium windows (``a``, ``b``, ``c``) on
  temporary profiles with the UC extension and ``--remote-debugging-port=0``,
  and one installed Google Chrome window (``native``) on a temporary profile,
  with the engine bundle injected over CDP.

A client *process* (this same file, ``--role client-b``) then drives the
service only through HTTP + CDP, which is what makes the same client usable
against the real service later.

**Service mode (``--service-url URL``).** The prototype is not started. The
same client scenario and the same attachment-thread probes run against the real
service named on the command line. Rows that need the prototype's internals
(stealth before/after, shutdown, leftover processes, DevToolsActivePort file)
are printed ``SKIP`` with the reason rather than faked. Rows that mutate
context-level state of a service window (8, 10, 11, 23) run only with
``--destructive``.

Service-mode row 20 also requires ``navigator.webdriver is False`` on the
native page, before any engine injection. The historical baseline keeps its
original port-0 launch for comparison and reports the value without imposing
this new service acceptance requirement.

Every browser and driver process the script starts is tracked and killed at
exit (also on Ctrl-C and SIGTERM); temporary profiles are removed.

Exit status: 0 all rows measured and the model holds; 1 a row failed or was not
measured; 2 a model assertion was overturned (row 8 absent, row 10 present, row
15 not far below row 14): stop and re-derive before building further.

Headed windows need the desktop session: ``DISPLAY`` and ``XAUTHORITY`` default
to ``:0`` and ``/home/rabbit/.Xauthority`` when unset.
"""

from __future__ import annotations

import argparse
import atexit
import concurrent.futures
import http.server
import importlib.metadata
import inspect
import json
import os
import queue
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("DISPLAY", ":0")
os.environ.setdefault("XAUTHORITY", "/home/rabbit/.Xauthority")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BUNDLE_PATH = REPO / "extension" / "dist" / "uc-extension.js"
EXT_DIR = REPO / "extension"
READY_JS = "window.__UC && window.__UC.ready === true"
RESULT_MARK = "@@RESULT@@"
EXPECTED_CONTROLS = 39  # Build Spec §3 rule 1 ("39 today")


def now() -> float:
    return time.perf_counter()


def fms(sec: float | None) -> str:
    return "n/a" if sec is None else f"{sec * 1000:.0f} ms"


def fs(sec: float | None) -> str:
    return "n/a" if sec is None else f"{sec:.2f} s"


# ═══════════════════════════════════════════════════════════════════════
# Results
# ═══════════════════════════════════════════════════════════════════════

ROWS: dict[str, dict] = {}
DATA: dict[str, dict] = {}
ASSERTIONS: list[dict] = []


def row(key, status: str, text: str, detail: list[str] | None = None, data: dict | None = None):
    """Record and print one row. status: ok | FAIL | SKIP."""
    key = str(key)
    ROWS[key] = {"key": key, "status": status, "text": text, "detail": detail or []}
    if data is not None:
        DATA[key] = data
    print(f"[{key:>3}] {status:<4} {text}", flush=True)
    for line in detail or []:
        print(f"        {line}", flush=True)


def merge_rows(rows: list[dict], data: dict | None = None):
    for r in rows:
        row(r["key"], r["status"], r["text"], r.get("detail"))
    for k, v in (data or {}).items():
        DATA[k] = v


def section(name: str, keys: list[str], fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a failed section must not hide the others
        tb = traceback.format_exc().strip().splitlines()
        for k in keys:
            if k not in ROWS:
                row(k, "FAIL", f"section '{name}' aborted before this row: {exc!r}", tb[-6:])
        if not keys:
            print(f"!! section '{name}' aborted: {exc!r}\n" + "\n".join(tb[-6:]), flush=True)


def assertion(name: str, status: str, text: str):
    ASSERTIONS.append({"name": name, "status": status, "text": text})


# Values the analysis doc recorded, shown beside what this run measured.
DOC = {
    "1": "works; both windows listed an endpoint",
    "2": "0.03-0.07 s",
    "3": "present",
    "4": "0.06-0.08 s; success/nativeSetter; client read value back",
    "5": "0.06-0.08 / 0.03-0.04 / 0.05-0.06 s",
    "6": "visible after 0.16-0.17 s (not immediately)",
    "7": "title, evaluate, content, goto, click(trial) all worked",
    "8": "PRESENT (init scripts leak between clients of one window)",
    "9": "all pages",
    "10": "neither present",
    "11": "both windows alive; b kept both pages; a: one page unaccounted (unverified)",
    "12": "345 MB/7 pages/17 procs; 205 MB/2 pages/11 procs",
    "13": "both browsers exited; no leftover processes",
    "14": "6.1 s, 24 polls",
    "15": "median 2 ms, max 14 ms",
    "16": "58 ms / 24 ms",
    "17": "S finished before L by 4.2 s",
    "18": "4 ms; L read the value",
    "19": "True / False",
    "20": "wrote DevToolsActivePort; attach worked",
    "21": "undefined",
    "22": "ready < 1.5 s; detect_all 0.09 s; set_text ok, value read back",
    "23": "ready true; __UC_setText is a function",
    "24": "1 driver process, 82 MB PSS",
}


# ═══════════════════════════════════════════════════════════════════════
# /proc helpers (Linux): process trees, listening sockets, PSS
# ═══════════════════════════════════════════════════════════════════════


def _ppid_map() -> dict[int, int]:
    out: dict[int, int] = {}
    for ent in os.listdir("/proc"):
        if not ent.isdigit():
            continue
        try:
            with open(f"/proc/{ent}/stat", "rb") as fh:
                stat = fh.read().decode(errors="replace")
            out[int(ent)] = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return out


def _state(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().decode(errors="replace").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def alive(pid: int) -> bool:
    st = _state(pid)
    return st is not None and st != "Z"


def descendants(pid: int, ppids: dict[int, int] | None = None) -> set[int]:
    ppids = ppids or _ppid_map()
    kids: dict[int, list[int]] = {}
    for p, pp in ppids.items():
        kids.setdefault(pp, []).append(p)
    out: set[int] = set()
    stack = [pid]
    while stack:
        for k in kids.get(stack.pop(), []):
            if k not in out:
                out.add(k)
                stack.append(k)
    return out


def cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def pids_with_cmdline(needle: str) -> set[int]:
    me = os.getpid()
    out = set()
    for ent in os.listdir("/proc"):
        if ent.isdigit() and int(ent) != me and needle in cmdline(int(ent)):
            out.add(int(ent))
    return out


def pss_kb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/smaps_rollup") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def listening_pids(port: int) -> set[int]:
    inodes: set[str] = set()
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(fn) as fh:
                next(fh)
                for line in fh:
                    f = line.split()
                    if f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                        inodes.add(f[9])
        except OSError:
            continue
    if not inodes:
        return set()
    want = {f"socket:[{i}]" for i in inodes}
    out = set()
    for ent in os.listdir("/proc"):
        if not ent.isdigit():
            continue
        fd_dir = f"/proc/{ent}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") in want:
                        out.add(int(ent))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return out


def window_tree(cdp: str) -> tuple[int, set[int]] | None:
    """Browser root pid (the process listening on the window's CDP port) and
    its whole process tree."""
    port = urllib.parse.urlparse(cdp).port
    pids = listening_pids(port) if port else set()
    if not pids:
        return None
    ppids = _ppid_map()
    roots = [p for p in pids if ppids.get(p) not in pids]
    root = roots[0] if roots else min(pids)
    return root, {root} | descendants(root, ppids)


def tree_pss(pids: set[int]) -> tuple[float, int, int]:
    """(MB PSS, processes counted, processes unreadable)"""
    total, n, bad = 0, 0, 0
    for p in pids:
        v = pss_kb(p)
        if v is None:
            bad += 1
        else:
            total += v
            n += 1
    return total / 1024.0, n, bad


def kill_pids(pids: set[int]):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        live = [p for p in pids if alive(p)]
        for p in live:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        if sig == signal.SIGTERM and live:
            deadline = now() + 3
            while now() < deadline and any(alive(p) for p in live):
                time.sleep(0.1)


# ═══════════════════════════════════════════════════════════════════════
# HTTP helpers
# ═══════════════════════════════════════════════════════════════════════

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(method: str, url: str, body=None, timeout: float = 120.0):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        obj = json.loads(raw) if raw else None
    except ValueError:
        obj = {"raw": raw[:200].decode(errors="replace")}
    return status, obj


def cdp_http_base(cdp: str) -> str:
    return f"http://127.0.0.1:{urllib.parse.urlparse(cdp).port}"


def json_pages(cdp: str) -> dict[str, str]:
    """Independent third view of a window's page targets: Chrome's own
    /json/list (target id -> url), page type only."""
    status, obj = http_json("GET", cdp_http_base(cdp) + "/json/list", timeout=10)
    if status != 200 or not isinstance(obj, list):
        raise RuntimeError(f"/json/list answered {status}")
    return {t["id"]: t.get("url", "") for t in obj if t.get("type") == "page"}


def json_version(cdp: str) -> dict | None:
    try:
        status, obj = http_json("GET", cdp_http_base(cdp) + "/json/version", timeout=5)
        return obj if status == 200 else None
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════
# Fixture server
# ═══════════════════════════════════════════════════════════════════════

FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>UC probe fixture</title></head>
<body>
<h1>Probe fixture</h1>
<p>A page with a search box and a login form, served from loopback.</p>
<form id="search" action="#" onsubmit="return false">
  <input id="q" type="text" name="q" placeholder="Search the fixture" aria-label="Search">
  <button id="go" type="submit">Search</button>
</form>
<form id="login" action="#" onsubmit="return false">
  <input id="email" type="email" name="email" placeholder="Email">
  <input id="pw" type="password" name="password" placeholder="Password">
  <button id="signin" type="submit">Sign in</button>
</form>
</body></html>
"""

PLAIN_HTML = "<!doctype html><html><head><title>probe page {n}</title></head><body><p>page {n}</p></body></html>"


class Fixture:
    def __init__(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                p = urllib.parse.urlparse(self.path).path
                if p in ("/", "/form"):
                    body = FORM_HTML
                elif p.startswith("/p/"):
                    body = PLAIN_HTML.format(n=p[3:])
                else:
                    self.send_error(404)
                    return
                raw = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):  # noqa: D401
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, name="fixture", daemon=True)
        outer._t.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str = "/form") -> str:
        return self.base + path

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ═══════════════════════════════════════════════════════════════════════
# Playwright-side helpers (used by the prototype, the client and the probes)
# ═══════════════════════════════════════════════════════════════════════


class Targets:
    """Maps a context's Playwright pages to CDP target ids (Build Spec §4
    "Resolving a target id to a Page"): a per-page CDP session sends
    ``Target.getTargetInfo``, then detaches; results are cached."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.known: dict = {}

    def scan(self) -> dict:
        out = {}
        for p in list(self.ctx.pages):
            if p.is_closed():
                self.known.pop(p, None)
                continue
            tid = self.known.get(p)
            if tid is None:
                s = self.ctx.new_cdp_session(p)
                try:
                    tid = s.send("Target.getTargetInfo")["targetInfo"]["targetId"]
                finally:
                    try:
                        s.detach()
                    except Exception:  # noqa: BLE001
                        pass
                self.known[p] = tid
            out[tid] = p
        return out

    def tid(self, page) -> str:
        self.scan()
        return self.known[page]

    def _anchor(self):
        for p in self.ctx.pages:
            if not p.is_closed():
                return p
        return None

    def pump(self, ms: int = 10):
        a = self._anchor()
        if a is not None:
            a.wait_for_timeout(ms)
        else:
            time.sleep(ms / 1000)

    def wait_for(self, tid: str, timeout: float = 10.0, interval_ms: int = 10):
        t0 = now()
        while True:
            m = self.scan()
            if tid in m:
                return m[tid], now() - t0
            if now() - t0 > timeout:
                raise TimeoutError(f"target {tid} not visible on this attachment after {timeout}s")
            self.pump(interval_ms)

    def settle_view(self, cdp: str, timeout: float = 5.0):
        """Poll until this attachment's page set equals /json/list's, or
        timeout. Returns (view ids, json ids, seconds)."""
        t0 = now()
        while True:
            view = set(self.scan())
            jl = set(json_pages(cdp))
            if view == jl or now() - t0 > timeout:
                return view, jl, now() - t0
            self.pump(20)


def control_names() -> list[str]:
    """The control allowlist (Build Spec §3 rule 1): public UCBrowser methods
    whose first parameter after ``self`` is ``page``, read from the signatures
    of the checked-out UCBrowser."""
    from uc_browser.browser import UCBrowser

    out = []
    for name, fn in inspect.getmembers(UCBrowser, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        if len(params) >= 2 and params[1] == "page":
            out.append(name)
    return sorted(out)


def read_devtools_active_port(profile: Path, proc: subprocess.Popen | None = None, timeout: float = 20.0) -> str:
    f = profile / "DevToolsActivePort"
    t0 = now()
    while now() - t0 < timeout:
        if proc is not None and proc.poll() is not None:
            raise ProfileHeld(f"browser exited at once (code {proc.returncode}); another process may hold {profile}")
        try:
            lines = f.read_text().splitlines()
            if len(lines) >= 2 and lines[0].isdigit():
                return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
        except OSError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"{f} not written within {timeout}s")


class ProfileHeld(RuntimeError):
    pass


class NoSuchTarget(TimeoutError):
    pass


# ═══════════════════════════════════════════════════════════════════════
# Prototype service (baseline only): windows, owner threads, control workers
# ═══════════════════════════════════════════════════════════════════════


class ControlWorker(threading.Thread):
    """One Playwright instance and one CDP attachment of its own, with an
    unstarted UCBrowser as the receiver for page-taking controls."""

    def __init__(self, win: "LocalWindow"):
        super().__init__(name=f"worker-{win.name}", daemon=True)
        self.win = win
        self.jobs: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.error: BaseException | None = None

    def submit(self, target_id: str, name: str, args: list, kwargs: dict) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.jobs.put((target_id, name, args, kwargs, fut))
        return fut

    def run(self):
        from playwright.sync_api import sync_playwright
        from uc_browser.browser import BrowserMode, UCBrowser

        pw = browser = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(self.win.cdp)
            ctx = browser.contexts[0]
            targets = Targets(ctx)
            receiver = UCBrowser(mode=BrowserMode.CHROMIUM_EXT)  # never started
            bundle = BUNDLE_PATH.read_text(encoding="utf-8") if self.win.engine == "inject" else None
        except BaseException as e:  # noqa: BLE001
            self.error = e
            self.ready.set()
            if pw:
                pw.stop()
            return
        self.ready.set()
        try:
            while True:
                item = self.jobs.get()
                if item is None:
                    break
                tid, name, args, kwargs, fut = item
                try:
                    try:
                        page, _ = targets.wait_for(tid, timeout=10)
                    except TimeoutError as e:
                        raise NoSuchTarget(str(e)) from e
                    if bundle is not None:
                        # engine: inject — evaluated at the page's first control
                        # call; navigation discards it, so check every call
                        if not page.evaluate("typeof window.__UC !== 'undefined'"):
                            page.evaluate(bundle)
                        try:
                            page.wait_for_function(READY_JS, timeout=5000)
                        except Exception:  # noqa: BLE001
                            pass
                    fut.set_result(getattr(receiver, name)(page, *args, **kwargs))
                except BaseException as e:  # noqa: BLE001
                    fut.set_exception(e)
        finally:
            try:
                browser.close()  # detach only
            except Exception:  # noqa: BLE001
                pass
            pw.stop()


class LocalWindow:
    """A browser window the prototype owns. Playwright objects are touched only
    on the owner thread (or the worker's own thread)."""

    def __init__(self, name: str, browser: str, engine: str, root: Path):
        self.name = name
        self.browser = browser  # chromium | chrome
        self.engine = engine  # extension | inject
        self.profile = root / f"profile_{name}"
        self.cdp: str | None = None
        self.running = False
        self.error: BaseException | None = None
        self.ctx = None
        self.targets: Targets | None = None
        self.proc: subprocess.Popen | None = None
        self.worker: ControlWorker | None = None
        self.owner: threading.Thread | None = None
        self.jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self.start_seconds: float | None = None

    # ── owner thread ──
    def start(self):
        with self._lock:
            if self.running:
                return
            if self.error is not None:
                raise self.error
            t0 = now()
            self.owner = threading.Thread(target=self._owner_main, name=f"owner-{self.name}", daemon=True)
            self.owner.start()
            self._ready.wait(90)
            if self.error is not None:
                raise self.error
            if not self.running:
                raise TimeoutError(f"window {self.name} not ready after 90 s")
            self.start_seconds = now() - t0

    def _launch_chromium(self, pw):
        self.profile.mkdir(parents=True)
        args = ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"]
        if self.engine == "extension":
            ext = str(EXT_DIR.resolve())
            args += [f"--load-extension={ext}", f"--disable-extensions-except={ext}"]
        self.ctx = pw.chromium.launch_persistent_context(
            str(self.profile), headless=False, channel=None, args=args
        )
        self.cdp = read_devtools_active_port(self.profile)

    def _launch_chrome(self, pw):
        self.profile.mkdir(parents=True)
        chrome = shutil.which("google-chrome") or "/usr/bin/google-chrome"
        cmd = [
            chrome,
            f"--user-data-dir={self.profile}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        self.cdp = read_devtools_active_port(self.profile, self.proc)
        # the process the service started must be the one answering the port
        port = urllib.parse.urlparse(self.cdp).port
        mine = {self.proc.pid} | descendants(self.proc.pid)
        if not (listening_pids(port) & mine):
            raise ProfileHeld(f"port {port} is answered by a process this window did not start")
        self._pw_browser = pw.chromium.connect_over_cdp(self.cdp)
        ctxs = self._pw_browser.contexts
        self.ctx = ctxs[0] if ctxs else self._pw_browser.new_context()

    def _owner_main(self):
        from playwright.sync_api import sync_playwright

        pw = None
        try:
            pw = sync_playwright().start()
            (self._launch_chromium if self.browser == "chromium" else self._launch_chrome)(pw)
            self.ctx.add_init_script(f"window.__SVC_SHIM = {json.dumps(self.name)};")
            self.targets = Targets(self.ctx)
            self.running = True
        except BaseException as e:  # noqa: BLE001
            self.error = e
            self._ready.set()
            self._teardown(pw)
            return
        self._ready.set()
        try:
            while True:
                try:
                    item = self.jobs.get(timeout=2.0)
                except queue.Empty:
                    # Build Spec §5: one cheap call between jobs drains queued events
                    try:
                        for p in self.ctx.pages:
                            p.title()
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if item is None:
                    break
                fn, fut = item
                try:
                    fut.set_result(fn())
                except BaseException as e:  # noqa: BLE001
                    fut.set_exception(e)
        finally:
            self.running = False
            self._teardown(pw)

    def _teardown(self, pw):
        try:
            if self.browser == "chromium":
                if self.ctx is not None:
                    self.ctx.close()
            else:
                if getattr(self, "_pw_browser", None) is not None:
                    self._pw_browser.close()
        except Exception:  # noqa: BLE001
            pass
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass

    def call(self, fn, timeout: float = 60.0):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.jobs.put((fn, fut))
        return fut.result(timeout)

    def pages(self) -> list[dict]:
        def job():
            # Drain target-created events before snapshotting ctx.pages.
            # Reading the property alone does not pump sync Playwright.
            self.targets.pump(0)
            return [{"target_id": t, "url": p.url} for t, p in self.targets.scan().items()]

        return self.call(job)

    def control(self, tid: str, name: str, args: list, kwargs: dict, timeout: float = 90.0):
        with self._lock:
            if self.worker is None:
                self.worker = ControlWorker(self)
                self.worker.start()
                self.worker.ready.wait(60)
            if self.worker.error is not None:
                raise self.worker.error
        return self.worker.submit(tid, name, args, kwargs).result(timeout)

    def stop(self):
        if self.worker is not None:
            self.worker.jobs.put(None)
            self.worker.join(30)
        if self.owner is not None and self.owner.is_alive():
            self.jobs.put(None)
            self.owner.join(40)


class ProbeService:
    """Generic loopback HTTP service for the baseline: a subset of wire
    contract v0 (Build Spec §3). No leases reaper, no sessions."""

    def __init__(self, windows: dict[str, LocalWindow]):
        self.windows = windows
        self.clients: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.first_seen: dict[str, float] = {}
        self.allow = set(control_names())
        self._n = 0
        self._lock = threading.Lock()
        svc = self

        class H(http.server.BaseHTTPRequestHandler):
            def _go(self, method: str):
                u = urllib.parse.urlparse(self.path)
                body = None
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                    except ValueError:
                        body = None
                try:
                    status, obj = svc.handle(method, u.path, urllib.parse.parse_qs(u.query), body or {})
                except Exception as e:  # noqa: BLE001
                    status, obj = 500, {"error": "internal", "detail": repr(e)}
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                self._go("GET")

            def do_POST(self):  # noqa: N802
                self._go("POST")

            def do_DELETE(self):  # noqa: N802
                self._go("DELETE")

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self._t = threading.Thread(target=self.httpd.serve_forever, name="service-http", daemon=True)
        self._t.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _err(status: int, code: str, detail: str = ""):
        return status, {"error": code, "detail": detail}

    def _entry(self, name: str) -> dict:
        w = self.windows[name]
        pages = []
        if w.running:
            try:
                for p in w.pages():
                    t = p["target_id"]
                    self.first_seen.setdefault(t, now())
                    lease = self.leases.get(t)
                    pages.append({
                        "target_id": t, "url": p["url"],
                        "holder": lease["holder"] if lease else None,
                        "age_s": round(now() - self.first_seen[t], 2),
                    })
            except Exception:  # noqa: BLE001
                pass
        return {
            "browser": w.browser, "engine": w.engine, "running": w.running,
            "offers": ["attach", "control"] if w.cdp else ["control"],
            "cdp": w.cdp,
            "clients": sum(1 for c in self.clients.values() if c["window"] == w.name),
            "pages": pages,
        }

    def handle(self, method: str, path: str, query: dict, body: dict):
        if method == "GET" and path == "/windows":
            return 200, {n: self._entry(n) for n in self.windows}
        m = re.fullmatch(r"/windows/([^/]+)/start", path)
        if m and method == "POST":
            w = self.windows.get(m.group(1))
            if not w:
                return self._err(404, "no_such_window", m.group(1))
            try:
                w.start()
            except ProfileHeld as e:
                return self._err(409, "profile_held", str(e))
            except Exception as e:  # noqa: BLE001
                return self._err(500, "launch_failed", repr(e))
            return 200, self._entry(w.name)
        if path == "/clients" and method == "POST":
            if not isinstance(body.get("pid"), int):
                return self._err(400, "bad_pid")
            if body.get("window") not in self.windows:
                return self._err(404, "no_such_window", str(body.get("window")))
            with self._lock:
                self._n += 1
                cid = f"c{self._n}"
                self.clients[cid] = {"pid": body["pid"], "label": body.get("label"), "window": body["window"]}
            return 200, {"client_id": cid}
        m = re.fullmatch(r"/clients/([^/]+)", path)
        if m and method == "DELETE":
            cid = m.group(1)
            if cid not in self.clients:
                return self._err(404, "no_such_client")
            closed = 0
            for tid, lease in list(self.leases.items()):
                if lease["holder"] == cid:
                    closed += self._close_page(lease["window"], tid)
                    self.leases.pop(tid, None)
            self.clients.pop(cid, None)
            return 200, {"closed": closed}
        m = re.fullmatch(r"/windows/([^/]+)/pages", path)
        if m and method == "POST":
            w = self.windows.get(m.group(1))
            if not w:
                return self._err(404, "no_such_window")
            cid = body.get("client_id")
            if cid not in self.clients:
                return self._err(404, "no_such_client")
            url = body.get("url")

            def job():
                p = w.ctx.new_page()
                if url:
                    try:
                        p.goto(url, wait_until="load")
                    except Exception as e:  # noqa: BLE001
                        p.close()
                        raise RuntimeError(f"navigation_failed: {e}") from e
                    if body.get("wait_ms"):
                        p.wait_for_timeout(int(body["wait_ms"]))
                return w.targets.tid(p)

            try:
                tid = w.call(job)
            except RuntimeError as e:
                if "navigation_failed" in str(e):
                    return self._err(502, "navigation_failed", str(e))
                raise
            self.leases[tid] = {"holder": cid, "window": w.name}
            self.first_seen.setdefault(tid, now())
            return 200, {"target_id": tid}
        m = re.fullmatch(r"/windows/([^/]+)/pages/([^/]+)", path)
        if m and method == "DELETE":
            if m.group(1) not in self.windows:
                return self._err(404, "no_such_window")
            if not self._close_page(m.group(1), m.group(2)):
                return self._err(404, "no_such_target")
            self.leases.pop(m.group(2), None)
            return 200, {"closed": True}
        m = re.fullmatch(r"/windows/([^/]+)/pages/([^/]+)/controls/([^/]+)", path)
        if m and method == "POST":
            w = self.windows.get(m.group(1))
            if not w:
                return self._err(404, "no_such_window")
            name = m.group(3)
            if name not in self.allow:
                return self._err(404, "no_such_control", name)
            if w.engine == "none":
                return self._err(409, "no_controls")
            if not w.running:
                return self._err(409, "not_running")
            try:
                res = w.control(m.group(2), name, list(body.get("args") or []), dict(body.get("kwargs") or {}))
            except NoSuchTarget as e:
                return self._err(404, "no_such_target", str(e))
            except Exception as e:  # noqa: BLE001
                return self._err(500, "control_failed", repr(e))
            return 200, {"result": res}
        return self._err(404, "no_route", f"{method} {path}")

    def _close_page(self, wname: str, tid: str) -> int:
        w = self.windows[wname]

        def job():
            p = w.targets.scan().get(tid)
            if p is None:
                return 0
            p.close()
            return 1

        return w.call(job)


class LocalBackend:
    """The baseline: fixture-independent prototype service + own windows."""

    mode = "baseline"
    destructive = True
    shim_js = "window.__SVC_SHIM"
    shim_expect = {"a": "a", "b": "b"}

    def __init__(self, root: Path):
        self.root = root
        self.windows = {
            "a": LocalWindow("a", "chromium", "extension", root),
            "b": LocalWindow("b", "chromium", "extension", root),
            "c": LocalWindow("c", "chromium", "extension", root),
            "native": LocalWindow("native", "chrome", "inject", root),
        }
        self.service = ProbeService(self.windows)
        self.base_url = self.service.base_url
        self.names = {"a": "a", "b": "b", "c": "c", "native": "native"}

    def apply_stealth(self, window: str):
        from playwright_stealth import Stealth

        w = self.windows[window]
        w.call(lambda: Stealth().apply_stealth_sync(w.ctx))
        return True

    def devtools_file(self, window: str) -> str:
        f = self.windows[window].profile / "DevToolsActivePort"
        return f"{f} exists={f.exists()}"

    def tracked_pids(self) -> set[int]:
        return descendants(os.getpid()) | pids_with_cmdline(str(self.root))

    def shutdown(self) -> dict:
        tracked = self.tracked_pids()
        t0 = now()
        self.service.stop()
        for w in self.windows.values():
            w.stop()
        deadline = now() + 15
        left = {p for p in tracked if alive(p)}
        while left and now() < deadline:
            time.sleep(0.2)
            left = {p for p in left if alive(p)}
        return {
            "tracked": len(tracked),
            "elapsed": now() - t0,
            "leftover": sorted((p, cmdline(p)[:110]) for p in left),
        }


class ExternalBackend:
    """A running service speaking contract v0."""

    mode = "service"

    def __init__(self, url: str, names: dict, destructive: bool):
        self.base_url = url.rstrip("/")
        self.names = names
        self.destructive = destructive
        self.shim_js = "typeof window.__UC_apiShim"
        self.shim_expect = {"a": "object", "b": "object"}

    apply_stealth = None
    shutdown = None
    devtools_file = None


# ═══════════════════════════════════════════════════════════════════════
# Probe B — the client scenario (runs in its own process)
# ═══════════════════════════════════════════════════════════════════════


def probe_b_client(cfg: dict) -> dict:
    from playwright.sync_api import sync_playwright

    base = cfg["base_url"]
    wn = cfg["windows"]  # {"a": name, "b": name|None}
    fx = cfg["fixture"]
    fx_form = fx + "/form"
    destructive = cfg["destructive"]
    shim_js = cfg["shim_js"]
    shim_expect = cfg["shim_expect"]
    out: dict = {"rows": [], "data": {}, "created": [], "pre": {}, "ids": {}, "errors": {}}

    def add(key, status, text, detail=None):
        out["rows"].append({"key": str(key), "status": status, "text": text, "detail": detail or []})

    def ctl(w, tid, name, *args, **kwargs):
        t = now()
        st, obj = http_json("POST", f"{base}/windows/{w}/pages/{tid}/controls/{name}",
                            {"args": list(args), "kwargs": kwargs})
        return st, obj, now() - t

    def hook_js(attr):
        return f"typeof window.{attr} === 'undefined' ? null : window.{attr}"

    st, obj = http_json("POST", f"{base}/clients",
                        {"pid": os.getpid(), "label": "service_probe", "window": wn["a"]})
    if st != 200:
        raise RuntimeError(f"POST /clients -> {st} {obj}")
    out["ids"]["client_id"] = obj["client_id"]
    cid = obj["client_id"]

    wins = {}
    for k in ("a", "b"):
        if wn.get(k):
            st, ent = http_json("POST", f"{base}/windows/{wn[k]}/start", timeout=120)
            if st != 200:
                raise RuntimeError(f"POST /windows/{wn[k]}/start -> {st} {ent}")
            wins[k] = ent
    cdp = {k: wins[k]["cdp"] for k in wins}
    out["ids"]["cdp"] = cdp

    pw = sync_playwright().start()
    try:
        # ── row 2: attach ──
        br, ctx, tg, dts = {}, {}, {}, {}
        for k in wins:
            t = now()
            br[k] = pw.chromium.connect_over_cdp(cdp[k])
            dts[k] = now() - t
            ctx[k] = br[k].contexts[0]
            tg[k] = Targets(ctx[k])
        row2 = {"a": dts["a"], "b": dts.get("b")}

        # client-created pages
        page, created = {}, {}
        for k in wins:
            page[k] = ctx[k].new_page()
            page[k].goto(fx_form, wait_until="load")
            created[k] = tg[k].tid(page[k])
            out["created"].append((wn[k], created[k]))

        # ── row 3: context init script from the service reaches a client page ──
        shim = {}
        for k in wins:
            shim[k] = page[k].evaluate(shim_js)
        ok3 = all(shim[k] == shim_expect[k] for k in shim)
        add(3, "ok", f"service init script in client-created page: {'present' if ok3 else 'ABSENT/UNEXPECTED'} "
                     f"({shim_js} -> {shim})")

        # ── row 4: service set_text on the client's page ──
        try:
            page["a"].wait_for_function(READY_JS, timeout=10000)
            ready_note = ""
        except Exception as e:  # noqa: BLE001
            ready_note = f"; client-side wait for engine ready failed: {type(e).__name__}"
        st, obj, dt = ctl(wn["a"], created["a"], "set_text", "#q", "hello from client")
        readback = page["a"].input_value("#q")
        good = st == 200 and isinstance(obj.get("result"), dict) and obj["result"].get("success") is True
        add(4, "ok" if good and readback == "hello from client" else "FAIL",
            f"set_text via route: HTTP {st} in {fms(dt)}; result={obj.get('result') if obj else obj}; "
            f"client read back {readback!r}{ready_note}")

        # ── row 5: generic route for three more controls ──
        parts, bad5 = [], False
        for name in ("has_login_wall", "find_inputs", "detect_all"):
            st, obj, dt = ctl(wn["a"], created["a"], name)
            r = obj.get("result") if isinstance(obj, dict) else None
            if st != 200:
                bad5 = True
                parts.append(f"{name}: HTTP {st} {obj}")
                continue
            if name == "find_inputs":
                summ = f"{len(r)} inputs"
            elif name == "detect_all":
                summ = f"{len(r)} pattern keys, {sum(1 for v in r.values() if v)} non-empty"
            else:
                summ = repr(r)
            parts.append(f"{name}: {fms(dt)} ({summ})")
        add(5, "FAIL" if bad5 else "ok", "; ".join(parts))

        # ── rows 6-7: service opens a page, client resolves it by target id ──
        t_post = now()
        st, obj = http_json("POST", f"{base}/windows/{wn['a']}/pages",
                            {"client_id": cid, "url": fx_form, "wait_ms": 0})
        t_ret = now()
        if st != 200:
            add(6, "FAIL", f"POST pages -> {st} {obj}")
            add(7, "FAIL", "no service-opened page")
        else:
            stid = obj["target_id"]
            out["created"].append((wn["a"], stid))
            immediate = stid in tg["a"].scan()
            sp, waited = tg["a"].wait_for(stid, timeout=10)
            add(6, "ok", f"service call took {fms(t_ret - t_post)}; visible immediately on the client's "
                         f"connection: {immediate}; visible after {fs(waited)} (poll incl. getTargetInfo)")
            steps = {}

            def step(label, fn):
                try:
                    fn()
                    steps[label] = "ok"
                except Exception as e:  # noqa: BLE001
                    steps[label] = f"FAIL {type(e).__name__}: {str(e)[:80]}"

            step("title", lambda: sp.title())
            step("evaluate", lambda: sp.evaluate("1 + 1"))
            step("content", lambda: sp.content())
            step("goto", lambda: sp.goto(fx + "/p/after", wait_until="load"))
            step("goto_form", lambda: sp.goto(fx_form, wait_until="load"))
            step("click(trial)", lambda: sp.click("#go", trial=True, timeout=5000))
            add(7, "ok" if all(v == "ok" for v in steps.values()) else "FAIL",
                "client drives service-opened page: " + ", ".join(f"{k}={v}" for k, v in steps.items()))

        # ── rows 8-9: init scripts and page visibility between two clients of window a ──
        if destructive:
            ctx["a"].add_init_script("window.__CLIENT_A_HOOK = 'from-client-a';")
        second: dict = {}

        def second_client():
            try:
                with sync_playwright() as p2:
                    t = now()
                    b2 = p2.chromium.connect_over_cdp(cdp["a"])
                    second["connect"] = now() - t
                    c2 = b2.contexts[0]
                    t2 = Targets(c2)
                    pg = c2.new_page()
                    pg.goto(fx_form, wait_until="load")
                    second["tid"] = t2.tid(pg)
                    second["hook"] = pg.evaluate(hook_js("__CLIENT_A_HOOK"))
                    view, jl, secs = t2.settle_view(cdp["a"])
                    second["view"], second["jl"], second["settle"] = sorted(view), sorted(jl), secs
                    b2.close()
            except Exception as e:  # noqa: BLE001
                second["error"] = repr(e)

        th = threading.Thread(target=second_client, name="second-client")
        th.start()
        th.join(90)
        if "error" in second or "tid" not in second:
            add(8, "FAIL", f"second client failed: {second.get('error')}")
            add(9, "FAIL", "second client failed")
        else:
            out["created"].append((wn["a"], second["tid"]))
            row2_b = second["connect"]
            if destructive:
                present = second["hook"] == "from-client-a"
                add(8, "ok", f"client A's add_init_script inside a page created by a second client of the same "
                             f"window: {'PRESENT' if present else 'ABSENT'} (value {second['hook']!r})")
                out["data"]["8"] = {"present": present}
            else:
                add(8, "SKIP", "needs --destructive: adds a context-level init script to the service's window")
            # client A's own view, after the second client's page exists
            tg["a"].wait_for(second["tid"], timeout=10)
            viewA, jlA, secsA = tg["a"].settle_view(cdp["a"])
            v2, j2 = set(second["view"]), set(second["jl"])
            eqA, eq2 = viewA == jlA, v2 == j2
            lines = [
                f"client A view ({len(viewA)}): " + ", ".join(sorted(viewA)),
                f"client 2 view ({len(v2)}): " + ", ".join(sorted(v2)),
                f"/json/list at client A's check ({len(jlA)}): " + ", ".join(sorted(jlA)),
            ]
            if not eqA:
                lines.append(f"client A: missing {sorted(jlA - viewA)} extra {sorted(viewA - jlA)}")
            if not eq2:
                lines.append(f"client 2: missing {sorted(j2 - v2)} extra {sorted(v2 - j2)}")
            add(9, "ok" if eqA and eq2 else "FAIL",
                f"window a: client A sees {len(viewA)}/{len(jlA)} pages (settled in {fs(secsA)}), "
                f"client 2 sees {len(v2)}/{len(j2)} (including each other's)", lines)
            row2["a2"] = row2_b
        add(2, "ok", f"attach: a {fms(row2['a'])}"
                     + (f", b {fms(row2['b'])}" if row2.get("b") is not None else "")
                     + (f", second client on a {fms(row2['a2'])}" if row2.get("a2") is not None else "")
                     + " (connect_over_cdp only; sync_playwright start excluded)")

        # ── row 10: nothing crosses windows ──
        if "b" not in wins:
            add(10, "SKIP", "no second window configured (--window-b)")
        elif not destructive:
            add(10, "SKIP", "needs --destructive: sets a cookie and a context init script in window a")
        else:
            ctx["a"].add_cookies([{"name": "probe_cookie", "value": "from-a", "url": fx}])
            page["a"].goto(fx_form, wait_until="load")
            page["b"].goto(fx_form, wait_until="load")
            a_cookie = "probe_cookie" in page["a"].evaluate("document.cookie")
            b_cookie = "probe_cookie" in page["b"].evaluate("document.cookie")
            a_hook = page["a"].evaluate(hook_js("__CLIENT_A_HOOK")) == "from-client-a"
            b_hook = page["b"].evaluate(hook_js("__CLIENT_A_HOOK")) is not None
            b_shim = page["b"].evaluate(shim_js)
            controls_ok = a_cookie and a_hook
            add(10, "ok" if controls_ok else "FAIL",
                f"window b: cookie set in a {'PRESENT' if b_cookie else 'absent'}, init script of a "
                f"{'PRESENT' if b_hook else 'absent'}; b's own shim {b_shim!r}. "
                f"Positive control in a: cookie {'present' if a_cookie else 'MISSING'}, "
                f"init script {'present' if a_hook else 'MISSING'}"
                + ("" if controls_ok else " (controls failed: row 10 not meaningful)"))
            out["data"]["10"] = {"cookie_in_b": b_cookie, "hook_in_b": b_hook, "controls_ok": controls_ok}

        # ── row 11 (pre-state) and the disconnects ──
        for k in wins:
            out["pre"][wn[k]] = {
                "client_view": sorted(tg[k].scan()),
                "json_list": sorted(json_pages(cdp[k])),
            }
        if not destructive:
            add("11a", "SKIP", "needs --destructive: calls context.close() / browser.close() on service windows")
        else:
            if "b" in wins:
                try:
                    ctx["b"].close()
                except Exception as e:  # noqa: BLE001
                    out["errors"]["ctx_b.close()"] = repr(e)[:200]
            try:
                br["a"].close()
            except Exception as e:  # noqa: BLE001
                out["errors"]["browser_a.close()"] = repr(e)[:200]
            out["data"]["11"] = {"disconnected": True}
    finally:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
    return out


# ═══════════════════════════════════════════════════════════════════════
# Parent-side orchestration of probe B
# ═══════════════════════════════════════════════════════════════════════


def run_probe_b(be, fx: Fixture, settle_s: float):
    names = be.names
    wa, wb = names["a"], names.get("b")
    windows = {"a": wa, "b": wb}
    t0 = now()
    cdp = {}
    for k, w in windows.items():
        if not w:
            continue
        st, ent = http_json("POST", f"{be.base_url}/windows/{w}/start", timeout=120)
        if st != 200:
            raise RuntimeError(f"POST /windows/{w}/start -> {st} {ent}")
        cdp[k] = ent["cdp"]
    # ── row 1 ──
    lines, ok1 = [], True
    st, listing = http_json("GET", f"{be.base_url}/windows")
    for k, w in windows.items():
        if not w:
            continue
        ent = listing.get(w, {})
        ver = json_version(cdp[k])
        if not ent.get("cdp") or not ver:
            ok1 = False
        lines.append(f"{w}: browser={ent.get('browser')} engine={ent.get('engine')} cdp port "
                     f"{urllib.parse.urlparse(cdp[k]).port} /json/version={ver.get('Browser') if ver else None}")
    row(1, "ok" if ok1 else "FAIL",
        "Playwright-launched Chromium serving CDP on a port; GET /windows lists an endpoint for "
        f"{', '.join(w for w in windows.values() if w)} (window start {fs(now() - t0)})", lines)

    cfg = {
        "base_url": be.base_url, "windows": windows, "fixture": fx.base,
        "destructive": be.destructive, "shim_js": be.shim_js, "shim_expect": be.shim_expect,
    }
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--role", "client-b", "--cfg", json.dumps(cfg)],
        capture_output=True, text=True, timeout=300, env=os.environ,
    )
    res = None
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_MARK):
            res = json.loads(line[len(RESULT_MARK):])
    if res is None:
        err = (proc.stderr or proc.stdout).strip().splitlines()[-8:]
        for k in ("2", "3", "4", "5", "6", "7", "8", "9", "10", "11"):
            if k not in ROWS:
                row(k, "FAIL", f"client process exited {proc.returncode} without a result", err)
        return cdp, None
    merge_rows([r for r in res["rows"] if r["key"] != "11a"], res.get("data"))
    created = res["created"]

    # ── row 11: after the client exited ──
    if "11a" in {r["key"] for r in res["rows"]}:
        row(11, "SKIP", "needs --destructive: the client does not call context.close()/browser.close() "
                        "on service windows, so disconnect semantics are not exercised")
    else:
        time.sleep(1.0)
        snaps = {}
        schedule = [("1s", 0.0)] + ([(f"{settle_s:g}s", settle_s - 1.0)] if settle_s > 1.0 else [])
        for label, delay in schedule:
            time.sleep(delay)
            st, listing = http_json("GET", f"{be.base_url}/windows")
            snap = {}
            for k, w in windows.items():
                if not w:
                    continue
                ver = json_version(cdp[k])
                try:
                    jl = sorted(json_pages(cdp[k]))
                except Exception:  # noqa: BLE001
                    jl = None
                svc_view = sorted(p["target_id"] for p in listing.get(w, {}).get("pages", []))
                snap[w] = {"alive": ver is not None, "json_list": jl, "service_view": svc_view}
            snaps[label] = snap
        lines, status = [], "ok"
        for label, snap in snaps.items():
            for w, s in snap.items():
                pre = res["pre"][w]
                before = set(pre["json_list"])
                after_j = set(s["json_list"] or [])
                after_s = set(s["service_view"])
                made_here = {t for ww, t in created if ww == w}
                lines.append(f"[{label} after exit] window {w}: alive={s['alive']}; before(/json/list)={len(before)} "
                             f"client-view-before={len(pre['client_view'])} after(/json/list)={len(after_j)} "
                             f"service-view={len(after_s)}")
                lines.append(f"    before: {sorted(before)}")
                lines.append(f"    after(/json/list): {sorted(after_j)}")
                lines.append(f"    after(service): {sorted(after_s)}")
                lines.append(f"    disappeared (before - after): {sorted(before - after_j)}; appeared: {sorted(after_j - before)}; "
                             f"service-vs-/json/list: only-service={sorted(after_s - after_j)} only-json={sorted(after_j - after_s)}; "
                             f"client-created pages still present: {sorted(made_here & after_j)} "
                             f"gone: {sorted(made_here - after_j)}")
                if not s["alive"]:
                    status = "FAIL"
                if "1s" == label and (before - after_j):
                    lines.append(f"    ^ {len(before - after_j)} page(s) vanished after disconnect")
        if res["errors"]:
            lines.append(f"client-side errors from the disconnect calls: {res['errors']}")
        alive_txt = ", ".join(f"{w}={'alive' if s['alive'] else 'DEAD'}"
                              for w, s in next(iter(snaps.values())).items())
        first = next(iter(snaps.values()))
        gone = {w: sorted(set(res['pre'][w]['json_list']) - set(s['json_list'] or [])) for w, s in first.items()}
        row(11, status, f"client ran context.close() (b) / browser.close() (a) then exited: windows {alive_txt}; "
                        f"pages that disappeared: " + ", ".join(f"{w}: {len(g)}" for w, g in gone.items()), lines)
    # cleanup of pages this scenario created (service mode: the service's windows persist)
    if be.mode == "service":
        for w, tid in created:
            try:
                http_json("DELETE", f"{be.base_url}/windows/{w}/pages/{tid}", timeout=20)
            except Exception:  # noqa: BLE001
                pass
    return cdp, res


# ═══════════════════════════════════════════════════════════════════════
# Row 12, row 24
# ═══════════════════════════════════════════════════════════════════════


def probe_memory(be, cdps: dict):
    lines, bad = [], False
    parts = []
    for k, cdp in cdps.items():
        w = be.names[k]
        tree = window_tree(cdp)
        try:
            npages = len(json_pages(cdp))
        except Exception:  # noqa: BLE001
            npages = None
        if tree is None:
            bad = True
            parts.append(f"{w}: no listening process found for the CDP port (other user?)")
            continue
        root, pids = tree
        mb, n, unread = tree_pss(pids)
        parts.append(f"{w}: {mb:.0f} MB PSS, {npages} pages, {len(pids)} processes")
        lines.append(f"{w}: root pid {root}, PSS summed over {n} readable of {len(pids)} processes"
                     + (f" ({unread} unreadable)" if unread else "") + f"; {npages} page targets")
    row(12, "FAIL" if bad else "ok", "window memory, PSS summed over the window's process tree: " + "; ".join(parts), lines)


def probe_driver():
    from playwright.sync_api import sync_playwright

    me = os.getpid()
    before = descendants(me)
    pw = sync_playwright().start()
    try:
        time.sleep(0.7)
        new = descendants(me) - before
        mb, n, unread = tree_pss(new)
        cmds = [cmdline(p)[:90] for p in sorted(new)]
    finally:
        pw.stop()
    row(24, "ok" if len(new) == 1 else "FAIL",
        f"one extra sync_playwright() in a process: {len(new)} new driver process(es), {mb:.0f} MB PSS", cmds)


# ═══════════════════════════════════════════════════════════════════════
# Probe C — attachment threads, stealth, native Chrome
# ═══════════════════════════════════════════════════════════════════════


def run_attachment_threads(cdp: str, fx: Fixture, window: str):
    from playwright.sync_api import sync_playwright
    from uc_browser.browser import BrowserMode, UCBrowser

    shared: dict = {}
    ev_l_ready, ev_s_ready, ev_go = threading.Event(), threading.Event(), threading.Event()
    res: dict = {"L": {}, "S": {}}
    TOKEN = f"s-{os.getpid()}-{int(time.time())}"

    def thread_l():
        try:
            with sync_playwright() as p:
                br = p.chromium.connect_over_cdp(cdp)
                ctx = br.contexts[0]
                tg = Targets(ctx)
                page = ctx.new_page()
                try:
                    page.goto(fx.url("/form"), wait_until="load")
                    shared["l_tid"] = tg.tid(page)
                    ev_l_ready.set()
                    if not ev_s_ready.wait(60):
                        raise RuntimeError("S never became ready")
                    t0 = now()
                    shared["l_t0"] = t0
                    ev_go.set()
                    polls = 0
                    # shape of _wait_chat_response: wait_for_timeout(250) then an evaluate
                    while now() - t0 < 6.0:
                        page.wait_for_timeout(250)
                        page.evaluate("1 + 1")
                        polls += 1
                    shared["l_end"] = now()
                    res["L"] = {"dur": shared["l_end"] - t0, "polls": polls,
                                "read": page.evaluate("typeof window.__S_WROTE === 'undefined' ? null : window.__S_WROTE")}
                finally:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            res["L"]["error"] = repr(e)
            ev_l_ready.set()
            ev_go.set()

    def thread_s():
        try:
            with sync_playwright() as p:
                br = p.chromium.connect_over_cdp(cdp)
                ctx = br.contexts[0]
                tg = Targets(ctx)
                receiver = UCBrowser(mode=BrowserMode.CHROMIUM_EXT)
                page = ctx.new_page()
                try:
                    page.goto(fx.url("/form"), wait_until="load")
                    try:
                        page.wait_for_function(READY_JS, timeout=10000)
                    except Exception:  # noqa: BLE001
                        res["S"]["engine_ready"] = False
                    ev_l_ready.wait(60)
                    ev_s_ready.set()
                    if not ev_go.wait(60):
                        raise RuntimeError("L never started")
                    page.wait_for_timeout(1000)  # one second into L's loop
                    times = []
                    for _ in range(20):
                        t = now()
                        page.evaluate("document.title")
                        times.append(now() - t)
                    t = now()
                    fi = receiver.find_inputs(page)
                    t_fi = now() - t
                    t = now()
                    da = receiver.detect_all(page)
                    t_da = now() - t
                    lpage, _ = tg.wait_for(shared["l_tid"], timeout=10)
                    t = now()
                    lpage.evaluate("(v) => { window.__S_WROTE = v; }", TOKEN)
                    t_w = now() - t
                    s_end = now()
                    shared["s_end"] = s_end
                    res["S"].update({
                        "times": times, "t_fi": t_fi, "n_fi": len(fi), "t_da": t_da,
                        "keys_da": len(da), "nonempty_da": sum(1 for v in da.values() if v),
                        "t_w": t_w,
                    })
                finally:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            res["S"]["error"] = repr(e)
            ev_s_ready.set()

    tl = threading.Thread(target=thread_l, name="attach-L")
    ts = threading.Thread(target=thread_s, name="attach-S")
    tl.start()
    ts.start()
    tl.join(120)
    ts.join(120)
    L, S = res["L"], res["S"]
    if "error" in L or "dur" not in L:
        row(14, "FAIL", f"thread L failed: {L.get('error')}")
    else:
        row(14, "ok", f"long control on attachment thread L (window {window}): ran {fs(L['dur'])}, {L['polls']} polls",
            data={"dur": L["dur"], "polls": L["polls"]})
    if "error" in S or "times" not in S:
        for k in ("15", "16", "17", "18"):
            row(k, "FAIL", f"thread S failed: {S.get('error')}")
        return
    ts_ = S["times"]
    med, mx = statistics.median(ts_), max(ts_)
    row(15, "ok", f"meanwhile on thread S: {len(ts_)} evaluate calls, median {fms(med)}, max {fms(mx)}",
        data={"median": med, "max": mx})
    row(16, "ok", f"meanwhile on S: find_inputs {fms(S['t_fi'])} ({S['n_fi']} inputs) / detect_all {fms(S['t_da'])} "
                  f"({S['keys_da']} keys, {S['nonempty_da']} non-empty)"
                  + ("" if S.get("engine_ready", True) else "; engine was NOT ready on S's page"))
    if "dur" in L and "l_end" in shared and "s_end" in shared:
        gap = shared["l_end"] - shared["s_end"]
        row(17, "ok", f"S finished before L by {fs(gap)}" if gap > 0 else f"S finished AFTER L by {fs(-gap)}",
            data={"gap": gap})
        row(18, "ok" if L.get("read") == TOKEN else "FAIL",
            f"S writes into L's page while L is mid-loop: {fms(S['t_w'])}; L read the value at the end: {L.get('read') == TOKEN}")
    else:
        row(17, "FAIL", "missing end timestamps")
        row(18, "FAIL", "missing L read")


def run_stealth_probe(be, cdp: str, fx: Fixture, window_key: str):
    from playwright.sync_api import sync_playwright

    out: dict = {}
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(cdp)
        ctx = br.contexts[0]
        p1 = ctx.new_page()
        p2 = None
        try:
            p1.goto(fx.url("/form"), wait_until="load")
            out["before"] = p1.evaluate("navigator.webdriver")
            if be.apply_stealth is not None:
                be.apply_stealth(window_key)
                p2 = ctx.new_page()
                p2.goto(fx.url("/form"), wait_until="load")
                out["after"] = p2.evaluate("navigator.webdriver")
        finally:
            for pg in (p1, p2):
                try:
                    if pg:
                        pg.close()
                except Exception:  # noqa: BLE001
                    pass
    return out


def run_native_probe(be, fx: Fixture, window: str, bundle: str, mutate: bool = True):
    from playwright.sync_api import sync_playwright
    from uc_browser.browser import BrowserMode, UCBrowser

    base = be.base_url
    t0 = now()
    st, ent = http_json("POST", f"{base}/windows/{window}/start", timeout=120)
    start_s = now() - t0
    if st != 200:
        for k in ("20", "21", "22", "23", "S1"):
            row(k, "FAIL", f"POST /windows/{window}/start -> {st} {ent}")
        return
    cdp = ent.get("cdp")
    ver = json_version(cdp) if cdp else None
    receiver = UCBrowser(mode=BrowserMode.CHROMIUM_EXT)  # never started
    pages = []
    with sync_playwright() as p:
        try:
            ta = now()
            br = p.chromium.connect_over_cdp(cdp)
            t_attach = now() - ta
            ctx = br.contexts[0]
            tg = Targets(ctx)
            extra = be.devtools_file(window) if be.devtools_file else "DevToolsActivePort check not available in service mode"

            # S1 (supplement): a control on a native page the engine has never seen, through the route
            page0 = ctx.new_page()
            pages.append(page0)
            page0.goto(fx.url("/form"), wait_until="load")
            webdriver = page0.evaluate("navigator.webdriver")
            row(20, "ok" if be.mode != "service" or webdriver is False else "FAIL",
                f"installed Chrome: window start {fs(start_s)}; attach worked in {fms(t_attach)}; "
                f"/json/version={ver.get('Browser') if ver else None}; navigator.webdriver={webdriver!r}",
                [extra], data={"webdriver": webdriver})
            tid0 = tg.tid(page0)
            t = now()
            st, obj = http_json("POST", f"{base}/windows/{window}/pages/{tid0}/controls/detect_all",
                                {"args": [], "kwargs": {}}, timeout=90)
            dt = now() - t
            if st == 200:
                r = obj.get("result") or {}
                row("S1", "ok" if r else "FAIL",
                    f"control on a native page with no engine, via the route (engine injected at first call): HTTP 200 in "
                    f"{fms(dt)}; detect_all -> {len(r)} keys, {sum(1 for v in r.values() if v)} non-empty")
            else:
                row("S1", "FAIL", f"control on a native page via the route: HTTP {st} {obj}")

            page1 = ctx.new_page()
            pages.append(page1)
            page1.goto(fx.url("/form"), wait_until="load")
            row(21, "ok", "native page before injection: typeof window.__UC_detectAll = "
                          + repr(page1.evaluate("typeof window.__UC_detectAll")),
                data={"value": page1.evaluate("typeof window.__UC_detectAll")})

            err, t_ready, ready = None, None, False
            try:
                page1.evaluate(bundle)
            except Exception as e:  # noqa: BLE001
                err = repr(e)[:160]
            t = now()
            try:
                page1.wait_for_function(READY_JS, timeout=5000)
                ready = True
            except Exception:  # noqa: BLE001
                pass
            t_ready = now() - t
            t = now()
            da = receiver.detect_all(page1)
            t_da = now() - t
            st_res = receiver.set_text(page1, "#q", "native text")
            rb = page1.input_value("#q")
            ok22 = err is None and ready and bool(da) and st_res.get("success") is True and rb == "native text"
            row(22, "ok" if ok22 else "FAIL",
                f"page.evaluate(bundle): error={err}; window.__UC.ready={ready} within {fs(t_ready)}; detect_all "
                f"{fms(t_da)} ({len(da)} keys); set_text -> {st_res}; value read back {rb!r}")

            if not mutate:
                row(23, "SKIP", "needs --destructive: adds a context-level init script to the service's native window")
                return
            ctx.add_init_script(bundle)
            page2 = ctx.new_page()
            pages.append(page2)
            page2.goto(fx.url("/form"), wait_until="load")
            try:
                page2.wait_for_function(READY_JS, timeout=5000)
                r2 = True
            except Exception:  # noqa: BLE001
                r2 = False
            tf = page2.evaluate("typeof window.__UC_setText")
            row(23, "ok" if r2 and tf == "function" else "FAIL",
                f"bundle as a context init script, then a new page: window.__UC.ready={r2}; typeof __UC_setText = {tf!r}")
        finally:
            for pg in pages:
                try:
                    pg.close()
                except Exception:  # noqa: BLE001
                    pass


# ═══════════════════════════════════════════════════════════════════════
# Orchestration, summary
# ═══════════════════════════════════════════════════════════════════════


def in_thread(fn, *a, **kw):
    box: dict = {}

    def run():
        try:
            box["v"] = fn(*a, **kw)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
            box["tb"] = traceback.format_exc()

    t = threading.Thread(target=run, name=getattr(fn, "__name__", "probe"), daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise RuntimeError(box["tb"]) from box["e"]
    return box.get("v")


def evaluate_model():
    d8 = DATA.get("8")
    if d8 is None:
        assertion("row 8 present", "SKIP", "row 8 not measured")
    elif d8["present"]:
        assertion("row 8 present", "ok", "init script of client A present in a page of client 2 (window is one scope)")
    else:
        assertion("row 8 present", "OVERTURNED", "row 8 reads ABSENT: init scripts did not leak between clients")
    d10 = DATA.get("10")
    if d10 is None:
        assertion("row 10 absent", "SKIP", "row 10 not measured")
    elif not d10["controls_ok"]:
        assertion("row 10 absent", "SKIP", "positive controls in window a failed; cannot conclude")
    elif d10["cookie_in_b"] or d10["hook_in_b"]:
        assertion("row 10 absent", "OVERTURNED",
                  f"row 10 reads PRESENT: cookie_in_b={d10['cookie_in_b']} hook_in_b={d10['hook_in_b']}")
    else:
        assertion("row 10 absent", "ok", "neither the cookie nor the init script of window a reached window b")
    d14, d15, d17 = DATA.get("14"), DATA.get("15"), DATA.get("17")
    if not (d14 and d15):
        assertion("row 15 far below 14", "SKIP", "rows 14/15 not measured")
    else:
        far = d15["median"] * 20 < d14["dur"] and d15["max"] < d14["dur"] / 4
        ahead = d17 is not None and d17["gap"] > 0
        txt = (f"median call {fms(d15['median'])}, max {fms(d15['max'])} vs long control {fs(d14['dur'])} "
               f"(ratio median/L = {d15['median'] / d14['dur']:.4f}); S finished before L: {ahead}")
        assertion("row 15 far below 14", "ok" if far and ahead else "OVERTURNED", txt)
    d21 = DATA.get("21")
    if d21 is not None:
        assertion("row 21 native page has no engine", "ok" if d21["value"] == "undefined" else "FAIL",
                  f"typeof window.__UC_detectAll = {d21['value']!r}")


def print_summary(mode: str, args):
    print("\n" + "=" * 100)
    print(f"SERVICE PROBE SUMMARY ({mode}) — measured this run vs the analysis doc (single run; order of magnitude)")
    print("=" * 100)

    def order(k):
        m = re.match(r"(\d+)", k)
        return (0, int(m.group(1)), k) if m else (1, 0, k)

    missing = []
    for n in range(1, 25):
        k = str(n)
        r = ROWS.get(k)
        if r is None:
            missing.append(k)
            print(f"[{k:>3}] ---- NOT RUN        | doc: {DOC[k]}")
            continue
        print(f"[{k:>3}] {r['status']:<4} {r['text']}")
        print(f"        doc: {DOC[k]}")
    extras = sorted((k for k in ROWS if not k.isdigit() or int(k) > 24), key=order)
    for k in extras:
        r = ROWS[k]
        print(f"[{k:>3}] {r['status']:<4} (supplement) {r['text']}")
    print("-" * 100)
    for a in ASSERTIONS:
        print(f"ASSERT {a['name']:<34} {a['status']:<10} {a['text']}")
    overturned = [a for a in ASSERTIONS if a["status"] == "OVERTURNED"]
    failed = [k for k, r in ROWS.items() if r["status"] == "FAIL"]
    skipped = [k for k, r in ROWS.items() if r["status"] == "SKIP"]
    print("-" * 100)
    if overturned:
        print("!!! MODEL OVERTURNED: " + ", ".join(a["name"] for a in overturned) +
              " — stop and re-derive the isolation/concurrency model before building further.")
    print(f"rows failed: {failed or 'none'}; skipped: {skipped or 'none'}; not run: {missing or 'none'}; "
          f"model assertions skipped: {[a['name'] for a in ASSERTIONS if a['status'] == 'SKIP'] or 'none'}")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "mode": mode, "rows": ROWS, "data": DATA, "assertions": ASSERTIONS, "doc": DOC,
        }, indent=2, default=str))
        print(f"wrote {args.json}")
    if overturned:
        return 2
    incomplete_assertions = [a for a in ASSERTIONS if a["status"] == "FAIL" or (mode == "baseline" and a["status"] == "SKIP")]
    if failed or missing or incomplete_assertions:
        return 1
    return 0


def header(be, args):
    def ver(pkg):
        try:
            return importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            return "not installed"

    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
                                    capture_output=True, text=True).stdout.strip())
    except Exception:  # noqa: BLE001
        rev, dirty = "?", False
    try:
        chrome = subprocess.run(["google-chrome", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        chrome = "not found"
    controls = control_names()
    print("service_probe: rows 1-24 of docs/08 - Analysis/00 - Service Prototype Measurements.md")
    print(f"  mode={be.mode}  service={be.base_url}  UC {rev}{'+dirty' if dirty else ''}  host={os.uname().nodename}")
    print(f"  python {sys.version.split()[0]}  playwright {ver('playwright')}  playwright-stealth {ver('playwright-stealth')}")
    print(f"  DISPLAY={os.environ.get('DISPLAY')}  XAUTHORITY={os.environ.get('XAUTHORITY')}  {chrome}")
    print(f"  bundle {BUNDLE_PATH} exists={BUNDLE_PATH.exists()}")
    mark = "" if len(controls) == EXPECTED_CONTROLS else f"  (Build Spec says {EXPECTED_CONTROLS}: DRIFT)"
    print(f"  UCBrowser page-taking controls (public, first param after self is 'page'): {len(controls)}{mark}")
    print(f"  {', '.join(controls)}")
    print(flush=True)


def main_parent(args) -> int:
    root = Path(tempfile.mkdtemp(prefix="uc_probe_"))
    fx = Fixture()
    be = None
    state = {"be": None}
    guard_ids = []

    def emergency():
        local = state["be"]
        pids = set()
        if isinstance(local, LocalBackend):
            pids |= local.tracked_pids()
        else:
            pids |= descendants(os.getpid())
        pids.discard(os.getpid())
        if pids:
            kill_pids(pids)
        shutil.rmtree(root, ignore_errors=True)

    atexit.register(emergency)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: (_ for _ in ()).throw(SystemExit(128 + s)))

    try:
        if args.service_url:
            names = {"a": args.window_a, "b": args.window_b, "c": args.window_c or args.window_a,
                     "native": args.native_window}
            be = ExternalBackend(args.service_url, names, args.destructive)
            st, listing = http_json("GET", be.base_url + "/windows", timeout=10)
            if st != 200 or not isinstance(listing, dict):
                print(f"GET {be.base_url}/windows -> {st}: not a contract-v0 service")
                return 1
            missing = [w for w in names.values() if w and w not in listing]
            if not args.window_a or missing:
                print(f"windows not offered by the service: {missing or ['--window-a is required']}; offered: {sorted(listing)}")
                return 1
            # Raw CDP probes hold pages for longer than the zero-client sweep
            # interval. Register the probe process while those pages are live.
            for window in set(names.values()) - {None, ""}:
                status, registered = http_json("POST", be.base_url + "/clients",
                    {"pid": os.getpid(), "label": "service-probe", "window": window})
                if status != 200:
                    raise RuntimeError(f"register probe on {window}: {status}")
                guard_ids.append(registered["client_id"])
        else:
            if not BUNDLE_PATH.exists():
                print(f"UC bundle not built: {BUNDLE_PATH}\nRun: cd extension && npm install && npx rollup -c")
                return 1
            be = LocalBackend(root)
        state["be"] = be
        header(be, args)
        bundle = BUNDLE_PATH.read_text(encoding="utf-8") if BUNDLE_PATH.exists() else None

        # row 24 first: the process is clean, so new descendants are exactly the driver
        section("row 24 driver cost", ["24"], lambda: in_thread(probe_driver))

        cdps: dict = {}

        def b():
            nonlocal cdps
            cdps, _ = run_probe_b(be, fx, args.settle_s)

        section("probe B", ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"], b)
        section("row 12 memory", ["12"], lambda: probe_memory(be, cdps) if cdps else row(12, "FAIL", "no windows to measure"))

        # probe C part 1
        wc = be.names["c"]

        def c1():
            st, ent = http_json("POST", f"{be.base_url}/windows/{wc}/start", timeout=120)
            if st != 200:
                raise RuntimeError(f"start {wc}: {st} {ent}")
            run_attachment_threads(ent["cdp"], fx, wc)

        section("probe C attachment threads", ["14", "15", "16", "17", "18"], c1)

        def c2():
            st, ent = http_json("POST", f"{be.base_url}/windows/{wc}/start", timeout=120)
            if st != 200:
                raise RuntimeError(f"start {wc}: {st} {ent}")
            res = in_thread(run_stealth_probe, be, ent["cdp"], fx, wc)
            if be.apply_stealth is None:
                row(19, "SKIP", f"before/after stealth needs the prototype's owner thread; a service window already has its "
                                f"stealth applied. navigator.webdriver in a client-created page: {res['before']}")
            else:
                row(19, "ok", f"navigator.webdriver in a client-created page, before / after stealth on the context: "
                              f"{res['before']} / {res.get('after')}")

        section("probe C stealth", ["19"], c2)

        def c3():
            if not be.names.get("native") or args.skip_native:
                for k in ("20", "21", "22", "23", "S1"):
                    row(k, "SKIP", "native window not configured/--skip-native")
                return
            in_thread(run_native_probe, be, fx, be.names["native"], bundle, be.destructive)

        section("probe C native Chrome", ["20", "21", "22", "23", "S1"], c3)

        # row 13
        if be.shutdown is None:
            row(13, "SKIP", "service mode: the probe does not stop the service; check its own shutdown separately "
                            "(UC build spec §8: systemctl restart leaves no browser)")
        else:
            def r13():
                rep = be.shutdown()
                lines = [f"{rep['tracked']} tracked processes (descendants + anything with the temp profile root in its command line); "
                         f"shutdown took {fs(rep['elapsed'])}"]
                for pid, cl in rep["leftover"]:
                    lines.append(f"LEFTOVER pid {pid}: {cl}")
                row(13, "FAIL" if rep["leftover"] else "ok",
                    "service shutdown: " + ("browsers exited; no leftover processes" if not rep["leftover"]
                                            else f"{len(rep['leftover'])} process(es) still alive"), lines)

            section("row 13 shutdown", ["13"], r13)

        evaluate_model()
        return print_summary(be.mode, args)
    finally:
        if be is not None:
            for client_id in guard_ids:
                try:
                    http_json("DELETE", be.base_url + "/clients/" + client_id)
                except Exception:
                    pass
        try:
            fx.stop()
        except Exception:  # noqa: BLE001
            pass
        emergency()
        left = [p for p in (descendants(os.getpid())) if alive(p)]
        if left:
            print(f"cleanup: {len(left)} own descendant process(es) still present: {left}")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service-url", help="run against a running contract-v0 service instead of the baseline prototype")
    ap.add_argument("--window-a", help="service mode: window for probe B/C (default: required)")
    ap.add_argument("--window-b", help="service mode: a second window for isolation rows 10-11 (optional)")
    ap.add_argument("--window-c", help="service mode: window for attachment threads/stealth (default: --window-a)")
    ap.add_argument("--native-window", help="service mode: a native (installed Chrome, engine inject) window for rows 20-23")
    ap.add_argument("--destructive", action="store_true",
                    help="service mode: allow rows that add context init scripts/cookies and close contexts on service windows")
    ap.add_argument("--skip-native", action="store_true", help="skip rows 20-23")
    ap.add_argument("--settle-s", type=float, default=1.0,
                    help="row 11: also snapshot this many seconds after client exit (service mode: set >= the reaper interval, e.g. 8)")
    ap.add_argument("--json", help="write rows, data and assertions to this path")
    ap.add_argument("--role", default="parent", choices=["parent", "client-b"], help=argparse.SUPPRESS)
    ap.add_argument("--cfg", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


if __name__ == "__main__":
    a = parse_args()
    if a.role == "client-b":
        result = probe_b_client(json.loads(a.cfg))
        print(RESULT_MARK + json.dumps(result), flush=True)
        sys.exit(0)
    sys.exit(main_parent(a))
