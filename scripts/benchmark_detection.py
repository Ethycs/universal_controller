"""Benchmark UC heuristic vs ML classifier on live pages.

Loads real sites with the UC extension, runs both detection methods,
and compares results side by side.

Usage:
  pixi run python scripts/benchmark_detection.py
  pixi run python scripts/benchmark_detection.py --headless
"""

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logger = logging.getLogger("benchmark")

# ── Test sites with known ground truth ────────────────────────────────

BENCHMARK_SITES = [
    # Chat interfaces (ground truth: chat_input)
    {"url": "https://chat.openai.com", "truth": "chat_input", "name": "chatgpt"},
    {"url": "https://claude.ai", "truth": "chat_input", "name": "claude"},
    {"url": "https://gemini.google.com", "truth": "chat_input", "name": "gemini"},
    {"url": "https://copilot.microsoft.com", "truth": "chat_input", "name": "copilot"},
    {"url": "https://www.perplexity.ai", "truth": "chat_input", "name": "perplexity"},
    {"url": "https://poe.com", "truth": "chat_input", "name": "poe"},
    {"url": "https://www.deepseek.com", "truth": "chat_input", "name": "deepseek"},
    # Chat widget platforms (ground truth: chat_input — they show demo widgets)
    {"url": "https://www.tidio.com", "truth": "chat_input", "name": "tidio"},
    {"url": "https://www.chatbase.co", "truth": "chat_input", "name": "chatbase"},
    {"url": "https://botpress.com", "truth": "chat_input", "name": "botpress"},
    # Search interfaces (ground truth: search)
    {"url": "https://www.google.com", "truth": "search", "name": "google"},
    {"url": "https://duckduckgo.com", "truth": "search", "name": "ddg"},
    {"url": "https://www.bing.com", "truth": "search", "name": "bing"},
    # Form-heavy pages (ground truth: form_field)
    {"url": "https://getbootstrap.com/docs/5.3/forms/form-control/", "truth": "form_field", "name": "bootstrap-forms"},
    {"url": "https://ant.design/components/input", "truth": "form_field", "name": "antd-input"},
    # Navigation-heavy pages (ground truth: navigation)
    {"url": "https://getbootstrap.com/docs/5.3/components/navs-tabs/", "truth": "navigation", "name": "bootstrap-nav"},
    # Modal examples (ground truth: modal)
    {"url": "https://getbootstrap.com/docs/5.3/components/modal/", "truth": "modal", "name": "bootstrap-modal"},
]

