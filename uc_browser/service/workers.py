"""Threads and per-thread Playwright runtimes for one window.

A *runtime* is one ``(thread, sync Playwright, browser attachment, context)``.
Playwright objects never cross threads: every runtime method checks that it is
called on the thread that created it and raises ``RuntimeError`` otherwise, so
a closure that leaks a runtime to another thread fails loudly.

Contents
--------
``Job``        a unit of work with a ``concurrent.futures.Future``; it can be
               claimed for execution or failed (cancel/expiry/shutdown), never
               both.
``JobQueue``   the owner thread's bounded queue.
``Deadlines``  one thread per window that expires queued jobs (``busy``).
``Runtime``    target identification, ``resolve``, engine placement, control
               receiver, heartbeat.
``Lane``       a bounded, lazily grown pool of worker threads; each worker owns
               an independent Playwright and CDP attachment.
"""

from __future__ import annotations

import concurrent.futures as cf
import heapq
import itertools
import logging
import threading
import time
import uuid
from collections import deque
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from uc_browser import _paths

from . import profiles
from .errors import ServiceError

log = logging.getLogger("uc_browser.service.workers")

OWNER_HEARTBEAT_S = 2.0
WORKER_HEARTBEAT_S = 5.0
WORKER_IDLE_RETIRE_S = 120.0
MAX_QUEUE = 256
TARGET_WAIT_S = 10.0
ENGINE_WAIT_MS = 5000

_READY_JS = "window.__UC && window.__UC.ready === true"
_PUMP_URL = "http://127.0.0.1:1/"  # cookie lookup that matches nothing: a cheap round trip


# ── errors ────────────────────────────────────────────────────────────────


def stopped(detail: str) -> ServiceError:
    """A job could not run because the window's browser is stopped/closing."""
    return ServiceError("window_stopped", detail, 503)


def no_such_target(detail: str) -> ServiceError:
    return ServiceError("no_such_target", detail, 404)


def is_closed_error(exc: BaseException) -> bool:
    """Does ``exc`` mean the page/context/browser is gone?"""
    try:
        from playwright._impl._errors import TargetClosedError

        if isinstance(exc, TargetClosedError):
            return True
    except ImportError:  # pragma: no cover
        pass
    if not isinstance(exc, PlaywrightError):
        return False
    msg = str(exc).lower()
    return any(s in msg for s in ("has been closed", "target closed", "page closed", "connection closed"))


@lru_cache(maxsize=1)
def engine_bundle_path() -> Path:
    return _paths.ext_dir() / "dist" / "uc-extension.js"


@lru_cache(maxsize=1)
def engine_bundle() -> str:
    return engine_bundle_path().read_text(encoding="utf-8")


# ── jobs ──────────────────────────────────────────────────────────────────


class Job:
    """Work plus its future. ``claim`` and ``fail`` are mutually exclusive."""

    QUEUED, RUNNING, DONE = 0, 1, 2

    def __init__(self, fn):
        self.fn = fn
        self.future: cf.Future = cf.Future()
        self.state = Job.QUEUED
        self._lock = threading.Lock()

    def claim(self) -> bool:
        """Mark the job running. False if it already failed/expired or its
        caller cancelled it while it was queued."""
        with self._lock:
            if self.state != Job.QUEUED:
                return False
            if not self.future.set_running_or_notify_cancel():
                self.state = Job.DONE
                return False
            self.state = Job.RUNNING
            return True

    def fail(self, exc: BaseException) -> bool:
        """Fail a still-queued job. False if it was already claimed/failed."""
        with self._lock:
            if self.state != Job.QUEUED:
                return False
            self.state = Job.DONE
        try:
            self.future.set_exception(exc)
        except cf.InvalidStateError:  # caller cancelled meanwhile
            pass
        return True

    def run(self, runtime) -> BaseException | None:
        """Run a claimed job; the outcome goes to the future. Returns the
        raised exception (if any) so the thread can check its attachment."""
        try:
            result = self.fn(runtime)
        except BaseException as exc:  # noqa: BLE001 - delivered through the future
            error: BaseException | None = exc
            self.state = Job.DONE
            self.future.set_exception(exc)
            return error
        self.state = Job.DONE
        self.future.set_result(result)
        return None


