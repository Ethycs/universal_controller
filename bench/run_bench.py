"""Run the generic-detection benchmark over captured MHTML fixtures.

For every fixture in bench/fixtures/, loads the snapshot offline, injects
the built UC bundle into every frame (CDP evaluation works even though
MHTML blocks page scripts), and scores the generic pipeline with honest
top-1 metrics:

  chat/widget fixtures
    input_top1      __UC_findInputs()[0] is the ground-truth input
    input_gate      ...and clears the score>=4 gate chat() actually uses
    input_top5      truth appears anywhere in the top 5 (recall)
    input_pipeline  the full chat() input path: heuristic gate, else ML
                    classifier fallback (conf > 0.5) — the real product metric
    send_top1       given the TRUE input, __UC_findButtons()[0] is the
                    ground-truth send button (stage-isolated)
    container_top1  detectAll('STRUCTURAL').chat[0] is the message stream
                    (only scored when the fixture contains messages)

  negative fixtures
    fp_input        top input scored >= 4 (chat() would engage the page)
    fp_chat         a chat pattern cleared the BEHAVIORAL threshold (0.5)

Exit codes: 0 ok; 1 regression vs baseline (with --check); 2 no fixtures.

Usage:
  pixi run python bench/run_bench.py                  # run + table
  pixi run python bench/run_bench.py --check          # fail on regression
  pixi run python bench/run_bench.py --update-baseline
  pixi run python bench/run_bench.py --sites chatgpt,pi --verbose
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench._common import (  # noqa: E402
    FIXTURES_DIR,
    ML_CANDIDATES_JS,
    ML_CLEANUP_JS,
    RESULTS_DIR,
    SCORE_JS,
    inject_bundle_all_frames,
    load_bundle,
    read_meta,
)

logger = logging.getLogger("bench.run")

BASELINE_PATH = RESULTS_DIR / "baseline.json"

POSITIVE_STAGES = ["input_top1", "input_gate", "input_top5",
                   "input_pipeline", "send_top1", "container_top1"]
NEGATIVE_STAGES = ["fp_input", "fp_chat"]


def _score_ml_fallback(frame, truth_selector: str | None) -> dict:
    """Mirror UCBrowser.ml_find_chat() on one frame: candidate containers
    → code-feature classifier → best chat_input above 0.5 confidence.
    Returns {found, hit, confidence}."""
    out = {"found": False, "hit": False, "confidence": 0.0}
    try:
        from uc_browser.dom_classifier import classify_code
    except ImportError:
        return out
    try:
        candidates = frame.evaluate(ML_CANDIDATES_JS) or []
    except Exception:
        return out
    best = None
    for sel in candidates:
        try:
            result = classify_code(frame, sel)
        except Exception:
            continue
        if result and result.get("label") == "chat_input":
            if not best or result["confidence"] > best[1]:
                best = (sel, result["confidence"])
    try:
        frame.evaluate(ML_CLEANUP_JS)
    except Exception:
        pass
    if best and best[1] > 0.5:
        out["found"] = True
        out["confidence"] = round(best[1], 3)
        if truth_selector:
            try:
                out["hit"] = frame.evaluate(
                    """(args) => {
                        const a = document.querySelector(args[0]);
                        const b = document.querySelector(args[1]);
                        return !!(a && b && (a === b || a.contains(b) || b.contains(a)));
                    }""",
                    [best[0], truth_selector],
                )
            except Exception:
                pass
    return out


def run_fixture(browser, bundle_js: str, fixture_dir: Path, verbose: bool) -> dict:
    meta = read_meta(fixture_dir)
    kind = meta.get("kind", "chat")
    result = {"name": meta["name"], "kind": kind, "stages": {}, "error": None}

    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    try:
        page.goto((fixture_dir / "page.mhtml").resolve().as_uri(),
                  timeout=30000)
        page.wait_for_timeout(500)
        injected = inject_bundle_all_frames(page, bundle_js)
        if injected == 0:
            result["error"] = "bundle injection failed in every frame"
            return result

        # Score every frame; the fixture's verdict comes from the frame
        # holding the truth annotation (positives) or the worst frame
        # (negatives — a false positive anywhere is a false positive).
        frame_reports = []
        for frame in page.frames:
            try:
                rep = frame.evaluate(SCORE_JS, {"truth": meta.get("truth", {})})
                if rep and not rep.get("error"):
                    rep["_frame"] = frame
                    frame_reports.append(rep)
            except Exception:
                continue
        if not frame_reports:
            result["error"] = "no frame produced a score report"
            return result

        if kind in ("chat", "widget"):
            truth_frames = [r for r in frame_reports if r["hasTruth"].get("input")]
            if not truth_frames:
                # No truth annotation resolved anywhere — the fixture is
                # unannotated (oracle never resolved at capture), not a
                # detection failure. Report as skipped, not scored.
                result["skipped"] = "no truth annotation (refine oracles + recapture)"
                return result
            rep = truth_frames[0]
            stages = dict(rep["stages"])

            # Full-pipeline input metric: heuristic gate, else ML fallback.
            if stages.get("input_gate"):
                stages["input_pipeline"] = True
                stages["ml_used"] = False
            else:
                ml = _score_ml_fallback(rep["_frame"], '[data-uc-truth="input"]')
                stages["input_pipeline"] = bool(ml["hit"])
                stages["ml_used"] = True
                stages["ml_confidence"] = ml["confidence"]
            result["stages"] = stages
            result["hasTruth"] = rep["hasTruth"]
            if verbose:
                result["detail"] = rep.get("detail")
        else:
            worst_score = max(r["neg"]["top_input_score"] for r in frame_reports)
            worst_chat = max(r["neg"]["chat_behavioral"] for r in frame_reports)
            result["stages"] = {
                "fp_input": worst_score >= 4,
                "fp_chat": worst_chat >= 0.5,
                "top_input_score": round(worst_score, 2),
                "chat_confidence": round(worst_chat, 2),
            }
            if verbose:
                result["detail"] = [
                    {k: v for k, v in r.items() if k in ("frameUrl", "neg")}
                    for r in frame_reports
                ]
    except Exception as e:
        result["error"] = str(e)[:300]
    finally:
        ctx.close()
    return result


def summarize(results: list[dict]) -> dict:
    pos = [r for r in results if r["kind"] in ("chat", "widget")
           and not r["error"] and not r.get("skipped")]
    neg = [r for r in results if r["kind"] == "negative" and not r["error"]]
    errors = [r for r in results if r["error"]]
    skipped = [r for r in results if r.get("skipped")]

    def frac(items, stage):
        scored = [r for r in items if stage in r["stages"]]
        hit = sum(1 for r in scored if r["stages"][stage])
        return hit, len(scored)

    summary = {"positive": {}, "negative": {}, "errors": len(errors),
               "skipped": len(skipped)}
    for stage in POSITIVE_STAGES:
        hit, n = frac(pos, stage)
        if n:
            summary["positive"][stage] = {"hit": hit, "n": n}
    for stage in NEGATIVE_STAGES:
        hit, n = frac(neg, stage)
        if n:
            summary["negative"][stage] = {"fp": hit, "n": n}
    return summary


def print_table(results: list[dict], summary: dict) -> None:
    print()
    print("mode: COLD-GENERIC (no signatures/profiles in play)")
    print(f"{'site':16s} {'kind':9s} {'in-top1':8s} {'gate':6s} {'pipeline':9s} "
          f"{'send':6s} {'stream':7s}")
    print("-" * 66)
    for r in sorted(results, key=lambda x: (x["kind"], x["name"])):
        if r["error"]:
            print(f"{r['name']:16s} {r['kind']:9s} ERROR: {r['error'][:40]}")
            continue
        if r.get("skipped"):
            print(f"{r['name']:16s} {r['kind']:9s} skipped: {r['skipped'][:44]}")
            continue
        s = r["stages"]
        if r["kind"] in ("chat", "widget"):
            def m(k):
                if k not in s:
                    return "-"
                return "✓" if s[k] else "✗"
            ml = " (ml)" if s.get("ml_used") and s.get("input_pipeline") else ""
            print(f"{r['name']:16s} {r['kind']:9s} {m('input_top1'):8s} "
                  f"{m('input_gate'):6s} {m('input_pipeline') + ml:9s} "
                  f"{m('send_top1'):6s} {m('container_top1'):7s}")
        else:
            fpi = "FP!" if s["fp_input"] else "ok"
            fpc = "FP!" if s["fp_chat"] else "ok"
            print(f"{r['name']:16s} {r['kind']:9s} {fpi:8s} "
                  f"(score {s['top_input_score']:.1f})  chat:{fpc} "
                  f"(conf {s['chat_confidence']:.2f})")
    print("-" * 66)
    for stage, d in summary["positive"].items():
        print(f"  {stage:16s} {d['hit']}/{d['n']}")
    for stage, d in summary["negative"].items():
        print(f"  {stage:16s} {d['fp']}/{d['n']} false positives")
    if summary["errors"]:
        print(f"  errors           {summary['errors']}")
    if summary.get("skipped"):
        print(f"  skipped          {summary['skipped']} (unannotated)")


def check_regression(results: list[dict], baseline: dict) -> list[str]:
    """A regression is any per-site stage that was passing in the baseline
    and now fails (or a negative that was clean and now false-positives).
    New sites and newly-passing stages are never regressions."""
    regressions = []
    base_sites = {r["name"]: r for r in baseline.get("results", [])}
    for r in results:
        b = base_sites.get(r["name"])
        if not b or b.get("error") or r.get("error"):
            if b and not b.get("error") and r.get("error"):
                regressions.append(f"{r['name']}: now errors ({r['error'][:60]})")
            continue
        for stage in POSITIVE_STAGES:
            if b["stages"].get(stage) is True and r["stages"].get(stage) is False:
                regressions.append(f"{r['name']}: {stage} regressed ✓→✗")
        for stage in NEGATIVE_STAGES:
            if b["stages"].get(stage) is False and r["stages"].get(stage) is True:
                regressions.append(f"{r['name']}: {stage} regressed ok→FP")
    return regressions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", help="comma-separated fixture names")
    ap.add_argument("--fixtures", default=str(FIXTURES_DIR))
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any stage regressed vs the baseline")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--verbose", action="store_true",
                    help="keep per-site detection detail in results")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    fixtures_root = Path(args.fixtures)
    wanted = set(args.sites.split(",")) if args.sites else None
    fixture_dirs = sorted(
        d for d in fixtures_root.iterdir()
        if d.is_dir() and (d / "page.mhtml").exists() and (d / "meta.json").exists()
        and (not wanted or d.name in wanted)
    ) if fixtures_root.is_dir() else []
    if not fixture_dirs:
        print(f"No fixtures under {fixtures_root}. Run bench/capture.py first.")
        return 2

    bundle_js = load_bundle()
    results = []
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for d in fixture_dirs:
            logger.info("scoring %s", d.name)
            results.append(run_fixture(browser, bundle_js, d, args.verbose))
        browser.close()

    summary = summarize(results)
    print_table(results, summary)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        # Fresh browser context, empty storage: no signatures/profiles are
        # in play, so these are COLD-GENERIC numbers by construction — the
        # headline metric per the Signature Lab design (constraint #2). A
        # signature-warmed mode, when built, must report separately.
        "mode": "cold-generic",
        "summary": summary,
        "results": [{k: v for k, v in r.items() if k != "detail" or args.verbose}
                    for r in results],
    }
    (RESULTS_DIR / "latest.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nResults → {RESULTS_DIR / 'latest.json'}")

    if args.update_baseline:
        BASELINE_PATH.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"Baseline updated → {BASELINE_PATH}")
        return 0

    if args.check:
        if not BASELINE_PATH.exists():
            print("No baseline to check against — run with --update-baseline first.")
            return 1
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        regressions = check_regression(results, baseline)
        if regressions:
            print("\nREGRESSIONS:")
            for r in regressions:
                print(f"  {r}")
            return 1
        print("No regressions vs baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
