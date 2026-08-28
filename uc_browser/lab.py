"""The Signature Lab — v1 automated pass (tiers 1-3 of the design).

For each target site: probe the registered URL; if the composer isn't
found there, crawl the site's likely chat locations (help / support /
contact links plus ``support.<domain>``), running the generic
launcher-open + multi-frame detection at each. On success, emit the
tier-3 products:

  * a **site profile** (chat_page_url, pre_steps, frame hint, score)
    into the profile store → served at ``GET /signatures``
  * an **MHTML fixture** of the winning page into ``bench/fixtures/``
    (unannotated — the bench reports it as skipped until oracles exist)
  * a **report record** (every page tried, every score) into
    ``data/lab/`` — failures double as ML-training leads

Design constraints honored (see ``docs/01 - Design/01 - Signature
Lab.md``): detection only — the lab never sends messages (constraint #3);
everything emitted is cache, not dependency (#1); no site-specific
selectors — the crawler and detector are fully generic (#5).

Run:  pixi run python -m uc_browser.lab                # all degraded sites
      pixi run python -m uc_browser.lab --sites klm,samsung
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from uc_browser._paths import _SUBMODULE_ROOT
from uc_browser.health import HealthMonitor, HealthStore
from uc_browser.registry import SiteEntry, get_registry
from uc_browser.site_profiles import SiteProfile, get_profile_store

logger = logging.getLogger("uc_browser.lab")

MAX_CANDIDATE_PAGES = 4
DETECT_GATE = 4.0  # same bar chat() and the health prober use

_DISMISS_CONSENT_JS = """() => {
  const words = /^(accept|accept all|accept all cookies|allow all|allow all cookies|agree|i agree|i accept|confirm|got it|ok|reject all|agree and close)$/i;
  let clicked = 0;
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const t = (b.innerText || '').trim().toLowerCase();
    if (t && t.length < 30 && words.test(t)) { try { b.click(); clicked++; } catch (e) {} }
    if (clicked >= 2) break;
  }
  return clicked;
}"""

# Same-site links that smell like the place a support chat lives.
_CHAT_PAGE_LINKS_JS = """() => {
  const kw = /help|support|contact|chat|faq|assistant|customer.?service/i;
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    const text = (a.innerText || '').trim().slice(0, 60);
    if (!href.startsWith('http')) continue;
    if (!kw.test(href) && !kw.test(text)) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    out.push({ href, text });
  }
  return out.slice(0, 20);
}"""


def _same_site(url: str, base: str) -> bool:
    """Same registrable domain, so support.<domain> and www.<domain> count."""
    def core(u: str) -> str:
        host = urlparse(u).hostname or ""
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    return core(url) == core(base) and core(url) != ""


def candidate_pages(page, base_url: str) -> list[str]:
    """Likely chat locations for the site currently loaded in ``page``."""
    candidates: list[str] = []
    try:
        links = page.evaluate(_CHAT_PAGE_LINKS_JS) or []
    except Exception:
        links = []
    for link in links:
        if _same_site(link["href"], base_url):
            candidates.append(link["href"])
    # The classic guess, even when unlinked from the homepage.
    host = urlparse(base_url).hostname or ""
    bare = host[4:] if host.startswith("www.") else host
    guess = f"https://support.{bare}"
    if guess not in candidates:
        candidates.append(guess)
    # De-dupe against the base itself, cap the crawl.
    candidates = [c for c in candidates if c.rstrip("/") != base_url.rstrip("/")]
    return candidates[:MAX_CANDIDATE_PAGES]


class Lab:
    """One automated lab pass over a set of registry sites."""

    def __init__(self, monitor: Optional[HealthMonitor] = None,
                 fixtures_dir: Optional[Path] = None):
        self.monitor = monitor or HealthMonitor()
        self.fixtures_dir = fixtures_dir or (_SUBMODULE_ROOT / "bench" / "fixtures")
        self.report_dir = _SUBMODULE_ROOT / "data" / "lab"

    # ── per-page detonation (detect-only, constraint #3) ────────────

    def _detonate_page(self, ctx, url: str) -> dict:
        """Load one page, consent-dismiss, launcher-open, detect.
        Returns {url, ok, score, frame, launcher_used, error}."""
        rec = {"url": url, "ok": False, "score": 0.0, "frame": "",
               "launcher_used": False, "error": None}
        bundle = self.monitor._bundle_js()
        if not bundle:
            rec["error"] = "UC bundle not built"
            return rec
        page = ctx.new_page()
        try:
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                rec["error"] = f"goto: {e}"[:120]
                return rec
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            for frame in page.frames:
                try:
                    frame.evaluate(_DISMISS_CONSENT_JS)
                except Exception:
                    pass

            score, where = self.monitor._detect_across_frames(page, bundle)
            if score < DETECT_GATE:
                if self.monitor._try_open_widget(page):
                    rec["launcher_used"] = True
                    page.wait_for_timeout(2500)
                    score, where = self.monitor._detect_across_frames(page, bundle)
            rec["score"] = round(float(score), 2)
            rec["frame"] = where or ""
            rec["ok"] = score >= DETECT_GATE
            if rec["ok"]:
                self._save_fixture(ctx, page, url)
            return rec
        finally:
            page.close()

    def _save_fixture(self, ctx, page, url: str) -> None:
        """MHTML snapshot of a winning page → bench corpus (unannotated)."""
        try:
            name = (urlparse(url).hostname or "site").replace(".", "-")
            out_dir = self.fixtures_dir / f"lab-{name}"
            out_dir.mkdir(parents=True, exist_ok=True)
            cdp = ctx.new_cdp_session(page)
            snap = cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            (out_dir / "page.mhtml").write_text(
                snap["data"], encoding="utf-8", newline="")
            (out_dir / "meta.json").write_text(json.dumps({
                "name": f"lab-{name}", "url": url, "kind": "widget",
                "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "truth": {}, "resolved": {}, "provenance": "lab-auto",
            }, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug("fixture save failed for %s: %s", url, e)

    # ── per-site pass ───────────────────────────────────────────────

    def run_site(self, ctx, site: SiteEntry) -> dict:
        started = time.time()
        result = {"site": site.name, "found": False, "profile": None,
                  "pages": []}

        # 1) The registered URL itself (homepage) first.
        rec = self._detonate_page(ctx, site.url)
        result["pages"].append(rec)
        best = rec if rec["ok"] else None

        # 2) Crawl likely chat pages until one clears the gate.
        if not best:
            page = ctx.new_page()
            try:
                try:
                    page.goto(site.url, timeout=30000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    cands = candidate_pages(page, site.url)
                except Exception:
                    cands = []
            finally:
                page.close()
            for url in cands:
                rec = self._detonate_page(ctx, url)
                result["pages"].append(rec)
                if rec["ok"]:
                    best = rec
                    break

        # 3) Emit the site profile.
        if best:
            pre = [{"type": "launcher-auto"}] if best["launcher_used"] else []
            profile = SiteProfile(
                site=site.name,
                chat_page_url=best["url"],
                pre_steps=pre,
                frame_url_hint=best["frame"],
                detect_score=best["score"],
                verify_level="detect",
                verified_at=time.time(),
                provenance="lab-auto",
                notes=f"lab pass {time.strftime('%Y-%m-%d')}",
            )
            get_profile_store().upsert(profile)
            result["found"] = True
            result["profile"] = {"chat_page_url": best["url"],
                                 "score": best["score"],
                                 "launcher": best["launcher_used"],
                                 "frame": bool(best["frame"])}
        result["elapsed_s"] = round(time.time() - started, 1)
        return result

    def run(self, sites: list[SiteEntry]) -> list[dict]:
        """Full pass: one browser, sites in sequence, report persisted."""
        from playwright.sync_api import sync_playwright

        results = []
        args = ["--disable-blink-features=AutomationControlled",
                "--window-position=-32000,-32000"]
        # Own profile: the status server's scheduler may hold
        # .health_profile at any time, and Chromium profiles are
        # single-instance.
        profile_dir = _SUBMODULE_ROOT / "data" / ".lab_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(profile_dir), headless=False, args=args,
                viewport={"width": 1440, "height": 900})
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(ctx)
            except Exception:
                pass
            for site in sites:
                logger.info("lab: %s", site.name)
                try:
                    results.append(self.run_site(ctx, site))
                except Exception as e:
                    results.append({"site": site.name, "found": False,
                                    "error": str(e)[:200], "pages": []})
            ctx.close()

        self.report_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        report_path = self.report_dir / f"report-{stamp}.json"
        report_path.write_text(json.dumps(results, indent=2),
                               encoding="utf-8")
        logger.info("lab report → %s", report_path)
        return results


def _default_targets() -> list[SiteEntry]:
    """Sites whose latest health verdict is 'degraded' — the lab's beat."""
    latest = HealthStore().latest()
    return [e for e in get_registry().list()
            if latest.get(e.name, {}).get("status") == "degraded"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", help="comma-separated site names "
                                    "(default: all currently 'degraded')")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    if args.sites:
        registry = get_registry()
        sites = []
        for name in args.sites.split(","):
            entry = registry.get(name.strip())
            if not entry:
                print(f"unknown site: {name}")
                return 2
            sites.append(entry)
    else:
        sites = _default_targets()
    if not sites:
        print("No target sites (nothing 'degraded' and none named).")
        return 0

    print(f"Lab pass over {len(sites)} site(s): "
          f"{', '.join(s.name for s in sites)}\n(detect-only — no messages sent)\n")
    results = Lab().run(sites)

    print(f"\n{'site':18s} {'found':6s} {'chat page':44s} {'score':>5s}")
    print("-" * 78)
    hits = 0
    for r in sorted(results, key=lambda x: (not x.get("found"), x["site"])):
        if r.get("found"):
            hits += 1
            p = r["profile"]
            extras = []
            if p["launcher"]:
                extras.append("launcher")
            if p["frame"]:
                extras.append("iframe")
            note = f" ({','.join(extras)})" if extras else ""
            print(f"{r['site']:18s} {'YES':6s} {p['chat_page_url'][:44]:44s} "
                  f"{p['score']:5.1f}{note}")
        else:
            tried = len(r.get("pages", []))
            err = r.get("error") or f"{tried} page(s) tried, none cleared the gate"
            print(f"{r['site']:18s} {'no':6s} {err[:50]}")
    print("-" * 78)
    print(f"{hits}/{len(results)} sites gained a profile → data/site_profiles.json"
          "\nRe-probe with: pixi run python scripts/import_sites_xlsx.py "
          "chatbot_domains.xlsx --probe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
