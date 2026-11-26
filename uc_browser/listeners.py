"""One ASGI process on loopback plus explicitly selected IPv4 interfaces."""
from __future__ import annotations

import argparse
import ipaddress
import logging
import socket
from contextlib import ExitStack
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)


def _listen_address(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a literal IPv4 address") from exc
    if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("expected a specific unicast IPv4 address, not a wildcard")
    return str(address)


def add_listener_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--listen-address", action="append", default=[], type=_listen_address,
        metavar="IPv4",
        help="add a specific IPv4 listener on the same port; repeatable. "
             "127.0.0.1 is always retained. This adds no authentication.",
    )


def serve(app_factory: Callable[[], Any], *, port: int, addresses: Iterable[str]) -> None:
    """Bind all required addresses before importing/starting the application.

    A failed additional bind releases earlier sockets; there is no partial
    startup, wildcard fallback, second worker or alternate-port fallback.
    """
    import uvicorn

    hosts = list(dict.fromkeys(["127.0.0.1", *(_listen_address(a) for a in addresses)]))
    with ExitStack() as listeners:
        sockets = []
        for address in hosts:
            sock = listeners.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
            if port == 0:
                port = sock.getsockname()[1]
            sockets.append(sock)
            log.info("Listener bound to %s:%s", address, port)
        config = uvicorn.Config(app_factory, factory=True, host="127.0.0.1",
                                port=port, log_level="info", workers=1)
        uvicorn.Server(config).run(sockets=sockets)
