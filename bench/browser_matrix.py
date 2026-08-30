"""Cross-engine detection matrix — probe each site on Chromium, Firefox,
and WebKit and compare, in case browser type changes what UC detects.

Detection differences DO happen across engines: a widget may hydrate only
on Chromium, resource-timing (the vendor scanner's signal) differs, and
anti-bot walls treat engines differently. This harness makes that visible
instead of assuming Chromium generalizes.

The matrix keeps per-engine results in throwaway stores — it never writes
the production (Chromium) health store, so it's a pure diagnostic. It
flags AGREEMENT (all engines same verdict) vs DISAGREEMENT (engines
differ — the interesting rows).

Usage:
  pixi run python -m bench.browser_matrix                 # default sites
  pixi run python -m bench.browser_matrix --sites grok,chatgpt,tawk
  pixi run python -m bench.browser_matrix --engines chromium,firefox
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uc_browser.health import HealthMonitor, HealthStore  # noqa: E402
from uc_browser.registry import SiteEntry, get_registry  # noqa: E402

# "chrome" = installed Google Chrome (real fingerprint, passes bot walls
# bundled Chromium trips); the rest are Playwright engines.
ALL_ENGINES = ["chromium", "chrome", "firefox", "webkit"]


def available_engines(requested: list[str]) -> list[str]:
    from playwright.sync_api import sync_playwright

    ok = []
    with sync_playwright() as p:
        for e in requested:
            try:
                if e == "chrome":
                    b = p.chromium.launch(headless=True, channel="chrome")
                else:
                    bt = {"chromium": p.chromium, "firefox": p.firefox,
                          "webkit": p.webkit}.get(e)
                    if bt is None:
                        continue
                    b = bt.launch(headless=True)
                b.close()
                ok.append(e)
            except Exception:
                hint = ("install Google Chrome" if e == "chrome"
                        else f"playwright install {e}")
                print(f"  (engine {e} unavailable — skipping; {hint})")
    return ok


def run_matrix(sites: list[SiteEntry], engines: list[str]) -> dict:
    """{site_name: {engine: ProbeResult}} — each engine in its own store."""
    matrix: dict = {s.name: {} for s in sites}
    for engine in engines:
        print(f"\n── engine: {engine} ──", flush=True)
        with tempfile.TemporaryDirectory() as td:
            mon = HealthMonitor(store=HealthStore(base_dir=Path(td)),
                                engine=engine)
            for r in mon.probe_sites(sites):
                matrix[r.site][engine] = r
                print(f"  {r.site:16s} {r.status:9s} "
                      f"{r.detail[:40]}", flush=True)
    return matrix


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sites", help="comma-separated registry site names")
    ap.add_argument("--engines", default=",".join(ALL_ENGINES))
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    reg = get_registry()
    if args.sites:
        sites = []
        for n in args.sites.split(","):
            e = reg.get(n.strip())
            if not e:
                print(f"unknown site: {n}")
                return 2
            sites.append(e)
    else:
        # A spread: fast LLM sites + a widget + a brand with a profile.
        want = ["chatgpt", "grok", "perplexity", "pi", "lmarena"]
        sites = [reg.get(n) for n in want if reg.get(n)]

    engines = available_engines(args.engines.split(","))
    if len(engines) < 2:
        print(f"Need >=2 engines for a matrix (have {engines}).")
        return 1
    print(f"Matrix: {len(sites)} sites x {len(engines)} engines "
          f"({', '.join(engines)})")

    matrix = run_matrix(sites, engines)

    # ── comparison table ──
    print("\n" + "=" * (18 + 11 * len(engines)))
    print(f"{'site':18s}" + "".join(f"{e:11s}" for e in engines) + "verdict")
    print("-" * (26 + 11 * len(engines)))
    agree = disagree = 0
    disagreements = []
    for name, per in matrix.items():
        cells = "".join(f"{per.get(e).status if per.get(e) else '-':11s}"
                        for e in engines)
        statuses = {per[e].status for e in engines if e in per}
        same = len(statuses) == 1
        agree += same
        disagree += not same
        verdict = "agree" if same else "DIFFER"
        if not same:
            disagreements.append((name, {e: per[e].status for e in per}))
        print(f"{name:18s}{cells}{verdict}")
    print("-" * (26 + 11 * len(engines)))
    print(f"{agree}/{len(matrix)} agree across engines; {disagree} differ")
    if disagreements:
        print("\nDISAGREEMENTS (browser type changes the verdict):")
        for name, d in disagreements:
            print(f"  {name}: {d}")
        print("\n→ These sites are engine-sensitive; the production probe "
              "(Chromium) may not reflect other engines.")
    else:
        print("\nAll probed sites agree across engines — detection is "
              "engine-robust for this set.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