class JobQueue:
    """Bounded FIFO with a timed ``get`` that ``wake`` can interrupt."""

    def __init__(self, maxsize: int = MAX_QUEUE):
        self._items: deque[Job] = deque()
        self._cv = threading.Condition()
        self._max = maxsize
        self._woken = False

    def put(self, job: Job) -> None:
        with self._cv:
            if len(self._items) >= self._max:
                raise ServiceError("busy", "window owner queue is full", 503)
            self._items.append(job)
            self._cv.notify()

    def get(self, timeout: float) -> Job | None:
        with self._cv:
            if not self._items and not self._woken:
                self._cv.wait(timeout)
            self._woken = False
            return self._items.popleft() if self._items else None

    def wake(self) -> None:
        with self._cv:
            self._woken = True
            self._cv.notify_all()

    def drain(self) -> list[Job]:
        with self._cv:
            items = list(self._items)
            self._items.clear()
            return items


class Deadlines:
    """One daemon thread (started on first use) running callbacks at deadlines."""

    def __init__(self, name: str):
        self._name = name
        self._cv = threading.Condition()
        self._heap: list = []
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._closed = False

    def add(self, when: float, callback) -> None:
        with self._cv:
            if self._closed:
                return
            heapq.heappush(self._heap, (when, next(self._seq), callback))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"uc-deadlines-{self._name}", daemon=True)
                self._thread.start()
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._closed:
                    if not self._heap:
                        self._cv.wait()
                        continue
                    delay = self._heap[0][0] - time.monotonic()
                    if delay > 0:
                        self._cv.wait(delay)
                        continue
                    break
                if self._closed:
                    return
                _, _, callback = heapq.heappop(self._heap)
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("deadline callback failed")

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._heap.clear()
            self._cv.notify_all()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(2)


# ── runtime ───────────────────────────────────────────────────────────────


