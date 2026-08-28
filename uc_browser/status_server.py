"""Uptime page + availability API + MCP endpoint for UC-driven sites.

One uvicorn process serving:
  GET  /              HTML uptime page (per-site cards, probe history bars)
  GET  /availability  JSON "live menu": registry x health, including which
                      litellm models are currently callable
  POST /probe/{name}  force an immediate probe of one site
  GET  /healthz       liveness of this server itself
  *    /mcp           MCP endpoint (streamable-http) exposing the site
                      registry tools: uc_site_add / uc_site_search /
                      uc_site_list / uc_site_probe / uc_availability

A background scheduler probes each registry site on its own interval
(default 15 min). Probes are serialized and run in a worker thread — the
browser is single-writer, same rule as the litellm proxy.

Run:
  pixi run -e dev python -m uc_browser.status_server          # port 4010
  UC_STATUS_PORT=4011 pixi run -e dev python -m uc_browser.status_server

The litellm proxy forwards /uc/availability to this server (see
pass_through_endpoints in litellm.config.yaml), so API clients get the
live menu from the same base URL they call for completions. Point an MCP
client at http://127.0.0.1:4010/mcp for the site tools; to expose it
remotely, front it with ngrok the same way scripts/serve_mcp_remote.py
does for the Grok tools.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from uc_browser.health import HealthMonitor, HealthStore, availability_snapshot
from uc_browser.registry import SiteRegistry, get_registry

logger = logging.getLogger("uc_browser.status_server")

SCHEDULER_TICK_S = 60
HISTORY_BAR_SLOTS = 48

_STATUS_COLORS = {
    "ok": "#2fbf71",
    "degraded": "#f5a623",
    "login": "#8884d8",
    "down": "#e0245e",
    "unknown": "#9aa0a6",
}


def _fmt_age(age_s: Optional[float]) -> str:
    if age_s is None:
        return "never"
    if age_s < 90:
        return f"{int(age_s)}s ago"
    if age_s < 5400:
        return f"{int(age_s // 60)}m ago"
    return f"{age_s / 3600:.1f}h ago"


def _render_page(registry: SiteRegistry, store: HealthStore) -> str:
    snap = availability_snapshot(registry, store)
    rows = []
    for s in snap["sites"]:
        color = _STATUS_COLORS.get(s["status"], _STATUS_COLORS["unknown"])
        uptime = s["uptime_24h"]
        uptime_txt = f"{uptime * 100:.1f}%" if uptime is not None else "–"
        model = s["litellm_model"] or ""
        model_badge = (
            f'<span class="model {"live" if s["callable"] else "dark"}">'
            f'{html.escape(model)}</span>' if model else "")
        # History bar: newest right.
        recs = store.history(s["name"])[-HISTORY_BAR_SLOTS:]
        cells = "".join(
            f'<span class="cell" title="{html.escape(r.get("status", "?"))} · '
            f'{time.strftime("%m-%d %H:%M", time.localtime(r.get("at", 0)))}" '
            f'style="background:{_STATUS_COLORS.get(r.get("status"), "#9aa0a6")}">'
            "</span>"
            for r in recs) or '<span class="nodata">no probes yet</span>'
        detail = html.escape(s["detail"] or "")
        rows.append(f"""
    <div class="card">
      <div class="head">
        <span class="dot" style="background:{color}"></span>
        <span class="name">{html.escape(s["name"])}</span>
        <span class="status">{html.escape(s["status"])}</span>
        {model_badge}
        <span class="spacer"></span>
        <span class="meta">uptime 24h: {uptime_txt}</span>
        <span class="meta">{_fmt_age(s["checked_age_s"])}</span>
        <span class="meta">{(str(s["latency_ms"]) + " ms") if s["latency_ms"] else ""}</span>
      </div>
      <div class="bar">{cells}</div>
      <div class="detail">{detail}</div>
    </div>""")

    n_ok = sum(1 for s in snap["sites"] if s["available"])
    generated = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(snap["generated_at"]))
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>UC Site Status</title>
<style>
  :root {{ --bg:#fff; --fg:#1a1a1a; --muted:#667; --card:#f6f7f9; --line:#e3e5e8; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#101216; --fg:#e8eaed; --muted:#9aa0a6; --card:#1a1e24; --line:#2a2f36; }}
  }}
  body {{ margin:0; padding:2rem; background:var(--bg); color:var(--fg);
         font:15px/1.5 system-ui, sans-serif; max-width:860px; margin-inline:auto; }}
  h1 {{ font-size:1.3rem; margin:0 0 .25rem; }}
  .sub {{ color:var(--muted); margin-bottom:1.5rem; }}
  .card {{ background:var(--card); border:1px solid var(--line);
           border-radius:10px; padding: .8rem 1rem; margin-bottom: .8rem; }}
  .head {{ display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; }}
  .dot {{ width:.7rem; height:.7rem; border-radius:50%; flex:none; }}
  .name {{ font-weight:600; }}
  .status {{ color:var(--muted); }}
  .spacer {{ flex:1; }}
  .meta {{ color:var(--muted); font-size:.85rem; }}
  .model {{ font:12px ui-monospace,monospace; padding:.1rem .45rem;
            border-radius:99px; border:1px solid var(--line); }}
  .model.live {{ color:#2fbf71; border-color:#2fbf71; }}
  .model.dark {{ color:var(--muted); }}
  .bar {{ display:flex; gap:2px; margin-top:.55rem; }}
  .cell {{ width:10px; height:18px; border-radius:2px; flex:none; }}
  .nodata {{ color:var(--muted); font-size:.85rem; }}
  .detail {{ color:var(--muted); font-size:.82rem; margin-top:.35rem;
             overflow-wrap:anywhere; }}
</style></head>
<body>
  <h1>Universal Controller — Site Status</h1>
  <div class="sub">{n_ok}/{len(snap["sites"])} sites available ·
    models live: {html.escape(", ".join(snap["models_available"]) or "none")} ·
    generated {generated} · auto-refreshes every 60s</div>
  {"".join(rows)}
</body></html>"""


