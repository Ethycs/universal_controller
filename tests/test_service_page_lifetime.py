"""Explicit page lifetimes on disposable native Chrome, never a live profile."""
import os
import signal
from urllib.parse import urlsplit

import httpx
import pytest

from test_service_native_migration import eventually, native_service, native_site
from uc_browser import UCBrowser
from uc_browser.service.errors import ServiceError
from uc_browser.service.profiles import established_clients, profile_holder


def missing(client, target):
    with pytest.raises(ServiceError) as error:
        client.page_info("native", target)
    assert error.value.code == "no_such_target"


def quiet(client, label="lifetime-test"):
    cid = client.register_client("native", label=label, unattached=True)
    client.start_window("native")
    return cid


@pytest.mark.parametrize("ending", ["close", "revoke", "death"])
def test_retained_page_outlives_client_without_blocking_quiet(native_service, native_site, ending):
    client, manager, leases, _ = native_service
    url, _ = native_site
    cid = quiet(client)
    retained = client.new_page("native", cid, url=url)
    ordinary = client.new_page("native", cid, url="about:blank")
    eventually(lambda: client.page_info("native", retained)["title"] == "Native fixture ready")
    result = client.set_page_lifetime("native", retained, cid, "window")
    assert result["holder"] == "window"
    if ending == "close":
        client.close_client(cid)
    elif ending == "revoke":
        assert client.revoke("lifetime-test") == {"clients": 1, "closed": 1}
    else:
        identity = leases.identity
        leases.identity = lambda _: None
        try:
            leases.reap()
        finally:
            leases.identity = identity
    leases.reap()
    missing(client, ordinary)
    assert client.page_info("native", retained)["url"] == url
    info = client.windows()["native"]
    assert next(p for p in info["pages"] if p["target_id"] == retained)["holder"] == "window"
    assert established_clients(urlsplit(info["cdp"]).port) == []
    next_client = quiet(client, "another-preparer")
    client.close_client(next_client)
    client.close_page("native", retained)
    leases.reap()
    missing(client, retained)
    assert ("native", retained) not in leases.internal


def test_window_lifetime_creation_can_be_reassigned_to_a_client(native_service, native_site):
    client, _, leases, _ = native_service
    url, _ = native_site
    cid = quiet(client)
    target = client.new_page("native", cid, url=url, lifetime="window")
    client.close_client(cid)
    leases.reap()
    new_client = quiet(client)
    result = client.set_page_lifetime("native", target, new_client, "client")
    assert result["holder"] == new_client
    assert client.close_client(new_client) == {"closed": 1}
    missing(client, target)


def test_lifetime_cannot_steal_pages_or_mutate_on_invalid_requests(native_service):
    client, manager, leases, _ = native_service
    client.start_window("native")
    first = client.register_client("native", label="first")
    second = client.register_client("native", label="second")
    target = client.new_page("native", first)
    internal = leases.internal_page("native", holder="session:test/example")
    for protected in (target, internal):
        with pytest.raises(ServiceError) as error:
            client.set_page_lifetime("native", protected, second, "window")
        assert error.value.code == "page_not_owned"
    endpoint = client.url + f"/windows/native/pages/{target}/lifetime"
    assert httpx.put(endpoint, json={"client_id": first, "lifetime": "forever"}).status_code == 422
    before = {p["target_id"] for p in client.windows()["native"]["pages"]}
    assert httpx.post(client.url + "/windows/native/pages", json={
        "client_id": first, "lifetime": "forever",
    }).status_code == 422
    assert {p["target_id"] for p in client.windows()["native"]["pages"]} == before
    assert leases.leases["native", target] == first
    assert leases.internal["native", internal] == "session:test/example"
    client.close_page("native", target)
    with pytest.raises(ServiceError) as error:
        client.set_page_lifetime("native", target, first, "window")
    assert error.value.code == "no_such_target"
    client.close_client(first)
    client.close_client(second)
    client.close_page("native", internal)


def test_browser_window_lifetime_survives_detach_but_not_browser_restart(native_service, native_site):
    client, manager, leases, profile = native_service
    url, _ = native_site
    with UCBrowser(service=client.url, window="native", label="retaining-browser") as browser:
        page = browser.new_page(lifetime="window")
        page.goto(url)
        ordinary = browser.new_page()
        ordinary.goto(url + "-ordinary")
        targets = client.windows()["native"]["pages"]
        target = next(p["target_id"] for p in targets if p["url"] == url)
        temporary = next(p["target_id"] for p in targets if p["url"] == url + "-ordinary")
    leases.reap()
    assert client.page_info("native", target)["url"] == url
    missing(client, temporary)
    # Kill only this fixture's browser. A new generation must not restore its tab.
    pid = profile_holder(profile)
    assert pid is not None
    os.kill(pid, signal.SIGTERM)
    eventually(lambda: not manager.get("native").running)
    client.start_window("native")
    leases.reap()
    info = client.windows()["native"]
    assert all(p["url"] != url and p["holder"] != "window" for p in info["pages"])
    assert ("native", target) not in leases.internal


def test_failed_navigation_and_resolution_do_not_leave_retained_tabs(native_service, monkeypatch):
    client, _, leases, _ = native_service
    client.start_window("native")
    cid = client.register_client("native")
    before = {p["target_id"] for p in client.windows()["native"]["pages"]}
    with pytest.raises(ServiceError) as error:
        client.new_page("native", cid, url="invalid-protocol://fixture", lifetime="window")
    assert error.value.code == "navigation_failed"
    assert {p["target_id"] for p in client.windows()["native"]["pages"]} == before
    client.close_client(cid)
    with UCBrowser(service=client.url, window="native") as browser:
        def failed_resolution(target):
            raise RuntimeError("target resolution failed")
        monkeypatch.setattr(browser._targets, "resolve", failed_resolution)
        with pytest.raises(RuntimeError, match="target resolution failed"):
            browser.new_page(lifetime="window")
        assert {p["target_id"] for p in client.windows()["native"]["pages"]} == before
    assert "window" not in leases.internal.values()


@pytest.mark.parametrize("kwargs", [{}, {"cdp_endpoint": "http://127.0.0.1:1"}])
def test_nonservice_persistence_rejected_before_page_creation(kwargs):
    browser = UCBrowser(**kwargs)
    class Context:
        def new_page(self):
            pytest.fail("invalid persistence must not create a tab")
    browser._context = Context()
    with pytest.raises(ValueError, match="requires a UC service window"):
        browser.new_page(lifetime="window")