class Runtime:
    """What a job sees: this thread's Playwright, attachment and context.

    ``context``/``browser``/``pw`` are the caller thread's own objects.
    ``target_id(page)`` and ``resolve(target_id)`` translate between the
    window's CDP target ids and this runtime's ``Page`` objects. On windows
    without CDP (Firefox, WebKit) ids are generated by the service and exist
    on the owner runtime only.
    """

    def __init__(self, *, window: str, engine: str, kind: str, pw, browser, context, cdp: str | None, role: str):
        self._tid = threading.get_ident()
        self.window = window
        self.engine = engine
        self.kind = kind
        self.cdp = cdp
        self.role = role
        self._port = urlsplit(cdp).port if cdp else None
        self._pw = pw
        self._browser = browser
        self._context = context
        self._ids: dict = {}  # Page -> target id
        self._seen: dict[str, float] = {}  # target id -> monotonic first seen
        self._target_session = None  # browser-level CDP; owned by this runtime
        self._receiver = None
        self._closed = False
        self._fetch_bindings: dict = {}  # per-page state held by operations (owner/worker local)
        context.on("close", self._on_close)
        if browser is not None:
            browser.on("disconnected", self._on_close)

    # thread discipline
    def _own(self) -> None:
        if threading.get_ident() != self._tid:
            raise RuntimeError(
                f"Playwright objects of window {self.window!r} ({self.role}) used from a thread that does not own them"
            )

    def _on_close(self, *_args) -> None:
        self._closed = True

    @property
    def pw(self):
        self._own()
        return self._pw

    @property
    def browser(self):
        self._own()
        return self._browser

    @property
    def context(self):
        self._own()
        return self._context

    @property
    def alive(self) -> bool:
        """Cheap liveness (no Playwright call): not closed, still connected."""
        if self._closed:
            return False
        b = self._browser
        return b is None or b.is_connected()

    # event pump
    def pump(self, ms: int = 0) -> None:
        """Round-trip a call so queued Playwright events are dispatched.

        Sync Playwright dispatches incoming messages only inside a call; an
        attachment that made none keeps stale ``context.pages``. Raises if the
        context is gone.
        """
        self._own()
        ctx = self._context
        for page in list(ctx.pages):
            if page.is_closed():
                continue
            try:
                page.wait_for_timeout(ms)
                return
            except PlaywrightError:
                continue
        if ms:
            time.sleep(ms / 1000)
        ctx.cookies(_PUMP_URL)

    def heartbeat(self) -> bool:
        """Pump once; False if the attachment is dead."""
        self._own()
        if not self.alive:
            return False
        try:
            self.pump(0)
        except Exception:  # noqa: BLE001 - any failure here means "not alive"
            return False
        return self.alive

    # targets
    def _identify(self, page) -> str | None:
        if self._port is None:
            tid = "svc-" + uuid.uuid4().hex[:24]
        else:
            try:
                session = self._context.new_cdp_session(page)
            except PlaywrightError:
                return None  # closed while we looked
            try:
                tid = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            except PlaywrightError:
                return None
            finally:
                try:
                    session.detach()
                except PlaywrightError:
                    pass
        self._ids[page] = tid
        self._seen.setdefault(tid, time.monotonic())
        return tid

    def scan(self) -> dict:
        """``{target_id: Page}`` for every open page, after pumping events."""
        self._own()
        self.pump(0)
        pages = [p for p in list(self._context.pages) if not p.is_closed()]
        live: dict = {}
        for page in pages:
            tid = self._ids.get(page)
            if tid is None:
                tid = self._identify(page)
                if tid is None:
                    continue
            live[tid] = page
        current = set(pages)
        for page in [p for p in self._ids if p not in current]:
            self._seen.pop(self._ids.pop(page), None)
        return live

    def target_id(self, page) -> str:
        """The target id of ``page`` (a page of this runtime's context)."""
        self.scan()
        tid = self._ids.get(page)
        if tid is None:
            raise no_such_target("page is closed or not part of this window")
        return tid

    def _cdp_targets(self) -> list[dict]:
        self._own()
        if self._target_session is None:
            self._target_session = self._browser.new_browser_cdp_session()
        return [t for t in self._target_session.send("Target.getTargets")["targetInfos"]
                if t["type"] == "page"]

    def pages_info(self) -> list[dict]:
        """Browser-authoritative page targets; non-CDP uses the owner Pages."""
        self._own()
        now = time.monotonic()
        if self.cdp is None:
            return [
                {"target_id": tid, "url": page.url, "age_s": round(now - self._seen.get(tid, now), 1)}
                for tid, page in self.scan().items()
            ]
        targets = self._cdp_targets()
        live = {t["targetId"] for t in targets}
        for tid in list(self._seen):
            if tid not in live:
                del self._seen[tid]
        return [
            {"target_id": t["targetId"], "url": t["url"],
             "age_s": round(now - self._seen.setdefault(t["targetId"], now), 1)}
            for t in targets
        ]

    def close_target(self, target_id: str) -> None:
        """Close a page without waiting for its Page object to reach the owner."""
        self._own()
        if self.cdp is None:
            self.resolve(target_id).close()
            return
        if not any(t["targetId"] == target_id for t in self._cdp_targets()):
            raise no_such_target(f"window {self.window} has no page {target_id}")
        try:
            result = self._target_session.send("Target.closeTarget", {"targetId": target_id})
        except PlaywrightError:
            # It may have closed between enumeration and the close command.
            # Transport/enumeration failure is not evidence of target absence.
            if not any(t["targetId"] == target_id for t in self._cdp_targets()):
                raise no_such_target(f"page {target_id} already closed") from None
            raise
        if not result["success"]:
            raise ServiceError("close_failed", f"browser refused to close page {target_id}", 500)

    def _browser_has(self, target_id: str) -> bool | None:
        """Does the browser itself still have this page target? None: unknown."""
        if self._port is None:
            return False  # non-CDP ids exist only in our own scan
        ids = profiles.list_page_targets(self._port)
        return None if ids is None else target_id in ids

    def resolve(self, target_id: str, timeout: float = TARGET_WAIT_S):
        """The ``Page`` with this target id on this attachment.

        A CDP target created through another attachment can take a moment to
        appear here, so a target the *browser* still lists is awaited up to
        ``timeout``. One the browser does not list (never existed, or closed)
        is ``no_such_target`` at once.
        """
        self._own()
        deadline = time.monotonic() + timeout
        while True:
            page = self.scan().get(target_id)
            if page is not None:
                return page
            known = self._browser_has(target_id)
            if known is False:
                raise no_such_target(f"window {self.window} has no page {target_id}")
            if time.monotonic() >= deadline:
                raise no_such_target(f"page {target_id} is not visible on this attachment after {timeout:.0f}s")
            self.pump(25)

    # engine
    def ensure_engine(self, page, timeout_ms: int = ENGINE_WAIT_MS) -> bool:
        """Place/await the UC engine on ``page``. True if it is ready.

        ``extension``: the extension content script loads on http(s) pages at
        ``document_idle``; wait for it there. Pages it never runs on (blank,
        ``chrome-error:``, ``chrome:``, ...) are left alone and *not* waited on:
        the control then runs its own absent-engine path once, with its own
        wait. ``inject``: evaluate the bundle if ``window.__UC`` is absent
        (every call: navigation discards it), then wait. ``none``: nothing.
        """
        self._own()
        if self.engine == "none":
            return False
        try:
            if self._engine_ready(page):
                return True
            if self.engine == "extension":
                if urlsplit(page.url).scheme not in ("http", "https"):
                    return False
            elif not page.evaluate("typeof window.__UC !== 'undefined'"):
                page.evaluate(engine_bundle())
            page.wait_for_function(_READY_JS, timeout=timeout_ms)
            return True
        except PlaywrightError as exc:
            if is_closed_error(exc) or page.is_closed():
                raise no_such_target("page closed while preparing the engine") from exc
            if self.engine == "inject" and "Timeout" not in type(exc).__name__:
                raise  # injection itself failed: surface it (control_failed), not 409
            return False

    def _engine_ready(self, page) -> bool:
        js = "!!(window.__UC && window.__UC.ready === true)"
        try:
            return bool(page.evaluate(js))
        except PlaywrightError as exc:
            if is_closed_error(exc) or page.is_closed():
                raise
            page.wait_for_timeout(200)  # navigation in flight: one retry
            return bool(page.evaluate(js))

    def receiver(self):
        """This thread's pristine in-process ``UCBrowser`` (never started)."""
        self._own()
        if self._receiver is None:
            from uc_browser.browser import BrowserMode, UCBrowser

            self._receiver = UCBrowser(mode=BrowserMode.CHROMIUM_EXT)
        return self._receiver

    def stop_playwright(self) -> None:
        """Stop this thread's Playwright driver (never closes the browser)."""
        self._own()
        self._closed = True
        try:
            self._pw.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("playwright stop failed in %s/%s: %s", self.window, self.role, exc)


