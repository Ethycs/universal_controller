"""Quiet (unattached) client wire behavior: API routes, client helper, operations.

No browser: windows, leases and operations are recording fakes, so these pin
only what the HTTP layer forwards and which executor runs each job.
"""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from uc_browser.service.api import create_router
from uc_browser.service.client import ServiceClient
from uc_browser.service.errors import ServiceError
from uc_browser.service.operations import Operations


def _done(value=None, exc=None):
    future = Future()
    if exc is not None:
        future.set_exception(exc)
    else:
        future.set_result(value)
    return future


class Window:
    def __init__(self, unattached=False, pages=None):
        self.spec = SimpleNamespace(name="native", browser="chrome", unattached=unattached)
        self.running = True
        self.generation = 1
        self.pages = pages if pages is not None else []
        self.calls = []
        self.rt = SimpleNamespace(
            pages_info=lambda: list(self.pages),
            context=SimpleNamespace(cookies=lambda: [
                {"name": "n", "value": "v", "domain": ".example.test", "path": "/"}],
                add_cookies=lambda cookies: None),
        )

    def _run(self, kind, fn, **kwargs):
        self.calls.append((kind, kwargs))
        try:
            return _done(fn(self.rt))
        except Exception as exc:
            return _done(exc=exc)

    def owner(self, fn, *, launch=True):
        return self._run("owner", fn, launch=launch)

    def work(self, fn, **kwargs):
        return self._run("work", fn, **kwargs)


class Manager:
    def __init__(self, window):
        self.window = window
        self.windows = {"native": window}

    def get(self, name):
        if name != "native":
            raise ServiceError("no_such_window", "unknown", 404)
        return self.window


class Leases:
    def __init__(self, quiet=()):
        self.quiet = set(quiet)
        self.calls = []
        self.error = None

    def register(self, *args, **kwargs):
        self.calls.append(("register", args, kwargs))
        if self.error:
            raise self.error
        return "c1"

    def is_unattached_client(self, name, client_id):
        return client_id in self.quiet

    def create_page(self, *args, **kwargs):
        self.calls.append(("create_page", args, kwargs))
        if self.error:
            raise self.error
        return "T1"

    def close_page(self, name, target):
        self.calls.append(("close_page", (name, target), {}))
        return True


class Ops:
    def __init__(self):
        self.calls = []

    def navigate(self, *args):
        self.calls.append(args)


@pytest.fixture
def rig():
    window = Window(unattached=True, pages=[
        {"target_id": "T1", "url": "https://x.test/#a&b=%20", "title": "Hello", "age_s": 1.5}])
    leases, ops = Leases(quiet={"quiet"}), Ops()
    app = FastAPI()
    app.include_router(create_router(Manager(window), leases, ops))

    @app.exception_handler(ServiceError)
    async def handle(request, exc):
        return JSONResponse(exc.as_dict(), status_code=exc.status)

    with TestClient(app) as http:
        yield SimpleNamespace(http=http, window=window, leases=leases, ops=ops)


def test_register_forwards_unattached_only_when_requested(rig):
    assert rig.http.post("/clients", json={"pid": 5, "label": "l", "window": "native"}).json() == {"client_id": "c1"}
    assert rig.leases.calls[-1] == ("register", (5, "l", "native"), {})
    rig.http.post("/clients", json={"pid": 5, "label": "l", "window": "native", "unattached": True})
    assert rig.leases.calls[-1] == ("register", (5, "l", "native"), {"unattached": True})


def test_register_propagates_busy_refusal(rig):
    rig.leases.error = ServiceError("busy", "other clients are registered", 409)
    reply = rig.http.post("/clients", json={"pid": 5, "window": "native", "unattached": True})
    assert (reply.status_code, reply.json()["error"]) == (409, "busy")


def test_quiet_client_page_is_created_without_navigation(rig):
    reply = rig.http.post("/windows/native/pages", json={"client_id": "quiet", "url": "https://x.test/?a=1&b=2#f"})
    assert reply.json() == {"target_id": "T1"}
    assert rig.leases.calls == [("create_page", ("native", "quiet"), {"url": "https://x.test/?a=1&b=2#f"})]
    assert rig.ops.calls == []
    rig.http.post("/windows/native/pages", json={"client_id": "quiet"})
    assert rig.leases.calls[-1] == ("create_page", ("native", "quiet"), {"url": None})


