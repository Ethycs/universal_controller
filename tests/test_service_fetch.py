"""Real Chromium regressions for retained fetch pages; no accounts/network."""

import concurrent.futures as cf
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from uc_browser.service.errors import ServiceError
from uc_browser.service.leases import LeaseStore
from uc_browser.service.operations import Operations
from uc_browser.service.windows import WindowManager


@pytest.fixture
def fetch_env(tmp_path, monkeypatch):
    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("requires playwright install chromium")
    monkeypatch.setenv("UC_SERVICE_HEADLESS", "1")
    requests = []
    release = threading.Event()
    servers = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.server.server_port, self.path))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{servers[1].server_port}/")
                self.end_headers()
                return
            slow = self.path == "/slow"
            data = (b"first-last" if slow else
                    json.dumps({"referrer": self.headers.get("Referer"),
                                "cookie": self.headers.get("Cookie")}).encode()
                    if self.path == "/echo" else b"<body>fixture</body>")
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Type", "text/plain" if slow or self.path == "/echo" else "text/html")
            if self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Credentials", "true")
            self.end_headers()
            if slow:
                self.wfile.write(b"first-")
                self.wfile.flush()
                release.wait(10)
                self.wfile.write(b"last")
            else:
                self.wfile.write(data)

    for _ in range(2):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    config = tmp_path / "windows.json"
    config.write_text(json.dumps({"windows": {"w": {
        "profile": str(tmp_path / "profile"), "engine": "none",
        "stealth": False, "queue_timeout_s": 2,
    }}}))
    manager = WindowManager(config)
    leases = LeaseStore(manager)
    ops = Operations(manager, leases)
    roots = [f"http://127.0.0.1:{s.server_port}" for s in servers]
    try:
        yield SimpleNamespace(ops=ops, manager=manager, requests=requests,
                              roots=roots, release=release)
    finally:
        release.set()
        leases.close()
        manager.close()
        for server in servers:
            server.shutdown()
            server.server_close()


def test_fetch_referrer_and_cross_origin_credentials_without_navigation(fetch_env):
    env = fetch_env
    a, b = env.roots
    env.ops.add_cookies("w", cookies=[{"name": "sid", "value": "synthetic", "url": a}])
    first = env.ops.fetch("w", {"url": a + "/echo", "from": a + "/one"})
    second = env.ops.fetch("w", {"url": a + "/echo", "from": a + "/two"})
    crossed = env.ops.fetch("w", {"url": b + "/echo", "from": a + "/three"})
    assert json.loads(first["body"]) == {"referrer": a + "/one", "cookie": "sid=synthetic"}
    assert json.loads(second["body"]) == {"referrer": a + "/two", "cookie": "sid=synthetic"}
    # Cross-origin (different port), same-site: credentials include is required.
    assert json.loads(crossed["body"]) == {"referrer": a + "/", "cookie": "sid=synthetic"}
    assert not any(path in ("/two", "/three") for _, path in env.requests)
    assert [p["url"] for p in env.manager.get("w").info()["pages"] if p["url"] != "about:blank"] == [a + "/one"]


def test_changed_from_preserves_inflight_stream_and_origin_page(fetch_env):
    env = fetch_env
    a = env.roots[0]
    events, cancel, stream = env.ops.stream("w", {"url": a + "/slow"})
    try:
        assert events.get(timeout=10) == {"ready": 200}
        first = events.get(timeout=10)
        assert first == {"chunk": "first-"}
        entered = threading.Event()

        def second():
            entered.set()
            return env.ops.fetch("w", {"url": a + "/echo", "from": a + "/later"})

        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            later = pool.submit(second)
            try:
                assert entered.wait(5)
                with pytest.raises(cf.TimeoutError):
                    later.result(0.1)
            finally:
                env.release.set()
            assert stream.result(10) == {"status": 200}
            assert json.loads(later.result(10)["body"])["referrer"] == a + "/later"
        chunks = first["chunk"]
        while (event := events.get(timeout=10)) is not None:
            if "chunk" in event:
                chunks += event["chunk"]
        assert chunks == "first-last"
        paths = [path for _, path in env.requests]
        assert paths.count("/") == 1 and "/later" not in paths
    finally:
        env.release.set()
        cancel()


def test_cross_origin_redirect_does_not_silently_drop_explicit_referrer(fetch_env):
    env = fetch_env
    a = env.roots[0]
    with pytest.raises(ServiceError) as exc:
        env.ops.fetch("w", {"url": a + "/echo", "from": a + "/redirect"})
    assert exc.value.code == "origin_unreachable"
    assert not any(path == "/echo" for _, path in env.requests)