# ── lanes ─────────────────────────────────────────────────────────────────


class Lane:
    """A bounded pool of worker threads, grown on demand.

    ``make_runtime()`` runs on the new worker thread and must return a
    ``Runtime``-like object (``heartbeat``, ``alive``, ``stop_playwright``). A
    job that no worker frees up for within its ``queue_timeout`` fails with
    ``busy`` (503). Idle workers beyond ``keep`` retire after
    ``WORKER_IDLE_RETIRE_S`` (each holds a Playwright driver, ~80 MB).
    """

    def __init__(self, name: str, size: int, make_runtime, deadlines: Deadlines, *, keep: int = 0,
                 max_queue: int = MAX_QUEUE, heartbeat_s: float = WORKER_HEARTBEAT_S,
                 idle_retire_s: float = WORKER_IDLE_RETIRE_S):
        self.name = name
        self.size = size
        self._make_runtime = make_runtime
        self._deadlines = deadlines
        self._keep = keep
        self._max_queue = max_queue
        self._heartbeat_s = heartbeat_s
        self._idle_retire_s = idle_retire_s
        self._cv = threading.Condition()
        self._queue: deque[Job] = deque()
        self._threads: set[threading.Thread] = set()
        self._idle = 0
        self._closed = False
        self._seq = itertools.count(1)

    @property
    def thread_count(self) -> int:
        with self._cv:
            return len(self._threads)

    def submit(self, fn, queue_timeout: float | None = 30.0) -> cf.Future:
        job = Job(fn)
        with self._cv:
            if self._closed:
                job.fail(stopped(f"window {self.name} workers are stopped"))
                return job.future
            if len(self._queue) >= self._max_queue:
                job.fail(ServiceError("busy", f"{self.name} queue is full", 503))
                return job.future
            self._queue.append(job)
            if self._idle < len(self._queue) and len(self._threads) < self.size:
                self._spawn_locked()
            self._cv.notify()
        if queue_timeout is not None:
            self._deadlines.add(time.monotonic() + queue_timeout, lambda: self._expire(job, queue_timeout))
        return job.future

    def _expire(self, job: Job, waited: float) -> None:
        err = ServiceError("busy", f"no {self.name} worker became free within {waited:.0f}s", 503)
        with self._cv:
            if job.fail(err):
                try:
                    self._queue.remove(job)
                except ValueError:
                    pass

    def _spawn_locked(self) -> None:
        t = threading.Thread(target=self._worker_main, name=f"uc-{self.name}-{next(self._seq)}", daemon=True)
        self._threads.add(t)
        t.start()

    def _worker_main(self) -> None:
        me = threading.current_thread()
        runtime = None
        failure: BaseException | None = None
        try:
            try:
                runtime = self._make_runtime()
            except BaseException as exc:  # noqa: BLE001
                failure = exc
                return
            idle_since = time.monotonic()
            while True:
                job = None
                with self._cv:
                    if self._closed:
                        break
                    if self._queue:
                        job = self._queue.popleft()
                    else:
                        self._idle += 1
                        try:
                            self._cv.wait(self._heartbeat_s)
                        finally:
                            self._idle -= 1
                        if self._closed:
                            break
                        job = self._queue.popleft() if self._queue else None
                if job is None:
                    if not runtime.heartbeat():
                        break  # attachment died; a later job starts a fresh worker
                    if time.monotonic() - idle_since >= self._idle_retire_s:
                        with self._cv:
                            if len(self._threads) > self._keep:
                                break
                    continue
                if job.claim():
                    job.run(runtime)
                    if not runtime.alive:
                        break
                idle_since = time.monotonic()
        finally:
            self._retire(me, runtime, failure)

    def _retire(self, me: threading.Thread, runtime, failure: BaseException | None) -> None:
        if runtime is not None:
            try:
                runtime.stop_playwright()
            except Exception as exc:  # noqa: BLE001
                log.debug("worker %s cleanup failed: %s", me.name, exc)
        orphaned: list[Job] = []
        with self._cv:
            self._threads.discard(me)
            if failure is not None and not self._threads:
                orphaned = list(self._queue)
                self._queue.clear()
            elif (not self._closed and self._queue and failure is None
                  and len(self._threads) < min(self.size, len(self._queue))):
                self._spawn_locked()  # a worker exited with work still queued
        if failure is not None:
            log.warning("worker %s could not attach: %s", me.name, failure)
            err = stopped(f"cannot attach a {self.name} worker: {failure}")
            for job in orphaned:
                job.fail(err)

    def close(self, join_timeout: float = 10.0) -> list[threading.Thread]:
        """Fail queued jobs, tell workers to stop, join them (bounded).

        Returns workers still alive after ``join_timeout`` (stuck in a call
        the closed browser did not interrupt); they are daemon threads.
        """
        with self._cv:
            self._closed = True
            jobs = list(self._queue)
            self._queue.clear()
            threads = list(self._threads)
            self._cv.notify_all()
        err = stopped(f"window {self.name} is stopping")
        for job in jobs:
            job.fail(err)
        deadline = time.monotonic() + join_timeout
        me = threading.current_thread()
        for t in threads:
            if t is not me:
                t.join(max(0.0, deadline - time.monotonic()))
        return [t for t in threads if t.is_alive()]
