# System Overview

**Status**: CURRENT — the single trusted map
**Date**: 2026-11-23 (service substrate reconciled; older subsystem descriptions retain their historical scope)
**Applies to**: the repo as built, including the independent local service through handoff task 5
**Depends on**: `docs/01 - Design/00 - Chat-Window-to-API Design.md`, `docs/02 - Implementation/01 - UC Service — Build Spec.md`

## Executive summary

UC turns any web chat UI into a callable API: a site-agnostic JS engine detects the chat structurally inside a controlled Chromium, a Python driver layer wraps that into clients, and a litellm provider exposes each working site as a `uc/<site>` model behind a live availability gate. Generalization is not assumed — a benchmark scores the engine's top-1 answers against ground-truth oracles on offline fixtures of real sites, with a regression gate. Site-specific knowledge exists only in the bench oracles and in the one earned adapter (grok); everything else is the generic engine.

## 1. Architecture

```
                 OpenAI-shaped client / litellm.completion
                                  │
        ┌─────────────────────────┼──────────────────────────┐
        │ litellm proxy (:4000)   │      GET /uc/availability │──► pass-through
        └─────────────────────────┼──────────────────────────┘        │
                                  ▼                                   ▼
   ┌───────────────── uc_browser/llm_providers/uc.py ────┐   ┌─ status_server (:4010) ─┐
   │  availability gate ── uc/grok ──► sites/grok_fast   │   │  uptime page  /availability │
   │  (health latest.json) uc/<site> ► sites/generic     │   │  /mcp: site tools           │
   │  [pinned thread A]        [pinned thread B]         │   └──────────┬──────────────┘
   └───────────┬─────────────────────┬───────────────────┘              │ probes
               ▼                     ▼                                  ▼
   ┌──────────────── uc_browser/browser.py  UCBrowser ─────┐   ┌─ health.py ──► data/health/ ─┐
   │  launches Chromium + extension, CDP connect            │   │  reach/detect/send levels     │
   └───────────────────────────┬───────────────────────────┘   └─ registry.py ─► data/sites.json┘
                               ▼
   ┌──────────── Chromium + extension/dist/uc-extension.js ────────────┐
   │   src/ engine (detection, actions, iframe RPC, llm verification)   │
   │   ml/ runtime + models/dom_classifier (fallback classification)    │
   └───────────────────────────────────────────────────────────────────┘
                               ▲
        bench/ ── MHTML fixtures + truth oracles ── injects same bundle,
                  scores honest top-1, gates regressions vs baseline.json
```

## 2. JS engine (`src/`)

- **Domain**: in-page chat detection and actuation. ~7,300 lines; **zero site-specific selectors** (enforced design rule).
- **Inputs**: the live DOM of whatever page it's injected into; optional ML weights (`models/dom_classifier/weights.json`).
- **Outputs**: the `window.__UC_*` API — `chatSend`, `chatGetMessages`, `chatOnMessage`, `detectAll`, `findInputs`, `findButtons`, `classify`, plus form/modal/dropdown helpers and LLM-context extraction.
- **Structure**: `core/` (signature store — a cache, not the product), `detection/` (scored structural patterns, scan-diff), `actions/` (chat-api and friends), `iframe/` (cross-frame RPC for embedded widgets), `llm/` (heap scanner, state machine verification, context extractor).
- **Constraints**: must run identically live and inside MHTML fixtures via CDP injection; input engagement is gated at `score >= 4` with an ML fallback at `conf > 0.5`.

## 3. ML classifier (`models/dom_classifier`)

- **Domain**: labeling DOM regions when structure alone is ambiguous (`chat_input` vs `search` vs `form_field`, 8 labels).
- **Inputs**: element bounding boxes rasterized to a 32×32×4 grid (`ml/rasterizer.js`) and structural code features.
- **Outputs**: `{label, confidence, scores}` per element; consumed by the engine's input-pipeline fallback.
- **Structure**: two stages — raster MLP (`raster_classifier.pkl`, exported to `weights.json` for pure-JS in-browser inference via `ml/dom_inference.js`) and structural RandomForest (`code_classifier.pkl`, Python-side via `uc_browser/dom_classifier.py`). Training pipeline in `scripts/`.
- **Constraints**: fallback only — the heuristic gate goes first; retrain only when a new UI framework misclassifies.

