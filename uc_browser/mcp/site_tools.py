"""MCP tool registrations for the site registry + availability.

Lets an MCP client (or its LLM) manage which sites UC watches and query
the live availability menu — the same data the litellm proxy serves at
``/uc/availability``.

Tools:
    uc_site_list      registry merged with live health
    uc_site_search    substring search over name/url/notes
    uc_site_add       register a new site (persisted; optional probe now)
    uc_site_remove    remove a user-added site
    uc_site_probe     probe one site immediately
    uc_availability   the live menu (currently-callable litellm models)

The current flow is registry-driven: a human or agent names a site and a
URL, UC probes it with the generic engine, and availability falls out of
the health history. Adaptive/agentic onboarding strategies (self-serve
discovery, auto-verification, signature learning) are deliberately NOT
implemented here yet — see the project notes.
"""

from __future__ import annotations

import functools
import logging

from uc_browser.health import HealthMonitor, availability_snapshot
from uc_browser.registry import get_registry

logger = logging.getLogger("uc_browser.mcp.site_tools")


def _wrap(fn):
    """Return errors as data — FastMCP serializes returns; raising is ugly
    client-side. Mirrors grok_tools._wrap_auth."""

    @functools.wraps(fn)  # keeps the signature FastMCP builds schemas from
    def _inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("site tool failed: %s", fn.__name__)
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

    return _inner


def register_site_tools(server, monitor: HealthMonitor | None = None) -> None:
    """Attach site-registry MCP tools to a FastMCP ``server``.

    Pass a shared ``monitor`` when the host process already runs one (the
    status server does) so probes reuse its store and serialization lock.
    """
    registry = get_registry()
    monitor = monitor or HealthMonitor()

    def _entry_view(entry, latest: dict) -> dict:
        rec = latest.get(entry.name, {})
        return {
            "name": entry.name, "url": entry.url, "kind": entry.kind,
            "notes": entry.notes, "litellm_model": entry.litellm_model,
            "login_required": entry.login_required, "source": entry.source,
            "status": rec.get("status", "unknown"),
            "last_checked": rec.get("at"),
            "detail": rec.get("detail", ""),
        }

    @server.tool(name="uc_site_list",
                 description="List all sites UC knows about, merged with "
                             "their latest health status.")
    @_wrap
    def uc_site_list(only_available: bool = False) -> dict:
        latest = monitor.store.latest()
        sites = [_entry_view(e, latest) for e in registry.list()]
        if only_available:
            sites = [s for s in sites if s["status"] == "ok"]
        return {"ok": True, "sites": sites}

    @server.tool(name="uc_site_search",
                 description="Search registered sites by substring over "
                             "name, URL, kind, and notes.")
    @_wrap
    def uc_site_search(query: str) -> dict:
        latest = monitor.store.latest()
        return {"ok": True,
                "sites": [_entry_view(e, latest)
                          for e in registry.search(query)]}

    @server.tool(name="uc_site_add",
                 description="Register a new chat site for UC to monitor. "
                             "name: short slug; url: the chat page. "
                             "Optionally probes it immediately.")
    @_wrap
    def uc_site_add(name: str, url: str, kind: str = "chat",
                    notes: str = "", probe_now: bool = True,
                    probe_interval_minutes: int = 15) -> dict:
        entry = registry.add(
            name, url, kind=kind, notes=notes,
            probe_interval_s=probe_interval_minutes * 60,
        )
        out = {"ok": True, "site": _entry_view(entry, {})}
        if probe_now:
            result = monitor.probe_site(entry)
            out["probe"] = result.to_dict()
        return out

    @server.tool(name="uc_site_remove",
                 description="Remove a user-added site from the registry "
                             "(built-in sites cannot be removed).")
    @_wrap
    def uc_site_remove(name: str) -> dict:
        removed = registry.remove(name)
        return {"ok": removed,
                **({} if removed else
                   {"error": "not_removable",
                    "message": f"{name!r} is not a user-added site."})}

    @server.tool(name="uc_site_probe",
                 description="Probe one registered site right now and "
                             "return its fresh health result.")
    @_wrap
    def uc_site_probe(name: str) -> dict:
        entry = registry.get(name)
        if not entry:
            return {"ok": False, "error": "unknown_site",
                    "message": f"No site named {name!r}. "
                               "Use uc_site_list to see them."}
        return {"ok": True, "probe": monitor.probe_site(entry).to_dict()}

    @server.tool(name="uc_availability",
                 description="The live menu: which sites are currently up "
                             "and which litellm models are callable now.")
    @_wrap
    def uc_availability() -> dict:
        return {"ok": True, **availability_snapshot(registry, monitor.store)}
