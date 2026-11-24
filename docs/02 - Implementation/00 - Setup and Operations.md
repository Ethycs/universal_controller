# Setup and Operations

**Status**: CURRENT
**Date**: 2026-11-23
**Applies to**: standing the repo up standalone (pixi workspace at repo root) and running its services
**Depends on**: `pyproject.toml` (pixi tasks), `litellm.config.yaml` (proxy config), `docs/01 - Design/00 - Chat-Window-to-API Design.md` (why the pieces exist)

## 1. Install

```bash
cd universal_controller
pixi install
```

The repo is a self-contained pixi workspace (win-64 and linux-64, Python 3.12, Node.js 22); the editable install pulls the `adapter` and `service` extras. Playwright is resolved from PyPI at 1.65.2 or newer. Keep stealth in the adapter's PyPI extra rather than duplicating it in conda: the first UC solve selected an incompatible older Playwright combination. This does **not** mean conda stealth universally pins Playwright 1.60; the exact solver interaction is uninvestigated. Shared dependencies re-solved both platforms; **Linux was exercised, Windows was solved but not run**. The repo also works as a git submodule under a parent — the two pixi workspaces are independent.

## 2. Build the extension

```bash
pixi run playwright install chromium
pixi run npm --prefix extension ci
pixi run npm --prefix extension run build
```

Produces `extension/dist/uc-extension.js` (~225 KB) — the bundle loaded into Chromium and injected into bench fixtures. Rebuild after any change to `src/`.

## 3. Test suite

```bash
pixi run -e dev test        # = pytest -v tests/
```

Independent Linux baseline on 2026-11-23 **with the extension bundle built**: **102 passed, 1 skipped, 1 failed**. The failing test was `tests/test_bench_runner.py::test_synthetic_fixtures_score_correctly`: Chromium build 1255 loaded synthetic MHTML captured from `about:blank` as an empty document. The generator now captures a routed document with a real URL; regenerated fixtures passed `pixi run -e dev pytest -q tests/test_bench_runner.py` (**1 passed**). The full suite was not repeated after this fix. PBX 210-1001's earlier **100 passed, 4 skipped** was without the bundle: three bundle-dependent checks did not run, including this test, so it is not a regression against that baseline. See Build Spec §8 for the corrected comparison and separately verified native injection.

## 4. Benchmark

```bash
pixi run bench              # score all fixtures, print the table
pixi run bench-check        # exit 1 on any regression vs bench/results/baseline.json
pixi run bench-baseline     # accept current results as the new baseline
pixi run bench-capture      # recapture fixtures from live sites (headful; minutes)
```

`bench` and `bench-check` are offline and deterministic (MHTML fixtures + CDP injection); only `bench-capture` touches the network.

## 5. Status and browser service

```bash
DISPLAY=:0 XAUTHORITY=/home/rabbit/.Xauthority pixi run service  # http://127.0.0.1:4010/
```

| Endpoint | What |
|---|---|
| `GET /` | Human uptime page (per-site cards, probe history bars, auto-refresh) |
| `GET /availability` | JSON live menu: registry × health, incl. currently callable litellm models |
| `POST /probe/{name}` | Force an immediate probe of one site |
| `*  /mcp` | MCP endpoint (streamable-http): `uc_site_add` / `uc_site_search` / `uc_site_list` / `uc_site_probe` / `uc_availability` |
| `/windows`, `/clients`, `/windows/{name}/pages` | Window/CDP discovery, pid-bound clients and leased pages |
| `/windows/{name}/controls/{control}`, `/fetch`, `/cookies`, `/snapshot` | Controls and HTTP-only operations; the last three also sit under `/windows/{name}` |

Background health probes are **off by default**. Opt in with `pixi run python -m uc_browser.status_server --run-scheduler` (or `create_app(run_scheduler=True)`). Opt-in probes each registered non-login-required site on its interval (default 15 min), serialized on one worker thread, and may open headed chat-site windows. `POST /probe/{name}` and MCP manual probes still work without the scheduler. Neither `UC_AUTO_BOOTSTRAP=0` nor an empty sites file disables the registry's built-in seeds; use the scheduler switch, not those settings, to control automatic probing.

`UC_WINDOWS_FILE` selects a windows config; with no file, the service defines one lazy local Chromium/extension window. Build `extension/dist/uc-extension.js` before starting even lazy extension/inject windows: startup refuses a missing bundle. No live profile or service unit is installed by these commands.

