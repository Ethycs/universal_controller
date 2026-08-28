"""Tests for the site registry, health store/uptime math, availability
snapshot, status server endpoints, and the litellm availability gate.

No browser is launched anywhere here — probes are stubbed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from uc_browser.health import (
    STATUS_DOWN,
    STATUS_LOGIN,
    STATUS_OK,
    HealthMonitor,
    HealthStore,
    ProbeResult,
    availability_snapshot,
)
from uc_browser.registry import SiteEntry, SiteRegistry


# ── registry ─────────────────────────────────────────────────────────


def _fresh_registry(tmp_path: Path) -> SiteRegistry:
    return SiteRegistry(path=tmp_path / "sites.json")


def test_registry_lists_builtins(tmp_path):
    reg = _fresh_registry(tmp_path)
    names = {e.name for e in reg.list()}
    assert "grok" in names and "chatgpt" in names
    assert reg.get("grok").litellm_model == "uc/grok"


def test_registry_add_persists_and_reloads(tmp_path):
    reg = _fresh_registry(tmp_path)
    reg.add("mysite", "https://chat.example.com", notes="internal helpdesk")
    reloaded = _fresh_registry(tmp_path)
    entry = reloaded.get("mysite")
    assert entry is not None
    assert entry.source == "user"
    assert entry.url == "https://chat.example.com"


def test_registry_add_validates(tmp_path):
    reg = _fresh_registry(tmp_path)
    with pytest.raises(ValueError):
        reg.add("Bad Name!", "https://x.example")
    with pytest.raises(ValueError):
        reg.add("okname", "ftp://x.example")
    with pytest.raises(ValueError):
        reg.add("okname", "https://x.example", kind="blog")


def test_registry_user_entry_overrides_builtin(tmp_path):
    reg = _fresh_registry(tmp_path)
    reg.add("chatgpt", "https://chatgpt.com/custom", notes="tweaked")
    assert reg.get("chatgpt").url == "https://chatgpt.com/custom"
    # Still exactly one chatgpt in the listing.
    assert sum(1 for e in reg.list() if e.name == "chatgpt") == 1


def test_registry_search_and_remove(tmp_path):
    reg = _fresh_registry(tmp_path)
    reg.add("helpdesk", "https://support.example.com", notes="acme widget")
    assert any(e.name == "helpdesk" for e in reg.search("acme"))
    assert reg.remove("helpdesk") is True
    assert reg.remove("grok") is False  # builtin: not removable
    assert reg.get("helpdesk") is None


# ── health store / uptime ────────────────────────────────────────────


def _rec(store: HealthStore, site: str, status: str, age_s: float):
    store.record(ProbeResult(site=site, status=status, level_reached="detect",
                             latency_ms=100, at=time.time() - age_s))


def test_uptime_math_and_windows(tmp_path):
    store = HealthStore(base_dir=tmp_path)
    _rec(store, "s", STATUS_OK, 3600)
    _rec(store, "s", STATUS_DOWN, 1800)
    _rec(store, "s", STATUS_OK, 60)
    assert store.uptime("s", 24 * 3600) == pytest.approx(2 / 3)
    # Narrow window only sees the newest probe.
    assert store.uptime("s", 600) == 1.0
    assert store.uptime("nonexistent") is None


def test_uptime_ignores_login_and_unknown(tmp_path):
    store = HealthStore(base_dir=tmp_path)
    _rec(store, "s", STATUS_LOGIN, 300)
    assert store.uptime("s") is None  # login says nothing about uptime
    _rec(store, "s", STATUS_OK, 60)
    assert store.uptime("s") == 1.0


def test_latest_tracks_newest_per_site(tmp_path):
    store = HealthStore(base_dir=tmp_path)
    _rec(store, "a", STATUS_DOWN, 500)
    _rec(store, "a", STATUS_OK, 5)
    _rec(store, "b", STATUS_DOWN, 5)
    assert store.latest("a")["status"] == STATUS_OK
    assert store.latest("b")["status"] == STATUS_DOWN
    assert set(store.latest()) == {"a", "b"}


# ── availability snapshot ────────────────────────────────────────────


def test_availability_fresh_vs_stale(tmp_path):
    reg = _fresh_registry(tmp_path)
    store = HealthStore(base_dir=tmp_path / "health")
    _rec(store, "grok", STATUS_OK, 60)              # fresh (interval 900)
    _rec(store, "chatgpt", STATUS_OK, 10 * 3600)    # stale
    snap = availability_snapshot(reg, store)
    by_name = {s["name"]: s for s in snap["sites"]}
    assert by_name["grok"]["available"] is True
    assert by_name["grok"]["callable"] is True
    assert "uc/grok" in snap["models_available"]
    assert by_name["chatgpt"]["status"] == "unknown"  # stale → no opinion
    assert by_name["chatgpt"]["available"] is False
    # chatgpt has no litellm model wired yet, so never callable.
    assert by_name["chatgpt"]["callable"] is False


# ── status server endpoints ──────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from uc_browser.status_server import create_app

    reg = _fresh_registry(tmp_path)
    store = HealthStore(base_dir=tmp_path / "health")
    monitor = HealthMonitor(store=store)

    def fake_probe(entry: SiteEntry) -> ProbeResult:
        result = ProbeResult(site=entry.name, status=STATUS_OK,
                             level_reached="detect", latency_ms=42,
                             at=time.time())
        store.record(result)
        return result

    monkeypatch.setattr(monitor, "probe_site", fake_probe)
    app = create_app(registry=reg, monitor=monitor,
                     run_scheduler=False, with_mcp=True)
    with fastapi_testclient.TestClient(app) as c:
        yield c


def test_availability_endpoint(client):
    data = client.get("/availability").json()
    assert "sites" in data and "models_available" in data
    assert any(s["name"] == "grok" for s in data["sites"])


def test_uptime_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Site Status" in resp.text
    assert "grok" in resp.text


def test_force_probe_endpoint(client):
    out = client.post("/probe/grok").json()
    assert out["site"] == "grok" and out["status"] == STATUS_OK
    assert client.post("/probe/nope").status_code == 404
    # The forced probe is now reflected in availability.
    data = client.get("/availability").json()
    grok = next(s for s in data["sites"] if s["name"] == "grok")
    assert grok["available"] is True


# ── MCP site tools ───────────────────────────────────────────────────


def test_mcp_site_tools_register_and_run(tmp_path, monkeypatch):
    mcp_mod = pytest.importorskip("mcp.server.fastmcp")
    from uc_browser.mcp.site_tools import register_site_tools

    monkeypatch.setenv("UC_SITES_FILE", str(tmp_path / "sites.json"))
    monkeypatch.setenv("UC_HEALTH_DIR", str(tmp_path / "health"))
    import uc_browser.registry as reg_mod
    monkeypatch.setattr(reg_mod, "_registry", None)

    store = HealthStore(base_dir=tmp_path / "health")
    monitor = HealthMonitor(store=store)
    def fake_probe(entry):
        result = ProbeResult(site=entry.name, status=STATUS_OK,
                             level_reached="detect", latency_ms=1,
                             at=time.time())
        store.record(result)
        return result

    monkeypatch.setattr(monitor, "probe_site", fake_probe)

    server = mcp_mod.FastMCP("test", stateless_http=True)
    register_site_tools(server, monitor=monitor)
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"uc_site_list", "uc_site_search", "uc_site_add",
            "uc_site_remove", "uc_site_probe", "uc_availability"} <= tools

    def call(name, args):
        """Normalize call_tool's return across SDK versions: it may be
        (content, structured) or just content blocks."""
        out = asyncio.run(server.call_tool(name, args))
        if isinstance(out, tuple) and len(out) == 2:
            return out[1]
        blocks = out if isinstance(out, list) else [out]
        text = next(b.text for b in blocks if hasattr(b, "text"))
        return json.loads(text)

    # add → probe happens (stubbed) → searchable
    call("uc_site_add", {
        "name": "acme", "url": "https://chat.acme.example", "notes": "test"})
    result = call("uc_site_search", {"query": "acme"})
    assert any(s["name"] == "acme" for s in result["sites"])
    listing = call("uc_site_list", {})
    assert any(s["name"] == "acme" and s["status"] == STATUS_OK
               for s in listing["sites"])


# ── litellm availability gate ────────────────────────────────────────


def test_provider_gate_blocks_down_site(tmp_path, monkeypatch):
    litellm = pytest.importorskip("litellm")
    from uc_browser.llm_providers.uc import _check_availability

    monkeypatch.setenv("UC_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.delenv("UC_AVAILABILITY_GATE", raising=False)
    store = HealthStore(base_dir=tmp_path / "health")
    _rec(store, "grok", STATUS_DOWN, 60)  # fresh and down

    with pytest.raises(litellm.exceptions.ServiceUnavailableError):
        _check_availability("grok", "grok", {})

    # Per-request override lets it through.
    _check_availability("grok", "grok",
                        {"extra_body": {"ignore_availability": True}})
    # Kill switch lets it through.
    monkeypatch.setenv("UC_AVAILABILITY_GATE", "0")
    _check_availability("grok", "grok", {})


def test_provider_gate_ignores_stale_and_missing(tmp_path, monkeypatch):
    pytest.importorskip("litellm")
    from uc_browser.llm_providers.uc import _check_availability

    monkeypatch.setenv("UC_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.delenv("UC_AVAILABILITY_GATE", raising=False)
    # No data at all → no opinion.
    _check_availability("grok", "grok", {})
    # Stale 'down' (older than 2x interval=900s) → no opinion.
    store = HealthStore(base_dir=tmp_path / "health")
    _rec(store, "grok", STATUS_DOWN, 3 * 3600)
    _check_availability("grok", "grok", {})
