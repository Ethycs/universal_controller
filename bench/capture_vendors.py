"""Probe chat-widget vendor pages and capture fixtures for the ones with
a live, detectable widget — the framework-diversity zoo expansion.

For each vendor: load, dismiss consent, run the generic launcher-open +
multi-frame detect. On a gate-clearing hit, snapshot MHTML into
bench/fixtures/vendor-<name>/ (unannotated — the bench reports it skipped
until oracles are added) and tag the winning frame + launcher steps in
meta so the fixture is reproducible.

Usage:
  pixi run python -m bench.capture_vendors                 # all vendors
  pixi run python -m bench.capture_vendors --only crisp,drift
  pixi run python -m bench.capture_vendors --probe-only    # no fixtures
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.vendors import VENDORS  # noqa: E402
from uc_browser._paths import _SUBMODULE_ROOT  # noqa: E402
from uc_browser.health import HealthMonitor  # noqa: E402
from uc_browser.lab import _DISMISS_CONSENT_JS  # noqa: E402

FIXTURES = _SUBMODULE_ROOT / "bench" / "fixtures"
DETECT_GATE = 4.0


def probe_vendor(ctx, mon, bundle, name, url, capture) -> dict:
    rec = {"name": name, "url": url, "score": 0.0, "launcher": False,
           "frame": "", "captured": False, "error": None}
    page = ctx.new_page()
    try:
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            rec["error"] = f"goto: {e}"[:80]
            return rec
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(5000)  # widgets load late
        for frame in page.frames:
            try:
                frame.evaluate(_DISMISS_CONSENT_JS)
            except Exception:
                pass

        score, where = mon._detect_across_frames(page, bundle)
        # Vendor-signature fast path: identify the engine, drive its known
        # launcher/open-API before falling back to the generic heuristic.
        try:
            from uc_browser import vendor_signatures as vs
            rec["vendors"] = vs.identify(page)
            if score < DETECT_GATE and rec["vendors"]:
                if vs.open_widget(page, rec["vendors"][0]):
                    rec["vendor_opened"] = rec["vendors"][0]
                    page.wait_for_timeout(4000)
                    score, where = mon._detect_across_frames(page, bundle)
        except Exception:
            rec["vendors"] = []
        if score < DETECT_GATE:
            if mon._try_open_widget(page):
                rec["launcher"] = True
                page.wait_for_timeout(3500)
                score, where = mon._detect_across_frames(page, bundle)
        rec["score"] = round(float(score), 2)
        rec["frame"] = where or ""
        hit = score >= DETECT_GATE

        if hit and capture:
            out_dir = FIXTURES / f"vendor-{name}"
            out_dir.mkdir(parents=True, exist_ok=True)
            cdp = ctx.new_cdp_session(page)
            snap = cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            (out_dir / "page.mhtml").write_text(
                snap["data"], encoding="utf-8", newline="")
            (out_dir / "meta.json").write_text(json.dumps({
                "name": f"vendor-{name}", "url": url, "kind": "widget",
                "vendor": name,
                "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "truth": {}, "resolved": {},
                "launcher_used": rec["launcher"],
                "detect_frame": rec["frame"],
                "provenance": "vendor-zoo",
            }, indent=2), encoding="utf-8")
            try:
                page.screenshot(path=str(out_dir / "screenshot.png"))
            except Exception:
                pass
            rec["captured"] = True
        return rec
    finally:
        page.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="comma-separated vendor names")
    ap.add_argument("--probe-only", action="store_true",
                    help="report scores, don't write fixtures")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    vendors = VENDORS
    if args.only:
        want = {v.strip() for v in args.only.split(",")}
        vendors = [v for v in VENDORS if v[0] in want]

    mon = HealthMonitor()
    bundle = mon._bundle_js()
    if not bundle:
        print("UC bundle not built — cd extension && npx rollup -c")
        return 1

    from playwright.sync_api import sync_playwright
    profile_dir = _SUBMODULE_ROOT / "data" / ".lab_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    results = []
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
        for name, url, _hint in vendors:
            print(f"probing {name} ...", flush=True)
            r = probe_vendor(ctx, mon, bundle, name, url,
                             capture=not args.probe_only)
            results.append(r)
        ctx.close()

    print(f"\n{'vendor':16s} {'score':>5s} {'hit':4s} {'fixture':8s} detail")
    print("-" * 72)
    hits = caps = 0
    for r in sorted(results, key=lambda x: -x["score"]):
        hit = r["score"] >= DETECT_GATE
        hits += hit
        caps += r["captured"]
        marks = []
        if r.get("vendor_opened"):
            marks.append(f"sig:{r['vendor_opened']}")
        elif r.get("vendors"):
            marks.append("id:" + "/".join(r["vendors"][:2]))
        if r["launcher"]:
            marks.append("launcher")
        if r["frame"]:
            marks.append("iframe")
        detail = r["error"] or ", ".join(marks)
        print(f"{r['name']:16s} {r['score']:5.1f} {'YES' if hit else 'no':4s} "
              f"{'saved' if r['captured'] else '-':8s} {detail[:34]}")
    print("-" * 72)
    print(f"{hits}/{len(results)} vendors detected; {caps} fixtures captured "
          f"→ bench/fixtures/vendor-*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
