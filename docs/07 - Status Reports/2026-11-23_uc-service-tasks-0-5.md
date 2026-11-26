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

## 5. Authorized wire-v0 follow-up

After the initial tasks 0–5 delivery, the owner authorized review, implementation, verification and push of PBX 210-1001's contract refinements. The following supersedes the fixed queue deadline and 409-first behavior recorded above:

- Windows accept positive finite `queue_timeout_s` (default 30) and expose it in discovery. A real occupied worker regression verifies configured expiry as `busy`; explicit internal `None` preserves an unbounded queue wait.
- Finite `ServiceClient` control timeouts are whole-operation budgets plus the discovered queue deadline and 5 seconds. Default reads remain unbounded because the controls have no universal wall-clock bound; `chat(timeout_s)` is only its response-polling bound.
- Fetch already used `credentials: 'include'`. A retained page now loads once (`domcontentloaded`), at explicit `from` or the origin root; later same-origin `from` becomes a Fetch referrer with no navigation. Existing per-page serialization remains. Chromium accepted a cross-origin redirected document's explicit referrer rather than raising; an explicit origin guard now fails that request as `origin_unreachable` before sending it.
- Snapshot rule 7 now says **of an existing target**. HTTP-only registration/page creation remains available without a browser library.
- `UCBrowser` uses the start response's `offers` to select faithful local engine-none behavior before any control POST; unexpected 409 responses still fall back. The source audit found zero `self._context` accesses in all 39 original page-taking bodies; behavioral tests cover caller-page text and errors.
- Corrected the solver diagnosis: the initial UC solve selected an incompatible combination; conda stealth does not universally pin Playwright 1.60. **Both platforms were re-solved; Windows was not run.** Carry that caveat into PR review.

Verification: the focused client/core/fetch run produced **102 passed, 1 failed** (the redirect assumption). After adding the explicit origin guard, the affected real-Chromium fetch file produced **3 passed**. These checks include cross-origin cookie inclusion, exact same-origin referrers, no later navigation, and an uninterrupted stream while another request waits on its page. The other 100 client/core tests passed in the first run; they were not repeated after the fetch-only fix. No live accounts, profiles or deployment were used.

## 6. Authorized navigation-cleanup follow-up

`open()` and `open_with_intercept()` now close an unreturned page best-effort if navigation or another post-creation step fails, preserving the original exception even if cleanup also fails. Neither releases the whole client nor closes its context; successful calls and public signatures are unchanged.

Focused verification: `pixi run -e dev pytest -q tests/test_service_client.py -k 'failed_open or open_and_open_with_intercept'` → **9 passed, 54 deselected**. The regression matrix covers both opening methods, service and in-process page creation, and successful/failed cleanup; the existing successful-open test also passed. This run uses the loopback fixture service and fake Playwright, not live accounts.

Recorded PBX 210-1001's baseline correction in Build Spec §8 and Setup §3: its 100/4 run had no extension bundle and skipped three bundle-dependent checks. Our built-bundle 102/1/1 run exposed `test_synthetic_fixtures_score_correctly`, later repaired by capturing routed-URL MHTML. This was not a regression against a previously passing Linux fixture test. No baseline or live injection checks were rerun for this documentation correction.

## 7. Authorized authoritative-target follow-up

Lease maintenance now uses browser-level `Target.getTargets` and `Target.closeTarget` on CDP windows, excluding non-page targets. Firefox/WebKit retain Page-based enumeration and closure. Reaping reads fresh owner inventory instead of the status endpoint's cached timeout/error fallback; enumeration failure leaves lease records intact. Cleanup owner jobs do not launch or relaunch windows.

Verification: `pixi run -e dev pytest -q tests/test_service_api.py tests/test_service_windows_core.py tests/test_service_targets.py tests/test_service_fetch.py` → **65 passed**. The real Chromium scenario created a raw page in a separate browser context, disabled owner `scan`/`resolve`, and revoked a client blocked in a 600-second wait; it failed with a closed-page error within the test's 5-second limit. The raw-page sweep removed that separate-context page while preserving the internal fetch page and one blank page. Unit coverage also verifies non-CDP closure, lease preservation on stale/failed inventory, and refusal to launch for cleanup. Native Chrome and Firefox/WebKit binaries were not rerun in this follow-up.

## Verification

```bash
pixi run -e dev pytest -q tests/test_service_api.py tests/test_service_client.py tests/test_service_windows_core.py tests/test_service_fetch.py tests/test_health.py
DISPLAY=:0 XAUTHORITY=/home/rabbit/.Xauthority pixi run python scripts/service_probe.py
```