def _build_mcp(monitor: HealthMonitor):
    """FastMCP server exposing the site tools, as a mountable ASGI app.

    Stateless streamable-http (per the FastMCP HTTP deployment docs) so it
    mounts cleanly inside the FastAPI app with no session affinity.
    """
    from mcp.server.fastmcp import FastMCP

    from uc_browser.mcp.site_tools import register_site_tools

    mcp = FastMCP("uc-sites", stateless_http=True)
    register_site_tools(mcp, monitor=monitor)
    return mcp


def create_app(registry: Optional[SiteRegistry] = None,
               monitor: Optional[HealthMonitor] = None,
               run_scheduler: bool = True,
               with_mcp: bool = True):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

    registry = registry or get_registry()
    monitor = monitor or HealthMonitor()
    mcp = _build_mcp(monitor) if with_mcp else None

    async def _scheduler():
        while True:
            try:
                sites = [s for s in registry.list() if not s.login_required]
                due = monitor.due_sites(sites)
                if due:
                    logger.info("Probing %d due site(s): %s",
                                len(due), ", ".join(s.name for s in due))
                    await asyncio.to_thread(monitor.probe_sites, due)
            except Exception:
                logger.exception("scheduler sweep failed")
            await asyncio.sleep(SCHEDULER_TICK_S)

    @asynccontextmanager
    async def _lifespan(app):
        task = asyncio.create_task(_scheduler()) if run_scheduler else None
        try:
            if mcp is not None:
                # The streamable-http transport needs its session manager
                # task group running for the mounted app to serve requests.
                async with mcp.session_manager.run():
                    yield
            else:
                yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="UC Site Status", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)
    app.state.registry = registry
    app.state.monitor = monitor

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return await asyncio.to_thread(_render_page, registry, monitor.store)

    @app.get("/availability")
    async def availability():
        return await asyncio.to_thread(
            availability_snapshot, registry, monitor.store)

    @app.post("/probe/{name}")
    async def probe(name: str):
        entry = registry.get(name)
        if not entry:
            raise HTTPException(404, f"unknown site: {name}")
        result = await asyncio.to_thread(monitor.probe_site, entry)
        return result.to_dict()

    @app.get("/signatures")
    async def signatures():
        """The Signature Lab distribution feed: site profiles with TTLs.
        Cache, don't depend — the generic engine is the product."""
        from uc_browser.site_profiles import get_profile_store
        return await asyncio.to_thread(lambda: get_profile_store().feed())

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "time": time.time()}

    if mcp is not None:
        # Mounted at root, AFTER the routes above (FastAPI matches its own
        # routes first). The MCP app serves its default path, /mcp.
        app.mount("/", mcp.streamable_http_app())

    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("UC_STATUS_PORT", "4010"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
