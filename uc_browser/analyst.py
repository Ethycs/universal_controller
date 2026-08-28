"""The analyst agent — LLM-guided site engineering for lab holdouts.

The self-hosting loop: sites UC already drives become the LLM power
supply (any callable ``uc/*`` model via litellm), and that intelligence
is pointed at the sites the automated lab pass could NOT crack. The
agent browses a resistant site step by step — reading a structural
digest of each page, deciding where the chat likely hides, clicking /
navigating — until the generic detector clears the gate. Success is
recorded as a site profile with ``provenance: analyst-agent`` and fed
back into the same distribution loop as lab-auto profiles.

Power supply resolution, in order:
  1. ``UC_ANALYST_MODEL`` env (any litellm-routable model)
  2. first callable ``uc/*`` model from the availability snapshot
  3. ``UC_BACKUP_MODEL`` env (conventional API)

Hard rules (inherited from the lab design):
  * DETECT-ONLY: the agent clicks and navigates; it never types into or
    submits anything. No messages reach human queues.
  * Same-site navigation only; max ``MAX_STEPS`` actions per site.
  * Success bar is the same score>=4 gate chat() uses.

Run:  pixi run python -m uc_browser.analyst --sites target,klm
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from typing import Optional

from uc_browser.health import HealthMonitor
from uc_browser.lab import _DISMISS_CONSENT_JS, _same_site
from uc_browser.registry import SiteEntry, get_registry
from uc_browser.site_profiles import SiteProfile, get_profile_store

logger = logging.getLogger("uc_browser.analyst")

MAX_STEPS = 8
DETECT_GATE = 4.0

_DIGEST_JS = """() => {
  const brief = (el, extra) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return null;
    const o = {
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || '').trim().slice(0, 50) || undefined,
      aria: el.getAttribute('aria-label') || undefined,
      id: el.id || undefined,
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 50) || undefined,
      ...extra,
    };
    return o;
  };
  const links = [...document.querySelectorAll('a[href]')]
    .map(a => brief(a, { href: (a.href || '').slice(0, 100) }))
    .filter(Boolean).slice(0, 25);
  const buttons = [...document.querySelectorAll('button, [role="button"]')]
    .map(b => brief(b, {})).filter(Boolean).slice(0, 20);
  const inputs = [...document.querySelectorAll(
      'textarea, input, [contenteditable]:not([contenteditable="false"])')]
    .map(i => brief(i, { placeholder: i.getAttribute('placeholder') || undefined }))
    .filter(Boolean).slice(0, 10);
  const iframes = [...document.querySelectorAll('iframe')]
    .map(f => brief(f, { src: (f.src || '').slice(0, 80),
                         title: f.title || undefined }))
    .filter(Boolean).slice(0, 10);
  return { url: location.href, title: document.title.slice(0, 80),
           links, buttons, inputs, iframes };
}"""

_SYSTEM_PROMPT = """You are a web-automation analyst. Goal: find the page/state on this \
site where a customer chat COMPOSER (a text input to chat with a bot) is visible, \
WITHOUT ever typing or sending anything.

Each turn you get: the current URL, a structural digest (links, buttons, inputs, \
iframes), and the chat-detector's current score (>= 4.0 means FOUND — you win).

Reply with EXACTLY ONE JSON object, nothing else:
  {"action": "goto",     "url": "<same-site url>",   "reason": "..."}
  {"action": "click",    "selector": "<css>",         "reason": "..."}  (buttons/links only)
  {"action": "launcher", "reason": "..."}   (auto-click the floating chat bubble)
  {"action": "give_up",  "reason": "..."}