These use no live profiles. If they disagree with this handoff, resolve the discrepancy before starting task 6. For service-mode probe invocation and disposable-window requirements, use Build Spec §8–Verification.

## 8. 2026-11-24 explicit-routing migration (branch `feat/local-service`)

Breaking change, owner-authorized, in `universal_controller` only. `UCBrowser(service=, window=, label=, timeout_ms=)` replaces environment-based selection; `ServiceClient(url)` requires an address; `start()` validates `GET /windows` first. Docs reconciled: Build Spec constraint 4, §4, §9; Design constraint 6 and §11 item 3; Setup; System Overview; Analysis 01. Consumer migration must pass standard sources to `harvest`, native and retry work to `native`; plain HTTP never uses a browser; Grok future site routes unchanged. No `event_tool` code was changed here.

Verification: `pixi run -e dev pytest -q tests/test_service_client.py` → **77 passed**. A throwaway real HTTP/CDP smoke scenario with fresh Chromium profiles also passed: conflicting legacy environment variables did not override the explicit URL/window/label; missing and non-attach windows failed before any registration or browser launch; explicit page creation, HTML snapshot and release succeeded. Graceful shutdown left the service thread and windows stopped. The smoke script was removed. No live profiles or deployment were used.

## 9. 2026-11-24 authorized deployment-blocker fixes

The owner authorized both fixes after PBX 210-1001 exercised `event_tool`'s native route against `c211ecd`. That peer reported its route ready but disabled; no consumer configuration, live profiles or deployed service was changed in this follow-up.

- Native Chrome now gets a dynamically selected explicit nonzero loopback debugging port. Its own stderr announcement, live process and `/json/version` browser path must agree before attachment; Linux additionally checks socket ownership. Allocation/bind races fail closed as `launch_failed`, without automatic retries. The held-profile guard remains, and explicit-port discovery requires no `DevToolsActivePort`. This shared launcher also serves in-process native mode.
- `python -m uc_browser.status_server` and `create_app()` default to no health scheduling. `--run-scheduler` / `run_scheduler=True` explicitly enables it; manual probe endpoints remain usable.
- Service-mode probe row 20 now requires native `navigator.webdriver is False` before engine injection. The historical prototype baseline retains port 0. Chromium launch/stealth code was not changed.

Verification: **81 passed** in `pixi run --frozen -e dev pytest -q tests/test_service_windows_core.py tests/test_health.py`. Tests cover foreign listener refusal, explicit endpoint proof without a file, held profiles, failure cleanup, bounded stderr draining, unchanged Chromium endpoint-file discovery, scheduler default/opt-in and manual probing. The real CLI smoke passed native probe rows 20–22 and S1, explicit `UCBrowser` attachment, release and graceful shutdown. Native Chrome 157.0.8103.78 and unchanged Chromium 159.0.8154.6 both returned `navigator.webdriver=False` on fresh client-created pages with Playwright 1.65.2. No automatic health probes or leftover test browsers/drivers; the throwaway smoke was removed. Conditions and timing are in Analysis 00 §8. Windows and real bot checks were not run.

Historical external verification, kept separate from this focused run: PBX 210-1001 reported **245 passed, 1 skipped** for the full UC suite at `c211ecd`. No whole-suite result is claimed for this follow-up.

## 10. 2026-11-24 authorized Grok/NSFW service migration

The owner explicitly authorized migrating the signed-in Grok window to UC and using the **NSFW** folder after the account merge. The existing project was identified from the profile's history, then verified in the live UI (`NSFW - Grok`, enabled composer) and Grok's workspace metadata. No new account folder was manufactured.

Built:
- Bounded, thread-affine site sessions, account send cap one, atomic URL-only persistent map, ephemeral stateless sessions, idle/LRU teardown, independent stop and NDJSON streaming.
- Grok adapter borrows a service page. Project identity/name and conversation membership fail closed; the actual project uses cards, so membership uses its workspace-filtered API rather than nonexistent anchors. Cached membership does not authorize a later continuation after removal.
- Provider and MCP implementations are HTTP-only Grok clients. Named sessions share the service map; blind ephemeral sends retain their page until eviction. Generic/health browser ownership remains separate. Local health-probe failures no longer gate a different service-owned identity.
- Versioned `systemd/uc-service.service`, `systemd/uc-grok-proxy.service`, and Grok-only production proxy config. Router retries/fallbacks are disabled because an uncertain send cannot safely be replayed.

