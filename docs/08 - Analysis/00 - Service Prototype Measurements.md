# Service Prototype Measurements

**Status**: CURRENT (each probe run on one machine on one day; see "What was not measured")
**Date**: 2026-11-23
**Applies to**: the service proposed in `docs/01 - Design/02 - UC as a Local Service.md`
**Depends on**: `docs/00 - Theory/00 - Windows, Transport and Control.md` (the vocabulary: window, target, transport, control, attachment)

## Verdict

1. One process can own several browser windows through Playwright, publish each window's CDP endpoint, and run `UCBrowser` page controls on pages that *other* processes created, addressed by CDP target id. Attaching costs 0.03–0.07 s; a control call costs 0.03–0.08 s.
2. Inside one window every client sees every page and context-level hooks reach every page; across windows nothing crosses.
3. Work on one window is concurrent **per attachment**: a six-second polling control on one attachment thread did not delay a second attachment thread (median 2 ms per call), and the second thread could act on the busy page.
4. Stealth applied once to a window's context reaches pages that clients create.
5. Installed Google Chrome serves CDP on a browser-chosen port, and the engine bundle runs in it when injected over CDP: `detect_all` and `set_text` worked.
6. In the field, a browser whose owner was abandoned stayed up for days and later runs silently attached to it (§5).

The original three probes were throwaway scripts. `scripts/service_probe.py` now rebuilds B and C with local HTTP fixtures and temporary profiles; all 24 rows passed on the main-derived independent checkout on 2026-11-23 (§7). The original numbers below remain historical measurements.

## 1. Conditions

| | |
|---|---|
| Host | `smallblackbox`, Ubuntu, x64, one desktop session (`DISPLAY=:0`) |
| Browsers | Playwright's bundled Chromium, build 1255, headed; installed Google Chrome (`/usr/bin/google-chrome`) for probe C part 3 |
| UC code | `99390a5` (the commit `event_tool` pins), extension bundle built from that tree. **Not re-run on `main` (`5e3dd0a`)** |
| Python env | `event_tool`'s pixi env: Python 3.12, sync Playwright 1.65.2, playwright-stealth 2.0.3 |
| Pages | `https://example.com`, `https://example.org` |
| Runs | probe A once; probe B three times (two aborted on test-script errors after the measured steps); probe C once. Ranges are min–max over the runs that reached the step |

## 2. What was built

**Probe A — shared browser, no service.** Chromium started by hand with the UC extension and `--remote-debugging-port`; two Python processes attached at the same time with `connect_over_cdp`.

**Probe B — the service shape.** One Python process:

1. launched two windows with `chromium.launch_persistent_context(<own temp profile>, headless=False, args=[--load-extension, --disable-extensions-except, --remote-debugging-port=0, --remote-debugging-address=127.0.0.1])`;
2. added one context init script per window (`window.__SVC_SHIM = '<window name>'`);
3. read each window's CDP endpoint from `<profile>/DevToolsActivePort`;
4. served three HTTP routes on loopback: list windows, open a page in a window, and run a named `UCBrowser` method on the page with a given CDP target id. The methods were called on a `UCBrowser` instance that was never started.

A second process acted as the client. Target ids were read on both sides with `Target.getTargetInfo` over a per-page CDP session.

**Probe C — lifecycles.** One Python process, three parts:

1. *Attachment threads.* The main thread launched one window as in B. Two more threads each started their own `sync_playwright()` and attached with `connect_over_cdp`. Thread L opened a page and ran a loop shaped like `_wait_chat_response` (`wait_for_timeout(250)` then an `evaluate`, for six seconds). One second in, thread S timed 20 `evaluate` calls and two real controls on its own page, then found L's page by target id and wrote a value into it.
2. *Context-level stealth.* A client-created page read `navigator.webdriver` before and after the launching thread called `Stealth().apply_stealth_sync(context)`. (`apply_stealth_sync` on a context is one `add_init_script`.) The window was launched without `--disable-blink-features=AutomationControlled` so the difference would show.
3. *Native Chrome.* Google Chrome started as a subprocess with `--remote-debugging-port=0` on a temporary profile. After attaching: evaluated the engine bundle (`extension/dist/uc-extension.js`) in a loaded page, as the bench does for fixtures, and ran controls; then added the bundle as a context init script and opened a second page.

