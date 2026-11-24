"""Site sessions: one page, one thread and one Playwright attachment per live
``(site, session_id)``, plus the persisted ``session_id -> conversation URL`` map.

Layering
--------
* ``SiteSpec`` says what the core may know about a site: its adapter factory,
  the operation allowlist and which operations spend the account's send permit,
  continue or end the session's conversation, or stop generation. Everything
  about what a *page* should look like (signed in, inside the right folder,
  deviating) is the adapter's; the core only runs ``adapter.execute`` and passes
  its ``ServiceError`` through (``auth_required``, ``page_deviation``,
  ``timeout`` with the partial text in ``detail`` ...).
* ``SessionStore`` is the map file: URL and last use, nothing else, written
  atomically.
* ``Session`` is one live session. Its worker thread attaches to the window's
  CDP endpoint with its own Playwright (``attach_runtime``), makes the page a
  service-owned lease (``session:<site>/<id>``, so the zero-client sweep keeps
  it), builds the adapter on it and runs queued operations strictly in order.
  A Playwright object never leaves that thread.
* ``SessionManager`` owns the live set: cap and LRU eviction of idle sessions,
  the idle sweep, the per-site send permit (``max_concurrent_sends``), the
  independent ``stop`` path and shutdown.

Rules that matter
-----------------
* A submitted send is **never retried**. Staleness (page closed, browser
  relaunched) is detected only *before* an operation is claimed; the session
  then rebuilds its worker and the same, still-unstarted operation runs on it.
  A failure after the adapter was entered goes to the caller as it is.
* ``stop`` does not use the session's queue. It finds the page's target and
  runs ``SiteSpec.stop_page`` on a ``Window.work`` short-lane worker of its own.
* An operation whose caller left while it was still queued (or waiting for the
  send permit) is cancelled; one already inside the adapter runs to completion,
  its map entry is saved, and its deltas are dropped.
* Closing a session interrupts a busy page (closes its target) *before*
  joining the worker. A normal close keeps the map entry; ``forget`` removes it.
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .errors import ServiceError
from .windows import ConfigError
from .workers import is_closed_error, stopped

log = logging.getLogger("uc_browser.service.sessions")

DEFAULT_SESSION = "default"
MAX_SESSION_ID = 256
JOIN_S = 10.0  # bound on joining one session worker
MAX_RESTARTS = 3  # consecutive rebuilds without running an operation
PERMIT_POLL_S = 0.1
SWEEP_INTERVAL_S = 30.0
OWNER_WAIT_S = 5.0  # bound on a browser-authoritative page inventory
STORE_VERSION = 1
MAX_URL = 2048

_UNSET = object()
_COMMON_KEYS = frozenset({"window", "max_sessions", "max_concurrent_sends", "session_idle_s"})


# ── site descriptions ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SiteSpec:
    """What the core knows about one site.

    ``adapter(runtime, page, **options)`` builds the adapter on the session
    thread; ``adapter.execute(op, kwargs, on_delta)`` returns a JSON result. The
    adapter never closes the page or the browser, and its result and errors go
    to the caller untouched.

    * ``generating`` operations spend one account send permit.
    * ``session_url_ops`` receive the session's mapped ``conversation_url`` when
      the caller gave none (a caller that sends ``"conversation_url": null``
      asks for a new conversation).
    * ``bind_ops`` maps an operation to the key of its result that holds the
      conversation URL the session now continues (``adapter.url``, read on the
      session thread, when the result has none; ``None`` leaves the map as it
      is). It is saved after success, or after a ``timeout`` error whose
      ``data["result"]`` carries the partial result. ``reset_ops`` forget the
      session's URL after success (a new chat).
    * ``end_ops`` are operations that delete the conversation named by their
      ``conversation_url`` argument and return ``true`` on success;
      ``bulk_end_ops`` maps one that deletes many to the result key listing the
      deleted conversation ids. Every map entry for those conversations loses
      its URL.
    * ``stop_op``/``stop_page`` are the independent stop path: ``stop_page(page)``
      runs on a ``Window.work`` worker and returns whether it stopped anything.
    * ``conversation_key`` maps a URL to the conversation's identity so that a
      stop by URL matches ``/c/<id>`` and ``?chat=<id>`` forms alike.
    * ``options`` are the config keys passed to the adapter constructor;
      ``check_options`` rejects an invalid combination at configuration time.
    * ``requires`` are the window offers the site needs.
    """

    name: str
    adapter: Callable[..., Any]
    operations: frozenset
    generating: frozenset = frozenset()
    session_url_ops: frozenset = frozenset()
    bind_ops: Mapping[str, str] = field(default_factory=dict)
    reset_ops: frozenset = frozenset()
    end_ops: frozenset = frozenset()
    bulk_end_ops: Mapping[str, str] = field(default_factory=dict)
    stop_op: str | None = None
    stop_page: Callable[[Any], bool] | None = None
    conversation_key: Callable[[str], str | None] | None = None
    options: frozenset = frozenset()
    check_options: Callable[[dict], None] | None = None
    requires: tuple = ("attach", "control")

    def __post_init__(self):
        ops = frozenset(self.operations)
        object.__setattr__(self, "operations", ops)
        for label in ("generating", "session_url_ops", "reset_ops", "end_ops"):
            group = frozenset(getattr(self, label))
            object.__setattr__(self, label, group)
            if not group <= ops:
                raise ValueError(f"site {self.name}: {label} names unknown operations {sorted(group - ops)}")
        for label in ("bind_ops", "bulk_end_ops"):
            mapping = dict(getattr(self, label))
            object.__setattr__(self, label, mapping)
            if not set(mapping) <= ops:
                raise ValueError(f"site {self.name}: {label} names unknown operations {sorted(set(mapping) - ops)}")
        if (self.stop_op is None) != (self.stop_page is None):
            raise ValueError(f"site {self.name}: stop_op and stop_page go together")
        if self.stop_op is not None and self.stop_op not in ops:
            raise ValueError(f"site {self.name}: stop_op {self.stop_op!r} is not an operation")
        object.__setattr__(self, "options", frozenset(self.options))


_GROK_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_GROK_PATH_RE = re.compile(rf"/c/({_GROK_UUID})(?:/|$)")
_GROK_BARE_RE = re.compile(_GROK_UUID)


def grok_conversation_key(url: str) -> str | None:
    """Conversation id of ``/c/<id>``, ``...?chat=<id>`` (project) URLs or a bare id."""
    text = url.strip()
    if _GROK_BARE_RE.fullmatch(text):
        return text.lower()
    parts = urlsplit(text)
    match = _GROK_PATH_RE.search(parts.path)
    if match:
        return match.group(1).lower()
    for chat in parse_qs(parts.query).get("chat", []):
        if _GROK_BARE_RE.fullmatch(chat):
            return chat.lower()
    return None


def _check_grok_options(options: dict) -> None:
    """The adapter's own constructor rules, applied when the config is read."""
    folder, folder_url = options.get("folder"), options.get("folder_url")
    if folder and not folder_url:
        raise ValueError("a folder needs a folder_url to route and verify against")
    if folder_url is not None:
        parts = urlsplit(folder_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password \
                or not parts.path.rstrip("/"):
            raise ValueError("folder_url must be an https URL of a project page")


def builtin_sites() -> dict[str, SiteSpec]:
    """The adapters this build serves: Grok only. Imported lazily, so a service
    without configured sites never loads the adapter."""
    from uc_browser.sites.grok_service import OPERATIONS, GrokSessionAdapter

    return {
        "grok": SiteSpec(
            name="grok",
            adapter=GrokSessionAdapter,
            operations=frozenset(OPERATIONS),
            generating=frozenset({"send", "regenerate"}),
            session_url_ops=frozenset({"send"}),
            bind_ops={"send": "url"},
            reset_ops=frozenset({"new_chat"}),
            end_ops=frozenset({"delete", "archive"}),
            bulk_end_ops={"cleanup_threads": "deleted_ids"},
            stop_op="stop",
            stop_page=GrokSessionAdapter.stop_page,
            conversation_key=grok_conversation_key,
            options=frozenset({"folder", "folder_url"}),
            check_options=_check_grok_options,
        )
    }


# ── configuration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SiteConfig:
    site: str
    window: str
    max_sessions: int = 4
    max_concurrent_sends: int = 1
    session_idle_s: float = 1800.0
    options: dict = field(default_factory=dict)  # adapter keyword arguments from the config


def _positive_int(site: str, key: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"site {site!r}: {key} must be an integer >= 1")
    return value


def _is_http_url(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc) and "@" not in parts.netloc


def parse_sites(raw, windows, specs: dict[str, SiteSpec]) -> dict[str, SiteConfig]:
    """Validate the ``sites`` section. Raises ``ConfigError`` at construction:
    unknown site (no adapter), unknown key, missing/unusable window, bad numbers."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'sites' must be an object")
    out: dict[str, SiteConfig] = {}
    for name, entry in raw.items():
        spec = specs.get(name)
        if spec is None:
            raise ConfigError(f"site {name!r} has no adapter (supported: {sorted(specs)})")
        if not isinstance(entry, dict):
            raise ConfigError(f"site {name!r} must be an object")
        unknown = set(entry) - _COMMON_KEYS - spec.options
        if unknown:
            raise ConfigError(f"site {name!r}: unknown keys {sorted(unknown)}")
        window_name = entry.get("window")
        if not isinstance(window_name, str) or not window_name:
            raise ConfigError(f"site {name!r}: window must name a configured window")
        try:
            window = windows.get(window_name)
        except ServiceError as exc:
            raise ConfigError(f"site {name!r}: no window named {window_name!r}") from exc
        offers = set(getattr(window.spec, "offers", ()))
        missing = [o for o in spec.requires if o not in offers]
        if missing:
            raise ConfigError(f"site {name!r}: window {window_name!r} does not offer {missing}")
        idle = entry.get("session_idle_s", 1800)
        if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle <= 0:
            raise ConfigError(f"site {name!r}: session_idle_s must be a finite number > 0")
        options = {}
        for key in spec.options & set(entry):
            value = entry[key]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"site {name!r}: {key} must be a non-empty string")
            if key.endswith("_url") and not _is_http_url(value):
                raise ConfigError(f"site {name!r}: {key} must be an http(s) URL")
            options[key] = value
        if spec.check_options is not None:
            try:
                spec.check_options(options)
            except ValueError as exc:
                raise ConfigError(f"site {name!r}: {exc}") from exc
        out[name] = SiteConfig(
            site=name,
            window=window_name,
            max_sessions=_positive_int(name, "max_sessions", entry.get("max_sessions", 4)),
            max_concurrent_sends=_positive_int(name, "max_concurrent_sends", entry.get("max_concurrent_sends", 1)),
            session_idle_s=float(idle),
            options=options,
        )
    return out


# ── persisted map ─────────────────────────────────────────────────────────


class SessionStore:
    """``{site: {session_id: {url, last_used}}}`` in one JSON file.

    Holds a conversation URL and a wall-clock last-use time per session; no
    message content, cookies or credentials. Every change is written to a
    temporary file in the same directory, fsynced and renamed over the map, so a
    reader sees the old or the new file, never a partial one.
    """

    def __init__(self, path, *, wall=time.time):
        self.path = Path(path).expanduser()
        self._wall = wall
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, dict]] = {}
        self._load()

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ConfigError(f"cannot read sessions file {self.path}: {exc}") from exc
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"sessions file {self.path} is not valid JSON: {exc}") from exc
        sessions = raw.get("sessions") if isinstance(raw, dict) and raw.get("version") == STORE_VERSION else None
        if not isinstance(sessions, dict):
            raise ConfigError(f"sessions file {self.path}: expected version {STORE_VERSION} and a 'sessions' object")
        for site, entries in sessions.items():
            if not isinstance(entries, dict):
                raise ConfigError(f"sessions file {self.path}: site {site!r} must be an object")
            for sid, entry in entries.items():
                url = entry.get("url") if isinstance(entry, dict) else None
                last = entry.get("last_used") if isinstance(entry, dict) else None
                if (not isinstance(entry, dict) or not (url is None or _is_http_url(url))
                        or isinstance(last, bool) or not isinstance(last, (int, float)) or not math.isfinite(last)):
                    raise ConfigError(f"sessions file {self.path}: invalid entry for {site}/{sid}")
                self._data.setdefault(site, {})[sid] = {"url": url, "last_used": float(last)}

    def _save(self) -> None:
        payload = {"version": STORE_VERSION, "sessions": self._data}
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            tmp = None
        except OSError as exc:
            raise ServiceError("session_store_failed", f"cannot write {self.path}: {exc}", 500) from exc
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def url(self, site: str, sid: str) -> str | None:
        with self._lock:
            entry = self._data.get(site, {}).get(sid)
            return entry["url"] if entry else None

    def has(self, site: str, sid: str) -> bool:
        with self._lock:
            return sid in self._data.get(site, {})

    def entries(self, site: str) -> dict[str, tuple[str | None, float]]:
        """``{session_id: (url, last_used wall time)}`` copy."""
        with self._lock:
            return {sid: (e["url"], e["last_used"]) for sid, e in self._data.get(site, {}).items()}

    def touch(self, site: str, sid: str, url=_UNSET) -> bool:
        """Record a use. Creates the entry only when there is a URL to keep."""
        with self._lock:
            entry = self._data.get(site, {}).get(sid)
            keep = url is not _UNSET and url is not None
            if keep and not _is_http_url(url):
                log.warning("not persisting a session url that is not a plain http(s) URL (%s/%s)", site, sid)
                keep = False
            if entry is None:
                if not keep:
                    return False
                entry = self._data.setdefault(site, {})[sid] = {"url": None, "last_used": 0.0}
            if keep:
                entry["url"] = url
            entry["last_used"] = self._wall()
            self._save()
            return True

    def clear_url(self, site: str, matches: Callable[[str], bool]) -> int:
        """Drop the URL (keep the entry) of every session whose URL ``matches``."""
        with self._lock:
            hit = [e for e in self._data.get(site, {}).values() if e["url"] is not None and matches(e["url"])]
            for entry in hit:
                entry["url"] = None
            if hit:
                self._save()
            return len(hit)

    def reset(self, site: str, sid: str) -> bool:
        """Forget the session's conversation (a new chat); keep the entry."""
        with self._lock:
            entry = self._data.get(site, {}).get(sid)
            if entry is None:
                return False
            entry["url"] = None
            entry["last_used"] = self._wall()
            self._save()
            return True

    def forget(self, site: str, sid: str) -> bool:
        with self._lock:
            entries = self._data.get(site, {})
            if sid not in entries:
                return False
            del entries[sid]
            if not entries:
                del self._data[site]
            self._save()
            return True


# ── send permit ───────────────────────────────────────────────────────────


class _Permits:
    """The account's concurrent-send limit: a counting permit handed out FIFO."""

    def __init__(self, limit: int):
        self._cv = threading.Condition()
        self._free = limit
        self._waiting: collections.deque = collections.deque()

    def try_acquire(self) -> bool:
        with self._cv:
            if self._free > 0 and not self._waiting:
                self._free -= 1
                return True
            return False

    def acquire(self, give_up: Callable[[], bool], poll: float = PERMIT_POLL_S) -> bool:
        """Wait for a permit; False as soon as ``give_up()`` says so."""
        ticket = object()
        with self._cv:
            self._waiting.append(ticket)
            try:
                while True:
                    if self._waiting[0] is ticket and self._free > 0:
                        self._waiting.popleft()
                        self._free -= 1
                        return True
                    if give_up():
                        return False
                    self._cv.wait(poll)
            finally:
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                    self._cv.notify_all()

    def release(self) -> None:
        with self._cv:
            self._free += 1
            self._cv.notify_all()


# ── operations in flight ──────────────────────────────────────────────────


class _Task:
    """One submitted operation and its future."""

    def __init__(self, op: str, kwargs: dict, on_delta, wait: bool, permits: _Permits | None):
        self.op = op
        self.kwargs = kwargs
        self.on_delta = on_delta
        self.wait = wait
        self.permits = permits
        self.future: cf.Future = cf.Future()
        self.abandoned = threading.Event()
        self._plock = threading.Lock()
        self._held = False
        # One hook covers every ending: result, error and queued cancellation.
        self.future.add_done_callback(self._release)

    @property
    def permit_held(self) -> bool:
        return self._held

    def grant(self) -> None:
        with self._plock:
            self._held = True

    def _release(self, _future) -> None:
        with self._plock:
            held, self._held = self._held, False
        if held:
            self.permits.release()

    def abandon(self) -> None:
        """The caller left: stop streaming deltas and cancel if not yet started."""
        self.abandoned.set()
        self.future.cancel()

    def fail(self, exc: BaseException) -> None:
        """Fail a task that no worker claimed."""
        try:
            if self.future.set_running_or_notify_cancel():
                self.future.set_exception(exc)
        except (RuntimeError, cf.InvalidStateError):
            pass


class Call:
    """Handle on one submitted operation."""

    def __init__(self, future: cf.Future, abandon: Callable[[], Any]):
        self.future = future
        self._abandon = abandon

    def result(self, timeout: float | None = None):
        return self.future.result(timeout)

    def abandon(self) -> None:
        self._abandon()


def _resolved(value) -> Call:
    future: cf.Future = cf.Future()
    future.set_result(value)
    return Call(future, lambda: None)


# ── helpers ───────────────────────────────────────────────────────────────


def _json_object(value, what: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ServiceError("bad_arguments", f"{what} must be a JSON object", 400)
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ServiceError("bad_arguments", f"{what} must be JSON", 400) from exc


def _check_session_id(sid) -> None:
    if not isinstance(sid, str) or not sid or len(sid) > MAX_SESSION_ID or not sid.isprintable():
        raise ServiceError("bad_arguments", f"session_id must be a printable string of 1-{MAX_SESSION_ID} characters", 400)


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}{query}"


def _url_key(spec: SiteSpec, url) -> tuple | None:
    """Comparable identity of a URL: the site's conversation id, else the URL
    without fragment and trailing slash. ``None`` for anything that is no URL."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        conversation = spec.conversation_key(url) if spec.conversation_key is not None else None
        return ("conversation", conversation) if conversation else ("url", _canonical_url(url))
    except ValueError:
        return None


def _stop_runtime(runtime) -> None:
    try:
        runtime.stop_playwright()
    except Exception as exc:  # noqa: BLE001
        log.debug("stopping a session's Playwright: %s", type(exc).__name__)


def attach_runtime(window):
    """A runtime with its own Playwright attached to ``window``'s CDP endpoint.
    Called on the thread that will own it (the session worker)."""
    from playwright.sync_api import sync_playwright

    from .workers import Runtime

    cdp = window.cdp
    if cdp is None:
        raise stopped(f"window {window.spec.name} is not running")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp)
        if not browser.contexts:
            raise ServiceError("launch_failed", "attached browser has no default context")
        return Runtime(
            window=window.spec.name, engine=window.spec.engine, kind=window.spec.browser, pw=pw,
            browser=browser, context=browser.contexts[0], cdp=cdp, role="session",
        )
    except BaseException:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
        raise


class _Binding:
    """What a worker thread holds while it serves: its Playwright objects."""

    __slots__ = ("runtime", "page", "adapter", "target", "generation")

    def __init__(self, runtime, page, adapter, target: str, generation: int):
        self.runtime, self.page, self.adapter, self.target, self.generation = runtime, page, adapter, target, generation


# ── one live session ──────────────────────────────────────────────────────


class Session:
    """A live ``(site, id)``. Mutable fields are guarded by ``manager._lock``."""

    def __init__(self, manager: "SessionManager", spec: SiteSpec, config: SiteConfig, session_id: str,
                 ephemeral: bool = False):
        self.manager = manager
        self.spec = spec
        self.config = config
        self.id = session_id
        self.key = (config.site, session_id)
        self.cond = threading.Condition(manager._lock)
        self.tasks: collections.deque[_Task] = collections.deque()
        self.running: _Task | None = None
        self.worker: threading.Thread | None = None
        self.target: str | None = None
        self.generation: int | None = None
        self.closing = False
        self.forgotten = False
        self.restarts = 0
        self.in_flight_url: str | None = None
        # Lifetime is fixed at creation. An ephemeral session is never written to
        # the map: its conversation URL lives here and dies with the session.
        self.ephemeral = ephemeral
        self.mem_url: str | None = None
        self.last_used = manager.clock()

    @property
    def busy(self) -> bool:
        return self.running is not None or any(not t.future.cancelled() for t in self.tasks)

    def mapped_url(self) -> str | None:
        """The conversation URL this session continues: its map entry, or, for
        an ephemeral session, the one it holds in memory."""
        if self.ephemeral:
            return self.mem_url
        return self.manager._store.url(self.config.site, self.id)

    def known_keys(self) -> set:
        """Identities of the conversations this session is known to hold: the
        mapped one and the one an operation in flight was told to use."""
        urls = [self.mapped_url(), self.in_flight_url]
        return {k for k in (_url_key(self.spec, u) for u in urls) if k is not None}

    # -- worker thread ----------------------------------------------------

    def run(self) -> None:
        manager = self.manager
        try:
            binding = self._bind()
        except BaseException as exc:  # noqa: BLE001 - delivered to the queued callers
            err = exc if isinstance(exc, ServiceError) else ServiceError(
                "session_unavailable",
                f"cannot open the {self.config.site} session page: {type(exc).__name__}: {exc}"[:400], 503)
            log.warning("session %s/%s could not start: %s", self.config.site, self.id, getattr(err, "code", err))
            manager._bind_failed(self, err)
            return
        restart = False
        try:
            restart = self._serve(binding)
        except BaseException as exc:  # noqa: BLE001 - a bug; rebuild rather than lose the session
            log.error("session %s/%s worker crashed: %s", self.config.site, self.id, type(exc).__name__)
            restart = True
        finally:
            self._unbind(binding)
            manager._worker_exited(self, restart)

    def _bind(self) -> _Binding:
        manager = self.manager
        window = manager.windows.get(self.config.window)
        window.start()
        generation = window.generation
        runtime = manager._runtime_factory(window)
        target = None
        try:
            target = manager.leases.internal_page(
                window.spec.name, holder=f"session:{self.config.site}/{self.id}")
            page = runtime.resolve(target)
            adapter = self.spec.adapter(runtime, page, **self.config.options)
        except BaseException:
            if target is not None:
                manager._close_page_quietly(window.spec.name, target)
            _stop_runtime(runtime)
            raise
        with manager._lock:
            self.target = target
            self.generation = generation
        return _Binding(runtime, page, adapter, target, generation)

    def _unbind(self, binding: _Binding | None) -> None:
        if binding is None:
            return
        manager = self.manager
        manager._close_page_quietly(self.config.window, binding.target)
        with manager._lock:
            if self.target == binding.target:
                self.target = None
                self.generation = None
        _stop_runtime(binding.runtime)

    def _stale(self, window, binding: _Binding) -> bool:
        try:
            return (not binding.runtime.alive or binding.page.is_closed()
                    or window.generation != binding.generation)
        except Exception:  # noqa: BLE001 - an unreadable page is a lost page
            return True

    def _serve(self, binding: _Binding) -> bool:
        """Run queued tasks until closed. True: rebuild the worker (the page or
        browser is gone) and hand the unstarted tasks to the new one."""
        manager = self.manager
        window = manager.windows.get(self.config.window)
        while True:
            with manager._lock:
                while not self.tasks and not self.closing:
                    self.cond.wait()
                if self.closing:
                    return False
            # Playwright state is read here, outside the lock, on this thread.
            stale = self._stale(window, binding)
            with manager._lock:
                if self.closing:
                    return False
                if not self.tasks:
                    continue
                if stale:
                    return True
                task = self.tasks.popleft()
                self.running = task
                self.restarts = 0
            self._execute(task, binding)

    def _take_permit(self, task: _Task) -> None:
        manager = self.manager

        def give_up() -> bool:
            return task.abandoned.is_set() or self.closing or manager._closed

        if not task.permits.acquire(give_up):
            if task.abandoned.is_set():
                raise ServiceError("cancelled", "the caller left before the send started", 409)
            raise ServiceError("session_closed", "the session closed before the send started", 409)
        task.grant()

    def _kwargs_for(self, task: _Task) -> dict:
        manager = self.manager
        kwargs = dict(task.kwargs)
        with manager._lock:
            if task.op in self.spec.session_url_ops and "conversation_url" not in kwargs:
                mapped = self.mapped_url()
                if mapped:
                    kwargs["conversation_url"] = mapped
            url = kwargs.get("conversation_url")
            self.in_flight_url = url if isinstance(url, str) else None
        return kwargs

    @staticmethod
    def _emitter(task: _Task):
        if task.on_delta is None:
            return None

        def emit(text) -> None:
            if task.abandoned.is_set():
                return
            try:
                task.on_delta(text)
            except Exception as exc:  # noqa: BLE001 - a broken consumer must not break the send
                task.abandoned.set()
                log.debug("delta consumer failed (%s); dropping further deltas", type(exc).__name__)

        return emit

    def _wrap(self, exc: Exception, binding: _Binding, op: str) -> ServiceError:
        try:
            closed = is_closed_error(exc) or binding.page.is_closed()
        except Exception:  # noqa: BLE001
            closed = True
        if closed:
            return ServiceError(
                "session_lost", f"the session page closed during {op}; the operation was not retried", 503)
        return ServiceError("site_failed", f"{type(exc).__name__}: {exc}"[:2000], 500)

    def _execute(self, task: _Task, binding: _Binding) -> None:
        manager = self.manager
        if not task.future.set_running_or_notify_cancel():  # the caller left while queued
            with manager._lock:
                self.running = None
                manager._changed.notify_all()
            return
        result = None
        error: ServiceError | None = None
        entered = False
        try:
            if task.permits is not None and not task.permit_held:
                self._take_permit(task)
            if task.abandoned.is_set():
                raise ServiceError("cancelled", "the caller left before the operation started", 409)
            kwargs = self._kwargs_for(task)
            entered = True
            result = binding.adapter.execute(task.op, kwargs, self._emitter(task))
            try:
                json.dumps(result, allow_nan=False)
            except (TypeError, ValueError) as exc:
                result = None
                raise ServiceError("site_failed", f"{task.op} returned a non-JSON result", 500) from exc
        except ServiceError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001
            # Class only: Playwright errors can quote the values a call was given.
            log.warning("session %s/%s %s failed: %s", self.config.site, self.id, task.op, type(exc).__name__)
            error = self._wrap(exc, binding, task.op)
        failure = error
        try:
            store_error = self._record(task, binding, result, error, entered)
            if error is None and store_error is not None:
                # The operation already ran; the caller must not retry it.
                failure = ServiceError(
                    "session_store_failed",
                    f"the operation ran but its session map entry was not saved; do not retry it: {store_error.detail}",
                    500)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never leave the session marked busy
            log.error("session %s/%s bookkeeping failed: %s", self.config.site, self.id, type(exc).__name__)
            with manager._lock:
                self.running = None
                self.in_flight_url = None
                manager._changed.notify_all()
        finally:
            try:
                if failure is not None:
                    task.future.set_exception(failure)
                else:
                    task.future.set_result(result)
            except cf.InvalidStateError:  # a timed-out close already failed this future
                pass

    def _record(self, task: _Task, binding: _Binding, result, error: ServiceError | None,
                entered: bool) -> ServiceError | None:
        """Settle the session after an operation and return the map's write
        error, if any. Running state is always released and waiters woken."""
        manager = self.manager
        spec = self.spec
        site = self.config.site
        # A timeout still created or continued a conversation: its error carries
        # the partial result in ``data["result"]``.
        outcome = result
        if error is not None and error.code == "timeout":
            partial = getattr(error, "data", None)
            outcome = partial.get("result") if isinstance(partial, dict) else None
        url = None
        bind_key = spec.bind_ops.get(task.op)
        if entered and bind_key is not None and (error is None or error.code == "timeout"):
            value = outcome.get(bind_key) if isinstance(outcome, dict) else None
            if not (isinstance(value, str) and value):
                try:
                    value = binding.adapter.url  # the adapter's own view of the session's conversation
                except Exception:  # noqa: BLE001
                    value = None
            if isinstance(value, str) and value:
                url = value
        reset = entered and error is None and task.op in spec.reset_ops
        gone: set = set()
        if entered and error is None:
            ended = task.kwargs.get("conversation_url")
            if task.op in spec.end_ops and result is True and isinstance(ended, str):
                key = _url_key(spec, ended)
                if key is not None:
                    gone.add(key)
            listed = spec.bulk_end_ops.get(task.op)
            ids = result.get(listed) if listed is not None and isinstance(result, dict) else None
            if isinstance(ids, list):
                gone.update(("conversation", i.lower()) for i in ids if isinstance(i, str))
        store_error: ServiceError | None = None
        with manager._lock:
            self.running = None
            self.in_flight_url = None
            self.last_used = manager.clock()
            if entered and not self.forgotten:
                try:
                    if self.ephemeral:
                        if url is not None:
                            self.mem_url = url
                        elif reset:
                            self.mem_url = None
                    elif reset:
                        manager._store.reset(site, self.id)
                    elif url is not None:
                        manager._store.touch(site, self.id, url=url)
                    else:
                        manager._store.touch(site, self.id)
                except ServiceError as exc:
                    store_error = exc
            if gone:
                for other in manager._live.values():
                    if (other.config.site == site and other.ephemeral
                            and _url_key(spec, other.mem_url) in gone):
                        other.mem_url = None
                try:
                    manager._store.clear_url(site, lambda stored: _url_key(spec, stored) in gone)
                except ServiceError as exc:
                    store_error = store_error or exc
            if store_error is not None:
                log.error("sessions map not saved (%s)", store_error.code)
            manager._changed.notify_all()
        return store_error


# ── the manager ───────────────────────────────────────────────────────────


class SessionManager:
    """Live sessions of every configured site.

    ``sites`` is the raw ``sites`` object (default: ``windows.config["sites"]``);
    ``specs`` the adapters (default: ``builtin_sites()``); ``store_path`` the map
    file (default ``UC_SESSIONS_FILE``, else ``data/sessions.json``);
    ``runtime_factory(window)`` runs on the session thread and returns the
    runtime its adapter will use (default ``attach_runtime``). ``clock`` is
    monotonic seconds (LRU, idle), ``wall`` epoch seconds (the map).
    """

    def __init__(self, windows, leases, *, sites=_UNSET, specs: dict[str, SiteSpec] | None = None,
                 store_path=None, runtime_factory=None, clock=time.monotonic, wall=time.time,
                 sweep_interval: float = SWEEP_INTERVAL_S):
        self.windows = windows
        self.leases = leases
        self.clock = clock
        self._runtime_factory = runtime_factory or attach_runtime
        self._sweep_interval = sweep_interval
        raw = (getattr(windows, "config", None) or {}).get("sites", {}) if sites is _UNSET else sites
        if specs is None:
            specs = builtin_sites() if raw else {}
        self.specs = specs
        self.configs = parse_sites(raw, windows, specs)
        self._store: SessionStore | None = None
        if self.configs:
            path = store_path or os.environ.get("UC_SESSIONS_FILE", "").strip() or "data/sessions.json"
            self._store = SessionStore(path, wall=wall)
        self._permits = {name: _Permits(cfg.max_concurrent_sends) for name, cfg in self.configs.items()}
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._live: dict[tuple[str, str], Session] = {}
        self._closing: set[Session] = set()
        self._closed = False
        self._slot_waiters = 0
        self._stop_event = threading.Event()
        self._sweeper: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._closed or self._sweeper is not None or not self.configs:
                return
            self._sweeper = threading.Thread(target=self._sweep_loop, name="uc-session-sweep", daemon=True)
            self._sweeper.start()

    def close(self) -> None:
        """Stop the sweep, then close every live session: interrupt busy pages
        first, then join the workers. Maps are kept. Idempotent."""
        with self._lock:
            self._closed = True
            self._changed.notify_all()
            sessions = list(self._live.values())
            sweeper, self._sweeper = self._sweeper, None
        self._stop_event.set()
        if sweeper is not None and sweeper is not threading.current_thread():
            sweeper.join(JOIN_S)
        self._close_many(sessions, forget=False, err=stopped("the service is shutting down"))

    def _check_open(self) -> None:
        if self._closed:
            raise stopped("the session manager is closed")

    def _sweep_loop(self) -> None:
        while not self._stop_event.wait(self._sweep_interval):
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001
                log.error("session sweep failed: %s", type(exc).__name__)

    # -- lookup ---------------------------------------------------------------

    def _site(self, site) -> tuple[SiteConfig, SiteSpec]:
        config = self.configs.get(site) if isinstance(site, str) else None
        if config is None:
            raise ServiceError("no_such_site", f"no site named {site!r}", 404)
        return config, self.specs[site]

    @property
    def sites(self) -> list[str]:
        return sorted(self.configs)

    def _occupied(self, site: str) -> int:
        return (sum(1 for s in self._live.values() if s.config.site == site)
                + sum(1 for s in self._closing if s.config.site == site))

    def _lru_idle(self, site: str) -> Session | None:
        idle = [s for s in self._live.values() if s.config.site == site and not s.closing and not s.busy]
        return min(idle, key=lambda s: s.last_used) if idle else None

    # -- submitting ---------------------------------------------------------------

    def submit(self, site: str, op: str, kwargs: dict | None = None, session_id: str | None = None, *,
               wait: bool = True, ephemeral: bool = False, on_delta=None) -> Call:
        """Queue ``op`` on the session and return its ``Call``. May block while
        waiting for a session slot (bounded by the window's ``queue_timeout_s``);
        call it from a plain thread. Errors that need no page (unknown site or
        operation, bad arguments, ``busy``, ``session_conflict``) are raised here.

        ``ephemeral`` fixes the lifetime of a session this call creates: it
        stays live until idle/LRU expiry or ``forget`` like any session, but its
        conversation URL is never persisted and goes away with it. An existing
        session (live, or mapped on disk) is never converted either way:
        asking for the other lifetime is ``session_conflict`` (409)."""
        config, spec = self._site(site)
        if not isinstance(op, str) or op not in spec.operations:
            raise ServiceError("no_such_op", f"site {site!r} has no operation {op!r}", 404)
        kwargs = _json_object(kwargs, "operation arguments")
        if session_id is not None:
            _check_session_id(session_id)
        if not isinstance(wait, bool) or not isinstance(ephemeral, bool):
            raise ServiceError("bad_arguments", "wait and ephemeral must be booleans", 400)
        if op == spec.stop_op:
            return self._stop(config, spec, kwargs, session_id)
        sid = session_id if session_id is not None else DEFAULT_SESSION
        task = _Task(op, kwargs, on_delta, wait, self._permits[site] if op in spec.generating else None)
        key = (site, sid)
        deadline = time.monotonic() + self.windows.get(config.window).spec.queue_timeout_s
        while True:
            evict = None
            with self._lock:
                self._check_open()
                session = self._live.get(key)
                if session is not None:
                    if session.ephemeral != ephemeral:
                        raise self._conflict(sid, session.ephemeral)
                    if not wait and session.busy:
                        raise ServiceError("busy", f"session {sid!r} is busy", 503)
                    self._grant(task)
                    self._enqueue(session, task)
                    return Call(task.future, task.abandon)
                if self._occupied(site) < config.max_sessions:
                    if ephemeral and self._store.has(site, sid):
                        raise self._conflict(sid, False)
                    self._grant(task)
                    session = Session(self, spec, config, sid, ephemeral)
                    self._live[key] = session
                    self._enqueue(session, task)
                    return Call(task.future, task.abandon)
                victim = self._lru_idle(site)
                if victim is not None:
                    evict = (victim, *self._begin_close(victim, forget=False))
                else:
                    remaining = deadline - time.monotonic()
                    if not wait or remaining <= 0:
                        raise ServiceError(
                            "busy", f"all {config.max_sessions} {site} sessions are busy", 503)
                    self._slot_waiters += 1
                    try:
                        self._changed.wait(remaining)
                    finally:
                        self._slot_waiters -= 1
                    continue
            victim, tasks, interrupt = evict
            self._interrupt(victim, interrupt)
            self._join(victim, tasks, ServiceError("session_closed", "the session was evicted for another", 409))

    @staticmethod
    def _conflict(sid: str, existing_ephemeral: bool) -> ServiceError:
        kind = "ephemeral" if existing_ephemeral else "persistent"
        return ServiceError(
            "session_conflict", f"session {sid!r} is {kind}; its lifetime cannot be changed", 409)

    def _grant(self, task: _Task) -> None:
        """``wait=False`` takes its permit at admission, so ``busy`` is an HTTP
        503 before anything starts; ``wait=True`` takes it in the worker."""
        if task.permits is not None and not task.wait:
            if not task.permits.try_acquire():
                raise ServiceError("busy", "the account's concurrent send limit is reached", 503)
            task.grant()

    def _enqueue(self, session: Session, task: _Task) -> None:
        session.tasks.append(task)
        session.last_used = self.clock()
        session.cond.notify_all()
        if session.worker is None:
            self._spawn(session)

    def _spawn(self, session: Session) -> None:
        worker = threading.Thread(
            target=session.run, name=f"uc-session-{session.config.site}-{session.id}"[:60], daemon=True)
        session.worker = worker
        worker.start()

    # -- stop (never queues behind the session's own operations) ----------------

    def _stop(self, config: SiteConfig, spec: SiteSpec, kwargs: dict, session_id: str | None) -> Call:
        extra = set(kwargs) - {"conversation_url"}
        if extra:
            raise ServiceError("bad_arguments", f"{spec.stop_op} takes only conversation_url, not {sorted(extra)}", 400)
        url = kwargs.get("conversation_url")
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise ServiceError("bad_arguments", "conversation_url must be a non-empty string", 400)
        wanted = _url_key(spec, url) if url else None
        window = self.windows.get(config.window)
        with self._lock:
            self._check_open()
            if session_id is not None:
                pool = [self._live.get((config.site, session_id))]
            elif url is not None:
                pool = [s for s in self._live.values() if s.config.site == config.site]
            else:
                pool = [self._live.get((config.site, DEFAULT_SESSION))]
            candidates = [(s.target, s.known_keys()) for s in pool
                          if s is not None and not s.closing and s.target is not None
                          and s.generation == window.generation]
        if not candidates or not window.running:
            return _resolved(False)
        pages: dict[str, Any] = {}
        if wanted is not None:
            # Browser-authoritative: the page's own URL, read without touching
            # the session's thread, so a send that has just been redirected to
            # its new conversation is matched before the map knows it.
            future = window.owner(lambda rt: rt.pages_info(), launch=False)
            try:
                pages = {p["target_id"]: p["url"] for p in future.result(timeout=OWNER_WAIT_S)}
            except cf.TimeoutError:
                future.cancel()
                raise ServiceError("busy", f"window {config.window} did not list its pages in time", 503) from None
        targets = []
        for target, known in candidates:
            if wanted is not None:
                keys = known | ({_url_key(spec, pages.get(target))} - {None})
                if session_id is not None:
                    # An explicit session is stopped unless its page is known to
                    # hold a different conversation; a landing page or an
                    # unreadable URL is no evidence of one.
                    comparable = {k for k in keys if k[0] == wanted[0]}
                    if comparable and wanted not in comparable:
                        continue
                elif wanted not in keys:
                    continue
            targets.append(target)
        if not targets:
            return _resolved(False)
        stop_page = spec.stop_page

        def job(rt) -> bool:
            stopped_any = False
            for target in targets:
                try:
                    page = rt.resolve(target)
                except ServiceError as exc:
                    if exc.code == "no_such_target":
                        continue
                    raise
                try:
                    stopped_any = bool(stop_page(page)) or stopped_any
                except ServiceError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if is_closed_error(exc) or page.is_closed():
                        continue
                    raise ServiceError("site_failed", f"stop failed: {type(exc).__name__}", 500) from exc
            return stopped_any

        future = window.work(job)
        return Call(future, future.cancel)

    # -- closing --------------------------------------------------------------------

    def _begin_close(self, session: Session, *, forget: bool) -> tuple[list[_Task], str | None]:
        """Under the lock: unlink the session, take its queue and say which
        target to close to interrupt a running operation."""
        session.closing = True
        if forget:
            session.forgotten = True
        if self._live.get(session.key) is session:
            del self._live[session.key]
        self._closing.add(session)
        tasks = list(session.tasks)
        session.tasks.clear()
        session.cond.notify_all()
        interrupt = session.target if session.running is not None else None
        return tasks, interrupt

    def _close_page_quietly(self, window: str, target: str) -> None:
        try:
            self.leases.close_page(window, target)
        except ServiceError as exc:
            log.debug("closing session page %s: %s", target, exc.code)
        except Exception as exc:  # noqa: BLE001
            log.warning("closing session page %s failed: %s", target, type(exc).__name__)

    def _interrupt(self, session: Session, target: str | None) -> None:
        """Close a busy session's page so its blocked Playwright call raises."""
        if target is not None:
            self._close_page_quietly(session.config.window, target)

    def _join(self, session: Session, tasks: list[_Task], err: ServiceError) -> None:
        for task in tasks:
            task.fail(err)
        worker = session.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(JOIN_S)
            if worker.is_alive():
                # Keep the slot occupied: ``_worker_exited`` frees it when the
                # thread (and its Playwright driver) really ends, so repeated
                # timed-out teardowns cannot exceed ``max_sessions``.
                log.warning("session %s/%s worker did not stop within %.0fs", *session.key, JOIN_S)
                with self._lock:
                    running = session.running
                if running is not None and not running.future.done():
                    try:
                        running.future.set_exception(err)
                    except cf.InvalidStateError:
                        pass
                return
        with self._lock:
            self._closing.discard(session)  # no worker was left to do it
            self._changed.notify_all()

    def _close_many(self, sessions: list[Session], *, forget: bool, err: ServiceError) -> int:
        begun = []
        with self._lock:
            for session in sessions:
                if not session.closing:
                    begun.append((session, *self._begin_close(session, forget=forget)))
        for session, _tasks, interrupt in begun:  # every interrupt before any join
            self._interrupt(session, interrupt)
        for session, tasks, _interrupt in begun:
            self._join(session, tasks, err)
        return len(begun)

    def _bind_failed(self, session: Session, err: ServiceError) -> None:
        with self._lock:
            if session.worker is threading.current_thread():
                session.worker = None
            session.closing = True
            tasks = list(session.tasks)
            session.tasks.clear()
            if self._live.get(session.key) is session:
                del self._live[session.key]
            self._closing.discard(session)  # this thread is ending
            self._changed.notify_all()
        for task in tasks:
            task.fail(err)

    def _worker_exited(self, session: Session, restart: bool) -> None:
        failed: list[_Task] = []
        err = None
        with self._lock:
            if session.worker is threading.current_thread():
                session.worker = None
            if restart and not session.closing:
                session.restarts += 1
                if session.restarts > MAX_RESTARTS:
                    err = ServiceError(
                        "session_unavailable", "the session page keeps closing; no page could be kept open", 503)
                    failed, _ = self._begin_close(session, forget=False)
                elif session.tasks:
                    self._spawn(session)  # a fresh thread and Playwright take over unstarted work
            if session.closing:
                # The slot of a closing session is free only once its thread has really ended.
                self._closing.discard(session)
            self._changed.notify_all()
        for task in failed:
            task.fail(err)

    # -- maintenance ------------------------------------------------------------------

    def sweep(self, now: float | None = None) -> int:
        """Close idle sessions older than their site's ``session_idle_s``, and
        idle sessions whose browser generation is gone (they hold a dead
        Playwright driver). Busy sessions are never swept. Maps are kept."""
        now = self.clock() if now is None else now
        begun = []
        with self._lock:
            for session in list(self._live.values()):
                if session.closing or session.busy:
                    continue
                window = self.windows.get(session.config.window)
                stale = session.generation is not None and session.generation != window.generation
                if stale or now - session.last_used >= session.config.session_idle_s:
                    begun.append((session, *self._begin_close(session, forget=False)))
        err = ServiceError("session_closed", "the session was closed after idling", 409)
        for session, _tasks, interrupt in begun:
            self._interrupt(session, interrupt)
        for session, tasks, _interrupt in begun:
            self._join(session, tasks, err)
        return len(begun)

    # -- the map ----------------------------------------------------------------------

    def list_sessions(self, site: str) -> dict[str, dict]:
        """``{session_id: {url, live, idle_s}}``: every mapped session, plus live
        sessions that have no map entry yet. A live ephemeral session also says
        ``"ephemeral": true`` and shows the URL it holds in memory."""
        config, _spec = self._site(site)
        window = self.windows.get(config.window)
        wall = self._store._wall()
        now = self.clock()
        out: dict[str, dict] = {}
        with self._lock:
            for sid, (url, last_used) in self._store.entries(site).items():
                out[sid] = {"url": url, "live": False, "idle_s": round(max(0.0, wall - last_used), 1)}
            for (name, sid), session in self._live.items():
                if name != site or session.closing:
                    continue
                entry = out.setdefault(sid, {"url": None, "live": False, "idle_s": 0.0})
                if session.ephemeral:
                    entry["url"] = session.mem_url
                    entry["ephemeral"] = True
                entry["live"] = (session.worker is not None
                                 and (session.generation is None or session.generation == window.generation))
                entry["idle_s"] = 0.0 if session.busy else round(max(0.0, now - session.last_used), 1)
        return out

    def forget(self, site: str, session_id: str) -> bool:
        """Drop the map entry and close the session's page. The conversation
        itself stays on the account."""
        self._site(site)
        _check_session_id(session_id)
        with self._lock:
            session = self._live.get((site, session_id))
            known = session is not None or self._store.has(site, session_id)
        if not known:
            raise ServiceError("no_such_session", f"no session {session_id!r} on {site!r}", 404)
        if session is not None:
            self._close_many([session], forget=True, err=ServiceError("session_closed", "the session was forgotten", 409))
        self._store.forget(site, session_id)
        return True