Prefer help/support/contact pages and elements mentioning chat, assistant, \
message, or support. Never choose form submits, logins, or anything that sends data."""


def _ask_llm(model_chain: list[str], messages: list[dict]) -> Optional[dict]:
    """One LLM decision. Runs litellm in a throwaway thread — it leaves an
    asyncio loop on its calling thread, which would poison this thread's
    sync Playwright (validated the hard way)."""
    result: dict = {}

    def _call():
        import litellm

        from uc_browser.llm_providers.uc import register_uc_provider
        register_uc_provider()
        for model in model_chain:
            try:
                resp = litellm.completion(model=model, messages=messages)
                result["text"] = resp.choices[0].message.content or ""
                result["model"] = model
                return
            except Exception as e:
                logger.warning("analyst model %s failed: %s", model,
                               str(e)[:120])
        result["error"] = "all models in the chain failed"

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=180)
    if t.is_alive() or "text" not in result:
        return None
    m = re.search(r"\{.*\}", result["text"], re.DOTALL)
    if not m:
        return None
    try:
        decision = json.loads(m.group(0))
        decision["_model"] = result["model"]
        return decision
    except json.JSONDecodeError:
        return None


def resolve_model_chain() -> list[str]:
    import os

    chain: list[str] = []
    if os.environ.get("UC_ANALYST_MODEL"):
        chain.append(os.environ["UC_ANALYST_MODEL"])
    try:
        from uc_browser.health import HealthStore, availability_snapshot
        snap = availability_snapshot(get_registry(), HealthStore())
        chain.extend(m for m in snap["models_available"] if m not in chain)
    except Exception:
        pass
    if os.environ.get("UC_BACKUP_MODEL"):
        if os.environ["UC_BACKUP_MODEL"] not in chain:
            chain.append(os.environ["UC_BACKUP_MODEL"])
    return chain


class Analyst:
    def __init__(self, monitor: Optional[HealthMonitor] = None,
                 model_chain: Optional[list[str]] = None):
        self.monitor = monitor or HealthMonitor()
        self.model_chain = model_chain or resolve_model_chain()

    def run_site(self, ctx, site: SiteEntry) -> dict:
        bundle = self.monitor._bundle_js()
        result = {"site": site.name, "found": False, "steps": [],
                  "model_chain": self.model_chain}
        if not bundle:
            result["error"] = "UC bundle not built"
            return result

        page = ctx.new_page()
        transcript: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        clicks_taken: list[dict] = []
        try:
            page.goto(site.url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            for frame in page.frames:
                try:
                    frame.evaluate(_DISMISS_CONSENT_JS)
                except Exception:
                    pass

            for step_no in range(MAX_STEPS):
                score, where = self.monitor._detect_across_frames(page, bundle)
                if score >= DETECT_GATE:
                    result["found"] = True
                    profile = SiteProfile(
                        site=site.name, chat_page_url=page.url,
                        pre_steps=clicks_taken, frame_url_hint=where or "",
                        detect_score=round(float(score), 2),
                        verified_at=time.time(),
                        provenance="analyst-agent",
                        notes=f"analyst pass, {step_no} action(s)",
                    )
                    get_profile_store().upsert(profile)
                    result["profile"] = {"chat_page_url": page.url,
                                         "score": round(float(score), 2),
                                         "pre_steps": clicks_taken}
                    return result

                try:
                    digest = page.evaluate(_DIGEST_JS)
                except Exception as e:
                    digest = {"url": page.url, "error": str(e)[:100]}
                transcript.append({
                    "role": "user",
                    "content": json.dumps({
                        "step": step_no, "detector_score": round(float(score), 2),
                        "digest": digest}, ensure_ascii=False)[:6000],
                })
                decision = _ask_llm(self.model_chain, transcript)
                if not decision:
                    result["error"] = "LLM chain unavailable / unparseable"
                    return result
                transcript.append({"role": "assistant",
                                   "content": json.dumps(
                                       {k: v for k, v in decision.items()
                                        if k != "_model"})})
                result["steps"].append(decision)
                action = decision.get("action")
                logger.info("  [%s via %s] %s %s", site.name,
                            decision.get("_model", "?"), action,
                            decision.get("url") or decision.get("selector") or "")

                if action == "give_up":
                    result["error"] = f"agent gave up: {decision.get('reason', '')[:120]}"
                    return result
                if action == "goto":
                    url = decision.get("url", "")
                    if not _same_site(url, site.url):
                        continue  # refuse off-site navigation, ask again
                    try:
                        page.goto(url, timeout=30000,
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(3000)
                        clicks_taken = []  # steps reset on navigation
                    except Exception:
                        continue
                elif action == "click":
                    sel = decision.get("selector", "")
                    old_url = page.url
                    try:
                        page.locator(sel).first.click(timeout=4000)
                        page.wait_for_timeout(2500)
                        if page.url == old_url:
                            clicks_taken.append({"type": "click",
                                                 "selector": sel})
                        else:
                            clicks_taken = []
                    except Exception:
                        continue
                elif action == "launcher":
                    if self.monitor._try_open_widget(page):
                        clicks_taken.append({"type": "launcher-auto"})
                        page.wait_for_timeout(2500)
                for frame in page.frames:
                    try:
                        frame.evaluate(_DISMISS_CONSENT_JS)
                    except Exception:
                        pass

            result["error"] = f"no composer after {MAX_STEPS} steps"
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            return result
        finally:
            page.close()

    def run(self, sites: list[SiteEntry]) -> list[dict]:
        from playwright.sync_api import sync_playwright

        from uc_browser._paths import _SUBMODULE_ROOT

        results = []
        profile_dir = _SUBMODULE_ROOT / "data" / ".lab_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(profile_dir), headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      "--window-position=-32000,-32000"],
                viewport={"width": 1440, "height": 900})
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(ctx)
            except Exception:
                pass
            for site in sites:
                logger.info("analyst: %s (power: %s)", site.name,
                            " -> ".join(self.model_chain) or "NONE")
                results.append(self.run_site(ctx, site))
            ctx.close()
        return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", required=True,
                    help="comma-separated registry site names")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    registry = get_registry()
    sites = []
    for name in args.sites.split(","):
        entry = registry.get(name.strip())
        if not entry:
            print(f"unknown site: {name}")
            return 2
        sites.append(entry)

    analyst = Analyst()
    if not analyst.model_chain:
        print("No LLM power available: no UC_ANALYST_MODEL, no callable "
              "uc/* model, no UC_BACKUP_MODEL.")
        return 1
    print(f"Power chain: {' -> '.join(analyst.model_chain)}")
    results = analyst.run(sites)

    print(f"\n{'site':14s} {'found':6s} detail")
    print("-" * 72)
    for r in results:
        if r.get("found"):
            p = r["profile"]
            print(f"{r['site']:14s} {'YES':6s} {p['chat_page_url'][:40]} "
                  f"score={p['score']} steps={len(r['steps'])}")
        else:
            print(f"{r['site']:14s} {'no':6s} {r.get('error', '')[:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