## 3. Results

### Probe A

| Check | Result |
|---|---|
| Attach and load a page, per client | 1.62 s and 1.65 s (page load included) |
| UC extension present in each client's page | yes (`typeof window.__UC_setText === 'function'`) |
| Cookie set by client A visible to client B | yes |
| Pages each client saw | all 3 in the window, including the other client's |

### Probe B

| # | Check | Result |
|---|---|---|
| 1 | Playwright-launched Chromium also serving CDP on a port | works; both windows listed an endpoint |
| 2 | Client attach to a window | 0.03–0.07 s |
| 3 | Service's context init script inside a page the client created | present |
| 4 | Service runs `set_text` on the client's page | 0.06–0.08 s; `{'success': True, 'method': 'nativeSetter'}`; the client read the typed value back from its own page object |
| 5 | `has_login_wall` / `find_inputs` / `detect_all` through the same generic route | 0.06–0.08 s / 0.03–0.04 s / 0.05–0.06 s |
| 6 | Service opens a page; client finds it by target id | visible to the client after 0.16–0.17 s (not immediately) |
| 7 | Client drives the service-opened page | `title`, `evaluate`, `content`, `goto`, `click(trial=True)` all worked |
| 8 | Client A's `add_init_script` inside a page created by a second client of the same window | **present** — init scripts leak between clients of one window |
| 9 | Pages each client of window `a` saw | all 8 |
| 10 | Window `b`: cookie set in window `a` / init script of window `a` | neither present |
| 11 | Client calls `context.close()` (window `b`) and `browser.close()` (window `a`), then exits | both windows still alive. Window `b` kept both pages, including the one the client created. Window `a`: clients counted 8 pages before, the service counted 7 after — **one page unaccounted for, `unverified`** (pages had piled up from the two aborted runs) |
| 12 | Memory, PSS summed over a window's processes | 345 MB with 7 pages (17 processes); 205 MB with 2 pages (11 processes) |
| 13 | Service shutdown | both browsers exited; no leftover processes |

### Probe C

| # | Check | Result |
|---|---|---|
| 14 | Long control on attachment thread L | ran 6.1 s, 24 polls |
| 15 | Meanwhile on attachment thread S: 20 `evaluate` calls | median 2 ms, max 14 ms |
| 16 | Meanwhile on S: `find_inputs` / `detect_all` on its own page | 58 ms / 24 ms |
| 17 | S finished before L | by 4.2 s |
| 18 | S writes into L's page while L is mid-loop | 4 ms; L read the value at the end |
| 19 | `navigator.webdriver` in a client-created page, before / after stealth on the context | `True` / `False` |
| 20 | Google Chrome with `--remote-debugging-port=0` | wrote `DevToolsActivePort`; attach worked |
| 21 | Native page before injection | `typeof window.__UC_detectAll === 'undefined'` |
| 22 | After `page.evaluate(bundle)` on the loaded page | no error; `window.__UC.ready` true within 1.5 s; `detect_all` returned its pattern map in 0.09 s; `set_text` succeeded and the value was read back |
| 23 | Bundle added as a context init script, then a new page | `window.__UC.ready` true; `__UC_setText` is a function |
| 24 | One extra `sync_playwright()` in a process | 1 driver process, 82 MB PSS |

### Launch note

Starting Playwright's Chromium binary by hand (probe A) failed with `No usable sandbox!` until `--no-sandbox` was passed: this Ubuntu restricts unprivileged user namespaces through AppArmor. Playwright-launched Chromium on this host already runs with `--no-sandbox`. Installed Google Chrome started as a plain subprocess without it (row 20).

