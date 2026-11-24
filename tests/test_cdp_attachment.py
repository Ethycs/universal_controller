"""Explicit external-browser attachment: disposable Chromium, never live profiles."""

import json
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from uc_browser.browser import BrowserMode, UCBrowser


def _request(endpoint, path, method="GET"):
    request = urllib.request.Request(endpoint + path, method=method)
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def _page_ids(endpoint):
    return {row["id"] for row in _request(endpoint, "/json/list") if row["type"] == "page"}


@pytest.fixture
def external_browser(tmp_path):
    # Discover the installed binary, then stop this driver before UC starts
    # its own sync Playwright instance on the same thread.
    with sync_playwright() as pw:
        executable = pw.chromium.executable_path
    profile = tmp_path / "external-profile"
    with (tmp_path / "chromium.log").open("w") as log:
        process = subprocess.Popen([
            executable, "--headless", "--no-sandbox", "--disable-gpu",
            "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            f"--user-data-dir={profile}", "about:blank",
        ], stdout=log, stderr=log)
        try:
            port_file = profile / "DevToolsActivePort"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None, "Disposable Chromium exited before CDP was ready"
                if port_file.exists():
                    lines = port_file.read_text().splitlines()
                    if len(lines) >= 2:
                        endpoint = f"http://127.0.0.1:{int(lines[0])}"
                        try:
                            _request(endpoint, "/json/version")
                        except OSError:
                            pass
                        else:
                            break
                time.sleep(0.05)
            else:
                pytest.fail("Disposable Chromium did not publish its CDP endpoint")
            yield endpoint, process
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def fixture_site():
    payload = {"items": ["fixture item " + str(i) for i in range(20)]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/api":
                body = json.dumps(payload).encode()
                content_type = "application/json"
            else:
                body = (
                    b"<p>Borrowed browser fixture</p><script>"
                    b"fetch('/api').then(r => r.json()).then(() => {"
                    b"document.body.dataset.loaded = 'yes';});</script>"
                )
                content_type = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("extra", [
    {"mode": BrowserMode.NATIVE_CDP},
    {"native_chrome": True}, {"native_chrome": False},
    {"headless": True}, {"headless": False},
    {"use_extension": True}, {"use_extension": False},
    {"channel": "chrome"}, {"stealth": True}, {"stealth": False},
    {"service": "http://127.0.0.1:4010", "window": "native"},
    {"window": "native"},
])
def test_attachment_rejects_launch_and_service_arguments(extra):
    with pytest.raises(ValueError):
        UCBrowser(cdp_endpoint="http://127.0.0.1:1", **extra)


@pytest.mark.parametrize("endpoint", ["", "  ", 123])
def test_attachment_requires_nonempty_explicit_endpoint(endpoint):
    with pytest.raises(ValueError, match="cdp_endpoint"):
        UCBrowser(cdp_endpoint=endpoint)


def test_refused_endpoint_is_named_and_failed_start_can_be_closed():
    # Reserve, but do not listen on, a port so another process cannot take it.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        uc = UCBrowser(cdp_endpoint=endpoint, timeout_ms=1000)
        with pytest.raises(RuntimeError) as error:
            uc.start()
        assert endpoint in str(error.value)
        assert not uc.is_alive()
        uc.close()
    # A failed attach must not strand a sync driver on this thread.
    with sync_playwright() as pw:
        assert Path(pw.chromium.executable_path).is_file()


def test_detach_preserves_browser_and_unowned_tabs(external_browser, fixture_site):
    endpoint, process = external_browser
    url, payload = fixture_site
    original = _page_ids(endpoint)
    assert original, "Fixture must contain a pre-existing user tab"
    uc = UCBrowser(cdp_endpoint=endpoint, label="isolated-client")
    with uc:
        blank = uc.new_page()
        opened = uc.open(url, wait_ms=0)
        intercepted, captured = uc.open_with_intercept(url, "/api$", wait_ms=0)
        intercepted.wait_for_function("document.body.dataset.loaded === 'yes'")
        assert json.loads(captured[0][1]) == payload
        assert uc.get_feed_text(opened) == "Borrowed browser fixture"
        # No stealth or API shim is installed into the borrowed context.
        assert opened.evaluate("typeof window.__UC_apiShim") == "undefined"
        assert opened.evaluate("navigator.webdriver") is True  # headless fixture, not stealth
        foreign = _request(endpoint, "/json/new?about:blank", method="PUT")["id"]
        with opened.expect_popup() as popup_event:
            opened.evaluate("window.open('about:blank')")
        popup = popup_event.value
        blank.close()  # caller-close before teardown remains safe
        with pytest.raises(RuntimeError, match="already started"):
            uc.start()
        assert uc.is_alive()
    uc.close()  # idempotent; never shuts down the external browser
    assert process.poll() is None
    assert _page_ids(endpoint) == original | {foreign}
    assert not uc.is_alive()
    # Reattachment proves disconnect, rather than remote Browser.close.
    with uc:
        assert uc.new_page().evaluate("1 + 1") == 2
    assert _page_ids(endpoint) == original | {foreign}


@pytest.mark.parametrize("intercept", [False, True])
def test_navigation_failure_does_not_leak_attached_tab(external_browser, intercept):
    endpoint, process = external_browser
    original = _page_ids(endpoint)
    with UCBrowser(cdp_endpoint=endpoint) as uc:
        with pytest.raises(PlaywrightError):
            if intercept:
                uc.open_with_intercept("not a valid URL", "/api", wait_ms=0)
            else:
                uc.open("not a valid URL", wait_ms=0)
        assert _page_ids(endpoint) == original
    assert process.poll() is None
    assert _page_ids(endpoint) == original
