"""Client side of the UC service: ``ServiceClient`` (pure HTTP), the target
resolver, and ``UCBrowser`` in service mode.

No browser anywhere: the service is a loopback fixture speaking wire v0 and
Playwright is replaced by small fakes whose error classes are the real
``playwright.sync_api.Error`` / ``TimeoutError``.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from uc_browser import browser as browser_mod
from uc_browser.browser import CONTROL_NAMES, BrowserMode, UCBrowser
from uc_browser.service.client import (
    ControlsUnavailable,
    ServiceClient,
    ServiceError,
    UCServiceGone,
)
from uc_browser.service.targets import TargetNotFound, TargetResolver

REPO = Path(__file__).resolve().parents[1]


# ── loopback fixture service ─────────────────────────────────────────────


class Ndjson:
    def __init__(self, lines, gate: threading.Event | None = None):
        self.lines = lines
        self.gate = gate


class Raw:
    def __init__(self, data: bytes):
        self.data = data


class Drop:
    """Close the connection without answering."""


class FakeService:
    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.routes: list = []
        self.controls: dict = {}
        svc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                parsed = urllib.parse.urlsplit(self.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw) if raw else None
                query = dict(urllib.parse.parse_qsl(parsed.query))
                svc.calls.append(SimpleNamespace(
                    method=self.command, path=parsed.path, query=query, body=body,
                ))
                reply = (404, {"error": "no_route", "detail": parsed.path})
                for method, rx, handler in svc.routes:
                    m = rx.fullmatch(parsed.path)
                    if method == self.command and m:
                        reply = handler(m, body, query)
                        break
                status, payload = reply
                if isinstance(payload, Drop):
                    self.close_connection = True
                    return
                self.send_response(status)
                if isinstance(payload, Ndjson):
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    try:
                        for i, line in enumerate(payload.lines):
                            self.wfile.write((json.dumps(line) + "\n").encode())
                            self.wfile.flush()
                            if i == 0 and payload.gate is not None:
                                payload.gate.wait(5)
                    except OSError:
                        pass
                    return
                data = payload.data if isinstance(payload, Raw) else json.dumps(payload).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, method: str, pattern: str, handler):
        """``handler(match, body, query) -> (status, payload)``. Routes added
        earlier win; ``override`` adds one in front."""
        self.routes.append((method, re.compile(pattern), handler))

    def override(self, method: str, pattern: str, handler):
        self.routes.insert(0, (method, re.compile(pattern), handler))

    def install_uc_routes(self):
        self.route("POST", "/clients", lambda m, b, q: (200, {"client_id": "c1"}))
        self.route("POST", r"/windows/[^/]+/start", lambda m, b, q: (
            200, {"cdp": "ws://127.0.0.1:1/devtools/browser/x", "offers": ["attach", "control"]},
        ))
        self.route("DELETE", r"/clients/[^/]+", lambda m, b, q: (200, {"closed": 0}))
        self.route("POST", r"/windows/[^/]+/pages", lambda m, b, q: (200, {"target_id": "T1"}))
        self.route("DELETE", r"/windows/[^/]+/pages/[^/]+", lambda m, b, q: (200, {"closed": True}))

        def control(m, body, query):
            handler = self.controls.get(m.group(1))
            return handler(body) if handler else (200, {"result": None})

        self.route("POST", r"/windows/[^/]+/pages/[^/]+/controls/([^/]+)", control)

    def control_names(self) -> list[str]:
        return [c.path.rsplit("/", 1)[1] for c in self.calls if "/controls/" in c.path]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def svc():
    service = FakeService()
    yield service
    service.close()


# ── fake Playwright ──────────────────────────────────────────────────────


class FakeCdpSession:
    def __init__(self, tid):
        self.tid = tid
        self.detached = False

    def send(self, method):
        assert method == "Target.getTargetInfo"
        return {"targetInfo": {"targetId": self.tid}}

    def detach(self):
        self.detached = True


class FakePage:
    def __init__(self, tid, context):
        self.tid = tid
        self.context = context
        self.closed = False
        self.url = "about:blank"
        self.text = "hello"
        self.body_error = None
        self.waits: list = []
        self.gotos: list = []
        self.handlers: dict = {}
        self.scroll_sleep = 0.0

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self.context.pump()
        if ms == 800 and self.scroll_sleep:
            import time as _t
            _t.sleep(self.scroll_sleep)

    def wait_for_function(self, expression, timeout=None):
        raise PlaywrightTimeout("Timeout exceeded: engine absent")

    def evaluate(self, expression, arg=None):
        if "innerText" in expression and "window.__UC" not in expression:
            if self.body_error is not None:
                raise self.body_error
            return self.text
        if "scrollBy" in expression or "scrollTop" in expression or expression == "window.__UC":
            return None
        raise PlaywrightError("TypeError: window.__UC_x is not a function")

    def goto(self, url, **kwargs):
        self.url = url
        self.gotos.append((url, kwargs))

    def on(self, event, handler):
        self.handlers[event] = handler


class FakeContext:
    def __init__(self):
        self._pages: list = []
        self.pending: list = []        # become visible after `pump_delay` pumps
        self.pump_delay = 1
        self.pump_count = 0
        self.cookie_calls = 0
        self.sessions: list = []
        self.created = 0
        self.closed = False
        self.init_scripts: list = []

    @property
    def pages(self):
        return list(self._pages)

    def add_page(self, tid, *, visible=True):
        page = FakePage(tid, self)
        (self._pages if visible else self.pending).append(page)
        return page

    def pump(self):
        self.pump_count += 1
        if self.pending and self.pump_count >= self.pump_delay:
            self._pages.extend(self.pending)
            self.pending.clear()

    def cookies(self, urls=None):
        self.cookie_calls += 1
        self.pump()
        return []

    def new_cdp_session(self, page):
        if page.closed:
            raise PlaywrightError("Target closed")
        session = FakeCdpSession(page.tid)
        self.sessions.append(session)
        return session

    def new_page(self):
        self.created += 1
        return self.add_page(f"L{self.created}")

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    def close(self):
        self.closed = True

    def is_connected(self):
        return not self.closed


class FakeChromium:
    def __init__(self, browser, context=None):
        self.browser = browser
        self.context = context
        self.cdp_urls: list = []
        self.connect_error = None
        self.persistent: list = []

    def connect_over_cdp(self, url):
        self.cdp_urls.append(url)
        if self.connect_error is not None:
            raise self.connect_error
        return self.browser

    def launch_persistent_context(self, profile, **kwargs):
        self.persistent.append((profile, kwargs))
        return self.context


class FakePw:
    def __init__(self, browser=None, context=None):
        self.chromium = FakeChromium(browser, context)
        self.started = False
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeStarter:
    def __init__(self, pw):
        self.pw = pw

    def start(self):
        self.pw.started = True
        return self.pw


@pytest.fixture
def rig(monkeypatch, svc):
    svc.install_uc_routes()
    ctx = FakeContext()
    browser = FakeBrowser(ctx)
    pw = FakePw(browser, ctx)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeStarter(pw))
    for name in ("UC_WINDOW", "UC_NATIVE_WINDOW", "UC_CLIENT_LABEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UC_SERVICE_URL", svc.url)
    return SimpleNamespace(svc=svc, ctx=ctx, browser=browser, pw=pw)


@pytest.fixture
def started(rig):
    """A UCBrowser attached to window ``default`` with page ``T1`` visible."""
    page = rig.ctx.add_page("T1")
    uc = UCBrowser(stealth=False)
    uc.start()
    rig.page = page
    rig.uc = uc
    yield rig
    uc.close()


# ── import isolation ─────────────────────────────────────────────────────


def test_client_import_loads_no_browser_modules():
    code = (
        "import sys\n"
        "import uc_browser.service.client\n"
        "import uc_browser\n"
        "uc_browser.ServiceClient\n"
        "bad = [m for m in sys.modules if m == 'playwright' or m.startswith('playwright.')"
        " or m in ('uc_browser.browser', 'uc_browser.service.targets')]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_lazy_exports_resolve():
    import uc_browser

    assert uc_browser.UCBrowser is UCBrowser
    assert uc_browser.BrowserMode is BrowserMode
    assert uc_browser.ServiceClient is ServiceClient
    with pytest.raises(AttributeError):
        uc_browser.nope


# ── ServiceClient ────────────────────────────────────────────────────────


def test_client_needs_http_url(monkeypatch):
    monkeypatch.delenv("UC_SERVICE_URL", raising=False)
    with pytest.raises(ValueError):
        ServiceClient()
    with pytest.raises(ValueError):
        ServiceClient("file:///etc/passwd")
    monkeypatch.setenv("UC_SERVICE_URL", "http://127.0.0.1:9/")
    assert ServiceClient().url == "http://127.0.0.1:9"


def test_identifiers_are_escaped_in_path_and_query(svc):
    svc.route("DELETE", r"/windows/.*", lambda m, b, q: (200, {"closed": True}))
    svc.route("DELETE", "/clients", lambda m, b, q: (200, {"clients": 0, "closed": 0}))
    client = ServiceClient(svc.url)
    client.close_page("w/1 x", "T 1")
    assert svc.calls[0].path == "/windows/w%2F1%20x/pages/T%201"
    client.revoke("job #1&x=y")
    assert svc.calls[1].path == "/clients"
    assert svc.calls[1].query == {"label": "job #1&x=y"}


def test_register_new_page_and_close_shapes(svc):
    svc.install_uc_routes()
    client = ServiceClient(svc.url)
    assert client.register_client("w", "job-7", pid=42) == "c1"
    assert svc.calls[-1].body == {"pid": 42, "window": "w", "label": "job-7"}
    client.register_client("w")
    assert svc.calls[-1].body == {"pid": os.getpid(), "window": "w"}
    assert client.new_page("w", client_id="c1", url="https://x.test", wait_ms=5) == "T1"
    assert svc.calls[-1].body == {"client_id": "c1", "url": "https://x.test", "wait_ms": 5}
    client.new_page("w")
    assert svc.calls[-1].body == {}
    client.close_client("c1")
    assert (svc.calls[-1].method, svc.calls[-1].path) == ("DELETE", "/clients/c1")


def test_cookie_fetch_snapshot_and_site_shapes(svc):
    svc.route("POST", r"/windows/w/cookies", lambda m, b, q: (200, {"added": len(b["cookies"])}))
    svc.route("GET", r"/windows/w/cookies", lambda m, b, q: (200, {"cookies": [{"name": q["domain"]}]}))
    svc.route("POST", r"/windows/w/fetch", lambda m, b, q: (200, {"status": 200, "headers": {}, "body": "x"}))
    svc.route("POST", r"/windows/w/pages/T1/snapshot", lambda m, b, q: (
        200, {"data": base64.b64encode(b"\x89PNG").decode() if b["format"] == "png" else "<html>"},
    ))
    svc.route("POST", r"/sites/grok/send", lambda m, b, q: (200, {"result": {"echo": b}}))
    client = ServiceClient(svc.url)
    assert client.add_cookies("w", cookies=[{"name": "a"}]) == {"added": 1}
    assert svc.calls[-1].body == {"cookies": [{"name": "a"}]}
    assert client.get_cookies("w", "grok.com") == [{"name": "grok.com"}]
    assert client.fetch("w", "https://x.test/a", from_url="https://x.test/")["status"] == 200
    assert svc.calls[-1].body == {"url": "https://x.test/a", "from": "https://x.test/"}
    assert client.snapshot("w", "T1") == b"\x89PNG"
    assert client.snapshot("w", "T1", "html", full_page=True) == "<html>"
    assert svc.calls[-1].body == {"format": "html", "full_page": True}
    out = client.site("grok", "send", {"text": "hi"}, session_id="s", wait=False)
    assert out == {"echo": {"text": "hi", "session_id": "s", "wait": False}}


def test_control_returns_result_and_posts_args(svc):
    svc.route("POST", r"/windows/w/pages/T1/controls/detect", lambda m, b, q: (200, {"result": [b]}))
    client = ServiceClient(svc.url)
    assert client.control("w", "T1", "detect", ("search",), {"guarantee": "X"}) == [
        {"args": ["search"], "kwargs": {"guarantee": "X"}},
    ]


@pytest.mark.parametrize("override", [False, True])
def test_control_read_budget_does_not_expire_during_queue_wait(svc, override):
    svc.route("GET", "/windows", lambda m, b, q: (200, {"w": {"queue_timeout_s": 0.2}}))

    def delayed_result(m, body, query):
        threading.Event().wait(0.05)  # exceeds the operation-only read budget
        return 200, {"result": {"selector": "#search"}}

    svc.route("POST", "/windows/w/pages/T1/controls/detect", delayed_result)
    client = ServiceClient(svc.url, control_timeout=None if override else 0.001)
    options = {"timeout": 0.001} if override else {}
    assert client.control("w", "T1", "detect", ["search"], **options) == {"selector": "#search"}


def test_control_409_no_controls_raises_without_a_value(svc):
    svc.route("POST", r"/windows/w/pages/T1/controls/detect_all", lambda m, b, q: (
        409, {"error": "no_controls", "detail": "window w has engine none"},
    ))
    client = ServiceClient(svc.url)
    with pytest.raises(ControlsUnavailable) as info:
        client.control("w", "T1", "detect_all")
    assert (info.value.window, info.value.name, info.value.status, info.value.code) == (
        "w", "detect_all", 409, "no_controls",
    )
    assert isinstance(info.value, ServiceError)


def test_other_409_and_wire_errors_stay_service_errors(svc):
    svc.route("POST", r"/windows/w/pages/T1/snapshot", lambda m, b, q: (
        409, {"error": "format_unsupported", "detail": "mhtml"},
    ))
    svc.route("POST", r"/windows/w/pages/T1/controls/chat", lambda m, b, q: (
        500, {"error": "control_failed", "detail": "Target closed"},
    ))
    client = ServiceClient(svc.url)
    with pytest.raises(ServiceError) as info:
        client.snapshot("w", "T1", "mhtml")
    assert not isinstance(info.value, ControlsUnavailable)
    assert (info.value.code, info.value.detail, info.value.status) == ("format_unsupported", "mhtml", 409)
    with pytest.raises(ServiceError) as info:
        client.control("w", "T1", "chat", ("hi",))
    assert (info.value.code, info.value.status) == ("control_failed", 500)


def test_non_wire_error_bodies(svc):
    svc.route("GET", "/windows", lambda m, b, q: (502, Raw(b"<html>bad gateway</html>")))
    svc.route("GET", "/clients", lambda m, b, q: (404, {"detail": "Not Found"}))
    client = ServiceClient(svc.url)
    with pytest.raises(ServiceError) as info:
        client.windows()
    assert (info.value.code, info.value.status) == ("http_502", 502)
    assert "bad gateway" in info.value.detail
    with pytest.raises(ServiceError) as info:
        client.request("GET", "/clients")
    assert (info.value.code, info.value.detail) == ("http_404", "Not Found")


def test_unreachable_service_is_gone():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(UCServiceGone) as info:
        ServiceClient(f"http://127.0.0.1:{port}", timeout=2).windows()
    assert isinstance(info.value, ServiceError)


def test_connection_dropped_without_reply_is_gone(svc):
    svc.route("GET", "/windows", lambda m, b, q: (200, Drop()))
    with pytest.raises(UCServiceGone):
        ServiceClient(svc.url).windows()


def test_non_json_body_is_rejected_before_sending(svc):
    svc.route("POST", "/x", lambda m, b, q: (200, {}))
    with pytest.raises(TypeError):
        ServiceClient(svc.url).request("POST", "/x", {"a": object()})
    assert svc.calls == []


def test_site_stream_yields_deltas_then_result_and_raises_error_lines(svc):
    svc.route("POST", "/sites/grok/send", lambda m, b, q: (
        200, Ndjson([{"delta": "a"}, {"delta": "b"}, {"result": {"response": "ab"}}]),
    ))
    svc.route("POST", "/sites/grok/chat", lambda m, b, q: (
        200, Ndjson([{"delta": "a"}, {"error": "timeout", "detail": "partial"}]),
    ))
    client = ServiceClient(svc.url)
    stream = client.site("grok", "send", {"text": "x"}, stream=True)
    assert svc.calls[-1].body == {"text": "x", "stream": True}
    assert list(stream) == [{"delta": "a"}, {"delta": "b"}, {"result": {"response": "ab"}}]
    assert stream.closed
    stream = client.site("grok", "chat", stream=True)
    assert next(stream) == {"delta": "a"}
    with pytest.raises(ServiceError) as info:
        next(stream)
    assert (info.value.code, info.value.detail) == ("timeout", "partial")
    assert stream.closed


def test_fetch_stream_and_generic_stream_do_not_interpret_error_lines(svc):
    svc.route("POST", r"/windows/w/fetch", lambda m, b, q: (
        200, Ndjson([{"chunk": "x"}, {"status": 200}]),
    ))
    svc.route("GET", "/odd", lambda m, b, q: (200, Ndjson([{"error": "x"}])))
    client = ServiceClient(svc.url)
    assert list(client.fetch("w", "https://x.test", stream=True)) == [{"chunk": "x"}, {"status": 200}]
    assert svc.calls[0].body == {"url": "https://x.test", "stream": True}
    assert list(client.stream("GET", "/odd")) == [{"error": "x"}]


def test_stream_close_closes_the_response(svc):
    gate = threading.Event()
    svc.route("GET", "/s", lambda m, b, q: (200, Ndjson([{"n": 1}, {"n": 2}], gate)))
    client = ServiceClient(svc.url)
    stream = client.stream("GET", "/s")
    assert next(stream) == {"n": 1}
    stream.close()
    gate.set()
    assert stream.closed
    with pytest.raises(StopIteration):
        next(stream)
    with client.stream("GET", "/s") as other:
        gate.set()
        next(other)
    assert other.closed


def test_stream_http_error_at_open_is_a_service_error(svc):
    svc.route("GET", "/s", lambda m, b, q: (404, {"error": "no_such_site", "detail": "x"}))
    with pytest.raises(ServiceError) as info:
        ServiceClient(svc.url).stream("GET", "/s")
    assert (info.value.code, info.value.status) == ("no_such_site", 404)


# ── TargetResolver ───────────────────────────────────────────────────────


def test_scan_pumps_events_before_reading_pages():
    ctx = FakeContext()
    ctx.add_page("A")
    ctx.add_page("B", visible=False)   # invisible until a real call is made
    resolver = TargetResolver(ctx)
    assert set(resolver.scan()) == {"A", "B"}
    assert resolver.resolve("B").tid == "B"


def test_resolve_polls_until_a_late_target_appears():
    ctx = FakeContext()
    ctx.add_page("A")
    ctx.add_page("B", visible=False)
    ctx.pump_delay = 4
    resolver = TargetResolver(ctx, poll_s=0.001)
    assert resolver.resolve("B").tid == "B"
    assert ctx.pump_count >= 4


def test_resolve_gives_up_with_no_such_target():
    ctx = FakeContext()
    ctx.add_page("A")
    resolver = TargetResolver(ctx, wait_s=0.1, poll_s=0.01)
    with pytest.raises(TargetNotFound) as info:
        resolver.resolve("ZZ")
    assert (info.value.code, info.value.status) == ("no_such_target", 404)
    assert isinstance(info.value, LookupError) and isinstance(info.value, ServiceError)


def test_forward_and_reverse_maps_are_cached_and_sessions_detached():
    ctx = FakeContext()
    a = ctx.add_page("A")
    resolver = TargetResolver(ctx)
    assert resolver.target_id(a) == "A"
    assert resolver.resolve("A") is a
    assert resolver.target_id(a) == "A"
    assert len(ctx.sessions) == 1 and ctx.sessions[0].detached


def test_closed_pages_are_evicted():
    ctx = FakeContext()
    a = ctx.add_page("A")
    b = ctx.add_page("B")
    resolver = TargetResolver(ctx, wait_s=0.05, poll_s=0.01)
    assert set(resolver.scan()) == {"A", "B"}
    b.closed = True
    assert set(resolver.scan()) == {"A"}
    with pytest.raises(TargetNotFound):
        resolver.resolve("B")
    assert resolver.resolve("A") is a


def test_target_id_of_a_closed_unknown_page_raises_the_raw_playwright_error():
    ctx = FakeContext()
    page = ctx.add_page("A")
    page.closed = True
    with pytest.raises(PlaywrightError, match="Target closed"):
        TargetResolver(ctx).target_id(page)


def test_pump_without_any_page_makes_a_real_call_on_the_context():
    ctx = FakeContext()
    ctx.add_page("B", visible=False)
    resolver = TargetResolver(ctx)
    assert set(resolver.scan()) == {"B"}
    assert ctx.cookie_calls == 1


# ── UCBrowser: signatures and construction ───────────────────────────────


def test_control_names_and_signatures_survive_wrapping():
    assert len(CONTROL_NAMES) == 39
    sig = inspect.signature(UCBrowser.detect)
    assert list(sig.parameters) == ["self", "page", "pattern_name", "guarantee"]
    assert sig.parameters["guarantee"].default == "BEHAVIORAL"
    assert inspect.signature(UCBrowser.scroll_feed).parameters["seconds"].default == 15
    assert inspect.signature(UCBrowser.chat).parameters["timeout_s"].default == 30
    for name in CONTROL_NAMES:
        fn = getattr(UCBrowser, name)
        assert inspect.isfunction(fn) and hasattr(fn, "__wrapped__")
        assert list(inspect.signature(fn).parameters)[1:2] == ["page"]
    # not controls
    for name in ("open", "new_page", "start", "close", "navigate_and_search"):
        assert name not in CONTROL_NAMES


def test_label_and_client_id_are_read_only(monkeypatch):
    monkeypatch.setenv("UC_CLIENT_LABEL", "from-env")
    assert UCBrowser().label == "from-env"
    assert UCBrowser(label="mine").label == "mine"
    uc = UCBrowser()
    assert uc.client_id is None
    with pytest.raises(AttributeError):
        uc.label = "x"
    with pytest.raises(AttributeError):
        uc.client_id = "x"


# ── UCBrowser: service-mode lifecycle ────────────────────────────────────


def test_start_registers_starts_and_attaches_without_touching_the_context(rig, monkeypatch):
    monkeypatch.setenv("UC_CLIENT_LABEL", "job-9")
    uc = UCBrowser()
    assert uc._service is None
    uc.start()
    assert [(c.method, c.path) for c in rig.svc.calls] == [
        ("POST", "/clients"), ("POST", "/windows/default/start"),
    ]
    assert rig.svc.calls[0].body == {"pid": os.getpid(), "window": "default", "label": "job-9"}
    assert rig.pw.chromium.cdp_urls == ["ws://127.0.0.1:1/devtools/browser/x"]
    assert uc.client_id == "c1" and uc.label == "job-9"
    assert uc._context is rig.ctx and uc._browser is rig.browser
    assert uc._stealth_plugin is None
    assert rig.ctx.init_scripts == []          # no init script from the client
    uc.close()


def test_window_names_follow_the_environment(rig, monkeypatch):
    monkeypatch.setenv("UC_WINDOW", "harvest")
    monkeypatch.setenv("UC_NATIVE_WINDOW", "nat")
    for mode, window in ((BrowserMode.CHROMIUM_EXT, "harvest"), (BrowserMode.HEADLESS, "harvest"),
                         (BrowserMode.NATIVE_CDP, "nat")):
        rig.svc.calls.clear()
        uc = UCBrowser(mode=mode)
        uc.start()
        assert rig.svc.calls[0].body["window"] == window
        assert rig.svc.calls[1].path == f"/windows/{window}/start"
        uc.close()


def test_close_releases_the_client_and_only_disconnects(started):
    started.uc.close()
    assert (started.svc.calls[-1].method, started.svc.calls[-1].path) == ("DELETE", "/clients/c1")
    assert started.browser.closed and started.pw.stopped
    assert not started.ctx.closed
    assert started.uc.client_id is None and started.uc._service is None
    started.uc.close()  # idempotent
    assert [c.path for c in started.svc.calls].count("/clients/c1") == 1


def test_start_failure_in_the_window_start_releases_the_registration(rig):
    rig.svc.override("POST", r"/windows/[^/]+/start", lambda m, b, q: (
        500, {"error": "launch_failed", "detail": "boom"},
    ))
    uc = UCBrowser()
    with pytest.raises(ServiceError) as info:
        uc.start()
    assert info.value.code == "launch_failed"
    assert ("DELETE", "/clients/c1") in [(c.method, c.path) for c in rig.svc.calls]
    assert not rig.pw.started
    assert uc._service is None and uc.client_id is None


def test_attach_failure_releases_registration_and_stops_playwright(rig):
    rig.pw.chromium.connect_error = PlaywrightError("connection refused")
    uc = UCBrowser()
    with pytest.raises(PlaywrightError):
        uc.start()
    assert ("DELETE", "/clients/c1") in [(c.method, c.path) for c in rig.svc.calls]
    assert rig.pw.stopped
    assert uc._service is None and uc.client_id is None and uc._context is None


def test_window_without_cdp_cannot_be_attached(rig):
    rig.svc.override("POST", r"/windows/[^/]+/start", lambda m, b, q: (
        200, {"cdp": None, "offers": ["fetch"]},
    ))
    uc = UCBrowser()
    with pytest.raises(RuntimeError, match="no CDP"):
        uc.start()
    assert ("DELETE", "/clients/c1") in [(c.method, c.path) for c in rig.svc.calls]
    assert not rig.pw.started


def test_service_gone_on_start_is_raised(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("UC_SERVICE_URL", f"http://127.0.0.1:{port}")
    uc = UCBrowser()
    with pytest.raises(UCServiceGone):
        uc.start()
    assert uc._service is None


# ── UCBrowser: pages ─────────────────────────────────────────────────────


def test_new_page_in_service_mode_posts_then_resolves_the_target(started):
    rig = started
    page = rig.uc.new_page()
    assert page is rig.page
    assert rig.svc.calls[-1].path == "/windows/default/pages"
    assert rig.svc.calls[-1].body == {"client_id": "c1"}
    assert rig.ctx.created == 0


def test_new_page_waits_for_propagation_to_this_connection(rig):
    rig.ctx.add_page("A")
    late = rig.ctx.add_page("T1", visible=False)
    uc = UCBrowser()
    uc.start()
    assert uc.new_page() is late
    uc.close()


def test_new_page_that_never_appears_is_closed_on_the_service(started):
    started.svc.override("POST", r"/windows/[^/]+/pages", lambda m, b, q: (200, {"target_id": "NOPE"}))
    started.uc._targets.wait_s = 0.1
    started.uc._targets.poll_s = 0.01
    with pytest.raises(TargetNotFound):
        started.uc.new_page()
    assert ("DELETE", "/windows/default/pages/NOPE") in [(c.method, c.path) for c in started.svc.calls]


def test_in_process_new_page_uses_the_local_context_and_stealth(monkeypatch):
    monkeypatch.delenv("UC_SERVICE_URL", raising=False)
    ctx = FakeContext()
    applied = []
    uc = UCBrowser()
    uc._context = ctx
    uc._stealth_plugin = SimpleNamespace(apply_stealth_sync=applied.append)
    page = uc.new_page()
    assert ctx.created == 1 and applied == [page]
    uc._stealth_plugin = None
    assert uc.new_page() is not page and len(applied) == 1


def test_open_and_open_with_intercept_use_new_page_and_keep_local_navigation(started):
    rig = started
    page = rig.uc.open("https://x.test/a", wait_ms=7)
    assert page is rig.page
    assert page.gotos == [("https://x.test/a", {"timeout": 30000, "wait_until": "domcontentloaded"})]
    assert 7 in page.waits
    assert rig.ctx.created == 0
    page2, captured = rig.uc.open_with_intercept("https://x.test/b", r"/api/", wait_ms=3)
    assert page2 is rig.page and captured == []
    assert "response" in page2.handlers
    assert [c.path for c in rig.svc.calls].count("/windows/default/pages") == 2


# ── UCBrowser: controls ──────────────────────────────────────────────────


def test_controls_dispatch_remotely_with_the_callers_arguments(started):
    started.svc.controls["detect"] = lambda body: (200, {"result": [{"selector": "#q"}]})
    assert started.uc.detect(started.page, "search", guarantee="STRUCTURAL") == [{"selector": "#q"}]
    call = started.svc.calls[-1]
    assert call.path == "/windows/default/pages/T1/controls/detect"
    assert call.body == {"args": ["search"], "kwargs": {"guarantee": "STRUCTURAL"}}


def test_bad_arguments_raise_the_same_type_error_as_a_local_call(started):
    with pytest.raises(TypeError):
        started.uc.detect(started.page)
    assert started.svc.control_names() == []


def test_unstarted_receiver_never_dispatches_even_with_the_environment_set(rig):
    page = rig.ctx.add_page("T1")
    receiver = UCBrowser()
    assert receiver.get_patterns(page) == {}   # local body: evaluate("window.__UC") -> None
    assert receiver.detect_all(page) == {}
    assert rig.svc.calls == []


def test_wire_errors_surface(started):
    started.svc.controls["detect"] = lambda body: (404, {"error": "no_such_target", "detail": "gone"})
    with pytest.raises(ServiceError) as info:
        started.uc.detect(started.page, "search")
    assert info.value.code == "no_such_target"
    started.svc.controls["detect"] = lambda body: (503, {"error": "busy", "detail": "no worker"})
    with pytest.raises(ServiceError) as info:
        started.uc.detect(started.page, "search")
    assert info.value.code == "busy"


def test_closed_page_keeps_the_raw_playwright_error(rig):
    uc = UCBrowser()
    uc.start()
    page = rig.ctx.add_page("T1")
    page.closed = True
    with pytest.raises(PlaywrightError, match="Target closed"):
        uc.detect(page, "search")
    assert rig.svc.control_names() == []
    uc.close()


def _no_controls(body):
    return 409, {"error": "no_controls", "detail": "window default has engine none"}


def test_409_runs_the_original_body_locally_and_remembers_it(started, caplog):
    started.svc.controls["detect_all"] = _no_controls
    with caplog.at_level(logging.WARNING, logger="uc_browser"):
        assert started.uc.detect_all(started.page) == {}
        assert started.uc.detect_all(started.page) == {}
    assert started.svc.control_names() == ["detect_all"]      # second call skipped the POST
    warned = [r for r in caplog.records if "controls unavailable on window default" in r.getMessage()]
    assert len(warned) == 1 and "detect_all" in warned[0].getMessage()


def test_window_without_control_offer_uses_live_page_without_posting(rig):
    rig.svc.override("POST", "/windows/default/start", lambda m, b, q: (
        200, {"cdp": "ws://127.0.0.1:1/devtools/browser/x", "offers": ["attach", "fetch"]},
    ))
    page = rig.ctx.add_page("T1")
    page.text = "text from the caller's page, not a service default"
    with UCBrowser(stealth=False) as uc:
        assert uc.get_feed_text(page) == page.text
        assert uc.detect_all(page) == {}
        assert rig.svc.control_names() == []
        page.body_error = PlaywrightError("Target page has been closed")
        with pytest.raises(PlaywrightError, match="has been closed"):
            uc.get_feed_text(page)


def test_409_fallback_is_the_real_body_not_a_default_table(started):
    started.svc.controls["get_feed_text"] = _no_controls
    started.page.text = "the real page text"
    assert started.uc.get_feed_text(started.page) == "the real page text"
    assert started.svc.control_names() == ["get_feed_text"]    # nested detect ran in-process


def test_409_fallback_preserves_the_original_error(started):
    started.svc.controls["get_feed_text"] = _no_controls
    started.page.body_error = PlaywrightError("Target page, context or browser has been closed")
    with pytest.raises(PlaywrightError, match="has been closed"):
        started.uc.get_feed_text(started.page)


def test_409_scope_covers_nested_controls_and_ends_with_the_call(started):
    started.svc.controls["has_login_wall"] = _no_controls
    assert started.uc.has_login_wall(started.page) is False
    assert started.svc.control_names() == ["has_login_wall"]   # nested detect did not POST
    assert not getattr(browser_mod._local_control_scope, "active", {})
    # other controls on the same engine-less window run locally without a POST
    assert started.uc.find_inputs(started.page) == []
    assert started.svc.control_names() == ["has_login_wall"]


def test_scan_action_orchestrates_locally_and_dispatches_nested_controls(started):
    started.svc.controls["first_scan"] = lambda b: (200, {"result": {"elements": 3}})
    started.svc.controls["next_scan"] = lambda b: (200, {"result": {"added": 1}})
    started.svc.controls["auto_detect"] = lambda b: (200, {"result": [{"pattern": "modal"}]})
    acted = []
    result = started.uc.scan_action(started.page, acted.append)
    assert acted == [started.page]
    assert 500 in started.page.waits
    assert result == [{"pattern": "modal"}]
    assert started.svc.control_names() == ["first_scan", "next_scan", "auto_detect"]


def test_scroll_feed_with_callback_runs_locally_and_without_it_dispatches(started):
    started.svc.controls["detect"] = lambda b: (200, {"result": []})
    started.svc.controls["get_feed_items"] = lambda b: (200, {"result": ["item one"]})
    started.page.scroll_sleep = 0.02
    seen = []
    items = started.uc.scroll_feed(started.page, seconds=0.05, on_item=seen.append)
    assert items == ["item one"] and seen == ["item one"]
    names = started.svc.control_names()
    assert names[0] == "detect" and set(names) == {"detect", "get_feed_items"}
    assert "scroll_feed" not in names

    started.svc.calls.clear()
    started.svc.controls["scroll_feed"] = lambda b: (200, {"result": ["a", "b"]})
    assert started.uc.scroll_feed(started.page, seconds=3) == ["a", "b"]
    assert started.svc.calls[-1].body == {"args": [], "kwargs": {"seconds": 3}}
    assert started.svc.control_names() == ["scroll_feed"]


# ── standalone (UC_SERVICE_URL unset) ────────────────────────────────────


def _standalone(monkeypatch, tmp_path):
    monkeypatch.delenv("UC_SERVICE_URL", raising=False)
    monkeypatch.setattr(browser_mod, "_CHROME_PROFILE", tmp_path / "data" / ".chrome_profile")
    monkeypatch.setattr(browser_mod, "_NATIVE_PROFILE", tmp_path / "data" / ".uc_chrome_profile")
    monkeypatch.setattr(browser_mod, "_STATE_FILE", tmp_path / "no-state.json")
    ctx = FakeContext()
    pw = FakePw(FakeBrowser(ctx), ctx)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakeStarter(pw))
    return ctx, pw


def test_standalone_persistent_launch_refuses_a_held_profile(monkeypatch, tmp_path):
    ctx, pw = _standalone(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(
        "uc_browser.service.profiles.assert_profile_available", lambda p: seen.append(Path(p)),
    )
    uc = UCBrowser(mode=BrowserMode.CHROMIUM_EXT, stealth=False)
    uc.start()
    assert seen == [(tmp_path / "data" / ".uc_chromium_profile").resolve()]
    assert len(ctx.init_scripts) == 1 and uc._service is None
    uc.close()
    assert ctx.closed                        # in-process behavior is unchanged

    def held(profile):
        raise ServiceError("profile_held", "held", 409)

    monkeypatch.setattr("uc_browser.service.profiles.assert_profile_available", held)
    pw.stopped = False
    uc = UCBrowser(mode=BrowserMode.CHROME, stealth=False)
    with pytest.raises(ServiceError) as info:
        uc.start()
    assert info.value.code == "profile_held"
    assert pw.stopped and uc._pw is None
    assert len(pw.chromium.persistent) == 1   # the refused launch never reached Playwright


def test_standalone_native_uses_the_shared_launcher_not_port_9222(monkeypatch, tmp_path):
    ctx, pw = _standalone(monkeypatch, tmp_path)
    keepass = tmp_path / "keepass"
    monkeypatch.setattr(browser_mod, "_ensure_keepassxc_extension", lambda: keepass)
    proc = SimpleNamespace(terminated=False)
    proc.terminate = lambda: setattr(proc, "terminated", True)
    proc.wait = lambda timeout=None: 0
    proc.kill = lambda: None
    calls = []

    def launch_native(playwright, profile, *, extra_args=()):
        calls.append((playwright, Path(profile), tuple(extra_args)))
        return proc, pw.chromium.browser, ctx, "ws://127.0.0.1:5/devtools/browser/y"

    monkeypatch.setattr("uc_browser.service.profiles.launch_native", launch_native)
    uc = UCBrowser(mode=BrowserMode.NATIVE_CDP)
    uc.start()
    assert calls == [(pw, tmp_path / "data" / ".uc_chrome_profile", (
        f"--load-extension={keepass}", f"--disable-extensions-except={keepass}",
    ))]
    assert uc._chrome_proc is proc and uc._context is ctx
    uc.close()
    assert proc.terminated

    monkeypatch.setattr(browser_mod, "_ensure_keepassxc_extension", lambda: None)
    calls.clear()
    UCBrowser(mode=BrowserMode.NATIVE_CDP).start()
    assert calls[0][2] == ()


def test_standalone_native_launch_failure_raises_and_stops_playwright(monkeypatch, tmp_path):
    ctx, pw = _standalone(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_mod, "_ensure_keepassxc_extension", lambda: None)

    def launch_native(playwright, profile, *, extra_args=()):
        raise ServiceError("launch_failed", "chrome exited", 500)

    monkeypatch.setattr("uc_browser.service.profiles.launch_native", launch_native)
    uc = UCBrowser(mode=BrowserMode.NATIVE_CDP)
    with pytest.raises(ServiceError) as info:
        uc.start()
    assert info.value.code == "launch_failed"
    assert pw.stopped and uc._context is None