## 4. What the numbers mean for the design

| Finding | Consequence |
|---|---|
| Rows 2, 4, 5 | A control call through the service adds tens of milliseconds, against a browser launch of seconds that it removes |
| Rows 3, 8, 9, 19 | A window is one shared scope. Per-window setup (shim, stealth) is done once on the context and covers every page; isolation must be bought with a separate window |
| Row 6 | A client must wait for a service-opened page by target id; it cannot read `context.pages` straight after the call |
| Row 11 | Disconnecting does not free a client's pages. The service must own page lifetime |
| Row 12 | Budget roughly 200 MB per near-idle window; tabs are cheaper than windows |
| Rows 14–18 | Concurrency is per attachment. A long control needs its own attachment thread, not its own window, and a second attachment can reach a busy page (a "stop" can interrupt a send) |
| Rows 20–23 | A native window can have controls: inject the engine over CDP. The two window kinds differ in how the engine arrives, not in whether controls exist |
| Row 24 | Each attachment thread costs one Playwright driver, about 80 MB. Attachment workers must be a small bounded pool |

## 5. Field observation: an abandoned owner leaves a live browser

Not a probe; read from the running `event_tool` deployment on 2026-11-23.

| Fact | Evidence |
|---|---|
| A Google Chrome on port 9222 had been running for 4 days 19 hours | process started 2026-11-19 01:57:49, `--remote-debugging-port=9222 --user-data-dir=…/data/.uc_chrome_profile` |
| It was launched by the harvest that began at 01:57:49 that day | harvest log: `Launching native Chrome` at that second |
| That harvest was abandoned at its wall-clock limit | job runner log at 03:27:49, exactly 90 minutes later: `… -> failed`; no `Web sources:` line that day |
| The browser held 13 pages | 8 on one source's site, 5 new-tab pages |
| Every later nightly harvest logged `Launching native Chrome` then `Connected … (port 9222)` within two seconds | each run attached to the four-day-old browser. Inferred cause: a second Chrome started on a held profile opens a tab in the first and exits, which matches the five new-tab pages |

Nothing in the current design can close that browser: the thread that owned it cannot be killed, and `UCBrowser.close()` terminates only the process it started itself. This is the lifecycle the service's leases exist for.

## 6. What was not measured

1. **Injected engine against a bot check.** Rows 22–23 used `example.com`. Whether the engine's globals affect a Cloudflare challenge is `unverified`.
2. **A real long control.** Row 14 is a loop shaped like `_wait_chat_response`, not `chat` against a model.
3. **Load.** At most three attachments at once. No parallel harvest.
4. **A signed-in, long-lived profile.** All windows used fresh temporary profiles.
5. **Original code revision.** The original rows used `99390a5`; §7 now repeats the model on main-derived `9c3d6ab`.
6. **Different Playwright versions on the two sides.** Service and clients shared one environment.
7. **An idle launching connection over days.** Whether events queue up behind a launching thread that makes no Playwright calls was reasoned about, not measured (Build Spec §6, gotcha 8).
8. **Variance.** Treat every figure as an order of magnitude.

## 7. Independent checkout run — 2026-11-23

Runtime: main-derived `9c3d6ab`, Python 3.12.16, Playwright 1.65.2, stealth 2.0.3, Chromium 159.0.8154.6, installed Chrome 157.0.8103.78. Local fixture server; fresh temporary profiles; no account data.

