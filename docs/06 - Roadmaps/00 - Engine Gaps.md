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

## Degraded brand-site triage (2026-08-28, validated)

Diagnosed the 5 `degraded` brand domains directly. Finding: **4 of 5 have
no reachable on-site chat composer** — the degraded verdict is *correct*,
not a detection bug:

| Site | Reality | Verdict |
|---|---|---|
| duolingo | reCAPTCHA wall; support bot is in-app | correct degraded |
| klm | reCAPTCHA; BlueBot lives in Messenger/WhatsApp, site routes to contact forms | correct degraded |
| lemonade | Maya lives in the app quote flow; site has only a `mailto:` | correct degraded |
| target | help pages top out at `#email-address` (an email field) score 3.94 — **gate correctly rejects a non-chat input** | correct degraded |
| klarna | real chat widget on homepage but **flaky** (scored 4.2 one load, 0.0 another — hydration race) | genuine target |

Takeaways: (1) the 3.94 near-miss is NOT gate-tuning bait — lowering the
gate would admit an email field; the gate is right. (2) Coverage ceiling
on this list is bounded by brands putting bots in-app / behind external
messaging, not by UC's engine. (3) Only klarna is a genuine
engine/timing target: needs a longer hydration settle before the detect
scan. Tracked as the flaky-hydration item below.

## Vendor-zoo finding (2026-08-28, validated)

Probed 20 chat-widget vendors (each dogfoods its own engine). **7/20
detected + captured as fixtures** (`bench/fixtures/vendor-*`): tawk 7.8,
chatling 7.2, drift 6.8, intercom-fin 6.0, livechat 5.2, zoho-salesiq
4.5, tidio-demo 4.2 — **all iframe-based, composer present or launcher
worked.**

The other 13 scored ~0.0. Hypothesized shadow-DOM piercing gap;
**verified false** — crisp/intercom/zendesk have no composer in the DOM
at probe time at all (crisp 0 inputs anywhere, intercom 1 shadow host but
0 inputs in it, zendesk only light-DOM search boxes). Actual gap: **the
generic launcher-open heuristic has low recall on modern widgets** — it
doesn't click their launcher, so the composer iframe never injects. This
is an interaction/timing gap, not a detection gap.

| Fix | Depends on | Blocks | Status |
|---|---|---|---|
| Launcher-open recall: widget launchers are often a fixed bottom-right button/iframe with vendor-classed wrappers or an SVG-only label — broaden `_try_open_widget` candidate scoring + click both the element and its center-point | nothing | 13/20 vendors, most brand widgets | 🔲 |
| Re-detect after async widget load: poll for the composer for ~5s post-click (iframe injects late) instead of a single 2.5s wait | nothing | post-click detection | 🔲 |
| The 7 vendor fixtures need oracle annotation to become *scored* (currently skipped/unannotated) | oracle pass | regression scoring of widget engines | 🔲 |

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