## 4. Python driver layer (`uc_browser/`)

- **Domain**: owning the browser process and turning the JS engine into Python clients.
- **Inputs**: site URLs + registry entries; the built extension bundle.
- **Outputs**: `UCBrowser` (launch modes `CHROME` / `CHROMIUM_EXT` / `NATIVE_CDP` / `HEADLESS`; `chat()` is the full generic round-trip: scored input discovery → ML fallback → framework-aware setText → proximity send-button → trigram-diff response extraction); `sites/grok*.py` (the hand-tuned grok adapter: fast path + conversation CRUD); `sites/generic.py` `GenericClient` (any registry site via `UCBrowser.chat()`, one tab per (site, session) pair for conversation continuity).
- **Constraints**: sync Playwright is thread-affine — one instance per thread, ever; the Chrome profile is single-writer; one send at a time (locks serialize).

With `UCBrowser(service=url, window=name)` (caller names both; no environment fallback), `start()` validates the window via `GET /windows`, then registers a pid-bound client, attaches over CDP, creates leased pages through HTTP, and sends page controls to the service. `new_page()` is public in both modes; raw page scripting and interception remain local. Without `service`, existing in-process use remains available; native launch uses a dynamically selected explicit loopback port and held-profile guards.

## 5. Registry (`uc_browser/registry.py`)

- **Domain**: which sites UC knows about, operationally (distinct from bench oracles, which never leave `bench/`).
- **Inputs**: `BUILTIN_SITES` seeds + user entries in `data/sites.json` (`UC_SITES_FILE` overrides; user wins name collisions). MCP tools and `scripts/import_sites_xlsx.py` write entries.
- **Outputs**: `SiteEntry` records — url, kind (chat|widget), `litellm_model` (only when a driver or bench-verified generic path exists), `login_required`, probe interval/level.
- **Constraints**: thread-safe, JSON-persisted; a site gets a `litellm_model` only by earning it (adapter, or bench `input_pipeline` pass).

## 6. Health monitor (`uc_browser/health.py`)

