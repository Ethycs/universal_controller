"""Process-bound page leases. Browser objects stay on their window's owner."""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass

import psutil

from .errors import ServiceError

log = logging.getLogger(__name__)


def process_identity(pid: int) -> float | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process.create_time()
    except psutil.NoSuchProcess:
        return None


@dataclass(frozen=True)
class Client:
    pid: int
    start_time: float
    label: str
    window: str
    unattached: bool = False


class LeaseStore:
    def __init__(self, manager, *, identity=process_identity, interval: float = 5):
        self.manager = manager
        self.identity = identity
        self.interval = interval
        self.clients: dict[str, Client] = {}
        self.leases: dict[tuple[str, str], str] = {}
        # Pages owned by the service, not by a registered process: fetch pages
        # (holder None) and site sessions (holder "session:<site>/<id>").
        self.internal: dict[tuple[str, str], str | None] = {}
        self.generations: dict[str, int] = {}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="uc-reaper", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def register(self, pid: int, label: str, window: str, unattached: bool = False) -> str:
        target_window = self.manager.get(window)
        try:
            identity = self.identity(pid)
        except psutil.AccessDenied as exc:
            raise ServiceError("bad_pid", "process identity is not readable", 400) from exc
        if identity is None:
            raise ServiceError("bad_pid", "pid must name a live local process", 400)
        client_id = uuid.uuid4().hex
        with self.lock:
            if unattached:
                holders = [
                    f"{c.label or cid} (pid {c.pid})"
                    for cid, c in self.clients.items() if c.window == window
                ]
                if holders:
                    raise ServiceError(
                        "busy", f"window {window!r} has registered clients: {', '.join(holders)}", 409,
                    )
                target_window.begin_unattached(client_id)
            elif getattr(target_window.spec, "unattached", False):
                holders = [
                    f"{c.label or cid} (pid {c.pid})"
                    for cid, c in self.clients.items() if c.window == window and c.unattached
                ]
                if holders:
                    raise ServiceError(
                        "busy", f"window {window!r} is reserved unattached by {', '.join(holders)}", 409,
                    )
                target_window.assert_attach_allowed()
            self.clients[client_id] = Client(pid, identity, label, window, unattached)
        return client_id

    def _client(self, client_id: str, window: str) -> Client:
        client = self.clients.get(client_id)
        if client is None or client.window != window:
            raise ServiceError("no_such_client", "client is not registered on this window", 404)
        return client

    def _generation(self, window):
        name = window.spec.name
        generation = window.generation
        if self.generations.get(name) != generation:
            self.leases = {k: v for k, v in self.leases.items() if k[0] != name}
            self.internal = {k: v for k, v in self.internal.items() if k[0] != name}
            self.generations[name] = generation

    def is_unattached_client(self, name: str, client_id: str) -> bool:
        with self.lock:
            return self._client(client_id, name).unattached

    @staticmethod
    def _new_target(window, url: str | None = None, *, launch: bool = True) -> str:
        if getattr(window.spec, "unattached", False):
            return window.owner(
                lambda rt: rt.new_page(url if url is not None else "about:blank"),
                launch=launch,
            ).result()
        return window.owner(
            lambda rt: rt.target_id(rt.context.new_page()), launch=launch,
        ).result()

    def create_page(self, name: str, client_id: str, url: str | None = None) -> str:
        window = self.manager.get(name)
        # Keep creation and lease assignment atomic against release/revocation.
        with self.lock:
            client = self._client(client_id, name)
            if url is not None and not client.unattached:
                raise ServiceError("bad_arguments", "direct URL creation requires an unattached client", 400)
            window.start()
            self._generation(window)
            target = self._new_target(window, url)
            self.leases[name, target] = client_id
            return target

    def internal_page(self, name: str, holder: str | None = None) -> str:
        """A service-owned page, kept by the zero-client sweep. ``holder``
        names its owner in ``/windows`` (``session:<site>/<id>``); fetch pages
        leave it ``None``."""
        window = self.manager.get(name)
        with self.lock:
            if getattr(window.spec, "unattached", False):
                window.assert_attach_allowed()
            window.start()
            self._generation(window)
            target = self._new_target(window)
            self.internal[name, target] = holder
            return target

    def close_page(self, name: str, target: str) -> bool:
        window = self.manager.get(name)
        with self.lock:
            if not window.running:
                raise ServiceError("no_such_target", "window is not running", 404)
            try:
                window.owner(lambda rt: rt.close_target(target), launch=False).result()
            except ServiceError as exc:
                if exc.code == "no_such_target":
                    # Already closed elsewhere: its lease is meaningless, answer as before.
                    self.leases.pop((name, target), None)
                    self.internal.pop((name, target), None)
                raise
            self.leases.pop((name, target), None)
            self.internal.pop((name, target), None)
            return True

    def _close_targets(self, window, targets) -> int:
        if not window.running:
            return 0
        def close(rt):
            count = 0
            for target in targets:
                try:
                    rt.close_target(target)
                    count += 1
                except ServiceError as exc:
                    if exc.code != "no_such_target":
                        raise
            return count
        return window.owner(close, launch=False).result()

    def release(self, client_id: str) -> int:
        with self.lock:
            client = self.clients.get(client_id)
            if client is None:
                raise ServiceError("no_such_client", "unknown client", 404)
            targets = [t for (w, t), holder in self.leases.items() if holder == client_id]
            window = self.manager.get(client.window)
            count = self._close_targets(window, targets)
            del self.clients[client_id]
            self.leases = {k: v for k, v in self.leases.items() if v != client_id}
            if client.unattached:
                window.end_unattached(client_id)
            return count

    def revoke(self, label: str) -> dict:
        with self.lock:
            ids = [i for i, c in self.clients.items() if c.label == label]
            return {"clients": len(ids), "closed": sum(self.release(i) for i in ids)}

    def window_info(self, name: str) -> dict:
        window = self.manager.get(name)
        with self.lock:
            info = window.info()
            self._generation(window)
            info["clients"] = sum(c.window == name for c in self.clients.values())
            for page in info["pages"]:
                page["holder"] = (self.leases.get((name, page["target_id"]))
                                  or self.internal.get((name, page["target_id"])))
            return info

    def reap(self):
        with self.lock:
            for client_id, client in list(self.clients.items()):
                try:
                    current = self.identity(client.pid)
                except psutil.AccessDenied:
                    # Inability to inspect is not evidence of process death.
                    continue
                if current != client.start_time:
                    self.release(client_id)
            for name, window in self.manager.windows.items():
                self._generation(window)
                # Never prune leases from Window.info's timeout/error fallback.
                # An enumeration failure must preserve ownership for the next pass.
                pages = (window.owner(lambda rt: rt.pages_info(), launch=False).result()
                         if window.running else [])
                live = {p["target_id"] for p in pages}
                self.leases = {k: v for k, v in self.leases.items() if k[0] != name or k[1] in live}
                self.internal = {k: v for k, v in self.internal.items() if k[0] != name or k[1] in live}
                if not window.running or any(c.window == name for c in self.clients.values()):
                    continue
                orphaned = [p for p in pages
                            if (name, p["target_id"]) not in self.leases
                            and (name, p["target_id"]) not in self.internal]
                blank = next((p for p in orphaned if p["url"] == "about:blank"), None)
                if blank is None:
                    # Keep one blank target before closing the last real tab.
                    self._new_target(window, launch=False)
                self._close_targets(window, [p["target_id"] for p in orphaned if p is not blank])

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.reap()
            except Exception:
                log.exception("lease reaper failed")