# Code features JS (same as in scraper / UC extension)
_CODE_FEATURES_JS = r"""(selector) => {
    const root = selector ? document.querySelector(selector) : document.body;
    if (!root) return null;
    const all = root.querySelectorAll('*');
    const totalEls = all.length || 1;
    const tags = {};
    let interactive = 0;
    const iTags = new Set(['INPUT','TEXTAREA','BUTTON','SELECT','A']);
    for (const el of all) {
        tags[el.tagName] = (tags[el.tagName]||0) + 1;
        if (iTags.has(el.tagName) || el.contentEditable === 'true') interactive++;
    }
    const cls = Array.from(all).map(e => ((e.className||'')+' '+(e.id||'')).toLowerCase()).join(' ');
    const text = (root.innerText||'').toLowerCase();
    const rect = root.getBoundingClientRect();
    return {
        n_input: tags['INPUT']||0, n_textarea: tags['TEXTAREA']||0,
        n_button: tags['BUTTON']||0, n_select: tags['SELECT']||0,
        n_a: tags['A']||0, n_iframe: root.querySelectorAll('iframe').length,
        interactive_ratio: interactive/totalEls,
        depth: (function d(e,n){let m=n;for(const c of e.children)m=Math.max(m,d(c,n+1));return m})(root,0),
        child_count: root.children.length,
        has_role: root.querySelectorAll('[role]').length,
        has_aria_label: root.querySelectorAll('[aria-label]').length,
        has_placeholder: root.querySelectorAll('[placeholder]').length,
        has_contenteditable: root.querySelectorAll('[contenteditable="true"]').length,
        role_textbox: root.querySelectorAll('[role="textbox"]').length,
        role_dialog: root.querySelectorAll('[role="dialog"]').length,
        role_search: root.querySelectorAll('[role="search"],[role="searchbox"]').length,
        role_navigation: root.querySelectorAll('[role="navigation"]').length,
        role_form: root.querySelectorAll('[role="form"]').length,
        kw_chat: /chat|message|compose|messenger/i.test(cls)?1:0,
        kw_search: /search|find|query|autocomplete|combobox/i.test(cls)?1:0,
        kw_login: /login|signin|sign-in|auth|password/i.test(cls)?1:0,
        kw_modal: /modal|dialog|overlay|popup|drawer/i.test(cls)?1:0,
        kw_nav: /nav|menu|sidebar|breadcrumb|tabs|pagination/i.test(cls)?1:0,
        kw_form: /form|field|input|label|control/i.test(cls)?1:0,
        kw_feed: /feed|list|card|grid|item|article/i.test(cls)?1:0,
        word_count: text.split(/\s+/).filter(w=>w.length>0).length,
        has_send: /\bsend\b|\bsubmit\b|\bpost\b/i.test(text)?1:0,
        has_search_text: /\bsearch\b|\bfind\b/i.test(text)?1:0,
        has_login_text: /\blogin\b|\bsign in\b|\bpassword\b/i.test(text)?1:0,
        has_live_region: root.querySelector('[aria-live]')?1:0,
        is_fixed: getComputedStyle(root).position==='fixed'?1:0,
        is_bottom_right: (rect.bottom>window.innerHeight*0.7&&rect.right>window.innerWidth*0.7)?1:0,
        rel_width: Math.round(rect.width/window.innerWidth*100)/100,
        rel_height: Math.round(rect.height/window.innerHeight*100)/100,
    };
}"""

CODE_FEATURE_NAMES = [
    "n_input", "n_textarea", "n_button", "n_select", "n_a", "n_iframe",
    "interactive_ratio", "depth", "child_count",
    "has_role", "has_aria_label", "has_placeholder", "has_contenteditable",
    "role_textbox", "role_dialog", "role_search", "role_navigation", "role_form",
    "kw_chat", "kw_search", "kw_login", "kw_modal", "kw_nav", "kw_form", "kw_feed",
    "word_count", "has_send", "has_search_text", "has_login_text", "has_live_region",
    "is_fixed", "is_bottom_right", "rel_width", "rel_height",
]

LABELS = [
    "search", "chat_input", "form_field", "modal",
    "login_form", "button", "navigation", "data_display",
]

# UC pattern → our label mapping
UC_TO_LABEL = {
    "chat": "chat_input",
    "search": "search",
    "form": "form_field",
    "modal": "modal",
    "login": "login_form",
    "feed": "data_display",
    "cookie": None,      # not a UI pattern we classify
    "dropdown": None,
}


def extension_args() -> list[str]:
    """Return Chromium launch args to load the UC extension, or [] if not available.

    Inlined here so this script stays self-contained inside UC (no event_tool dep).
    """
    ext_dir = Path(__file__).resolve().parent.parent / "extension"
    if not ext_dir.is_dir() or not (ext_dir / "manifest.json").exists():
        return []
    ext_path = str(ext_dir.resolve())
    return [
        f"--load-extension={ext_path}",
        f"--disable-extensions-except={ext_path}",
    ]


