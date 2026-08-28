# Setup and Operations

**Status**: CURRENT
**Date**: 2026-08-27
**Applies to**: standing the repo up standalone (pixi workspace at repo root) and running its services
**Depends on**: `pyproject.toml` (pixi tasks), `litellm.config.yaml` (proxy config), `docs/01 - Design/00 - Chat-Window-to-API Design.md` (why the pieces exist)

## 1. Install

```bash
cd universal_controller
pixi install
```

The repo is a self-contained pixi workspace (win-64, Python 3.12); the editable install pulls the `adapter` extras (litellm, mcp, stealth, rookiepy). It also works as a git submodule under a parent repo — the two pixi workspaces are independent.

## 2. Build the extension

```bash
cd extension
npm install
npx rollup -c
```

Produces `extension/dist/uc-extension.js` (~225 KB) — the bundle loaded into Chromium and injected into bench fixtures. Rebuild after any change to `src/`.

## 3. Test suite

```bash
pixi run -e dev test        # = pytest -v tests/
```

Expected state as of 2026-08-27: **64 passed + 1 skipped** (validated).

## 4. Benchmark

```bash
pixi run bench              # score all fixtures, print the table
pixi run bench-check        # exit 1 on any regression vs bench/results/baseline.json
pixi run bench-baseline     # accept current results as the new baseline
pixi run bench-capture      # recapture fixtures from live sites (headful; minutes)
```

`bench` and `bench-check` are offline and deterministic (MHTML fixtures + CDP injection); only `bench-capture` touches the network.

## 5. Status server

```bash
pixi run status-server      # http://127.0.0.1:4010/
```

| Endpoint | What |
|---|---|
| `GET /` | Human uptime page (per-site cards, probe history bars, auto-refresh) |
| `GET /availability` | JSON live menu: registry × health, incl. currently callable litellm models |
| `POST /probe/{name}` | Force an immediate probe of one site |
| `*  /mcp` | MCP endpoint (streamable-http): `uc_site_add` / `uc_site_search` / `uc_site_list` / `uc_site_probe` / `uc_availability` |

A background scheduler probes each registered site on its interval (default 15 min); probes are serialized on one worker thread.

## 6. litellm proxy

```bash
pixi run -e dev litellm --config litellm.config.yaml --port 4000 --num_workers 1
```

`--num_workers 1` is mandatory — the browser profile is single-writer (the config sets `worker_concurrency: 1` as belt-and-braces). The proxy pass-through forwards `GET /uc/availability` → the status server's `/availability`, so run the status server alongside it. Any OpenAI-shaped client then works against `http://localhost:4000/v1` with models `uc-grok`, `uc-chatgpt`, `uc-perplexity`, `uc-pi`, `uc-lmarena`, `uc-grok-generic`.

## 7. Demo pipeline

```bash
pixi run -e dev python scripts/demo_pipeline.py --skip-probe --single --models uc/chatgpt
```

Shows the live menu, then a real completion through litellm against the named model(s). `--skip-probe` trusts the cached health data; `--menu-only` skips sends entirely.

## 8. Importing sites from a spreadsheet

```bash
pixi run python scripts/import_sites_xlsx.py chatbot_domains.xlsx --probe
```

First sheet needs header columns `Domain | Brand | Notes`. Rows land in the registry (`data/sites.json`) as `kind="widget"` entries; `--probe` sweeps them all in one browser session immediately, `--only klarna,lego` restricts the set.

## Gotchas (validated — each of these cost real debugging time)

1. **Only ONE sync Playwright instance can ever start per thread.** The first instance binds its greenlet to the thread; a second start there dies with "Cannot switch to a different thread". This is why `uc_browser/llm_providers/uc.py` gives the grok adapter and the generic client **each their own pinned single-worker executor thread** (`uc-grok-pw`, `uc-generic-pw`) and hops every browser-touching call onto the right one.
2. **`litellm.completion` leaves an asyncio loop on the calling thread** (its async logging machinery), and sync Playwright refuses to run on a thread that has a loop. Any code that calls litellm and then touches the browser must hop to a pinned worker thread — see `demo_pipeline.py:demo_call` for the pattern.
3. **grok.com hydrates 15–25 s past `networkidle`.** The DOM you see at networkidle is not the DOM you can drive; wait for hydration before detecting or sending.
4. **MHTML snapshots block page scripts, but CDP-injected JS runs fine.** `page.evaluate` (and bundle injection via CDP) executes normally inside an MHTML fixture — that's the entire trick that lets the bench drive real-site DOMs offline and deterministically.
5. **Killed runs can leave zombie Chromium processes holding profile locks**, which poison every later launch against that profile. Kill only Playwright's Chromium, not your real Chrome:

   ```powershell
   Get-Process chrome | Where-Object { $_.Path -like "*ms-playwright*" } | Stop-Process -Force
   ```

## Verification

```bash
pixi run -e dev test && pixi run bench-check
```

Both green means the environment is correctly stood up: engine builds, drivers import, fixtures score at baseline. If `bench-check` fails right after a fresh clone, rebuild the extension first (step 2) — the bench injects `extension/dist/uc-extension.js`, which is gitignored.