- **Domain**: live probing and uptime history.
- **Inputs**: registry entries; a headful (off-screen) probe browser.
- **Outputs**: `ProbeResult` per probe → JSONL history at `data/health/history.jsonl` + `data/health/latest.json` (what the availability gate and status page read).
- **Structure**: tiered levels — `reach` (page loads, real DOM) → `detect` (generic engine finds an input clearing the same `score>=4` gate `chat()` uses) → `send` (real round-trip; costs quota, opt-in per site). Statuses: `ok` / `degraded` / `login` / `down` / `unknown`. Includes structural launcher discovery (clicks the floating chat bubble on widget sites — no site selectors) and multi-frame detection for iframe composers.
- **Constraints**: `login` does not count against uptime (site is up, our credentials aren't); probes serialized on one thread.

## 7. Status server (`uc_browser/status_server.py`)

- **Domain**: one loopback uvicorn process presenting registry × health and owning configured browser windows.
- **Inputs**: registry/health store, opt-in `--run-scheduler` (or `create_app(run_scheduler=True)`), `UC_WINDOWS_FILE` (default `data/windows.json`). Both service entrypoints default to no automatic probes; manual probe routes remain available.
- **Outputs**: existing status/availability/probe/MCP routes plus `/windows`, `/clients`, pages, controls, cookies, fetch and snapshots. Site-session routes are not implemented.
- **Constraints**: `127.0.0.1:4010` (`UC_STATUS_PORT` overrides); the litellm proxy pass-through depends on it. Native Chrome and Chromium each use their own profile and loopback CDP port. Native uses an explicitly selected port with launched-process endpoint proof; Playwright Chromium keeps browser-selected port 0 and its existing stealth setup.

### Service substrate (`uc_browser/service/`)

| Component | Owns |
|---|---|
| `windows.py`, `profiles.py` | validated window specs, owner threads, lazy launch/relaunch, process/profile ownership and graceful shutdown |
| `workers.py` | thread-local Playwright runtimes; bounded short/long lanes with per-window `queue_timeout_s`; Page resolution/event pumping; browser-level CDP target inventory and closure; on-demand native engine injection |
| `leases.py` | pid/start-time client identities, page leases, label revocation, dead-client reaping and raw-page sweep using fresh authoritative inventory, never cached status; cleanup cannot launch a stopped window |
| `operations.py`, `api.py` | JSON/NDJSON HTTP operations; retained service-owned fetch page per origin, serialized requests with explicit credentials and per-request referrer (no same-origin re-navigation); cookies and HTML/PNG/MHTML |
| `client.py`, `targets.py` | browser-independent HTTP client; finite control read budgets include discovered queue allowance and margin, default unbounded; separately loaded CDP target resolver for `UCBrowser` |

The owner closes leased pages independently of a blocked client. Measured revocation: `wait_for_timeout(600000)` failed with `TargetClosedError` in 0.04 s. Service-owned fetch pages survive the zero-client sweep. Each active window can use up to one owner plus two lanes of `control_workers` drivers.

CDP lease maintenance uses `Target.getTargets`/`Target.closeTarget`, including raw pages created in another browser context. Firefox/WebKit keep their owner-Page path. Enumeration failure does not discard ownership records. The focused lease/core/fetch checks passed 65 tests, including real Chromium revocation of a blocked client with owner Page lookup disabled; the sweep preserved internal fetch pages and one blank page.

Site adapters, provider pinned threads, health-probe ownership and Grok MCP hosting remain unchanged pending tasks 6–8. No live deployment or profile migration has occurred. Firefox/WebKit owner-thread operation paths exist but were not live-tested; Chromium and installed Chrome were.

## 8. litellm provider (`uc_browser/llm_providers/uc.py`)

- **Domain**: the API surface — `model="uc/<site>"` in any litellm-speaking client.
- **Inputs**: chat completions requests; `extra_body` for `session_id` / `conversation_url` / `wait_for_response` / `ignore_availability`; `data/health/latest.json` for the gate.
- **Outputs**: `ModelResponse` with the extracted reply; `_hidden_params.uc_conversation_url` for continuity. No streaming; token counts are length estimates.
- **Structure**: two routes — `uc/grok` → the adapter (`grok_fast.send_with_fallback`, session store mapping ids to conversation URLs); `uc/<site>` → `GenericClient` for any registry entry advertising `litellm_model == "uc/<site>"`. Availability gate fails fast with `ServiceUnavailableError` on `down`/`login`. **Dual pinned browser threads**: the adapter and the generic client each own a single-worker executor (`uc-grok-pw`, `uc-generic-pw`) because only one sync Playwright instance can ever start per thread and callers may carry litellm's leftover asyncio loop.
- **Constraints**: proxy must run with one worker; single-writer profile.

## 9. Bench (`bench/`)

- **Domain**: measuring the generalization thesis. The development loop: change the engine, re-run, watch the table.
- **Inputs**: MHTML fixtures of real sites (`bench/fixtures/`, captured by `capture.py` with truth stamped as `data-uc-truth` from the oracles in `bench/sites.py`); the built bundle.
- **Outputs**: honest top-1 scores per fixture (`input_top1/gate/top5/pipeline`, `send_top1`, `container_top1`; negatives `fp_input`/`fp_chat`); `bench/results/baseline.json` as the regression gate (`bench-check` exits 1 on any regression).
- **Constraints**: offline, deterministic, login-free, CI-able; site selectors allowed here **only**; no truth-aware reranking, ever.

## Design axioms (non-negotiable)

1. No site-specific selectors outside `bench/sites.py`.
2. Scoring is top-1 and truth-blind.
3. The generic engine is the product; adapters are earned exceptions.
4. One sync Playwright instance per thread; one writer per browser profile.
5. Signatures are a cache — correctness never depends on them.

## Verification

```bash
pixi run -e dev test && pixi run bench-check
```

If either disagrees with this map, the map is stale — reconcile it before trusting it.
