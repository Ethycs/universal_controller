# Signature Lab — build-out roadmap

**Status:** Living Document — 2026-08-27
**Applies to:** the lab designed in `01 - Design/01 - Signature Lab.md`
**Depends on:** bench baseline (`bench/results/baseline.json`) · health monitor (`uc_browser/health.py`) · registry (`uc_browser/registry.py`)

## Current status

- ✅ Sample zoo, detonation chamber, ground truth, regression QA — exist as bench/health (validated)
- ✅ Signature store + `autoBind` runtime — exists in `src/core/signature-store.js` (**unused muscle**, fingerprint-only schema)
- ✅ Site-profile schema (tier 3) — `uc_browser/site_profiles.py`, `data/site_profiles.json` (UNCOMMITTED)
- ✅ Subpage-crawling lab pass — `uc_browser/lab.py` (`pixi run lab`); first live pass landed 2026-08-27: 7/11 degraded sites gained profiles
- ✅ ~50% Distribution feed — `GET /signatures` served by status server; client consumption still 🔲
- 🔲 Telemetry return loop
- ✅ ~50% Cold/warm bench split — bench output now explicitly labeled `mode: cold-generic`; warm mode itself 🔲
- 🔲 Analyst agent tier

Target v1 outcome, measurable: the 20-domain brand sweep moves from
**2/20 detect-ok (homepage-only)** to a majority detect-ok via lab-derived
chat-page URLs + launcher steps — without a single site-specific line in `src/`.

**Measured 2026-08-27 (validated):** re-probe with profiles active =
**8/20 detect-ok** (was 2/20), all 7 lab profiles confirmed `[profile]`
under the prober, and profiled probes run 2-10x faster (1-5 s vs 9-25 s —
straight to the chat page, no homepage + launcher hunt). Remaining:
duolingo/klm/lemonade/target resisted the automated pass (analyst-tier
candidates); aeromexico/marriott/h-m bot-challenged; klarna flaky
(passed launcher-path earlier, degraded this sweep — next lab pass will
profile it); 4 `login` classifications still suspect (heuristic misfires).

## Tier 0 — Quick wins (<1 day each)

| Gap | Depends on | Blocks | Status |
|---|---|---|---|
| **Site-profile schema**: extend registry entries (or a parallel `data/site_profiles.json`) with `chat_page_url`, `pre_steps`, `frame_path`, `verified_at`, `ttl_s`, `provenance` | nothing | every other tier | 🔲 |
| **Cold/warm bench split**: `run_bench.py --signatures` flag; headline table stays cold-generic (hard constraint #2 of the design) | nothing | trusting any signature work | 🔲 |
| **Health prober honors `chat_page_url`** when present (probe the chat page, not the domain root) | schema | honest brand-site uptime | 🔲 |
| **Serve `signatures.json`** from the status server (`GET /signatures`), versioned, TTL-annotated | schema | client consumption | 🔲 |

## Tier 1 — The v1 lab pass (1–3 days)

| Gap | Depends on | Blocks | Status |
|---|---|---|---|
| **Subpage crawler**: for each `degraded` site, collect candidate chat pages (links matching help/support/contact/chat + `support.<domain>`), cap ~4/site | schema | lab pass | 🔲 |
| **Lab pass = crawl → launcher-open → multi-frame detect → emit** site profile + MHTML fixture + probe record; detect-only for widget kind (design constraint #3) | crawler | the 2/20 → majority goal | 🔲 |
| **Fixture ingestion**: lab-emitted fixtures join `bench/fixtures/` with oracle-less "unannotated" status until truth is added | lab pass | corpus growth across UI generations | 🔲 |
| **ML label emission**: detection failures logged as labeled hard examples for `scripts/train_code_classifier.py` | lab pass | classifier flywheel | 🔲 |

## Tier 2 — Closing the loop (3–7 days)

| Gap | Depends on | Blocks | Status |
|---|---|---|---|
| **Client consumption**: UCBrowser loads the feed; `__UC_autoBindSignatures` warm-starts from site profiles; generic path remains the fallback (constraint #1) | feed | latency wins, brand-site coverage in product | 🔲 |
| **TTL revalidation**: health scheduler re-probes signatures nearing expiry; expired → advisory | feed, prober | trustworthy feed | 🔲 |
| **Telemetry return**: on generic-path detection failure, capture MHTML + context into a lab inbox (local dir first; no phone-home) | client consumption | self-improving corpus | 🔲 |
| **Send-level verification stage** where policy allows (model-backed sites only): proves dispatch + extraction, upgrades `verify_level` | lab pass | "callable" claims stronger than detect | 🔲 |

## Tier 3 — Analyst tier (agentic; discuss before building)

| Gap | Depends on | Blocks | Status |
|---|---|---|---|
| **Analyst agent**: investigates sites the automated pass can't crack (consent flows, odd DOMs, aeromexico/marriott-class challenges); same outputs as tier-3, `provenance: analyst-agent` | lab pass | long-tail coverage | 🔲 — bookmarked, needs user sign-off |
| **Discovery**: propose *new* sites (directory crawls); human approves before registry entry | analyst | — | 🔲 — proposal-only by policy |

## Explicitly out of scope

- Signatures for login-required LLM sites (claude/gemini/etc.) — blocked on
  logged-in capture profiles, tracked in `00 - Engine Gaps.md`.
- Any canary sends to human-staffed support queues (design constraint #3;
  not a gap, a policy).

**Verification / first action:** `pixi run bench-check` must stay green
before and after each tier lands; Tier 0 "cold/warm split" is the first
item to build because every later tier's honesty depends on it.
