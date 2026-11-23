"""Profile guards and the native Chrome launcher.

Shared by the window core (``windows.py``) and the standalone native-Chrome
launch path of ``UCBrowser``. Nothing here touches Playwright objects except
``launch_native`` (which receives the caller's ``pw`` and attaches on the
calling thread).

Rules enforced here:

* A profile is launched only after ``assert_profile_available``: Chromium's
  ``SingletonLock`` symlink (``<host>-<pid>``) and Firefox's ``lock`` symlink
  (``<ip>:+<pid>``) name a holder; a holder counts only while its pid is alive
  and its command line names this profile (so a recycled pid does not block a
  launch). Held-browser lock files are never deleted.
* Concurrent launches on one profile are serialised by an OS file lock
  (``launch_lock``) held only for the launch.
* The debugging port is always ``0`` on loopback. ``DevToolsActivePort`` is
  deleted before our own launch (the profile is known to be free and the lock
  is held) and accepted only if it is fresh, answers ``/json/version`` with the
  recorded browser path, and belongs to the process that holds the profile.
* Every launch failure terminates the processes this launch started.
* The installed Chrome sandbox is preserved: no ``--no-sandbox`` here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ServiceError

try:  # POSIX only; Windows runs without the cross-process launch lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

log = logging.getLogger("uc_browser.service.profiles")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ENDPOINT_FILE = "DevToolsActivePort"
_LOCK_WAIT_S = 60.0


# ── /proc helpers (Linux). Every helper answers None when it cannot tell ──


def _read_stat(pid: int) -> tuple[str, int] | None:
    """(state, ppid) of a pid, or None if it does not exist / not readable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            tail = fh.read().decode(errors="replace").rsplit(")", 1)[1].split()
        return tail[0], int(tail[1])
    except (OSError, IndexError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live (non-zombie) process."""
    if pid <= 0:
        return False
    stat = _read_stat(pid)
    if stat is not None:
        return stat[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _cmdline_args(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _same_path(a: str, b: Path) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def _cmdline_names_profile(pid: int, profile: Path) -> bool | None:
    args = _cmdline_args(pid)
    if args is None:
        return None
    if len(args) == 1 and " --" in args[0]:
        # Installed Chrome can rewrite argv as one process-title string.
        # Losing NUL boundaries is not evidence that a live lock is stale.
        marker = f"--user-data-dir={Path(profile).resolve()}"
        return True if f" {marker} " in f" {args[0]} " else None
    for arg in args:
        if arg.startswith("--user-data-dir="):
            arg = arg.split("=", 1)[1]
        if _same_path(arg, profile):
            return True
    return False


def _descendants(root: int) -> set[int]:
    """``root`` and every live descendant (Linux), else just ``{root}``."""
    out = {root}
    try:
        entries = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return out
    children: dict[int, list[int]] = {}
    for pid in entries:
        stat = _read_stat(pid)
        if stat is not None:
            children.setdefault(stat[1], []).append(pid)
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def _listening_inodes(port: int) -> set[int] | None:
    inodes: set[int] = set()
    seen = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        seen = True
        for row in rows:
            cols = row.split()
            if len(cols) < 10 or cols[3] != "0A":  # 0A = LISTEN
                continue
            try:
                if int(cols[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(int(cols[9]))
            except (ValueError, IndexError):
                continue
    return inodes if seen else None


def port_owned_by(port: int, pids: set[int]) -> bool | None:
    """Does one of ``pids`` own the loopback listener on ``port``?

    None when the answer cannot be read (not Linux, or fds unreadable); the
    caller then relies on the other launch checks.
    """
    listening = _listening_inodes(port)
    if listening is None:
        return None
    if not listening:
        return False
    readable = False
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        readable = True
        for name in names:
            try:
                target = os.readlink(f"{fd_dir}/{name}")
            except OSError:
                continue
            m = re.fullmatch(r"socket:\[(\d+)\]", target)
            if m and int(m.group(1)) in listening:
                return True
    return False if readable else None


# ── Held-profile detection ────────────────────────────────────────────────


def _lock_owner(profile: Path) -> tuple[str | None, int] | None:
    """(host or None, pid) named by a browser lock symlink in ``profile``."""
    try:
        target = os.readlink(profile / "SingletonLock")  # Chromium: <host>-<pid>
        host, _, pid = target.rpartition("-")
        if pid.isdigit():
            return host or None, int(pid)
    except OSError:
        pass
    try:
        target = os.readlink(profile / "lock")  # Firefox: <ip>:+<pid>
        m = re.search(r":\+?(\d+)$", target)
        if m:
            return None, int(m.group(1))
    except OSError:
        pass
    return None


def profile_holder(profile: Path) -> int | None:
    """Pid of the live browser holding ``profile``, or None if free.

    A lock naming a dead pid, or a live pid whose command line does not name
    this profile (pid reuse), is stale and does not count. A lock from another
    host cannot be judged and counts as held.
    """
    owner = _lock_owner(Path(profile))
    if owner is None:
        return None
    host, pid = owner
    if host is not None and host != socket.gethostname():
        return pid
    if not pid_alive(pid):
        return None
    names = _cmdline_names_profile(pid, Path(profile))
    if names is False:
        return None
    return pid  # names it, or unreadable: refuse rather than guess


def assert_profile_available(profile: Path) -> None:
    """Raise ``ServiceError(profile_held, ..., 409)`` if a browser holds it.

    Call before any persistent launch and before any attach to a profile's
    endpoint. Creates nothing and deletes nothing.
    """
    holder = profile_holder(Path(profile))
    if holder is not None:
        raise ServiceError(
            "profile_held",
            f"profile {profile} is held by running browser process {holder}",
            409,
        )


@contextlib.contextmanager
def launch_lock(profile: Path, timeout: float = _LOCK_WAIT_S):
    """OS-wide mutual exclusion of UC launches on one profile.

    Held only while a launch is in flight (the browser's own SingletonLock
    takes over afterwards). Another launcher still inside its launch after
    ``timeout`` is reported as ``profile_held``.
    """
    if fcntl is None:
        yield
        return
    uid = os.getuid() if hasattr(os, "getuid") else 0
    root = Path(tempfile.gettempdir()) / f"uc-profile-locks-{uid}"
    root.mkdir(mode=0o700, exist_ok=True)
    key = hashlib.sha1(os.path.realpath(profile).encode()).hexdigest()[:24]
    fd = os.open(root / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ServiceError(
                        "profile_held",
                        f"another launch on profile {profile} is still in progress",
                        409,
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── Endpoint discovery ────────────────────────────────────────────────────


def devtools_http(port: int, path: str, timeout: float = 1.0):
    """GET a JSON document from a loopback DevTools HTTP endpoint (no proxy)."""
    with _OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
        return json.load(resp)


def list_page_targets(port: int) -> set[str] | None:
    """Target ids of every ``page`` the browser on ``port`` has, or None if
    the endpoint cannot be read right now."""
    try:
        return {t["id"] for t in devtools_http(port, "/json/list", 1.5) if t.get("type") == "page"}
    except Exception:  # noqa: BLE001 - "cannot tell" is a valid answer here
        return None


def remove_stale_endpoint(profile: Path) -> None:
    """Delete ``DevToolsActivePort`` left by an earlier run.

    Only legitimate while the profile is known free and the launch lock is
    held: the file then belongs to no live browser.
    """
    try:
        (Path(profile) / _ENDPOINT_FILE).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ServiceError(
            "launch_failed", f"cannot remove stale {_ENDPOINT_FILE} in {profile}: {exc}"
        ) from exc


def _exited(profile: Path, proc: subprocess.Popen) -> ServiceError:
    code = proc.returncode
    holder = profile_holder(profile)
    if code == 0 or (holder is not None and holder != proc.pid):
        who = f" (process {holder})" if holder else ""
        return ServiceError(
            "profile_held",
            f"browser exited at once with code {code}: another browser{who} holds {profile}",
            409,
        )
    return ServiceError("launch_failed", f"browser exited with code {code} before serving CDP")


def wait_for_endpoint(
    profile: Path,
    proc: subprocess.Popen | None = None,
    *,
    started: float,
    timeout: float = 30.0,
) -> tuple[int, str]:
    """(port, browser_path) once a *fresh* ``DevToolsActivePort`` is served.

    ``started`` is ``time.time()`` taken before the launch; older files are
    ignored. The file is trusted only if the port answers ``/json/version``
    with the recorded browser path. With ``proc``, an early exit is reported
    as ``profile_held`` (another browser holds the profile) or
    ``launch_failed``.
    """
    profile = Path(profile)
    path = profile / _ENDPOINT_FILE
    deadline = time.monotonic() + timeout
    last: object = "no DevToolsActivePort yet"
    while True:
        if proc is not None and proc.poll() is not None:
            raise _exited(profile, proc)
        try:
            if path.stat().st_mtime >= started - 2:
                lines = path.read_text().splitlines()
                if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/browser/"):
                    port, bpath = int(lines[0]), lines[1].strip()
                    version = devtools_http(port, "/json/version")
                    if urlsplit(version.get("webSocketDebuggerUrl", "")).path == bpath:
                        return port, bpath
                    last = "endpoint answers with a different browser path"
            else:
                last = "stale DevToolsActivePort ignored"
        except (OSError, ValueError) as exc:
            last = exc
        except Exception as exc:  # noqa: BLE001 - urllib/json errors: keep waiting
            last = exc
        if time.monotonic() >= deadline:
            raise ServiceError(
                "launch_failed", f"browser did not serve CDP within {timeout:.0f}s: {last}"
            )
        time.sleep(0.05)


def verify_endpoint_owner(profile: Path, port: int, *, launched: set[int] | None = None) -> int | None:
    """Check that the browser answering ``port`` is the one that holds the
    profile (and, if given, one we launched). Returns the holder's pid.

    Raises ``profile_held`` when another browser holds the profile, and
    ``launch_failed`` when the listener is not the holder's. Checks that
    cannot be read on this platform are skipped, never faked.
    """
    holder = profile_holder(profile)
    if holder is not None and launched is not None and holder not in launched:
        raise ServiceError(
            "profile_held",
            f"profile {profile} is held by process {holder}, which this launch did not start",
            409,
        )
    pids: set[int] = set(launched or ())
    if holder is not None:
        pids |= _descendants(holder)
    if pids and port_owned_by(port, pids) is False:
        raise ServiceError(
            "launch_failed",
            f"CDP port {port} is not answered by the process holding {profile}",
        )
    return holder


# ── Native Chrome ─────────────────────────────────────────────────────────


def find_chrome() -> str | None:
    """Installed Google Chrome executable, or None."""
    found = (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chrome")
    )
    if found:
        return found
    for cand in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    ):
        if cand.exists():
            return str(cand)
    return None


def terminate_process(proc: subprocess.Popen, timeout: float = 8.0) -> None:
    """Stop a browser this process started: SIGTERM (clean profile flush) on
    its process group, SIGKILL if it lingers. Never raises."""
    if proc.poll() is not None:
        return
    posix = os.name == "posix"

    def send(sig):
        try:
            if posix:
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    send(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    send(signal.SIGKILL if posix else signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def kill_profile_browser(pid: int, profile: Path, timeout: float = 8.0) -> None:
    """Last-resort stop of the browser ``pid`` that this service launched on
    ``profile`` (used when its owner thread cannot clean up). The pid is
    signalled only while it is alive and its command line still names the
    profile. Never raises."""
    if not pid_alive(pid) or _cmdline_names_profile(pid, Path(profile)) is False:
        return
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.1)


def launch_native(pw, profile: Path, *, extra_args=(), chrome_path: str | None = None, timeout: float = 30.0):
    """Launch installed Chrome on ``profile`` and attach ``pw`` to it.

    Returns ``(proc, browser, context, cdp)``; ``cdp`` is
    ``ws://127.0.0.1:<port>/devtools/browser/<id>``. ``pw`` is the caller's
    sync Playwright and is used on the calling thread only. The caller owns
    teardown (``terminate_process(proc)`` once it is done with the browser).

    Raises ``ServiceError``: ``profile_held`` (409) if a browser holds the
    profile or exits at once because one does; ``launch_failed`` otherwise.
    Processes this call started are terminated on any failure.
    """
    profile = Path(profile).expanduser().resolve()
    chrome = chrome_path or find_chrome()
    if chrome is None:
        raise ServiceError("launch_failed", "Google Chrome is not installed")
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceError("launch_failed", f"cannot create profile {profile}: {exc}") from exc

    with launch_lock(profile):
        assert_profile_available(profile)
        remove_stale_endpoint(profile)
        cmd = [
            chrome,
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            *extra_args,
            "about:blank",
        ]
        started = time.time()
        log.info("launching native Chrome on %s", profile)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ServiceError("launch_failed", f"cannot start {chrome}: {exc}") from exc
        try:
            port, bpath = wait_for_endpoint(profile, proc, started=started, timeout=timeout)
            verify_endpoint_owner(profile, port, launched=_descendants(proc.pid))
            cdp = f"ws://127.0.0.1:{port}{bpath}"
            browser = pw.chromium.connect_over_cdp(cdp)
            contexts = browser.contexts
            context = contexts[0] if contexts else browser.new_context()
            return proc, browser, context, cdp
        except BaseException as exc:
            terminate_process(proc)
            if isinstance(exc, (ServiceError, KeyboardInterrupt, SystemExit)):
                raise
            raise ServiceError("launch_failed", f"native Chrome attach failed: {exc}") from exc
