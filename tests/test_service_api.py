"""Lease ownership and HTTP contract edges, with no browser or live profile."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from uc_browser.service.errors import ServiceError
from uc_browser.service.leases import LeaseStore
from uc_browser.service.operations import Operations, origin


class Page:
    def __init__(self, rt, target):
        self.rt, self.target = rt, target
        self.url = "about:blank"

    def close(self):
        self.rt.pages.pop(self.target)

    def content(self):
        return "<html><body>fixture</body></html>"

    def screenshot(self, **kwargs):
        return b"\x89PNG\r\n\x1a\n"


class Runtime:
    def __init__(self):
        self.context = self
        self.pages = {}
        self.jar = []
        self.seq = 0

    def new_page(self):
        self.seq += 1
        page = Page(self, str(self.seq))
        self.pages[page.target] = page
        return page

    def target_id(self, page):
        return page.target

    def resolve(self, target):
        if target not in self.pages:
            raise ServiceError("no_such_target", "closed", 404)
        return self.pages[target]

    def add_cookies(self, cookies):
        self.jar.extend(cookies)

    def cookies(self):
        return list(self.jar)


class Window:
    def __init__(self, name):
        self.spec = SimpleNamespace(name=name, browser="chromium", engine="extension")
        self.running = False
        self.generation = 0
        self.rt = Runtime()

    def start(self):
        if not self.running:
            self.running = True
            self.generation += 1
        return self.info()

    def owner(self, fn):
        self.start()
        future = Future()
        try:
            future.set_result(fn(self.rt))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def work(self, fn, **kwargs):
        return self.owner(fn)

    def info(self):
        return {"running": self.running, "cdp": None,
                "browser": self.spec.browser, "engine": self.spec.engine,
                "offers": [], "pages": [{"target_id": p.target, "url": p.url, "age_s": 0}
                                        for p in self.rt.pages.values()]}

    def control(self, *args):
        raise ServiceError("no_controls", "disabled", 409)


class Manager:
    def __init__(self):
        self.windows = {n: Window(n) for n in ("a", "b")}

    def get(self, name):
        if name not in self.windows:
            raise ServiceError("no_such_window", "unknown", 404)
        return self.windows[name]

    def missing_engine_files(self):
        return []

    def start_eager(self):
        pass

    def close(self):
        pass


@pytest.fixture
def state():
    manager = Manager()
    processes = {101: 1.0, 102: 2.0}
    leases = LeaseStore(manager, identity=processes.get)
    return manager, leases, processes


def test_dead_and_reused_pids_release_only_their_pages(state):
    manager, leases, processes = state
    dead = leases.register(101, "job", "a")
    live = leases.register(102, "other", "a")
    gone = leases.create_page("a", dead)
    kept = leases.create_page("a", live)
    processes[101] = 99.0  # Same pid now identifies a different process.
    leases.reap()
    assert dead not in leases.clients
    assert gone not in manager.get("a").rt.pages
    assert kept in manager.get("a").rt.pages
    del processes[102]
    leases.reap()
    assert not leases.clients
    assert kept not in manager.get("a").rt.pages
    assert [p.url for p in manager.get("a").rt.pages.values()] == ["about:blank"]


def test_label_revocation_spans_windows_without_releasing_other_labels(state):
    manager, leases, _ = state
    clients = [leases.register(101, "job", w) for w in ("a", "b")]
    other = leases.register(102, "other", "a")
    for c, w in zip(clients, ("a", "b")):
        leases.create_page(w, c)
    kept = leases.create_page("a", other)
    assert leases.revoke("job") == {"clients": 2, "closed": 2}
    assert leases.revoke("job") == {"clients": 0, "closed": 0}
    assert kept in manager.get("a").rt.pages


def test_sweep_preserves_raw_pages_while_client_alive_and_internal_pages_after(state):
    manager, leases, _ = state
    client = leases.register(101, "job", "a")
    window = manager.get("a")
    raw = window.owner(lambda rt: rt.target_id(rt.new_page())).result()
    window.rt.pages[raw].url = "https://fixture.test/"
    internal = leases.internal_page("a")
    leases.reap()
    assert raw in window.rt.pages
    leases.release(client)
    leases.reap()
    assert raw not in window.rt.pages
    assert internal in window.rt.pages


def test_client_cannot_lease_page_on_another_window(state):
    manager, leases, _ = state
    client = leases.register(101, "job", "a")
    with pytest.raises(ServiceError, match="not registered"):
        leases.create_page("b", client)
    assert not manager.get("b").running


def test_crash_generation_drops_old_leases(state):
    manager, leases, _ = state
    client = leases.register(101, "job", "a")
    target = leases.create_page("a", client)
    window = manager.get("a")
    window.running = False
    window.rt.pages.clear()
    window.start()
    leases.window_info("a")
    assert ("a", target) not in leases.leases


def test_cookie_domain_filter_does_not_leak_siblings(state):
    manager, leases, _ = state
    ops = Operations(manager, leases)
    ops.add_cookies("a", cookies=[
        {"name": "shared", "value": "1", "domain": ".example.test", "path": "/"},
        {"name": "other", "value": "2", "domain": "other.example.test", "path": "/"},
        {"name": "unrelated", "value": "3", "domain": ".unrelated.test", "path": "/"},
    ])
    assert [c["name"] for c in ops.cookies("a", "app.example.test")["cookies"]] == ["shared"]
    with pytest.raises(ServiceError) as exc:
        ops.cookies("a", None)
    assert exc.value.code == "domain_required"
    assert not leases.clients


def test_snapshot_formats_and_non_cdp_mhtml(state):
    manager, leases, _ = state
    ops = Operations(manager, leases)
    target = leases.internal_page("a")
    assert ops.snapshot("a", target, "html")["data"] == "<html><body>fixture</body></html>"
    assert ops.snapshot("a", target, "png")["data"] == "iVBORw0KGgo="
    manager.get("a").spec.browser = "firefox"
    with pytest.raises(ServiceError) as exc:
        ops.snapshot("a", target, "mhtml")
    assert (exc.value.status, exc.value.code) == (409, "format_unsupported")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@example.test/", "https://example.test:bad/"])
def test_fetch_origin_rejects_non_http_or_credential_urls(url):
    with pytest.raises(ServiceError) as exc:
        origin(url)
    assert exc.value.code == "origin_unreachable"


def test_http_errors_and_independent_cookie_access(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uc_browser.registry import SiteRegistry
    from uc_browser.health import HealthMonitor, HealthStore
    from uc_browser.status_server import create_app
    manager = Manager()
    app = create_app(registry=SiteRegistry(path=tmp_path / "sites.json"),
                     monitor=HealthMonitor(store=HealthStore(base_dir=tmp_path / "health")),
                     run_scheduler=False, with_mcp=False, window_manager=manager)
    with TestClient(app) as client:
        assert client.get("/windows/a/cookies").json()["error"] == "domain_required"
        assert client.post("/windows/missing/start").status_code == 404
        assert client.post("/clients", json={"pid": -1, "window": "a"}).json()["error"] == "bad_pid"
        assert client.post("/windows/a/pages/x/controls/_wait_ready", json={}).json()["error"] == "no_such_control"
        assert client.post("/windows/a/pages/x/controls/detect", json={}).status_code == 400
        assert client.post("/windows/a/pages/x/controls/detect_all", json={}).json()["error"] == "no_controls"
        assert client.post("/windows/a/cookies", json={"cookies": []}).json() == {"added": 0}
        assert client.get("/windows").json()["a"]["clients"] == 0
        assert client.post("/windows/a/cookies", json={}).status_code == 400
        monkeypatch.setitem(__import__("sys").modules, "rookiepy", None)
        assert client.post("/windows/a/cookies", json={"from_chrome": ["example.test"]}).status_code == 501


@pytest.mark.parametrize("missing", [True, False])
def test_service_startup_failure_closes_managers(tmp_path, missing):
    from fastapi.testclient import TestClient
    from uc_browser.registry import SiteRegistry
    from uc_browser.health import HealthMonitor, HealthStore
    from uc_browser.status_server import create_app
    manager = Manager()
    closed = []
    manager.close = lambda: closed.append(True)
    manager.missing_engine_files = lambda: ["a"] if missing else []
    manager.start_eager = lambda: {"a": ServiceError("profile_held", "held", 409)}
    app = create_app(registry=SiteRegistry(path=tmp_path / "sites.json"),
                     monitor=HealthMonitor(store=HealthStore(base_dir=tmp_path / "health")),
                     run_scheduler=False, with_mcp=False, window_manager=manager)
    with pytest.raises(ServiceError) as exc:
        with TestClient(app):
            pytest.fail("startup should refuse an unusable configured window")
    assert exc.value.code == ("launch_failed" if missing else "profile_held")
    assert closed == [True]


def test_flattened_chrome_title_does_not_hide_held_profile(tmp_path, monkeypatch):
    from uc_browser.service import profiles
    profile = tmp_path / "profile with spaces"
    monkeypatch.setattr(profiles, "_cmdline_args",
                        lambda pid: [f"/opt/google/chrome/chrome --user-data-dir={profile} --no-first-run"])
    assert profiles._cmdline_names_profile(101, profile) is True
    # Ambiguous process titles must not authorize deleting a live lock.
    assert profiles._cmdline_names_profile(101, tmp_path / "other") is None
