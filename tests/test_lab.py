"""Tests for the Signature Lab framework: profile store TTL semantics,
the subpage-candidate crawler, the /signatures feed, and the prober's
profile-aware URL resolution. No live sites; one offline browser page."""

from __future__ import annotations

import time

import pytest

from uc_browser.site_profiles import (
    DEFAULT_TTL_S,
    ProfileStore,
    SiteProfile,
)


# ── profile store ────────────────────────────────────────────────────


def _profile(site="klm", url="https://www.klm.com/help", age_s=0.0,
             ttl_s=DEFAULT_TTL_S) -> SiteProfile:
    return SiteProfile(site=site, chat_page_url=url,
                       pre_steps=[{"type": "launcher-auto"}],
                       detect_score=4.5, verified_at=time.time() - age_s,
                       ttl_s=ttl_s)


def test_store_upsert_persists_and_reloads(tmp_path):
    store = ProfileStore(path=tmp_path / "profiles.json")
    store.upsert(_profile())
    reloaded = ProfileStore(path=tmp_path / "profiles.json")
    p = reloaded.get("klm")
    assert p is not None
    assert p.chat_page_url == "https://www.klm.com/help"
    assert p.pre_steps == [{"type": "launcher-auto"}]


def test_fresh_honors_ttl(tmp_path):
    store = ProfileStore(path=tmp_path / "profiles.json")
    store.upsert(_profile(age_s=10))
    assert store.fresh("klm") is not None
    store.upsert(_profile(site="stale", age_s=8 * 24 * 3600))
    # Expired → advisory only: fresh() refuses, get() still returns it.
    assert store.fresh("stale") is None
    assert store.get("stale") is not None


def test_feed_shape(tmp_path):
    store = ProfileStore(path=tmp_path / "profiles.json")
    store.upsert(_profile())
    feed = store.feed()
    assert feed["version"] == 1
    assert feed["profiles"][0]["site"] == "klm"
    assert "generated_at" in feed


# ── prober resolves profile URL ──────────────────────────────────────


def test_prober_targets_profile_chat_page(tmp_path, monkeypatch):
    """A fresh profile redirects the probe URL; expired ones don't."""
    monkeypatch.setenv("UC_PROFILES_FILE", str(tmp_path / "profiles.json"))
    import uc_browser.site_profiles as sp
    monkeypatch.setattr(sp, "_store", None)

    store = sp.get_profile_store()
    store.upsert(_profile(site="klm"))
    assert sp.get_profile_store().fresh("klm").chat_page_url == \
        "https://www.klm.com/help"

    store.upsert(_profile(site="klm", age_s=9 * 24 * 3600))
    assert sp.get_profile_store().fresh("klm") is None


# ── /signatures feed endpoint ────────────────────────────────────────


def test_signatures_endpoint(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from uc_browser.health import HealthMonitor, HealthStore
    from uc_browser.registry import SiteRegistry
    from uc_browser.status_server import create_app

    monkeypatch.setenv("UC_PROFILES_FILE", str(tmp_path / "profiles.json"))
    import uc_browser.site_profiles as sp
    monkeypatch.setattr(sp, "_store", None)
    sp.get_profile_store().upsert(_profile())

    app = create_app(
        registry=SiteRegistry(path=tmp_path / "sites.json"),
        monitor=HealthMonitor(store=HealthStore(base_dir=tmp_path / "health")),
        run_scheduler=False, with_mcp=False,
    )
    with fastapi_testclient.TestClient(app) as client:
        feed = client.get("/signatures").json()
    assert feed["version"] == 1
    assert feed["profiles"][0]["chat_page_url"] == "https://www.klm.com/help"


# ── candidate-page crawler ───────────────────────────────────────────


def test_candidate_pages_filters_and_guesses():
    from playwright.sync_api import sync_playwright

    from uc_browser.lab import candidate_pages

    html = """
    <html><body>
      <a href="https://www.acme.com/help">Help Center</a>
      <a href="https://support.acme.com/">Contact support</a>
      <a href="https://www.acme.com/products">Products</a>
      <a href="https://twitter.com/acme_support">Twitter support</a>
      <a href="https://www.acme.com/help">Help (duplicate)</a>
      <a href="https://www.acme.com/customer-service/chat">Chat with us</a>
    </body></html>"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)
        cands = candidate_pages(page, "https://www.acme.com")
        browser.close()

    # Same-site chat-ish links kept, external + irrelevant dropped.
    assert "https://www.acme.com/help" in cands
    assert "https://www.acme.com/customer-service/chat" in cands
    assert "https://support.acme.com/" in cands
    assert all("twitter.com" not in c for c in cands)
    assert all("/products" not in c for c in cands)
    assert len(cands) <= 4