Consumers opt in explicitly with `UCBrowser(service="http://127.0.0.1:4010", window="harvest")`; omitting both `service` and `cdp_endpoint` retains local launching. `UC_SERVICE_URL`, `UC_WINDOW`, `UC_NATIVE_WINDOW` and `UC_CLIENT_LABEL` are no longer read, and `ServiceClient(url)` requires an explicit address. Complete configuration, wire routes and disposable live-probe instructions are in `docs/02 - Implementation/01 - UC Service — Build Spec.md`. Grok uses the site-session routes through its explicitly configured HTTP consumers (§6); this does not migrate harvest, generic or health browsers.

### 5.1 Attach to a caller-owned browser

For a browser the operator already launched and cleared, the consumer explicitly names its CDP endpoint:

```python
from uc_browser import UCBrowser

with UCBrowser(cdp_endpoint="http://127.0.0.1:9222", label="event_tool") as browser:
    page = browser.open("https://example.com")
```

This is direct attachment, not a service window or a native launch. UC never selects the endpoint from the environment, probes for one, launches a browser, checks a profile lock, injects cookies, applies stealth, or installs context scripts. It borrows the first context. `mode`, legacy launch flags, explicit `channel`/`stealth`, `service` and `window` cannot be combined with `cdp_endpoint`.

`new_page`, `open` and `open_with_intercept` own their resulting tabs and popups. Exit closes those tabs best-effort and disconnects the driver; pre-existing tabs and tabs opened independently during attachment remain. A refused connection raises `RuntimeError` naming the endpoint, without falling back to a launch. The label is caller metadata, not a service registration or revocation handle. Controls execute locally with the existing engine/fallback behavior; no engine is injected.

**Verification:** `pixi run --frozen -e dev pytest -q tests/test_cdp_attachment.py tests/test_service_client.py` uses disposable headless Chromium profiles and loopback fixture pages, never port 9222 or signed-in profiles. Real-site clearance after attachment remains **unverified**; CDP attachment itself can affect bot checks.

## 6. Grok proxy and service deployment

`systemd/uc-service.service` and `systemd/uc-grok-proxy.service` are Linux user-unit templates for an independent checkout at `~/universal_controller`. The service alone owns the signed-in browser; the Grok-only proxy uses `grok-proxy.config.yaml`, exposing the existing `uc-grok` and `grok-browser` model names on loopback port 4000.

Host-specific configuration belongs in private `~/.config/universal-controller/windows.json`; the session map belongs in `~/.local/state/universal-controller/sessions.json`. Configure a `grok` window and site, with the intended `folder` and verified `folder_url`. The current migration uses the existing `NSFW` project and keeps the Grok profile at its existing path. Do not copy stale state-file cookies over a profile whose account/session has just changed.

Before first cutover, stop the old `uc-grok-proxy` browser owner, confirm its profile is no longer held, and back up the closed profile. Only then start the UC service on that profile. Never run two owners against it. Install the user units, reload systemd, and start the UC service before restarting the proxy. Do not alter the separate native browser/port 9222.

```bash
# By hand, after the service owns the configured Grok profile:
UC_GROK_SERVICE_URL=http://127.0.0.1:4010 \
  pixi run --frozen litellm --config grok-proxy.config.yaml \
  --host 127.0.0.1 --port 4000 --num_workers 1
```

`UC_GROK_SERVICE_URL` is consumer configuration, passed explicitly to `ServiceClient`; it does not select a `UCBrowser` mode. The provider and Grok MCP tools fail with a configuration error when it is absent, rather than opening a private browser. MCP hosts must use the same deployed client revision and set this address too. Named sessions are shared through the service; restarting a consumer does not close its browser or discard that map.

The older `litellm.config.yaml` remains a mixed-site development example. Its generic non-Grok clients still own local browsers and require one worker; that task-8 migration is separate. No generic models are added to the production Grok-only proxy by this cutover.

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

1. **Only ONE sync Playwright instance can ever start per thread.** Browser objects stay on the thread that created them. Grok's owner and bounded site-session workers are in the UC service; its proxy and MCP tools hold only HTTP clients. The generic provider retains its own `uc-generic-pw` executor until its separate migration.
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
