"""Unattached windows: an HTTP-only owner and temporary per-operation attachments.

A window with ``unattached: true`` is a native Chrome process the service
launches and owns but never attaches to while it idles. Site checks can be
sensitive to CDP attachments; an HTTP-only owner lets the caller load a page
and decide whether it is ready before attaching. This is not a guarantee
that a site's check will pass.

``HttpRuntime``
    The owner of such a window. Metadata, heartbeats, new page and close page
    are DevTools HTTP requests on the loopback debugging port the service
    itself chose (no Playwright instance, no websocket). It has no ``context``,
    ``browser`` or ``pw`` and no shim for them: an owner job that needs
    Playwright cannot run here, it belongs on ``Window.work``.

``TransientLane``
    Same ``submit``/``close`` surface as ``workers.Lane``, but every job gets
    its own thread and its own Playwright attachment, created after the job
    starts and **stopped before the job's future completes**. Nothing is
    retained while idle. A gate (``enter``/``leave``, supplied by ``Window``)
    runs around each attachment so a quiet reservation can refuse new
    attachments and count live ones: ``enter`` is called before the
    attachment is made and may refuse, ``leave`` after it has been stopped.

Not covered: a raw CDP client that connects *after* a quiet reservation began
(the debugging endpoint is published; without a proxy in front of it there
is no way to revoke it). The reservation refuses clients that are already
connected (``HttpRuntime.assert_no_clients``), service workers, and every
later registration through the service.
"""

from __future__ import annotations

import concurrent.futures as cf
import http.client
import itertools
import json
import logging
import threading
import time
from collections import deque
from urllib.parse import quote, urlsplit

from . import profiles
from .errors import ServiceError
from .workers import MAX_QUEUE, Deadlines, Job, no_such_target, stopped

log = logging.getLogger("uc_browser.service.unattached")

HTTP_TIMEOUT_S = 5.0
BEAT_MISSES = 3  # consecutive failed heartbeat requests of a live process before the browser counts as gone
CLIENT_SETTLE_S = 2.0  # how long a just-detached attachment may take to leave the socket table


def _snippet(body: bytes) -> str:
    return body[:200].decode("utf-8", "replace").strip()


