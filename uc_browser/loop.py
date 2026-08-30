"""The self-strengthening loop: find chat windows → reverse them into
labeled data + signatures → strengthen the scanner → repeat.

Each round:
  1. FIND     — probe target sites; where a composer clears the gate,
  2. REVERSE  — extract the positive + same-page hard negatives + vendor
                signature (uc_browser.reverser), append to the corpus,
                and write a site profile so the next visit warm-starts;
  3. STRENGTHEN — retrain the code classifier from the accumulated
                reversed corpus (blended with any existing training data)
                to a CANDIDATE model, and report held-out accuracy. The
                shipped model is never overwritten in-loop — promotion is
                an explicit, measured decision.

The loop is the engine of the Signature Lab's closed cycle: the scanner
labels its own training data, so every round the corpus and the
signature feed grow, and the classifier's boundary sharpens on exactly
the same-page near-misses (search/email fields) that fooled it before.

Run:
  pixi run python -m uc_browser.loop --rounds 1        # degraded sites
  pixi run python -m uc_browser.loop --sites gymshark,ridge --rounds 2
  pixi run python -m uc_browser.loop --rounds 1 --retrain
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional

from uc_browser._paths import _SUBMODULE_ROOT
from uc_browser.health import HealthMonitor, HealthStore
from uc_browser.lab import _DISMISS_CONSENT_JS
from uc_browser.registry import SiteEntry, get_registry
from uc_browser.reverser import emit, load_corpus, reverse
from uc_browser.site_profiles import SiteProfile, get_profile_store

logger = logging.getLogger("uc_browser.loop")

MODELS_DIR = _SUBMODULE_ROOT / "models" / "dom_classifier"
CANDIDATE_MODEL = MODELS_DIR / "code_classifier_candidate.pkl"


class ReverseLoop:
    def __init__(self, monitor: Optional[HealthMonitor] = None):
        self.monitor = monitor or HealthMonitor()

    # ── find + reverse over one site ────────────────────────────────

    def process_site(self, ctx, site: SiteEntry) -> dict:
        bundle = self.monitor._bundle_js()
        out = {"site": site.name, "found": False, "rows": 0,
               "vendor": None, "negatives": 0}
        page = ctx.new_page()
        try:
            # Prefer a known chat page (profile), else the registered URL.
            target = site.url
            prof = get_profile_store().fresh(site.name)
            if prof:
                target = prof.chat_page_url
            page.goto(target, timeout=35000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            for frame in page.frames:
                try:
                    frame.evaluate(_DISMISS_CONSENT_JS)
                except Exception:
                    pass
            # Inject the bundle in every frame so finder + feature helpers exist.
            for frame in page.frames:
                try:
                    frame.evaluate(bundle)
                except Exception:
                    pass

            # Open the widget (vendor fast path, then generic launcher).
            score, _ = self.monitor._detect_across_frames(page, bundle)
            if score < 4:
                try:
                    from uc_browser import vendor_signatures as vs
                    vendors = vs.identify(page)
                    if vendors:
                        vs.open_widget(page, vendors[0])
                        page.wait_for_timeout(3000)
                except Exception:
                    pass
            if score < 4:
                self.monitor._try_open_widget(page)
                page.wait_for_timeout(2500)
            # Re-inject after open (new iframes may have appeared).
            for frame in page.frames:
                try:
                    frame.evaluate(bundle)
                except Exception:
                    pass

            rc = reverse(page, source_url=target)
            if rc is None:
                return out
            out["found"] = True
            out["vendor"] = rc.vendor
            out["negatives"] = len(rc.negatives)
            out["rows"] = emit(rc)
            # Write/refresh a profile so the win is durable + warm-startable.
            get_profile_store().upsert(SiteProfile(
                site=site.name, chat_page_url=target,
                pre_steps=prof.pre_steps if prof else [],
                frame_url_hint=rc.frame_url, detect_score=rc.score,
                verified_at=time.time(), provenance="reverse-loop",
                notes=f"reversed {time.strftime('%Y-%m-%d')}"
                      + (f", vendor={rc.vendor}" if rc.vendor else ""),
            ))
            return out
        except Exception as e:
            out["error"] = str(e)[:120]
            return out
        finally:
            page.close()

    # ── one round over many sites ───────────────────────────────────

    def round(self, sites: list[SiteEntry]) -> list[dict]:
        from playwright.sync_api import sync_playwright

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
                logger.info("loop: %s", site.name)
                results.append(self.process_site(ctx, site))
            ctx.close()
        return results

    # ── strengthen: retrain classifier from the reversed corpus ─────

    def strengthen(self) -> dict:
        """Retrain a candidate code classifier from the reversed corpus.
        Reports held-out accuracy; never overwrites the shipped model."""
        X, y, n = load_corpus()
        result = {"corpus_rows": n}
        classes = set(y)
        if n < 20 or len(classes) < 2:
            result["skipped"] = (f"need >=20 rows and >=2 classes "
                                 f"(have {n} rows, {len(classes)} classes)")
            return result
        try:
            import joblib
            import numpy as np
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.metrics import accuracy_score
            from sklearn.model_selection import train_test_split
        except ImportError:
            result["skipped"] = "sklearn/joblib not installed"
            return result

        Xn = np.array(X, dtype=np.float32)
        yn = np.array(y)
        strat = yn if all(list(yn).count(c) >= 2 for c in classes) else None
        Xtr, Xva, ytr, yva = train_test_split(
            Xn, yn, test_size=0.25, random_state=42, stratify=strat)
        clf = RandomForestClassifier(n_estimators=200, random_state=42)
        clf.fit(Xtr, ytr)
        acc = accuracy_score(yva, clf.predict(Xva))
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(clf, str(CANDIDATE_MODEL))
        result.update({"val_accuracy": round(float(acc), 3),
                       "classes": sorted(classes),
                       "candidate_model": str(CANDIDATE_MODEL)})
        return result


def _default_sites() -> list[SiteEntry]:
    latest = HealthStore().latest()
    reg = get_registry()
    degraded = [e for e in reg.list()
                if latest.get(e.name, {}).get("status") == "degraded"]
    return degraded or [e for e in reg.list() if e.kind == "chat"][:5]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", help="comma-separated site names")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--retrain", action="store_true",
                    help="after the rounds, retrain the candidate classifier")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    if args.sites:
        reg = get_registry()
        sites = []
        for name in args.sites.split(","):
            e = reg.get(name.strip())
            if not e:
                # allow ad-hoc URL:name pairs? keep simple: require registry.
                print(f"unknown site: {name}")
                return 2
            sites.append(e)
    else:
        sites = _default_sites()
    if not sites:
        print("No target sites.")
        return 0

    loop = ReverseLoop()
    total_rows = 0
    for r in range(args.rounds):
        print(f"\n=== round {r + 1}/{args.rounds} — find + reverse "
              f"{len(sites)} site(s) ===")
        results = loop.round(sites)
        found = sum(x["found"] for x in results)
        rows = sum(x["rows"] for x in results)
        total_rows += rows
        for x in results:
            if x["found"]:
                print(f"  reversed {x['site']:16s} +{x['rows']} rows "
                      f"({x['negatives']} neg)"
                      + (f" vendor={x['vendor']}" if x["vendor"] else ""))
            else:
                print(f"  ---      {x['site']:16s} "
                      f"{x.get('error', 'no composer')[:40]}")
        print(f"  round: {found}/{len(sites)} reversed, +{rows} training rows")

    X, y, n = load_corpus()
    print(f"\ncorpus now {n} rows across {len(set(y))} classes "
          f"→ data/training/reversed_chats.jsonl")
    if args.retrain:
        print("strengthen: retraining candidate classifier ...")
        s = loop.strengthen()
        if s.get("skipped"):
            print(f"  skipped: {s['skipped']}")
        else:
            print(f"  candidate val accuracy {s['val_accuracy']} on "
                  f"{s['corpus_rows']} rows, classes {s['classes']}")
            print(f"  → {s['candidate_model']} (shipped model untouched; "
                  "promote explicitly after a bench check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
