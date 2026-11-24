"""Native service ownership on a disposable Chrome profile; never port 9222."""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import psutil
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from uc_browser.browser import UCBrowser
from uc_browser.service.api import create_router
from uc_browser.service.client import ServiceClient
from uc_browser.service.errors import ServiceError
from uc_browser.service.leases import LeaseStore
from uc_browser.service.operations import Operations
from uc_browser.service.profiles import find_chrome, profile_holder
from uc_browser.service.sessions import builtin_sites, parse_sites
from uc_browser.service.windows import ConfigError, WindowManager


def eventually(fn, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError("native fixture condition not reached")


@pytest.fixture
def native_service(tmp_path, monkeypatch):
    if sys.platform != "linux" or find_chrome() is None:
        pytest.skip("isolated installed-Chrome socket ownership check requires Linux and Chrome")
    monkeypatch.setenv("UC_SERVICE_HEADLESS", "1")
    profile = tmp_path / "native-profile"
    config = tmp_path / "windows.json"
    config.write_text(json.dumps({"windows": {"native": {
        "browser": "chrome", "engine": "inject", "unattached": True,
        "stealth": False, "profile": str(profile),
    }}}))
    manager = WindowManager(config)
    leases = LeaseStore(manager)
    app = FastAPI()

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return JSONResponse(error.as_dict(), status_code=error.status)

    app.include_router(create_router(manager, leases, Operations(manager, leases)))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        eventually(lambda: server.started)
        yield ServiceClient(f"http://127.0.0.1:{port}"), manager, leases, profile
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        manager.close()
        listener.close()


@pytest.fixture
def native_site():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            body = b"<title>Native fixture ready</title><p>fixture content</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture?a=one&b=two%20words#part", paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def assert_busy(call):
    with pytest.raises(ServiceError) as error:
        call()
    assert error.value.code == "busy"


def test_native_quiet_load_attach_detach_and_revoke(native_service, native_site):
    client, manager, leases, profile = native_service
    url, paths = native_site
    quiet = client.register_client("native", "prepare", unattached=True)
    target = client.new_page("native", quiet, url=url)
    page = eventually(lambda: (
        info if (info := client.page_info("native", target))["title"] == "Native fixture ready" else None
    ))
    assert page["url"] == url
    assert "/fixture?a=one&b=two%20words" in paths
    info = client.windows()["native"]
    port = urlsplit(info["cdp"]).port
    pid = profile_holder(profile)
    assert pid is not None

    def connections():
        return [c for c in psutil.Process(pid).net_connections(kind="tcp")
                if c.laddr.port == port and c.status == psutil.CONN_ESTABLISHED]

    assert connections() == []
    assert_busy(lambda: client.register_client("native", "other"))
    assert_busy(lambda: client.get_cookies("native", "127.0.0.1"))
    assert_busy(lambda: client.snapshot("native", target, format="html"))
    leases.reap()
    assert client.page_info("native", target)["title"] == "Native fixture ready"
    assert connections() == []
    client.close_client(quiet)
    with pytest.raises(ServiceError) as missing:
        client.page_info("native", target)
    assert missing.value.code == "no_such_target"

    # Ordinary service attachment follows preparation. Its close must detach
    # before releasing the lease, not expose a false quiet window.
    with UCBrowser(service=client.url, window="native", label="native-client") as browser:
        page = browser.open(url, wait_ms=0)
        assert page.title() == "Native fixture ready"
        assert page.evaluate("typeof window.__UC") == "undefined"
        attached = len(connections())
        assert attached > 0
        browser.detect_all(page)
        assert page.evaluate("window.__UC.ready") is True
        assert len(connections()) == attached  # control worker has disconnected
        assert_busy(lambda: client.register_client("native", "prepare", unattached=True))
        # Revocation closes leased targets, but the client's browser-level CDP
        # websocket is still connected until it leaves the with block.
        assert client.revoke("native-client")["closed"] == 1
        assert_busy(lambda: client.register_client("native", "prepare", unattached=True))
    eventually(lambda: not connections())

    # HTTP-only offers attach transiently and have disconnected on return.
    assert client.add_cookies("native", [{
        "name": "fixture", "value": "present", "domain": "127.0.0.1", "path": "/",
    }])["added"] == 1
    assert connections() == []
    assert any(c["name"] == "fixture" for c in client.get_cookies("native", "127.0.0.1"))
    assert connections() == []

    quiet = client.register_client("native", "next-source", unattached=True)
    target = client.new_page("native", quiet, url=url)
    eventually(lambda: client.page_info("native", target)["title"] == "Native fixture ready")
    assert connections() == []
    # A disappeared preparer must release both its page and quiet reservation.
    identity = leases.identity
    leases.identity = lambda _pid: None
    try:
        leases.reap()
    finally:
        leases.identity = identity
    next_client = client.register_client("native", "after-death", unattached=True)
    client.close_client(next_client)
    assert client.windows()["native"]["clients"] == 0
    assert len(client.windows()["native"]["pages"]) == 1
    assert connections() == []
    assert profile_holder(profile) == pid


def test_unconfirmed_disconnect_keeps_lease_and_sessions_cannot_attach(native_service):
    client, manager, leases, _ = native_service
    registered = client.register_client("native", "failed-disconnect")

    class FailedDisconnect:
        def close(self):
            raise RuntimeError("transport did not stop")

        stop = close

    UCBrowser._release_service(client, registered, FailedDisconnect(), FailedDisconnect())
    assert_busy(lambda: client.register_client("native", "prepare", unattached=True))
    client.close_client(registered)
    quiet = client.register_client("native", "prepare", unattached=True)
    client.close_client(quiet)
    with pytest.raises(ConfigError, match="persistent sessions"):
        parse_sites({"grok": {"window": "native"}}, manager, builtin_sites())
