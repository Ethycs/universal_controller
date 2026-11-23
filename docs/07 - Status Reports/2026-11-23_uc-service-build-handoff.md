# UC Service Build — Handoff

**Date**: 2026-11-23 **Status**: 🟡 IN FLIGHT — nothing is built
**Scope**: `Ethycs/universal_controller`, `main` at `5e3dd0a` — the docs this brief cites are **uncommitted**, in `/home/rabbit/event_tool/ext/universal_controller/docs/` on host `smallblackbox`
**From**: PBX `210-1001` (the `event_tool` session that wrote the design) **To**: PBX `210-1002`
**Depends on**: `docs/01 - Design/02 - UC as a Local Service.md`, `docs/02 - Implementation/01 - UC Service — Build Spec.md`, `docs/08 - Analysis/00 - Service Prototype Measurements.md`, `docs/00 - Theory/00 - Windows, Transport and Control.md`

## Goal

Universal Controller runs as one independent, long-lived local service on `127.0.0.1:4010` that owns every browser window and the signed-in identity in it, and offers attach, control, fetch, cookies, snapshot, pages and site sessions to clients over HTTP and CDP.

"Independent" means: its own checkout of this repo on `main`, its own pixi environment, its own systemd user unit. It does not run from `event_tool`'s submodule checkout or environment. The wire contract is all that the service and a consumer share.

## What already exists (validated)

| What | State |
|---|---|
| Design, with normative lifecycle tables E0–E14 and G1–G15 | written, `DRAFT`, uncommitted |
| Build spec: file layout, `windows.json`, wire contract v0, `UCBrowser` service mode, internals, gotchas, unit, tests, consumer-move order | written, `DRAFT`, uncommitted |
| Prototype measurements, rows 1–24 | measured once on `99390a5`, **not on `main`**. The three probe scripts were throwaway and are not in the repo |
| `uc_browser/status_server.py` on `main` | exists: FastAPI + uvicorn on `127.0.0.1`, `UC_STATUS_PORT` default 4010, `create_app()` with a lifespan, mounts an MCP app. It is the service's host |
| `uc_browser/chrome_cookies.py`, `browser.py::_inject_chrome_cookies` | exist on `main`; the helper has no caller |
| Test suite on `main` | 100 passed, 4 skipped — run 2026-11-23 inside `event_tool`'s environment, not in UC's own |
| `uc_browser/service/` | does not exist |

Measured, and safe to build on (Analysis §3): one process can launch windows with `--remote-debugging-port=0`, read `<profile>/DevToolsActivePort`, and serve CDP (rows 1, 20). Attach costs 0.03–0.07 s; a control addressed by target id 0.03–0.08 s (rows 2, 4, 5). A never-started `UCBrowser` works as the receiver for page-taking controls (probe B). Concurrency is per attachment thread (rows 14–18). Stealth applied to a window's context reaches client-created pages (row 19). The engine bundle evaluated into installed Google Chrome runs (rows 21–23).

## Critical facts (learned the hard way — honor these)

1. **There is no single access technique.** The service owns the identity (a profile one process may hold) and offers each technique separately. No route may require another: fetch, cookies, snapshot and sessions must work for a caller that never registered or attached (Design §1, constraint 2; Build Spec §3 rule 7).
2. **One sync Playwright instance per thread.** No Playwright object is touched from an HTTP handler. Each service thread that touches a browser is a driver process of about 80 MB (row 24).
3. **The service never attaches to a browser it did not launch.** It answers `profile_held`. A second Chrome on a held profile exits at once and the first one answers the port; that is how four days of harvests attached to an abandoned browser (Analysis §5).
4. **`UC_SERVICE_URL` unset means `UCBrowser` behaves exactly as today.** That is the rollback for every consumer.
5. **Within a window, init scripts and cookies reach every client; across windows nothing crosses** (rows 8–10). The client library must never call `add_init_script` on an attached context or close a context.
6. **A service-opened page is not on the client's connection when the call returns.** Resolve by target id with a wait; 0.16–0.17 s measured (row 6).
7. **UC's pixi workspace lists `win-64` only** (`pyproject.toml`, `[tool.pixi.workspace]`). Add `linux-64` and re-solve `pixi.lock` first. Whether it solves is `unverified`.
8. **`node` and `npm` are not on this host's PATH.** The extension bundle (`extension/dist/uc-extension.js`, gitignored) needs them. `event_tool`'s pixi environment has both; adding `nodejs` to UC's own pixi dependencies is the independent answer.
9. **Playwright-launched Chromium here runs with `--no-sandbox`**; the binary started by hand dies with `No usable sandbox!`. Installed Google Chrome starts as a plain subprocess without the flag.
10. **Headed browsers need `DISPLAY=:0` and `XAUTHORITY=/home/rabbit/.Xauthority`**, and they open on the owner's desktop.
11. **The Chromium build follows the service's Playwright version.** The live profiles were last written by build 1255 (Playwright 1.65.2). Opening them with an older build is `unverified`; pin Playwright at 1.65.2 or newer.
12. **The spec was changed today in three places.** `UCBrowser.new_page()` is a new public method and the `_context` wrapper is gone (Build Spec §4, Design E4 and §5). Test rows for fetch, cookies and snapshot were added (Build Spec §8). The service runs from its own checkout (Build Spec §7, gotcha 7). If you hold an earlier copy, re-read.

## Known bugs to fix

1. `BUG:` Grok's signed-out page ("Sign up to continue") is not recognised by `has_login_wall`, so a signed-out send hangs until its timeout (Design G9; observed 2026-11-17). The service needs this to return `auth_required`.
2. `BUG:` `_start_native_chrome` uses fixed port 9222 (`browser.py:614` on `main`) and attaches to whatever answers it. Replace with port 0 and the held-profile check (Build Spec §5).
3. `unverified`: in probe B, one page of window `a` was unaccounted for after a client disconnected (row 11). Re-check when the probe is rebuilt.