def run_benchmark(headless: bool = True):
    import joblib

    model_path = Path("models/dom_classifier/code_classifier.pkl")
    if not model_path.exists():
        logger.error("No code classifier found. Train first.")
        return

    code_clf = joblib.load(str(model_path))

    from playwright.sync_api import sync_playwright

    results = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            "",
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                *extension_args(),
            ],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for site in BENCHMARK_SITES:
            url = site["url"]
            truth = site["truth"]
            name = site["name"]

            logger.info("%-20s %s", name, url)

            try:
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning("  SKIP (load failed): %s", e)
                results.append({
                    "name": name, "truth": truth,
                    "uc_label": None, "uc_conf": 0,
                    "ml_label": None, "ml_conf": 0,
                })
                continue

            # ── UC detection ──
            uc_label = None
            uc_conf = 0.0
            uc_all_hits = {}
            try:
                # Wait for UC extension
                page.wait_for_function(
                    "window.__UC && window.__UC.ready === true",
                    timeout=5000,
                )
                uc_patterns = page.evaluate("window.__UC_detectAll('STRUCTURAL')")

                # Collect all detections
                best_uc = None
                for pattern_name, hits in (uc_patterns or {}).items():
                    mapped = UC_TO_LABEL.get(pattern_name)
                    if not mapped or not hits:
                        continue
                    top_conf = hits[0].get("confidence", 0)
                    uc_all_hits[mapped] = (len(hits), top_conf)
                    if not best_uc or top_conf > best_uc[1]:
                        best_uc = (mapped, top_conf)

                # Prefer detection that matches truth if it exists
                truth_pattern = next(
                    (k for k, v in UC_TO_LABEL.items() if v == truth), None,
                )
                if truth_pattern and uc_patterns:
                    hits = uc_patterns.get(truth_pattern, [])
                    if hits:
                        best_uc = (truth, hits[0].get("confidence", 0))

                if best_uc:
                    uc_label, uc_conf = best_uc

                # Also try findInputs for chat/search
                if truth in ("chat_input", "search") and not uc_label:
                    inputs = page.evaluate("window.__UC_findInputs()")
                    if inputs and len(inputs) > 0:
                        top = inputs[0]
                        # UC scores inputs by chat-likelihood
                        if top.get("score", 0) > 3:
                            uc_label = "chat_input"
                            uc_conf = min(top["score"] / 10, 1.0)
                        elif top.get("score", 0) > 1:
                            uc_label = "search"
                            uc_conf = min(top["score"] / 10, 1.0)

            except Exception as e:
                logger.debug("  UC detection failed: %s", e)

            # ── ML classification (element-level) ──
            # Find candidate elements, classify each, pick best match
            ml_label = None
            ml_conf = 0.0
            ml_detail = ""
            try:
                # Discover interactive containers (same approach UC would use)
                candidate_selectors = page.evaluate("""() => {
                    const seen = new Set();
                    const sels = [];

                    // Find all interactive elements and walk up to containers
                    const inputs = document.querySelectorAll(
                        'input, textarea, [contenteditable="true"], [role="textbox"], '
                        + '[role="search"], [role="dialog"], [role="navigation"], '
                        + 'nav, form, [class*="chat" i], [class*="modal" i], '
                        + '[class*="search" i], [class*="nav" i], [class*="widget" i]'
                    );

                    for (const el of inputs) {
                        // Walk up to find a meaningful container (2-4 levels)
                        let target = el;
                        for (let i = 0; i < 3; i++) {
                            if (target.parentElement &&
                                target.parentElement !== document.body &&
                                target.parentElement.children.length < 20) {
                                target = target.parentElement;
                            }
                        }

                        // Tag it
                        const id = 'bench-' + sels.length;
                        if (seen.has(target)) continue;
                        seen.add(target);
                        target.setAttribute('data-bench-id', id);
                        sels.push('[data-bench-id="' + id + '"]');
                        if (sels.length >= 30) break;
                    }
                    return sels;
                }""")

                best_ml = None
                for sel in (candidate_selectors or []):
                    try:
                        feats = page.evaluate(_CODE_FEATURES_JS, sel)
                        if not feats:
                            continue
                        X = np.array(
                            [[feats.get(f, 0) for f in CODE_FEATURE_NAMES]],
                            dtype=np.float32,
                        )
                        probs = code_clf.predict_proba(X)[0]
                        best_idx = int(np.argmax(probs))
                        label = LABELS[best_idx]
                        conf = float(probs[best_idx])

                        if not best_ml or conf > best_ml[1]:
                            best_ml = (label, conf, sel)

                        # Also track best match for the truth class
                        truth_idx = LABELS.index(truth) if truth in LABELS else -1
                        if truth_idx >= 0:
                            truth_conf = float(probs[truth_idx])
                            if truth_conf > 0.3 and (
                                not best_ml or
                                (label == truth and conf >= best_ml[1])
                            ):
                                best_ml = (label, conf, sel)
                    except Exception:
                        continue

                # Clean up bench tags
                page.evaluate("""() => {
                    document.querySelectorAll('[data-bench-id]').forEach(
                        el => el.removeAttribute('data-bench-id')
                    );
                }""")

                if best_ml:
                    ml_label, ml_conf, ml_detail = best_ml

            except Exception as e:
                logger.debug("  ML classification failed: %s", e)

            uc_match = "✓" if uc_label == truth else "✗"
            ml_match = "✓" if ml_label == truth else "✗"

            logger.info(
                "  truth=%-12s  UC: %s %-12s (%.2f)  ML: %s %-12s (%.2f)",
                truth,
                uc_match, uc_label or "none", uc_conf,
                ml_match, ml_label or "none", ml_conf,
            )

            results.append({
                "name": name, "truth": truth,
                "uc_label": uc_label, "uc_conf": round(uc_conf, 3),
                "ml_label": ml_label, "ml_conf": round(ml_conf, 3),
            })

        ctx.close()

    # ── Summary ──
    print()
    print("=" * 72)
    print("  BENCHMARK RESULTS")
    print("=" * 72)
    print()
    print(f"{'site':20s} {'truth':12s} {'UC':15s} {'ML':15s}")
    print("-" * 72)

    uc_correct = 0
    ml_correct = 0
    total = 0
    per_class = defaultdict(lambda: {"uc": 0, "ml": 0, "n": 0})

    for r in results:
        truth = r["truth"]
        uc_ok = r["uc_label"] == truth
        ml_ok = r["ml_label"] == truth
        uc_str = f"{'✓' if uc_ok else '✗'} {r['uc_label'] or 'none':12s}"
        ml_str = f"{'✓' if ml_ok else '✗'} {r['ml_label'] or 'none':12s}"
        print(f"{r['name']:20s} {truth:12s} {uc_str} {ml_str}")

        total += 1
        if uc_ok:
            uc_correct += 1
        if ml_ok:
            ml_correct += 1
        per_class[truth]["n"] += 1
        if uc_ok:
            per_class[truth]["uc"] += 1
        if ml_ok:
            per_class[truth]["ml"] += 1

    print("-" * 72)
    print(f"{'TOTAL':20s} {'':12s} {uc_correct}/{total}             {ml_correct}/{total}")
    print()
    print(f"  UC accuracy:  {uc_correct/max(total,1)*100:.0f}%")
    print(f"  ML accuracy:  {ml_correct/max(total,1)*100:.0f}%")
    print()

    print("Per-class:")
    for cls in sorted(per_class):
        d = per_class[cls]
        n = d["n"]
        print(
            f"  {cls:15s}  UC: {d['uc']}/{n} ({d['uc']/n*100:.0f}%)  "
            f"ML: {d['ml']}/{n} ({d['ml']/n*100:.0f}%)"
        )

    # Save results
    out = Path("data/training/benchmark_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nResults saved → {out}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark UC vs ML detection")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    run_benchmark(headless=args.headless)


if __name__ == "__main__":
    main()
