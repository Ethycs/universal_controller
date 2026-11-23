# UC Service — Tasks 0–5 Built and Verified

**Date**: 2026-11-23
**Status**: CURRENT — authorized tasks 0–5 complete; stopped before task 6
**Scope**: independent `/home/rabbit/universal_controller`, branch `feat/local-service`, based on `main` at `5e3dd0a`; environment/docs baseline committed as `9c3d6ab`
**Depends on**: `docs/02 - Implementation/01 - UC Service — Build Spec.md`, `docs/08 - Analysis/00 - Service Prototype Measurements.md`, `docs/08 - Analysis/01 - Engine-Absent Control Behavior.md`

## 1. Delivered

| Task | Result |
|---|---|
| 0 | Own Linux/Windows pixi solve, Python 3.12, Playwright >=1.65.2, Node.js 22; Chromium installed; extension built; inherited docs committed and authoritative here |
| 1 | Retained `scripts/service_probe.py`; all 24 baseline rows and isolation/concurrency assertions passed on main-derived code |
| 2 | Window manager, thread-affine owners and bounded short/long CDP worker lanes; loopback HTTP and CDP; context shim/stealth; lazy launch/relaunch; graceful shutdown |
| 3 | Pid/start-time clients, leases, label revocation and reaper; `UCBrowser` service mode, public `new_page()`, delayed target resolution; no client context closure/init scripts |
| 4 | HTTP pages, cookie read/import, retained per-origin fetch, real incremental NDJSON stream, PNG/HTML/MHTML; stdlib-only `ServiceClient` with lazy package exports |
| 5 | Native Chrome port 0 with profile/process ownership checks; first-control engine injection; all 39 engine-absent return/exception outcomes tabulated and measured |

No changes were made to `event_tool`, its submodule, live profiles, abandoned Chrome on 9222, or any existing systemd service. Test windows used fresh temporary profiles and were closed. No service was deployed or left running.

## 2. Verification actually run

| Check | Observed result |
|---|---|
| Independent baseline `pytest -q tests/` | 102 passed, 1 skipped, 1 failed |
| Baseline repair: synthetic MHTML captured from a real routed URL instead of `about:blank` | Targeted `test_synthetic_fixtures_score_correctly`: 1 passed |
| Service/API/client/core plus existing health tests | 115 passed |
| Additional startup/profile-title regressions | 3 passed, 11 deselected |
| Baseline probes | All 24 rows passed; clean shutdown |
| Real-service probes | All model assertions passed; no failed rows. Rows 13 and 19 are intentionally baseline-only; graceful service shutdown checked separately, live stealth value False |
| Revocation | A separate client blocked in `wait_for_timeout(600000)` raised `TargetClosedError` 0.04 s after label deletion |
| Browser-free HTTP caller | Cookie-bearing in-page fetch without registration; one retained page per origin; UTF-8 split across streamed chunks reconstructed correctly |
| Snapshots and controls | HTML fixture content, PNG signature, MHTML archive, remote set_text read back through client Page |
| Native | Engine undefined before first control; eight-key pattern map after injection; second independent launch HTTP 409 `profile_held` |
| Held-profile prelaunch refinement | Refusal before launching another Chrome; original endpoint file and page count unchanged |
| Final shutdown | No process remained with any acceptance profile path |

The full suite was not rerun after the targeted repair or service implementation. Tests above are separate observed runs, not one claimed aggregate suite.

## 3. Findings that affect consumers

1. **Stale page lists, not lost tabs.** `context.pages` does not pump sync Playwright events. Pump before enumeration; the corrected probe matched exact owner/client/Chrome target sets.
2. **Chrome flattens argv.** Installed Chrome rewrites its process command line into a single string on this host. Treating missing argument boundaries as pid reuse bypassed the prelaunch lock check. The guard now fails closed on ambiguous titles and recognizes the held profile. Regression and real-launch checks passed.
3. **No constant 409 fallback table.** Six of 39 absent-engine outcomes require real page data or Playwright errors. `UCBrowser` runs original bodies locally on `no_controls`; HTTP-only callers get `ControlsUnavailable`.
4. **Callable controls cannot be JSON.** `scan_action` and callback-bearing `scroll_feed` orchestrate locally; nested controls remain remote. Wire routes/response shapes remain v0. `bad_arguments` (400), `window_stopped` (503), fixed 30-second worker admission deadline and GET-default fetch are documented clarifications sent to PBX 210-1001.
5. **Grok auth detection is still a task-6 issue.** `has_login_wall` expects a `blocking` field the current engine does not emit. Adding the phrase “Sign up to continue” alone cannot repair it. See Engine-Absent Analysis §6 before site migration.
6. **Do not use the destructive probe on live windows.** It deliberately adds context scripts/cookies and creates/closes pages. Its service-mode guard clients keep raw CDP test pages alive through the zero-client sweep.

## 4. Next work — requires coordination first

1. Coordinate task 6 with the owner and PBX 210-1001: live Grok profile ownership/location, sessions, adapter auth detection, streaming sends/stop and thin provider/MCP clients. Do not open the live profiles before that coordination.
2. Task 7: install and verify the independent systemd user unit, including restart cleanup. No unit was installed in this build.
3. Task 8: migrate health/generic clients and remove provider-owned pinned browser threads. Consumer moves remain PBX 210-1001's scope.

Unverified: Firefox/WebKit live execution, Windows runtime profile guards, multi-day idle operation, injected engine against real bot checks, mixed client/service Playwright versions, and live Grok account concurrency. Keep concurrent sends at one when task 6 is built.

## Verification

```bash
pixi run -e dev pytest -q tests/test_service_api.py tests/test_service_client.py tests/test_service_windows_core.py tests/test_health.py
DISPLAY=:0 XAUTHORITY=/home/rabbit/.Xauthority pixi run python scripts/service_probe.py
```

These use no live profiles. If they disagree with this handoff, resolve the discrepancy before starting task 6. For service-mode probe invocation and disposable-window requirements, use Build Spec §8–Verification.