class HttpRuntime:
    """What an owner job sees on an unattached window: DevTools HTTP only.

    Methods run on the owner thread; every request is serial there, so no
    request of ours is open while ``assert_no_clients`` reads the socket
    table. ``port`` and ``browser_path`` are the endpoint the launch proved
    (``profiles.launch_native_process``); ``proc`` is the launched process,
    the only authority for "the browser is gone" that needs no request.
    """

    role = "owner"

    def __init__(self, *, window: str, port: int, browser_path: str, proc=None, timeout: float = HTTP_TIMEOUT_S):
        self.window = window
        self.port = port
        self.browser_path = browser_path
        self._proc = proc
        self._timeout = timeout
        self._seen: dict[str, float] = {}  # target id -> monotonic first seen
        self._misses = 0
        self._stopped = False

    @property
    def alive(self) -> bool:
        """Cheap liveness (no request): not stopped and the process still runs."""
        if self._stopped:
            return False
        return self._proc is None or self._proc.poll() is None

    def stop_playwright(self) -> None:
        """There is no driver to stop; marks the runtime finished."""
        self._stopped = True

    # transport
    def _call(self, method: str, path: str) -> tuple[int, bytes]:
        if not self.alive:
            raise stopped(f"window {self.window} browser is not running")
        try:
            return profiles.devtools_request(self.port, path, method=method, timeout=self._timeout)
        except (OSError, http.client.HTTPException) as exc:
            raise ServiceError(
                "devtools_failed",
                f"window {self.window}: DevTools {method} {path.split('?', 1)[0]} failed: "
                f"{type(exc).__name__}: {exc}",
                502,
            ) from exc

    def _json(self, what: str, status: int, body: bytes):
        if status != 200:
            raise ServiceError("devtools_failed", f"window {self.window}: {what} answered HTTP {status}: {_snippet(body)}", 502)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ServiceError("devtools_failed", f"window {self.window}: {what} did not return JSON: {exc}", 502) from exc

    # owner contract
    def new_page(self, url: str = "about:blank") -> str:
        """Open ``url`` in a new tab and return its target id.

        ``/json/new`` takes the text after ``?`` as the URL after one round of
        percent-decoding and requires PUT, so the whole URL is percent-encoded
        (``&``, ``#``, ``%`` and non-ASCII reach Chrome unchanged).
        """
        if not isinstance(url, str) or not url:
            raise ServiceError("bad_arguments", "url must be a non-empty string", 400)
        try:
            query = quote(url, safe="")
        except UnicodeEncodeError as exc:
            raise ServiceError("bad_arguments", f"url is not valid text: {exc}", 400) from exc
        target = self._json("/json/new", *self._call("PUT", f"/json/new?{query}"))
        tid = target.get("id") if isinstance(target, dict) else None
        if not isinstance(tid, str) or not tid:
            raise ServiceError("devtools_failed", f"window {self.window}: /json/new returned no target id", 502)
        self._seen.setdefault(tid, time.monotonic())
        return tid

    def pages_info(self) -> list[dict]:
        """Browser-authoritative page targets ``[{target_id, url, title, age_s}]``.

        An unreachable or malformed endpoint raises ``devtools_failed``; it is
        never reported as "no pages".
        """
        data = self._json("/json/list", *self._call("GET", "/json/list"))
        if not isinstance(data, list):
            raise ServiceError("devtools_failed", f"window {self.window}: /json/list is not a list", 502)
        now = time.monotonic()
        pages: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                raise ServiceError("devtools_failed", f"window {self.window}: malformed /json/list entry", 502)
            if entry.get("type") != "page":
                continue
            tid, url, title = entry.get("id"), entry.get("url"), entry.get("title", "")
            if not isinstance(tid, str) or not tid or not isinstance(url, str) or not isinstance(title, str):
                raise ServiceError("devtools_failed", f"window {self.window}: malformed page target in /json/list", 502)
            pages.append({"target_id": tid, "url": url, "title": title, "age_s": round(now - self._seen.setdefault(tid, now), 1)})
        live = {p["target_id"] for p in pages}
        for tid in [t for t in self._seen if t not in live]:
            del self._seen[tid]
        return pages

    def close_target(self, target_id: str) -> None:
        """Close a page. ``/json/close`` answers plain text; 404 is an unknown target."""
        if not isinstance(target_id, str) or not target_id:
            raise no_such_target(f"window {self.window} has no page {target_id!r}")
        status, body = self._call("GET", f"/json/close/{quote(target_id, safe='')}")
        if status == 404:
            self._seen.pop(target_id, None)
            raise no_such_target(f"window {self.window} has no page {target_id}")
        if status != 200:
            raise ServiceError(
                "close_failed", f"window {self.window}: closing page {target_id} answered HTTP {status}: {_snippet(body)}", 500
            )
        self._seen.pop(target_id, None)

    def heartbeat(self) -> bool:
        """False once the browser is gone: its process exited, another browser
        answers on its port, or the endpoint stayed silent for ``BEAT_MISSES``
        consecutive beats. One slow answer from a busy browser is not death."""
        if not self.alive:
            return False
        try:
            version = self._json("/json/version", *self._call("GET", "/json/version"))
            served = urlsplit(version["webSocketDebuggerUrl"]).path
        except (ServiceError, KeyError, TypeError, ValueError, AttributeError) as exc:
            self._misses += 1
            log.debug("window %s heartbeat miss %d: %s", self.window, self._misses, exc)
            return self._misses < BEAT_MISSES and self.alive
        if served != self.browser_path:
            log.warning("window %s: port %d is answered by a different browser", self.window, self.port)
            return False
        self._misses = 0
        return self.alive

    def assert_no_clients(self, settle_s: float | None = None) -> None:
        """Raise ``busy`` (409) unless no peer is connected to the debugging port.

        Unknown is refused as well: an unreadable socket table cannot prove
        quiet. Connections that are leaving (a worker that just stopped its
        Playwright) get ``settle_s`` to disappear; a client that stays is
        named by pid when known.
        """
        deadline = time.monotonic() + (CLIENT_SETTLE_S if settle_s is None else settle_s)
        while True:
            peers = profiles.established_clients(self.port)
            if peers is None:
                raise ServiceError(
                    "busy",
                    f"window {self.window}: cannot prove that nothing is attached to its debugging port "
                    f"{self.port} on this platform",
                    409,
                )
            if not peers:
                return
            if time.monotonic() >= deadline:
                known = sorted({p for p in peers if p is not None})
                who = f"process {', '.join(map(str, known))}" if known else "an unidentified process"
                raise ServiceError(
                    "busy",
                    f"window {self.window}: {len(peers)} external connection(s) are still attached to its "
                    f"debugging port {self.port} ({who})",
                    409,
                )
            time.sleep(0.05)


