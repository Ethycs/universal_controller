"""Real sockets: explicit interfaces share one app, with no wildcard fallback."""
import argparse
import socket
import sys
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from uc_browser.listeners import serve


@pytest.mark.skipif(sys.platform != "linux", reason="uses Linux loopback aliases")
@pytest.mark.parametrize("addresses", [[], ["127.0.0.2", "127.0.0.2", "127.0.0.1"]])
def test_explicit_listeners_share_one_app_and_never_bind_wildcard(monkeypatch, addresses):
    servers, errors = [], []
    real_server = uvicorn.Server

    def capture_server(config):
        server = real_server(config)
        servers.append(server)
        return server

    monkeypatch.setattr(uvicorn, "Server", capture_server)

    def application():
        app = FastAPI()
        app.state.requests = 0

        @app.get("/healthz")
        def healthz():
            app.state.requests += 1
            return {"requests": app.state.requests}

        return app

    def run():
        try:
            serve(application, port=0, addresses=addresses)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not servers or not servers[0].started:
            if errors:
                raise errors[0]
            assert time.monotonic() < deadline, "listener startup did not finish"
            time.sleep(.01)
        port = servers[0].config.port
        with httpx.Client(trust_env=False, timeout=2) as client:
            assert client.get(f"http://127.0.0.1:{port}/healthz").json() == {"requests": 1}
            if addresses:
                assert client.get(f"http://127.0.0.2:{port}/healthz").json() == {"requests": 2}
            else:
                with pytest.raises(httpx.ConnectError):
                    client.get(f"http://127.0.0.2:{port}/healthz")
            with pytest.raises(httpx.ConnectError):
                client.get(f"http://127.0.0.3:{port}/healthz")
    finally:
        if servers:
            servers[0].should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors


@pytest.mark.skipif(sys.platform != "linux", reason="uses Linux loopback aliases")
def test_failed_additional_bind_releases_loopback_and_never_starts_app():
    def forbidden_application():
        pytest.fail("application must not start before every address is bound")

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.2", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        with pytest.raises(OSError):
            serve(forbidden_application, port=port, addresses=["127.0.0.2"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


@pytest.mark.parametrize("address", ["0.0.0.0", "::", "localhost", "224.0.0.1", "255.255.255.255"])
def test_unsafe_or_nonliteral_listener_is_rejected_before_app_start(address):
    def forbidden_application():
        pytest.fail("invalid interface must not start the app")

    with pytest.raises(argparse.ArgumentTypeError):
        serve(forbidden_application, port=0, addresses=[address])