Deployment:
- Stopped the old `uc-grok-proxy` owner before opening its profile. Private closed-profile/unit backup: `~/.local/state/universal-controller/backups/20261124T044452Z/`.
- Enabled independent user units: UC on loopback 4010; the HTTP-only proxy on loopback 4000, preserving `uc-grok` and `grok-browser`.
- Profile remains `/home/rabbit/event_tool/data/.grok/.uc_chromium_profile`; no stale state-file cookies are injected. Private windows config is `~/.config/universal-controller/windows.json`; the session map is `~/.local/state/universal-controller/sessions.json`.
- Native Chrome, its profile, port 9222 and `uc-native-window` were not changed.
- Gateway MCP cutover is live: event_tool explicitly passes its `EH_UC_SERVICE_URL` configuration to `register_grok_tools`. Its system unit restarted at 2026-11-24 05:47:44 UTC; no further administrator restart was needed.

Observed verification:
1. Focused session/adapter/client/provider/Grok/core/fetch/health run: **499 passed, 1 skipped, 1 failed**. The failure was a test race releasing a fake blocked read before it observed closure; removing that premature release retained the error assertion, and the targeted case then passed (**1 passed**).
2. Live project discovery correction and fresh-membership regressions: **104 passed** in `tests/test_grok_service_adapter.py`.
3. Throwaway LiteLLM Router proof against a synthetic uncertain-delivery provider: **one invocation**, zero retries and zero fallbacks with the production router settings. No account request in this proof.
4. One real one-word `Ping` through the proxy's streaming endpoint completed with a nonempty reply. One content chunk was observed; this short reply does not prove multi-chunk live delivery. Grok's actual URL retained the project and chat id; the backend confirmed its `workspaceId` matched NSFW.
5. Proxy restart preserved both browser PID/CDP endpoint and named session URL.
6. UC restart removed the previous browser, left the lazy window stopped, and restored the named URL map with `live: false`. A subsequent service read reopened the same signed-in identity, verified project membership, and returned the existing two-message conversation without another prompt. Project listing returned the one test conversation; exactly one process owned the profile.
7. Through the running event_tool MCP gateway, `list_chat_sessions` returned the shared `uc-migration-smoke` session and `grok_list_conversations` returned the one conversation in the configured NSFW project. Neither check sent another prompt.

No prompt/reply text or cookies are included in these logs. Full-suite, Windows, multi-day idle and real long-generation stop/concurrency are not claimed. Independent stop and concurrency were exercised with deterministic blocked-worker tests.

## 11. 2026-11-24 authorized explicit external-CDP attachment

PBX 210-1001 reported native harvest sources failing with `profile_held` after the pin move: native launch no longer implicitly attaches to the operator's already-open Chrome. The owner authorized an explicit attachment path and isolated tests, leaving live Chrome and the consumer tree untouched.

- Added `UCBrowser(cdp_endpoint=..., label=...)`, exclusive with service/window and launch arguments. No environment selection, profile lock, browser launch, stealth, cookie injection or context scripts.
- Public page-opening methods and their popups are client-owned. Exit closes those tabs and disconnects; unrelated tabs/context/browser survive. Connection errors name the endpoint; there is no local-launch fallback.
- Verification: `pixi run --frozen -e dev pytest -q tests/test_cdp_attachment.py tests/test_service_client.py` — **96 passed in 41.83 s**. Disposable Chromium proved interception, local text controls, caller-close, popup cleanup, failed-navigation cleanup, unrelated tabs opened before/during attachment, and reattachment to the surviving browser.
- No extension sources changed. No service/proxy restart, event_tool edit, signed-in profile access or port-9222 connection was made. Real erobay/ra-sf access and Cloudflare clearance are **unverified** here; consumer pin/routing and the live check remain with PBX 210-1001.

## 12. 2026-11-24 native-only service migration

Owner selected **native-only** scope: service ownership, quiet loading, consumer cutover and live verification. Standard harvest, generic/health migration and the separately requested Grok network exposure are outside this authorization.

