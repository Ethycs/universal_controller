# Engine Gaps

**Status:** Living Document — 2026-08-27
**Applies to:** `src/` engine, `bench/` scoring coverage, `uc_browser/health.py` heuristics
**Depends on:** `bench/results/baseline.json` (the numbers below come from it and from the 2026-08-27 domain sweep — see `docs/07 - Status Reports/2026-08-27_session-handoff.md`)

## Current status

- ✅ Input pipeline: 7/7 fixtures, send_top1 6/6, container_top1 1/1 (only 1 scorable)
- 🟡 Negatives: 1 false positive of 7 (bootstrap-forms) — the gate has no headroom
- 🔲 Send-level verification: not measured at all — the bench stops at detection
- 🔲 Login-walled and widget-iframe coverage: large blind spots in the corpus

## Tier 0 — Quick wins

| Gap | Depends on | Blocks | Status |
|---|---|---|---|
| False-positive gate headroom: bootstrap-forms datalist input scores 5.9 ≥ the 4.0 gate (`fp_input` true); google (3.62) and hackernews (3.56) sit uncomfortably close under it | — | Trusting `input_gate` on unseen non-chat pages; raising site coverage without FP creep | 🔲 Open |
| Chat-container detector ranks junk: `html>head` and zero-size `aria-live` announcer nodes can win `detectAll('STRUCTURAL').chat` — needs a visibility/size floor before scoring | — | Meaningful `container_top1` beyond the synthetic fixture | 🔲 Open |
| Login-wall heuristic misfires on marketing pages: lego / lg / sephora classified `login` because "sign in / sign up" text hits, though no wall blocks a chat | — | Honest sweep statuses; availability gate wrongly refusing those sites | 🔲 Open |

## Tier 1 — Real work

| Gap | Depends on | Blocks | Status |
|---|---|---|---|
| Widget iframes never load in capture: tidio and intercom fixtures carry no truth annotation and are skipped — the third-party iframe content is absent from the MHTML | Capture-time iframe settling (or an interactive capture step) | Scoring the entire widget class the iframe RPC exists for | 🔲 Open |
| LM Arena dual-panel battle UI stalls response extraction: two concurrent streams defeat the trigram-diff extractor | — | Reliable `uc/lmarena` completions (detection passes; extraction hangs) | 🔲 Open |
| Send-level verification stage missing from bench: detection ≠ dispatch ≠ extraction — nothing offline scores whether `chatSend` actually dispatches and a reply lands | Fixture design for actuation (MHTML pages can't round-trip) | Catching regressions in the half of the product after detection | 🔲 Open |
| grok logged-out signup wall blocks responses on `uc/grok-generic` | Logged-in profile support in the generic client + per-adapter profiles | The adapter-vs-generic A/B on grok.com; any logged-in generic site | 🔲 Open |

## Tier 2 — Corpus expansion

| Gap | Depends on | Blocks | Status |
|---|---|---|---|
| Active conversations in fixtures: `container_top1` is scorable on only 1 fixture (synthetic-chat) — real captures are empty-conversation | Logged-in / interactive capture (`--include-login`) | Any confidence in message-stream detection on real sites | 🔲 Open |
| Login-required site coverage: claude, gemini, deepseek, poe, copilot, huggingchat all skipped | Same logged-in capture machinery | Generalization evidence on the highest-value chat UIs | 🔲 Open |
| Brand-site chat-page URLs: 12 sweep domains are `degraded` because the bot lives on a subpage (help/contact), not the landing page we probe | Per-site `chat_url` field in the registry + a discovery pass | Converting the imported brand domains from `degraded` to `ok` | 🔲 Open |

## Verification

```bash
pixi run bench          # current numbers vs the claims above
pixi run bench-check    # regression gate — must stay green while closing gaps
```

Closing any Tier 0 gap changes scores: rerun the bench, then `bench-baseline` deliberately (a baseline update is a claim, not a chore).
