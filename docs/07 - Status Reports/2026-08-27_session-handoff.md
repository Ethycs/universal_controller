# Session Handoff — bench, registry/health/status stack, litellm generic driver

**Date**: 2026-08-27 **Status**: ✅ LANDED
**Scope**: `universal_controller` repo — **committed 2026-08-27** as the series `43db2ac` (bench) → `36d16d5` (availability stack + generic driver) → `04d9093` (Signature Lab v1) → `5d22578` (docs tree) → `1e96815` (chore). The log is the source of truth; an earlier revision of this doc said UNCOMMITTED — that was true when written, reconciled after the commits landed.

## 1. What this session did (chronological)

1. Built `bench/` — the generalization benchmark: MHTML fixtures of real chat sites, truth oracles stamped as `data-uc-truth` at capture, offline CDP injection of the built bundle, honest top-1 scoring, regression gate vs `bench/results/baseline.json`.
2. Captured 16 fixtures and established the baseline: input pipeline **7/7**, send **6/6**, container **1/1** (one scorable), **1 false positive** (bootstrap-forms clears the input gate at 5.88).
3. Built the registry + health monitor + status server stack: `uc_browser/registry.py` (JSON-backed site registry, builtins + user entries), `uc_browser/health.py` (tiered reach/detect/send probes, JSONL history), `uc_browser/status_server.py` (uptime page, `/availability` API, mounted MCP site tools).
4. Wired litellm: availability gate in `uc_browser/llm_providers/uc.py`, proxy pass-through for `/uc/availability`, and the generic driver `uc_browser/sites/generic.py` exposing `uc/chatgpt`, `uc/perplexity`, `uc/pi`, `uc/lmarena`, and `uc/grok-generic` alongside the adapter-backed `uc/grok`.
5. Imported 20 brand domains from `chatbot_domains.xlsx` via `scripts/import_sites_xlsx.py` and swept them. Sweep result: **2 ok** (klarna + squarespace, via the new generic launcher-opening path), **12 degraded**, **2 down**, **4 login**.
6. Validated the pipeline live: `uc/chatgpt` through the pure generic engine answered in **8.6 s** (single-message mode).

## 2. Changes in the working tree

| File | Change | Test state |
|---|---|---|
| `bench/` (whole tree) | New: capture, runner, oracles, 16 fixtures, results | `pixi run bench-check` green vs baseline |
| `bench/results/baseline.json` | New checked-in baseline | is the gate |
| `uc_browser/registry.py` | New site registry | covered indirectly |
| `uc_browser/health.py` | New health monitor | `tests/test_health.py` |
| `uc_browser/status_server.py` | New status server + MCP mount | manual (page + `/availability` verified) |
| `uc_browser/sites/generic.py` | New generic litellm driver | live-validated (uc/chatgpt 8.6 s) |
| `uc_browser/mcp/site_tools.py` | New MCP site tools | manual via `/mcp` |
| `uc_browser/llm_providers/uc.py` | Availability gate; generic route; dual pinned threads | live-validated |
| `scripts/demo_pipeline.py` | New end-to-end demo | is itself the validation |
| `scripts/import_sites_xlsx.py` | New xlsx importer + probe sweep | ran against 20 domains |
| `tests/test_health.py`, `tests/test_bench_runner.py` | New tests | in the 64-pass suite |
| `data/sites.json` | New: 20 imported user entries | — |
| `docs/` | This documentation tree | — |

Suite state: **64 passed + 1 skipped** (`pixi run -e dev test`, validated 2026-08-27).

## 3. Findings (the valuable part — verify, then fix)

1. **validated** — Only one sync Playwright instance can ever start per thread; the grok adapter and generic client therefore each own a pinned executor thread in `uc_browser/llm_providers/uc.py` (`uc-grok-pw`, `uc-generic-pw`). Do not "simplify" this into one shared thread pool.
2. **validated** — `litellm.completion` leaves an asyncio loop on the calling thread; browser-touching code after a litellm call must hop to a pinned thread (see `scripts/demo_pipeline.py:demo_call`).
3. **BUG:** bootstrap-forms negative fixture clears the input gate (datalist input scores 5.88 ≥ 4). google (3.62) and hackernews (3.56) are close behind. The gate needs a real false-positive margin.
4. **BUG:** the container detector will rank `html>head` and zero-size `aria-live` announcers as the message stream — needs a visibility/size floor.
5. **BUG:** the login-wall heuristic misfires on marketing pages (lego / lg / sephora classified `login` on "sign in/sign up" text hits alone).
6. **validated** — MHTML blocks page scripts but CDP-injected JS runs fine; this is what makes the offline bench possible at all.
7. **validated** — grok.com hydrates 15–25 s past networkidle; `uc/grok-generic` additionally hits a logged-out signup wall that blocks responses (needs logged-in profile support in the generic client).
8. **known rough edge** — demo follow-up message is flaky if the browser window is closed mid-run; single-message mode (`--single`) is the reliable demo path.
9. **validated** — killed runs can leave zombie Chromium holding profile locks; kill only `chrome.exe` with `Path -like "*ms-playwright*"`.

## 4. Open work, in priority order

1. Decide whether to commit the working tree (blocking everything — an uncommitted tree this large is one bad `git clean` from gone). First action below.
2. Tier 0 gate fixes (false-positive margin, container visibility floor, login-wall heuristic) — see `docs/06 - Roadmaps/00 - Engine Gaps.md`.
3. Widget-iframe capture (tidio/intercom unscorable) and the missing send-level bench stage — same roadmap, Tier 1.
4. Logged-in profiles for the generic client (unblocks grok-generic and the login-required corpus) — Tier 1/2.

## 5. Artifacts

| Path | What |
|---|---|
| `bench/` | Generalization benchmark: capture, runner, oracles, fixtures |
| `bench/results/baseline.json` | Regression baseline (the gate) |
| `uc_browser/registry.py` | Site registry (builtins + `data/sites.json`) |
| `uc_browser/health.py` | Tiered probe health monitor |
| `uc_browser/status_server.py` | Uptime page + `/availability` + MCP mount |
| `uc_browser/sites/generic.py` | Generic engine → litellm driver |
| `uc_browser/mcp/site_tools.py` | MCP site-registry tools |
| `uc_browser/llm_providers/uc.py` | uc/* provider: gate, routes, pinned threads |
| `scripts/demo_pipeline.py` | End-to-end live demo |
| `scripts/import_sites_xlsx.py` | xlsx → registry importer + sweep |
| `tests/test_health.py`, `tests/test_bench_runner.py` | New test coverage |
| `data/sites.json` | 20 imported brand-domain entries |
| `docs/` | This documentation tree |

**First action for the next session**: run `pixi run -e dev pytest tests/ -q` and `pixi run bench-check`; if both are green, decide whether to commit the tree as-is or split it (bench / driver stack / docs) into separate commits.
