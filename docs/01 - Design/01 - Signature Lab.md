# The Signature Lab — an AV-style pipeline that manufactures site intelligence

**Status**: DRAFT **Date**: 2026-08-27
**Applies to**: `bench/`, `uc_browser/health.py`, `uc_browser/registry.py`, `src/core/signature-store.js`, `uc_browser/status_server.py`
**Depends on**: `01 - Design/00 - Chat-Window-to-API Design.md` · `docs/06 - Roadmaps/01 - Signature Lab Roadmap.md` (build-out) · bench baseline (`bench/results/baseline.json`)

UC detects chat UIs the way an AV engine detects malware families: structural
heuristics first, learned classifiers second, cached signatures third. That
parallel suggests the missing organ — an **automated lab** that continuously
manufactures the cached tier: probe sites, derive per-site intelligence,
verify it, distribute it to every UC client, and ingest client failures as
new lab samples. This doc is the design; the roadmap doc holds the build-out.

## Hard constraints (non-negotiable)

1. **The generic engine is the product; signatures are a cache.** This is
   the deliberate inversion of the AV model (where signatures are the
   product). Every lab output is a warm-start or a fallback, never a
   dependency. A site with no signature MUST still work through the generic
   path.
2. **Cold-generic is the headline metric.** The bench MUST always score the
   generic engine with signatures disabled, reported separately from
   signature-warmed numbers. A feed that masks engine regressions
   reintroduces the self-grading-benchmark failure the bench was built to
   kill.
3. **No live detonation against human-staffed queues.** Lab probes of
   widget-class sites (customer-service chats) stop at *detect* level —
   composer found, nothing sent. Send-level verification is reserved for
   sites where the counterpart is a model, and is opt-in per site
   (`probe_level="send"` in the registry).
4. **Every signature carries a TTL and a revalidation path.** Stale
   intelligence served as fresh is worse than none. A signature past its
   TTL is advisory until a health probe re-confirms it.
5. **Site-specific knowledge lives in lab outputs and bench oracles only** —
   never in `src/`. The lab may *record* that Klarna's launcher is at a
   given selector; the engine may only *consume* that as data.

## The AV analogy, mapped honestly

| AV lab organ | UC equivalent | State (2026-08-27) |
|---|---|---|
| Sample zoo | MHTML fixture corpus `bench/fixtures/` | validated — 16 fixtures |
| Detonation chamber | Health prober (`uc_browser/health.py`: launcher-open, multi-frame detect) + offline fixture runner (`bench/run_bench.py`) | validated |
| Analyst ground truth | Oracle selectors `bench/sites.py` | validated |
| Signature compiler + DB | `src/core/signature-store.js`, `__UC_saveSignature` / `__UC_autoBindSignatures` | exists, **unused muscle** — nothing populates it systematically; schema too thin (fingerprint only) |
| Regression QA | Bench baseline + `pixi run bench-check` | validated |
| Distribution feed | — | missing |
| Telemetry return loop | — | missing |

Where the analogy breaks, design follows constraint #1: AV ships signature
updates because heuristics are the backstop; UC ships engine improvements
because signatures are the backstop.

## Architecture

```
            ┌────────────────────────  THE LAB  ────────────────────────┐
            │                                                           │
 registry ──►  1. CORPUS        capture MHTML of registered sites       │
 (sites.json)│     tier         + candidate pages (subpage crawl)       │
            │        │                                                  │
            │        ▼                                                  │
            │  2. DETONATION    offline: fixture runner (cheap, CI)     │
            │     tier          live: launcher-open + multi-frame       │
            │        │          detect; detect-only for widgets (#3)    │
            │        ▼                                                  │
            │  3. EXTRACTION    emit per-site: SITE PROFILE             │
            │     tier          (chat-page URL, launcher steps, frame   │
            │        │          path, signature fingerprints)           │
            │        ▼          + fixture + ML training label           │
            │  4. VERIFICATION  bench-style truth scoring; canary       │
            │     tier          round-trip ONLY where policy allows     │
            │        │                                                  │
            │        ▼                                                  │
            │  5. DISTRIBUTION  signatures.json — versioned feed,       │
            │     tier          served by status server                 │
            └────────┬──────────────────────────────────────────────────┘
                     ▼
       UC clients: __UC_autoBindSignatures at page load (warm start)
                     │ detection failure
                     ▼
            6. TELEMETRY: failure → captured fixture → back to tier 1
```

Six tiers; 1, 2, and 4 exist today in bench/health form. 3 needs a schema,
5 needs a file + endpoint, 6 needs a hook. The **analyst tier** — an agent
that investigates sites the automated pass can't crack — mounts on top of
tier 3 later (see the adaptive-onboarding roadmap note in project memory).

## The site profile (tier-3 output schema)

The current `SignatureStore` records a DOM fingerprint per domain. The lab's
unit of output is richer — everything a cold client needs to skip discovery:

| Field | Why it exists | Learned from |
|---|---|---|
| `chat_page_url` | The single biggest lesson of the 20-domain sweep: bots live on `/help`, `support.<domain>`, not the homepage | 12/20 brand sites `degraded` on homepage |
| `pre_steps` (launcher clicks, consent) | Widget chats hide behind fixed-position bubbles | Klarna + Squarespace pass only via launcher-open |
| `frame_path` | Widget composers live in cross-origin iframes | multi-frame detect requirement |
| `signature` (existing fingerprint) | DOM-shape warm start for `autoBind` | existing store |
| `verified_at`, `ttl_s`, `verify_level` (`detect`/`send`) | Constraint #4 | health-freshness model |
| `provenance` (`lab-auto` / `analyst-agent` / `user`) | Trust tiering when entries conflict | — |

## Why not just improve the generic engine instead?

Both, and the lab feeds the engine: tier-3 failures are labeled hard
examples for the DOM classifier (`scripts/train_code_classifier.py`), and
every lab pass adds a fixture generation to the bench corpus. The lab is
how the engine's training and regression data stops being hand-gathered.
One pass, four products: site profile, fixture, training label, freshness.

## Verification

```
pixi run bench                      # cold-generic headline numbers (constraint #2)
pixi run python bench/run_bench.py --check   # regression gate intact
```

Then decide: build Tier 0 of the roadmap (`06 - Roadmaps/01 - Signature
Lab Roadmap.md`) or park the lab and continue engine-gap work
(`06 - Roadmaps/00 - Engine Gaps.md`).