Implementation:
- `unattached: true` native windows own plain Chrome with an HTTP-only owner/reaper; no startup Playwright, context scripts, cookie import, stealth or extension flags.
- Explicit quiet client registration, URL creation and fresh title/URL metadata. Readiness is entirely consumer-side. Temporary workers preserve HTTP offers and detach before completion.
- Registered holder labels, active workers and existing endpoint sockets block quiet admission. Unreadable socket state fails closed. Revocation does not pretend an abandoned live CDP connection has disconnected.
- Consumer teardown disconnects before lease release; persistent site sessions cannot target quiet-native windows. Grok's existing window/configuration remains ordinary.
- Native is deployed with `engine: none`, preserving the peer's measured fallback path. Optional page-local injection is tested but is not the chosen site-access method.

Verification: **316 passed in 82.08 s**:
```bash
pixi run --frozen -e dev pytest -q tests/test_service_unattached_owner.py tests/test_service_unattached_api.py tests/test_service_native_migration.py tests/test_service_windows_core.py tests/test_service_api.py tests/test_service_client.py tests/test_service_fetch.py tests/test_service_targets.py tests/test_service_sessions.py tests/test_cdp_attachment.py
```
Real disposable Chrome proved quiet title/URL loading (including encoded URL delimiters), zero established debugging-port connections during quiet operations, transient control/cookie attachment cleanup, rejection of a still-attached revoked client, process-death lease cleanup and continued browser ownership. Other checks covered bounded worker races and existing service/Grok-session contracts. No production native profile or port 9222 was touched by these checks.

Deployment handoff: PBX 210-1001 reported event_tool `e575802`, pin `5f55401`, **1954 passed with the same four unrelated failures**, and no running/queued job. It stopped `uc-native-window` at 17:54 UTC. The UC session confirmed no live profile holder, privately backed up the closed profile, and added only the native window to `~/.config/universal-controller/windows.json`.

UC restarted and launched native PID `1941470`. Production proof: quiet registration → start → blank page → metadata → close/release passed; headed `navigator.webdriver` was `false`, no engine was present, and attachment teardown left zero clients/connections and one blank page. Grok config/sites were unchanged, its `uc-migration-smoke` mapping survived, and the proxy was not restarted. Backups are private under `~/.local/state/universal-controller/backups/native-20261124T1747/`.

At 17:57 UTC, PBX 210-1001 activated `WEB_UC_NATIVE_WINDOW=native`, restarted the consumer and began live checks. At 18:25 UTC it confirmed the cutover and disabled the old `uc-native-window` unit, retaining its file for rollback.

Live results reported by the consumer owner:

| Source | Route | Result | Messages | Conditions |
|---|---|---|---:|---|
| erobay | `service:native` | ok, 18:20:32 UTC | 273 | Human clicked the Cloudflare checkbox; quiet preparation passed at 24 s, then the client attached, fetched and detached |
| ra-sf | `service:native` | ok, 18:22:38 UTC | 20 | 81 s; window mapped/on top in every 20 s sample |

The first ra-sf run timed out at 1200 s with the window later found iconified. Revocation closed five pages for one client and left the window clean. One run with each visibility condition is correlation, **not proof of cause**. UC's launcher/service and `browser.py` contain no minimize/iconify/unmap call; no automatic visibility restoration was added.

Final UC-side check: native running, zero clients, one blank page, zero established debugging-port connections; old unit inactive and disabled. Grok proxy remains a separate HTTP endpoint, not dependent on Grok MCP tools.

Migration is complete. Optional tab/window persistence was subsequently authorized as separate follow-on work; the current reaper still closes unheld pages and no persistence behavior is claimed here.

## 13. 2026-11-24 optional window-lifetime tabs

The owner authorized persistence as an option and explicitly selected **until the browser/window ends**, not URL storage or restart restoration. Default cleanup remains unchanged; no automatic focus/mapping was authorized.

Implemented optional lifetime at page creation, explicit promotion/reassignment, discovery as `holder: "window"`, and service-mode `UCBrowser.new_page(lifetime="window")`. Retention uses the existing service-owned inventory. Other clients' and internal service pages cannot be taken; invalid requests and missing targets fail. Explicit close, failed navigation/resolution and browser-generation changes still clean up retained pages.

Verification: **193 passed in 61.29 s**:
```bash
pixi run --frozen -e dev pytest -q tests/test_service_page_lifetime.py tests/test_service_native_migration.py tests/test_service_unattached_api.py tests/test_service_api.py tests/test_service_client.py tests/test_service_targets.py tests/test_service_sessions.py tests/test_cdp_attachment.py
```
Disposable real Chrome covered release/revoke/process-death survival, subsequent quiet admission with zero sockets, explicit closure, reassignment to client cleanup, ownership refusal, invalid requests, navigation/resolution failures, and no restoration after browser termination/relaunch. No live profile was used in these checks.

