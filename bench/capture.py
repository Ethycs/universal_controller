"""Capture live sites into MHTML fixtures for the offline benchmark.

For each site in the registry:
  1. Open the page (headful Chromium + stealth by default — several
     targets are behind Cloudflare and block headless).
  2. Best-effort consent dismissal + optional pre-clicks (widget launchers).
  3. Resolve oracle selectors and stamp ``data-uc-truth`` attributes into
     the live DOM so the annotation is baked into the snapshot.
  4. Snapshot: page.mhtml (CDP Page.captureSnapshot), screenshot.png,
     meta.json (including a probe dump of every input/button per frame,
     used to refine oracles when they miss).

Usage:
  pixi run python bench/capture.py                    # all no-login sites
  pixi run python bench/capture.py --sites chatgpt,pi
  pixi run python bench/capture.py --include-login    # needs logged-in profile
  pixi run python bench/capture.py --headless
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench._common import (  # noqa: E402
    DISMISS_CONSENT_JS,
    FIXTURES_DIR,
    PROBE_JS,
    TAG_TRUTH_JS,
    write_meta,
)
from bench.sites import Site, get_sites  # noqa: E402

logger = logging.getLogger("bench.capture")

PROFILE_DIR = Path(__file__).resolve().parent / ".capture_profile"
VIEWPORT = {"width": 1440, "height": 900}


def _truth_dict(site: Site) -> dict:
    return {
        "input": site.truth_input,
        "send": site.truth_send,
        "messages": site.truth_messages,
    }


def _all_frames(page):
    return [f for f in page.frames if f.url not in ("", "about:blank")] or page.frames


def _try_pre_clicks(page, selectors: list[str]) -> list[str]:
    """Click launcher elements (searched in every frame). Best-effort."""
    clicked = []
    for sel in selectors:
        for frame in _all_frames(page):
            try:
                loc = frame.locator(sel).first
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    clicked.append(sel)
                    page.wait_for_timeout(2500)
                    break
            except Exception:
                continue
    return clicked


_READY_INPUT_JS = """() => {
  const q = 'textarea, input[type="text"], input:not([type]), '
    + '[contenteditable]:not([contenteditable="false"]), [role="textbox"]';
  return [...document.querySelectorAll(q)].some(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
}"""


def _wait_for_ready(page, timeout_ms: int = 25000) -> bool:
    """Poll all frames until some visible text input exists. Slow-hydrating
    apps (grok.com) sit on a loading splash long past networkidle; capturing
    before hydration snapshots the splash, not the UI."""
    deadline = timeout_ms
    while deadline > 0:
        for frame in _all_frames(page):
            try:
                if frame.evaluate(_READY_INPUT_JS):
                    return True
            except Exception:
                continue
        page.wait_for_timeout(1000)
        deadline -= 1000
    return False


def _reveal_send_button(page, resolved: dict) -> bool:
    """Many composers (ChatGPT, Perplexity) render their send button only
    once text is present. Type into the truth input so the button exists
    in the snapshot. The typed text stays in the fixture — a filled
    composer is a realistic state, and matches how UC's own chat() flow
    discovers buttons (after setText)."""
    hit = resolved.get("input")
    if not hit:
        return False
    for frame in _all_frames(page):
        if frame.url != hit.get("frame"):
            continue
        try:
            loc = frame.locator('[data-uc-truth="input"]').first
            loc.click(timeout=3000)
            loc.type("Hello there", delay=30)
            page.wait_for_timeout(800)
            return True
        except Exception:
            return False
    return False


def capture_site(ctx, site: Site, out_root: Path) -> dict:
    page = ctx.new_page()
    status = {"name": site.name, "ok": False, "resolved": {}, "error": None}
    try:
        page.goto(site.url, timeout=45000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(site.wait_ms)

        # Consent banners, readiness, then widget launchers.
        for frame in _all_frames(page):
            try:
                frame.evaluate(DISMISS_CONSENT_JS)
            except Exception:
                pass
        if site.kind in ("chat", "widget"):
            if not _wait_for_ready(page):
                logger.warning("%s: no visible input appeared before timeout",
                               site.name)
        clicked = _try_pre_clicks(page, site.pre_clicks)
        if clicked:
            page.wait_for_timeout(1500)
            for frame in _all_frames(page):
                try:
                    frame.evaluate(DISMISS_CONSENT_JS)
                except Exception:
                    pass

        # Tag ground truth in every frame; record where each role resolved.
        def tag_pass(resolved: dict[str, dict]) -> None:
            for frame in _all_frames(page):
                try:
                    report = frame.evaluate(TAG_TRUTH_JS, _truth_dict(site))
                except Exception:
                    continue
                for role, hit in (report or {}).items():
                    if hit and role not in resolved:
                        resolved[role] = {**hit, "frame": frame.url}

        resolved: dict[str, dict] = {}
        tag_pass(resolved)

        # Composer buttons often exist only once text is typed — type into
        # the truth input and re-tag the send button.
        if "input" in resolved and "send" not in resolved and site.truth_send:
            if _reveal_send_button(page, resolved):
                tag_pass(resolved)

        probes = []
        for frame in _all_frames(page):
            try:
                probes.append(frame.evaluate(PROBE_JS))
            except Exception:
                pass
        status["resolved"] = {r: v["selector"] for r, v in resolved.items()}

        if site.kind in ("chat", "widget") and "input" not in resolved:
            logger.warning("%s: no oracle resolved for 'input' — fixture will "
                           "be captured but unscorable until oracles are fixed "
                           "(see probe dump in meta.json)", site.name)

        # Snapshot.
        out_dir = out_root / site.name
        out_dir.mkdir(parents=True, exist_ok=True)
        cdp = ctx.new_cdp_session(page)
        snap = cdp.send("Page.captureSnapshot", {"format": "mhtml"})
        (out_dir / "page.mhtml").write_text(snap["data"], encoding="utf-8", newline="")
        try:
            page.screenshot(path=str(out_dir / "screenshot.png"))
        except Exception:
            pass

        write_meta(out_dir, {
            "name": site.name,
            "url": site.url,
            "kind": site.kind,
            "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "final_url": page.url,
            "truth": _truth_dict(site),
            "resolved": resolved,
            "pre_clicks_done": clicked,
            "site": asdict(site),
            "probe": probes,
        })
        status["ok"] = True
        logger.info("%s: captured (%s)",
                    site.name, ", ".join(sorted(resolved)) or "NO TRUTH RESOLVED")
    except Exception as e:
        status["error"] = str(e)[:300]
        logger.error("%s: capture failed: %s", site.name, status["error"])
    finally:
        page.close()
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", help="comma-separated site names (default: all)")
    ap.add_argument("--include-login", action="store_true",
                    help="also capture sites flagged login_required")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--out", default=str(FIXTURES_DIR))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sites = get_sites(args.sites.split(",") if args.sites else None,
                      include_login=args.include_login)
    out_root = Path(args.out)

    from playwright.sync_api import sync_playwright

    stealth = None
    try:
        from playwright_stealth import Stealth
        stealth = Stealth()
    except ImportError:
        logger.warning("playwright-stealth not installed; captures may be blocked")

    statuses = []
    with sync_playwright() as p:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=args.headless,
            viewport=VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if stealth:
            try:
                stealth.apply_stealth_sync(ctx)
            except Exception as e:
                logger.debug("stealth apply failed: %s", e)
        for site in sites:
            statuses.append(capture_site(ctx, site, out_root))
        ctx.close()

    ok = [s for s in statuses if s["ok"]]
    print(f"\nCaptured {len(ok)}/{len(statuses)} sites → {out_root}")
    for s in statuses:
        mark = "ok " if s["ok"] else "ERR"
        roles = ",".join(sorted(s["resolved"])) or "-"
        print(f"  [{mark}] {s['name']:16s} truth: {roles}"
              + (f"  ({s['error']})" if s["error"] else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