def test_quiet_client_rejects_wait_before_creating_anything(rig):
    reply = rig.http.post("/windows/native/pages", json={"client_id": "quiet", "url": "https://x.test", "wait_ms": 5})
    assert (reply.status_code, reply.json()["error"]) == (400, "wait_unsupported")
    assert rig.leases.calls == []
    assert rig.ops.calls == []


def test_quiet_page_creation_error_propagates(rig):
    rig.leases.error = ServiceError("window_stopped", "DevTools unreachable", 503)
    reply = rig.http.post("/windows/native/pages", json={"client_id": "quiet", "url": "https://x.test"})
    assert (reply.status_code, reply.json()["error"]) == (503, "window_stopped")
    assert rig.ops.calls == []


def test_ordinary_client_page_shape_is_unchanged(rig):
    reply = rig.http.post("/windows/native/pages", json={"client_id": "other", "url": "https://x.test", "wait_ms": 7})
    assert reply.json() == {"target_id": "T1"}
    assert rig.leases.calls == [("create_page", ("native", "other"), {})]
    assert rig.ops.calls == [("native", "T1", "https://x.test", 7)]


def test_page_info_reads_owner_metadata_without_launching(rig):
    reply = rig.http.get("/windows/native/pages/T1")
    assert reply.json() == {"target_id": "T1", "url": "https://x.test/#a&b=%20", "title": "Hello", "age_s": 1.5}
    assert rig.window.calls == [("owner", {"launch": False})]


def test_page_info_distinguishes_missing_target_from_metadata_failure(rig):
    reply = rig.http.get("/windows/native/pages/nope")
    assert (reply.status_code, reply.json()["error"]) == (404, "no_such_target")

    def broken():
        raise ServiceError("devtools_unavailable", "metadata failed", 503)

    rig.window.rt.pages_info = broken
    reply = rig.http.get("/windows/native/pages/T1")
    assert (reply.status_code, reply.json()["error"]) == (503, "devtools_unavailable")
    assert rig.http.get("/windows/missing/pages/T1").status_code == 404


def test_client_wire_shapes(monkeypatch):
    client = ServiceClient("http://127.0.0.1:9")
    seen = []

    def request(method, path, body=None, **kwargs):
        seen.append((method, path, body))
        return {"client_id": "c1"}

    monkeypatch.setattr(client, "request", request)
    client.register_client("native", "job", pid=3)
    client.register_client("native", "job", pid=3, unattached=False)
    client.register_client("native", "job", pid=3, unattached=True)
    client.page_info("native", "T 1/x")
    assert seen == [
        ("POST", "/clients", {"pid": 3, "window": "native", "label": "job"}),
        ("POST", "/clients", {"pid": 3, "window": "native", "label": "job"}),
        ("POST", "/clients", {"pid": 3, "window": "native", "label": "job", "unattached": True}),
        ("GET", "/windows/native/pages/T%201%2Fx", None),
    ]


@pytest.mark.parametrize("flagged,kind", [(True, "work"), (False, "owner")])
def test_cookie_operations_use_work_only_for_unattached_windows(flagged, kind):
    window = Window(unattached=flagged)
    ops = Operations(Manager(window), Leases())
    assert ops.add_cookies("native", cookies=[{"name": "n"}]) == {"added": 1}
    assert [c["name"] for c in ops.cookies("native", "app.example.test")["cookies"]] == ["n"]
    assert [c[0] for c in window.calls] == [kind, kind]


def test_stream_cancel_interrupts_through_work_on_unattached_window():
    window = Window(unattached=True)
    ops = Operations(Manager(window), Leases())
    pending = Future()
    pending.set_running_or_notify_cancel()  # a fetch already executing cannot be cancelled
    entry = SimpleNamespace(target="T1")
    window.rt.resolve = lambda target: SimpleNamespace(evaluate=lambda *a: None)
    ops._fetch_job = lambda name, request, **kw: (window, entry, "tok", pending)
    _, cancel, _ = ops.stream("native", {"url": "https://x.test/"})
    cancel()
    assert [c[0] for c in window.calls] == ["work"]