| Rows | Measured |
|---|---|
| 1–3 | Both Chromium windows publish CDP; attachments 13–44 ms; owner init script visible to clients |
| 4–5 | First control including cold worker startup 431 ms; warmed controls 8–46 ms |
| 6–7 | Page appeared on client after 20 ms polling; all six raw page operations succeeded |
| 8–10 | Context script shared within window, exact four-page client sets match; cookies/scripts absent across windows |
| 11 | No targets disappeared on disconnect. Initial owner list missed one pending page-created event; pumping Playwright before snapshotting `context.pages` fixed it. Narrow B rerun matched every owner/client/Chrome target id (4 pages in a, 2 in b) |
| 12 | 275 MB PSS / 4 pages / 13 processes; 218 MB / 2 pages / 11 processes |
| 13 | All 56 tracked browser/driver processes exited; shutdown 0.79 s |
| 14–18 | Long loop 6.11 s / 24 polls; concurrent evaluates median 2 ms, max 5 ms; second attachment finished 5.00 s earlier and its write reached the busy page |
| 19 | Client-created page webdriver: True before context stealth, False after |
| 20–23 | Chrome port 0 works; engine absent before injection, ready after 0.05 s; detect/set_text work; context init injection reaches new pages |
| 24 | One Playwright driver, 117 MB PSS |
| Supplement S1 | Native control route injects on first call; eight-key pattern map, 583 ms including cold worker startup |

All isolation/concurrency model assertions passed. Row 11 was stale event enumeration, not page destruction: every owner listing must pump events before taking its page snapshot. Only that probe section was repeated after the correction; the full probe was not rerun.

## 8. Native launch and scheduler acceptance — 2026-11-24

Base: `c211ecd` plus the native-launch/scheduler fixes described here. Linux, Playwright 1.65.2, headed fresh temporary profiles, local HTTP form fixture only. Native used `engine: inject`, default `stealth: false`, and `UC_WINDOW_KEEPASSXC=0`; Chromium used `engine: extension` and its unchanged default stealth setup. These are one-run observations, not a bot-check benchmark.

| Window | Browser version | Debugging port selection | `navigator.webdriver` on a client-created page |
|---|---|---|---|
| Native | Chrome 157.0.8103.78 | explicit nonzero loopback port | `False`, before engine injection and through `UCBrowser` attachment |
| Playwright Chromium | Chromium 159.0.8154.6 | unchanged `--remote-debugging-port=0` | `False` with existing launch flags/context stealth |

The earlier port-0 native runs in §§3 and 7 measured CDP and injection, **not** native `navigator.webdriver`. PBX 210-1001 subsequently measured port 0 → `True`, explicit port → `False` on Chrome 157; that reported failure was the repro and was not rerun here. The retained prototype baseline still reproduces its original port-0 launch. Real-service probe row 20 now requires `False` before native injection.

Native service rows 20–22 and S1 passed: start 0.86 s, attach 34 ms, first remote control 636 ms with an eight-key pattern map, manually injected engine ready in 0.05 s, `detect_all` 29 ms, and `set_text` read back correctly. Row 23 was deliberately skipped (no context-wide script mutation needed). Explicit-port launch produced no `DevToolsActivePort`; a second launch was refused as `profile_held` without changing the running endpoint.

The actual `python -m uc_browser.status_server` entrypoint started with the scheduler off despite built-in registry seeds, `UC_AUTO_BOOTSTRAP=0`, and an empty user sites file. Before the first browser-client registration, neither configured lazy window nor any browser/driver descendant was running. Health and availability endpoints answered; no automatic probe was logged during the run. Both windows remained running with zero clients after release, then graceful service shutdown stopped all owned browser/driver processes. Unit lifespan checks cover explicit scheduler opt-in and manual probes while scheduling is off.

Verification: `pixi run --frozen -e dev pytest -q tests/test_service_windows_core.py tests/test_health.py` → **81 passed**; disposable real CLI/CDP scenario → **passed**, then removed. Windows runtime and real-site bot-check outcomes remain unverified.

## Verification

Run the retained baseline probe (opens disposable headed windows on the desktop):

```bash
pixi run python scripts/service_probe.py
```

It should print rows 1–24. Row 8 reading "absent" or row 10 reading "present" would overturn the isolation model in the Theory doc; row 15 rising to the scale of row 14 would overturn the concurrency model. Either way, stop and re-derive before building further.