## Tasks (in priority order)

Tasks 0–5 need no consumer and no live profile. Stop after 5 and report before starting 6.

| # | Task | Spec | Done when |
|---|---|---|---|
| 0 | Clone `Ethycs/universal_controller` (proposed: `/home/rabbit/universal_controller`), branch off `main`. Copy `docs/` from the path in Scope into it; no path collides with `main`'s `docs/`. Delete the repo-root `Agentic Technique Master.md` (its copy is `docs/05 - Standards/UC-STD-DOCS-v0 — …`). Add `linux-64`, solve, `playwright install chromium`, build the extension | Build Spec §6 gotchas 5, 7 | the suite passes in UC's own environment; compare with 100 passed, 4 skipped |
| 1 | `scripts/service_probe.py`: rebuild probes B and C from Analysis §2 and run them on `main` | Design §12 step 1 | rows 1–24 printed. Row 8 "absent", row 10 "present", or row 15 near row 14 overturns the model: stop and call |
| 2 | `uc_browser/service/windows.py`, `api.py`: window manager, owner thread, CDP listing, control route with a worker pool, one Chromium window | Build Spec §1, §3, §5 | `GET /windows` lists a `cdp` endpoint; the probe passes against the real service |
| 3 | `leases.py`, `client.py`, `UCBrowser` service mode: registration, `new_page()`, reaper, revocation by label | Build Spec §4, §5 | unit rows of §8; the live revocation test (a client blocked in `wait_for_timeout` errors within seconds of `DELETE /clients?label=`) |
| 4 | Pages, fetch, cookies, snapshot routes; `ServiceClient` with no browser import | Build Spec §3 | unit and live rows of §8. None of these three routes was prototyped |
| 5 | `native` window: installed Chrome on port 0, `profile_held`, engine injected at a page's first control call. Tabulate what each of the 39 controls returns when the engine is absent (only `detect_all` → `{}` has been read) | Build Spec §4, §5; Design §2 | a control on a native page returns a pattern map; a second launch on the held profile answers `profile_held` |
| 6 | `sessions.py`, site routes, grok adapter in-service, provider and Grok MCP tools as thin clients | Design §7; Build Spec §5 | **needs the live Grok profile: coordinate with 210-1001 and the owner first** |
| 7 | systemd user unit `uc-service` | Build Spec §7 | after `systemctl --user restart uc-service`, `GET /windows` answers and no browser from the previous run is left |
| 8 | Health probes and the generic client become sessions; provider pinned threads removed | Design §12 step 8 | `grep -rn 'UCBrowser(' uc_browser` shows only the window manager and the client library |

Consumer moves (Build Spec §9 steps 0, 2–4, 6, 7) are `event_tool` changes and belong to 210-1001. `event_tool` is consolidating its browser access into one module first, so that its move is a one-file change; no code has changed there yet.

## How to run

```bash
# by hand, from the service's own checkout
UC_WINDOWS_FILE=data/windows.json pixi run python -m uc_browser.status_server
curl -s http://127.0.0.1:4010/windows
pixi run -e dev pytest -q tests/
pixi run python scripts/service_probe.py
```

Ports on this host today: 4000 the Grok proxy, 8765 `event_tool`'s MCP service, 9222 an abandoned Chrome. 4010 is free.

## Deliverables

**Acceptance.** Each task's "Done when" above, plus: every lifecycle row E0–E14 and G1–G15 still has a pathway (Design constraint 5), the service binds loopback only, and the docs are reconciled in the same pass as the code (`docs/05 - Standards/UC-STD-DOCS-v0 — …`).

**The interface between us.** Build Spec §3 (wire contract v0) and §4 (`UCBrowser` in service mode). `event_tool`'s module is being written against them. Call 210-1001 before changing either.

**Docs ownership.** Once the docs are committed in your clone, that copy is authoritative. 210-1001 stops editing the uncommitted copy and sends spec changes by PBX.

**Do not touch.**

1. `/home/rabbit/event_tool`, including `ext/universal_controller` inside it. The running harvest (`event-harvester-mcp`) and the Grok proxy (`uc-grok-proxy`) import UC from that checkout at `99390a5`.
2. The live profiles under `/home/rabbit/event_tool/data/`: `.uc_chromium_profile`, `.uc_chrome_profile`, `.grok/`. Use fresh profiles under your own `data/` through task 5.
3. The Chrome on port 9222 (pid 470884). The owner has been asked whether to close it and has not answered.
4. The units `event-harvester-mcp`, `event-harvester-site`, `uc-grok-proxy`, `pbx-co`.
5. `main` and the branch `fix/grok-ws-reply` on GitHub: push your own branch; `event_tool` pins `99390a5`, which only that branch holds.
6. The owner's accounts: no sends on the Grok account beyond a one-word test, no cookies or message contents in logs or replies.

**Not decided by the owner** (proposals above, not instructions): where the checkout lives; whether the live profiles stay under `event_tool/data/` or move to the service.

**`unverified` in the docs you inherit:** the injected engine against a real bot check; revocation unwinding a stuck thread; how many concurrent sends one Grok account tolerates (keep 1); a client and service on different Playwright versions; an idle launching connection over days; everything on `main` rather than `99390a5`.

## Verification

```bash
ls uc_browser/service 2>/dev/null || echo "not built"
```

`not built` is the expected output on 2026-11-23. After task 2, replace this with the probe run and compare its rows with Analysis §3; a row that differs means the build or the spec is wrong, and that is decided before the next task starts.
