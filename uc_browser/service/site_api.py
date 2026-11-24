"""HTTP routes of the site sessions (Build Spec §3, "Sites").

``POST /sites/{site}/{op}``     the body is the operation's keyword arguments plus
                                 the request metadata ``session_id`` (default
                                 ``"default"``; absent for ``stop`` means "find
                                 the page by ``conversation_url``"), ``stream``,
                                 ``wait`` (default true) and ``ephemeral``.
                                 Metadata never reaches the adapter.
``GET /sites/{site}/sessions``   ``{session_id: {url, live, idle_s}}``
``DELETE /sites/{site}/sessions/{id}``  ``{"forgotten": true}``

Unstreamed: ``{"result": ...}``. Streamed (``stream: true``): newline-delimited
JSON, ``{"delta": text}`` lines, then exactly one ``{"result": ...}`` or
``{"error": code, "detail": text}`` line. Everything that needs no page (unknown
site or operation, bad arguments, ``busy``, ``session_conflict``) is a normal
HTTP error, also for streams.

Handlers never touch a browser: every manager call runs in a thread, and the
operation itself runs on the session's worker.

Client disconnects
------------------
ASGI servers do not cancel a handler when its client leaves, so the handler
watches the connection itself (``Request.is_disconnected``, a non-blocking
poll) while it waits for admission and, unstreamed, for the result. A caller
that has left cancels what has not started (a queued operation, one waiting for
the account's send permit); an operation that has entered the adapter is never
cancelled or repeated: it completes, its map entry is saved, and its result is
dropped. A streamed response is watched by ``StreamingResponse`` once it starts
(the poll is stopped before the response is returned, so the two never compete
for ``receive``); there the remaining deltas are dropped the same way.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .errors import ServiceError

log = logging.getLogger("uc_browser.service.site_api")

DISCONNECT_POLL_S = 0.25


class _ClientGone(Exception):
    """The caller disconnected before its operation answered."""


def _gone_response() -> JSONResponse:
    # Nobody is listening; the body only documents what happened.
    return JSONResponse(ServiceError("cancelled", "the caller disconnected", 409).as_dict(), status_code=409)


async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ServiceError("bad_arguments", "request body is not JSON", 400) from exc
    if not isinstance(body, dict):
        raise ServiceError("bad_arguments", "request body must be a JSON object", 400)
    return body


def _flag(body: dict, name: str, default: bool) -> bool:
    value = body.pop(name, default)
    if not isinstance(value, bool):
        raise ServiceError("bad_arguments", f"{name} must be a boolean", 400)
    return value


async def _disconnected(request: Request) -> None:
    try:
        while not await request.is_disconnected():
            await asyncio.sleep(DISCONNECT_POLL_S)
    except Exception:  # noqa: BLE001
        # A failing poll is no evidence the caller left: stay quiet until cancelled.
        await asyncio.Event().wait()


async def _race(request: Request, work: asyncio.Future):
    """Await ``work`` unless the client disconnects first (``_ClientGone``).
    ``work`` itself is left as it is; the watcher never outlives this call."""
    watcher = asyncio.ensure_future(_disconnected(request))
    try:
        done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        raise _ClientGone
    finally:
        watcher.cancel()


def _abandon_late(done: asyncio.Future) -> None:
    if not done.cancelled() and done.exception() is None:
        done.result().abandon()


async def _admit(request: Request, sessions, *args, **kwargs):
    """``sessions.submit`` in a thread. If the caller leaves while admission is
    still pending, whatever gets queued afterwards is abandoned."""
    pending = asyncio.ensure_future(asyncio.to_thread(sessions.submit, *args, **kwargs))
    try:
        return await _race(request, pending)
    except (_ClientGone, asyncio.CancelledError):
        pending.add_done_callback(_abandon_late)
        raise


async def run_unstreamed(request: Request, sessions, site: str, op: str, body: dict, session_id,
                         wait: bool, ephemeral: bool):
    """The result of one operation. Raises ``ServiceError`` as the operation or
    admission failed, ``_ClientGone`` if the caller left first."""
    call = await _admit(request, sessions, site, op, body, session_id, wait=wait, ephemeral=ephemeral)
    waiter = asyncio.wrap_future(call.future)
    try:
        return await _race(request, waiter)
    except (_ClientGone, asyncio.CancelledError):
        log.info("caller left; a queued operation is cancelled, one already running completes")
        call.abandon()  # cancels only what has not started
        waiter.cancel()
        raise


class StreamSink:
    """Bridge from the session worker thread to one NDJSON response.

    ``on_delta`` is called on the worker; items cross to the event loop with
    ``call_soon_threadsafe``, so no thread ever blocks on a slow or vanished
    consumer. After ``abandon`` deltas are dropped; the operation itself is not
    touched unless it has not started.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._lock = threading.Lock()
        self._abandoned = False
        self._call = None

    def _put(self, item) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # the loop is closed: nobody is listening
            pass

    def on_delta(self, text) -> None:
        if self._abandoned:
            return
        self._put({"delta": text if isinstance(text, str) else str(text)})

    def attach(self, call) -> None:
        with self._lock:
            self._call = call
            abandoned = self._abandoned
        if abandoned:
            call.abandon()
            return
        call.future.add_done_callback(self._finished)

    def _finished(self, future) -> None:
        if future.cancelled():
            item = ServiceError("cancelled", "the operation was cancelled before it started", 409).as_dict()
        else:
            exc = future.exception()
            if exc is None:
                item = {"result": future.result()}
            elif isinstance(exc, ServiceError):
                item = exc.as_dict()
            else:
                item = {"error": "site_failed", "detail": type(exc).__name__}
        self._put(item)
        self._put(None)

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            call = self._call
        if call is not None and not call.future.done():
            log.info("stream consumer left; a running operation completes, a queued one is cancelled")
            call.abandon()

    async def lines(self):
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield json.dumps(item, allow_nan=False) + "\n"
        finally:
            self.abandon()


class _SessionStreamResponse(StreamingResponse):
    """Release queued work even if disconnect precedes the first body yield."""

    def __init__(self, sink: StreamSink):
        super().__init__(sink.lines(), media_type="application/x-ndjson")
        self._sink = sink

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._sink.abandon()


def create_site_router(sessions) -> APIRouter:
    router = APIRouter()

    @router.get("/sites/{site}/sessions")
    async def list_sessions(site: str):
        return await asyncio.to_thread(sessions.list_sessions, site)

    @router.delete("/sites/{site}/sessions/{session_id}")
    async def forget_session(site: str, session_id: str):
        await asyncio.to_thread(sessions.forget, site, session_id)
        return {"forgotten": True}

    @router.post("/sites/{site}/{op}")
    async def operation(site: str, op: str, request: Request):
        body = await _body(request)
        session_id = body.pop("session_id", None)
        stream = _flag(body, "stream", False)
        wait = _flag(body, "wait", True)
        ephemeral = _flag(body, "ephemeral", False)
        if not stream:
            try:
                result = await run_unstreamed(request, sessions, site, op, body, session_id, wait, ephemeral)
            except _ClientGone:
                return _gone_response()
            return {"result": result}
        sink = StreamSink(asyncio.get_running_loop())
        try:
            call = await _admit(request, sessions, site, op, body, session_id, wait=wait, ephemeral=ephemeral,
                                on_delta=sink.on_delta)
        except _ClientGone:
            return _gone_response()
        sink.attach(call)
        return _SessionStreamResponse(sink)

    return router
