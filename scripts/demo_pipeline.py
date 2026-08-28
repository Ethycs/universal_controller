"""End-to-end integration demo: registry → health → live menu → litellm call.

What it shows, in order:
  1. The live menu — every registered site with its current health, and
     which litellm models are callable right now (same data the proxy
     serves at /uc/availability).
  2. A real completion through litellm against each callable model
     (today: uc/grok — a browser-driven send, needs a logged-in profile).
  3. Session continuity — a follow-up message with the same session_id
     lands in the same site conversation.
  4. Prints the proxy-mode recipe (OpenAI client against localhost:4000).

Usage:
  pixi run -e dev python scripts/demo_pipeline.py               # full demo
  pixi run -e dev python scripts/demo_pipeline.py --menu-only   # no sends
  pixi run -e dev python scripts/demo_pipeline.py --skip-probe  # trust cache
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uc_browser.health import HealthMonitor, HealthStore, availability_snapshot  # noqa: E402
from uc_browser.registry import get_registry  # noqa: E402

_SINGLE_MESSAGE = False


def show_menu(snapshot: dict) -> list[str]:
    print("\n─── Live menu (what /uc/availability serves) " + "─" * 24)
    print(f"{'site':18s} {'status':10s} {'model':12s} {'uptime24h':>9s}  detail")
    print("-" * 78)
    for s in sorted(snapshot["sites"],
                    key=lambda x: (not x["callable"], x["status"], x["name"])):
        up = f"{s['uptime_24h']*100:.0f}%" if s["uptime_24h"] is not None else "–"
        model = s["litellm_model"] or ""
        mark = " ◀ callable" if s["callable"] else ""
        print(f"{s['name']:18s} {s['status']:10s} {model:12s} {up:>9s}  "
              f"{(s['detail'] or '')[:28]}{mark}")
    models = snapshot["models_available"]
    print(f"\nmodels_available: {models or 'none'}")
    return models


def demo_call(model: str) -> bool:
    """Run one model demo in a worker thread.

    litellm.completion leaves an asyncio loop behind on the calling
    thread (its async logging machinery); sync Playwright then refuses to
    run there. A fresh thread per call sidesteps that entirely.
    """
    import threading

    result: dict = {}
    t = threading.Thread(target=_demo_call_inner, args=(model, result),
                         daemon=True)
    t.start()
    # Two sends x 60s cap each, plus page-open overhead.
    t.join(timeout=150)
    if t.is_alive():
        print(f"TIMEOUT: {model} demo still running after 150s")
        return False
    return result.get("ok", False)


def _demo_call_inner(model: str, out: dict) -> None:
    out["ok"] = _demo_call_sync(model)


def _demo_call_sync(model: str) -> bool:
    import litellm

    from uc_browser.llm_providers.uc import register_uc_provider

    register_uc_provider()
    from uc_browser.llm_providers.uc import SUPPORTED_MODELS

    site = model.split("/", 1)[-1]
    pathway = "site adapter" if site in SUPPORTED_MODELS else "GENERIC engine"
    session = f"demo-{int(time.time())}"
    print(f"\n─── Calling {model} via {pathway} (session_id={session}) "
          + "─" * 10)
    try:
        t0 = time.time()
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user",
                       "content": "Reply with exactly: UC pipeline demo OK"}],
            extra_body={"session_id": session},
        )
        text = resp.choices[0].message.content
        url = (getattr(resp, "_hidden_params", {}) or {}).get("uc_conversation_url")
        print(f"[{time.time()-t0:5.1f}s] reply: {text!r}")
        if url:
            print(f"         conversation: {url}")

        if _SINGLE_MESSAGE:
            return True

        print("Follow-up in the same session (continuity check)...")
        t0 = time.time()
        resp2 = litellm.completion(
            model=model,
            messages=[{"role": "user",
                       "content": "What did I ask you to reply with? One line."}],
            extra_body={"session_id": session},
        )
        print(f"[{time.time()-t0:5.1f}s] reply: {resp2.choices[0].message.content!r}")
        return True
    except Exception as e:
        print(f"CALL FAILED: {type(e).__name__}: {str(e)[:300]}")
        if "auth" in str(e).lower() or "login" in str(e).lower():
            print("→ The site profile isn't logged in. Log in once, then retry.")
        return False


PROXY_RECIPE = """
─── Proxy mode (the API surface for any OpenAI client) ──────────────
Terminal 1:  pixi run status-server
             → uptime page http://127.0.0.1:4010/   MCP at /mcp
Terminal 2:  pixi run -e dev litellm --config litellm.config.yaml --port 4000 --num_workers 1

Then from anywhere:
    from openai import OpenAI
    client = OpenAI(api_key="anything", base_url="http://localhost:4000/v1")
    r = client.chat.completions.create(
        model="uc-grok",
        messages=[{"role": "user", "content": "hi"}],
        extra_body={"session_id": "my-thread"},
    )

Live menu through the proxy:   GET http://localhost:4000/uc/availability
(Sites the health monitor marks down fail fast with ServiceUnavailable;
override per-call with extra_body={"ignore_availability": true}.)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--menu-only", action="store_true",
                    help="show the live menu, skip the litellm calls")
    ap.add_argument("--skip-probe", action="store_true",
                    help="don't refresh stale health data before the demo")
    ap.add_argument("--models",
                    help="comma-separated subset to demo (e.g. "
                         "uc/grok,uc/pi) — resume a partial run")
    ap.add_argument("--single", action="store_true",
                    help="one message per model, no continuity follow-up")
    args = ap.parse_args()
    global _SINGLE_MESSAGE
    _SINGLE_MESSAGE = args.single
    try:
        # Line-buffered so a backgrounded run shows live progress.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    registry = get_registry()
    store = HealthStore()
    monitor = HealthMonitor(store=store)

    # Freshen any callable-model site whose health is stale, so the menu
    # (and the availability gate) reflects reality, not history.
    if not args.skip_probe:
        stale = [
            e for e in registry.list() if e.litellm_model
            and time.time() - store.latest(e.name).get("at", 0)
            > 2 * e.probe_interval_s
        ]
        if stale:
            print(f"Refreshing stale health for: "
                  f"{', '.join(e.name for e in stale)} (one probe sweep)...")
            monitor.probe_sites(stale)

    models = show_menu(availability_snapshot(registry, store))
    if args.models:
        wanted = {m.strip() for m in args.models.split(",")}
        unknown = wanted - set(models)
        if unknown:
            print(f"\nNot callable right now: {', '.join(sorted(unknown))}")
        models = [m for m in models if m in wanted]

    if args.menu_only:
        print(PROXY_RECIPE)
        return 0

    if not models:
        print("\nNo callable models right now — the health monitor doesn't "
              "have a fresh 'ok' for any site with a litellm driver.")
        print(PROXY_RECIPE)
        return 1

    outcomes = {m: demo_call(m) for m in models}  # run ALL, no short-circuit
    print("\n─── Demo summary " + "─" * 52)
    for m, ok in outcomes.items():
        print(f"  {'✓' if ok else '✗'} {m}")
    print(PROXY_RECIPE)
    return 0 if all(outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