class TransientLane:
    """A bounded lane whose workers attach for one job and detach before it completes.

    ``make_runtime()`` runs on the job's own thread and must return a
    ``Runtime``-like object (``stop_playwright``). A job that no slot frees up
    for within its ``queue_timeout`` fails with ``busy`` (503). ``enter`` is
    called after the job started and before ``make_runtime``; raising refuses
    the job, so a refused job never attaches. ``leave`` runs after the
    attachment is stopped, on every path, and only then does the job's future
    complete: cancelling the caller's own future never releases a worker that
    is still attached.
    """

    def __init__(self, name: str, size: int, make_runtime, deadlines: Deadlines, enter, leave, *,
                 max_queue: int = MAX_QUEUE):
        self.name = name
        self.size = size
        self._make_runtime = make_runtime
        self._deadlines = deadlines
        self._enter = enter
        self._leave = leave
        self._max_queue = max_queue
        self._cv = threading.Condition()
        self._queue: deque[Job] = deque()
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._seq = itertools.count(1)

    @property
    def thread_count(self) -> int:
        with self._cv:
            return len(self._threads)

    def submit(self, fn, queue_timeout: float | None = 30.0) -> cf.Future:
        job = Job(lambda _unused: self._attached(fn))
        with self._cv:
            if self._closed:
                job.fail(stopped(f"window {self.name} workers are stopped"))
                return job.future
            if len(self._queue) >= self._max_queue:
                job.fail(ServiceError("busy", f"{self.name} queue is full", 503))
                return job.future
            self._queue.append(job)
            self._spawn_locked()
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
        while self._queue and len(self._threads) < self.size:
            job = self._queue.popleft()
            if job.state != Job.QUEUED:  # expired or cancelled while queued
                continue
            t = threading.Thread(target=self._run, args=(job,), name=f"uc-{self.name}-{next(self._seq)}", daemon=True)
            self._threads.add(t)
            t.start()

    def _run(self, job: Job) -> None:
        me = threading.current_thread()
        try:
            if job.claim():
                job.run(None)  # the wrapper below attaches, runs, detaches; then the future completes
        finally:
            with self._cv:
                self._threads.discard(me)
                if not self._closed:
                    self._spawn_locked()

    def _attached(self, fn):
        with self._cv:
            if self._closed:
                raise stopped(f"window {self.name} workers are stopped")
        self._enter()
        try:
            try:
                runtime = self._make_runtime()
            except ServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise stopped(f"cannot attach a {self.name} worker: {exc}") from exc
            try:
                return fn(runtime)
            finally:
                try:
                    runtime.stop_playwright()
                except Exception as exc:  # noqa: BLE001
                    log.warning("worker of %s could not stop its Playwright: %s", self.name, exc)
        finally:
            self._leave()

    def close(self, join_timeout: float = 10.0) -> list[threading.Thread]:
        """Fail queued jobs, join running workers (bounded). Returns workers
        still alive after ``join_timeout`` (stuck in a call the closed browser
        did not interrupt); they are daemon threads, and each still holds its
        slot in the window's attachment count until it really finishes."""
        with self._cv:
            self._closed = True
            jobs = list(self._queue)
            self._queue.clear()
            threads = list(self._threads)
        err = stopped(f"window {self.name} is stopping")
        for job in jobs:
            job.fail(err)
        deadline = time.monotonic() + join_timeout
        me = threading.current_thread()
        for t in threads:
            if t is not me:
                t.join(max(0.0, deadline - time.monotonic()))
        return [t for t in threads if t.is_alive()]