Service code `d855385` is deployed after the consumer owner confirmed no jobs or clients and reported event_tool `8832bb8`, **1964 passed with the same four unrelated failures**. Production HTTP-only verification promoted a blank tab, released its client, waited beyond the live five-second reaper, and found it retained with zero connections. A subsequent quiet reservation succeeded; explicit closure removed the retained tab. Native returned to zero clients/one blank page; Grok's named-session map survived and the proxy was not restarted.

Consumer activation and live replacement verification **passed**, reported by PBX 210-1001 at 18:58 UTC. `keep_page_open` is enabled on erobay only. The restarted native browser (PID `1968729`) was drawn according to the consumer's own check.

| Run | Quiet preparation | Extraction | Retention result |
|---|---|---|---|
| 18:55:06 UTC | Human checkbox click; through at 23 s | 273 messages | Promoted preparation tab survived client exit, attachment and repeated reaper passes; zero clients, one retained erobay tab plus blank |
| 18:57:12 UTC | Through in 1 s, no checkbox | 273 messages | New tab promoted, old same-host retained tab explicitly closed; still exactly one retained erobay tab plus blank 12 s later |

No unexpected busy response, leased-page leak or lifetime warning was reported. This proves optional retention and consumer replacement, **not** longer-lived clearance. Whether keeping the tab open extends the site's clearance is unverified; the next scheduled harvest is the first observation. Do not restart the service/native window merely to recheck it: restart intentionally ends this retained tab.

Retained tabs are still attached whenever any Playwright client/worker is connected to that window; retention alone does not attach. No network exposure, Grok prompt, automatic mapping/focus or restart-restoration feature was added.

## 14. 2026-11-26 explicit ZeroTier listeners

The owner explicitly authorized the **full UC API** on loopback plus `10.158.218.150:4010`, without authentication or a front, then requested the Grok proxy on ZeroTier too. Both retain local callers unchanged; the network boundary trusts every reachable ZeroTier peer. No wildcard/LAN bind, Grok prompt, conversation cleanup or reliability-contract change is included.

Added a shared all-or-fail socket runner and a Grok proxy entrypoint that runs LiteLLM's real ASGI app directly. One app serves loopback and selected additional IPv4 addresses. Unit templates remain loopback by default; private deployment units opt in. Missing/busy addresses abort startup before browser ownership begins.

Verification: **28 passed in 4.23 s** (`tests/test_listeners.py`, `tests/test_health.py`). A real proxy process on disposable ports served liveness and both configured model names through `127.0.0.1` and `127.0.0.2`; an unselected `127.0.0.3` connection was refused. Its UC upstream was deliberately unreachable, and no account request was sent.

PBX 210-1001 gave a safe-restart signal at 00:01:49 UTC: no jobs, native clients or live Grok session. The retained erobay tab was still present after 29 h 5 min. Its 2026-11-25 nightly quiet page had nevertheless returned to a check after 11 h 16 min: retention worked but did not preserve clearance in that observation.

Code `358c8ba` is pushed and deployed. Both user units restarted at **2026-11-26 00:13:32 UTC**, ending the retained tab by design. Their previous units are backed up privately under `~/.local/state/universal-controller/backups/listeners-20261126T001257Z/`. `windows.json`, profiles, session-map configuration and event_tool's checkout/configuration were not changed. Both windows are lazy/stopped with zero clients/pages; the next caller starts its window explicitly.

Production verification from this host: `/healthz` on 4010, `/health/liveliness` and `/v1/models` on 4000 all returned 200 through both `127.0.0.1` and `10.158.218.150`. The proxy listed `grok-browser` and `uc-grok`. Socket inventory showed exactly the two selected listening addresses per port, one process per app, no wildcard listener.

PBX 204-1016 independently reported those same three URLs returning 200 **from blackbox (`10.158.218.141`) by `smallblackbox.zt`**, resolving to `10.158.218.150`; model names matched. It made four read-only GETs (model listing twice), no DELETE or prompt. This proves remote reachability, not browser readiness or completion correctness.

The current user units report `RestartUSec=3s`, `StartLimitIntervalUSec=10s`, `StartLimitBurst=5`: automatic startup retries alone cannot exhaust that limit (at most four starts in ten seconds). A missing ZeroTier address still prevents both interfaces from starting; deployment must preserve this retry policy or explicitly configure an unlimited startup interval. No user-unit dependency on the system-level ZeroTier unit was added. No account cleanup or client deployment was performed.
