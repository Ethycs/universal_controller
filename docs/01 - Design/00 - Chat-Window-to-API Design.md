# Chat-Window-to-API Design

**Status**: CURRENT (active design)
**Date**: 2026-08-27
**Applies to**: `src/` (JS engine), `uc_browser/` (Python driver layer), `bench/` (measurement), `litellm.config.yaml` (API surface)
**Depends on**: `bench/README.md` (the measuring methodology this design is scored against)

Any chat UI on the web should be callable as an API. That is the whole thesis: a page with a composer, a send button, and a message stream is already an LLM endpoint — it just lacks a programmatic surface. UC supplies that surface (`window.__UC_chatSend`, `__UC_chatGetMessages`) by detecting the chat *structurally* — DOM patterns, ARIA, geometry, an ML classifier for ambiguous cases — never by hardcoding per-site CSS selectors. If the engine only works because someone taught it `#prompt-textarea`, the thesis is dead; it's just another scraper with a maintenance treadmill.

## 1. Two pathways: the generic engine is the product; adapters are earned exceptions

Every site routes through one of exactly two pathways:

| Pathway | Code | When | Cost |
|---|---|---|---|
| **Generic engine** | `uc_browser/sites/generic.py` → `UCBrowser.chat()` → `src/` engine | Default. Any site that passes the bench's `input_pipeline` stage becomes a callable litellm model with **zero new code** | Free per site |
| **Hand-tuned adapter** | `uc_browser/sites/grok_fast.py` (+ `grok.py`, `grok_api.py`) | Only when a site *needs* it — speed, conversation management (list/rename/delete), auth quirks the generic path can't absorb | A driver to maintain, forever |

The asymmetry is deliberate. `grok_fast` exists because grok.com traffic was the original production need and warranted a fast path with full conversation CRUD. Every other site (`uc/chatgpt`, `uc/perplexity`, `uc/pi`, `uc/lmarena`) rides the generic engine, and `uc/grok-generic` deliberately drives grok.com *through the generic engine with the adapter bypassed* — a standing verification that the generic path handles the one site we know most intimately. An adapter is not a template to copy for the next site; it is an exception a site must earn. If the generic engine keeps needing adapters, that's a bug in the engine, not a backlog of adapters to write.

## 2. Site-specific selectors live ONLY in bench oracles

Rule (non-negotiable, restated from `bench/sites.py`): site-specific selectors are allowed in exactly one place — the bench's ground-truth oracles — and nowhere in `src/` or `uc_browser/`.

The reasoning is measurement hygiene. To score "did the engine find the real chat input?", *something* has to know which element the real chat input is. That something is the oracle (`bench/sites.py`), which stamps `data-uc-truth` attributes into fixtures at capture time. The oracle is the **measuring stick**; the engine is the **product**. The moment a product selector leaks in, the benchmark measures self-agreement, not generalization — the exact failure mode that made the old `scripts/benchmark_detection.py` numbers meaningless (it reranked hits toward known truth). Corollary: scoring is honest top-1 — UC's #1 answer must *be* the truth element, with no truth-aware reranking, and negative fixtures (search pages, form docs) must not clear the engagement gate.

## 3. Signatures are a cache, not the product

The engine keeps a signature store (`src/core/`) of previously resolved page structures. This is an optimization: on a repeat visit, skip re-derivation and go straight to the known elements. It must never become load-bearing. The correctness claim always rests on the generic detection path — a cold cache, a redesigned page, or a wiped profile must all degrade to "detect from scratch," not to failure. A system whose signatures are required for operation has quietly re-invented per-site selectors with extra steps; a system whose signatures merely make repeat visits faster keeps the thesis intact.

## 4. The availability-gated litellm menu

Browser-driven backends fail differently from API backends: a site can be up but degraded (composer not found), login-walled, or down — and a blind call burns 60+ seconds of browser time discovering that. So the API surface is a **live menu**, not a static model list:

1. The health monitor (`uc_browser/health.py`) probes each registered site on its own interval at tiered levels (reach → detect → send) and writes `data/health/latest.json`.
2. The status server (`uc_browser/status_server.py`) serves that as `GET /availability` — every site with status, uptime, and whether its litellm model is currently *callable*.
3. The litellm provider (`uc_browser/llm_providers/uc.py`) enforces the gate: a call to a site whose latest probe says `down` or `login` fails fast with `ServiceUnavailableError` instead of launching a doomed browser session. The gate is graceful by design — no health data or stale data means "no opinion, proceed" — and bypassable (`UC_AVAILABILITY_GATE=0`, or per-request `extra_body={"ignore_availability": true}`).
4. The litellm proxy forwards `GET /uc/availability` to the status server, so clients ask the *same base URL* they call for completions which `uc/*` models are worth calling right now.

The design intent: clients treat UC like a restaurant menu where dishes get crossed off in real time, rather than a menu that lets you order and then tells you the kitchen is closed.

## Verification

```bash
pixi run bench-check    # the thesis, measured: generic engine vs. truth oracles, regression-gated
pixi run status-server  # then GET http://127.0.0.1:4010/availability — the live menu
```

If `bench-check` passes and `/availability` lists callable models, the design above is operating as described. If a new site tempts you toward an adapter, first check whether it fails the bench — fix the engine before writing the exception.
