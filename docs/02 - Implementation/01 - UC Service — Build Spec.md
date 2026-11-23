# UC Service — Build Spec

**Status**: DRAFT — a spec for code that does not exist. Every section is `not implemented` unless it says *measured* or *exists*
**Date**: 2026-11-23
**Applies to**: new package `uc_browser/service/`; changes to `uc_browser/browser.py`, `uc_browser/status_server.py`, `uc_browser/sites/`, `uc_browser/llm_providers/uc.py`, `uc_browser/mcp/grok_tools.py`, `pyproject.toml`; consumer changes in `event_tool`
**Depends on**: `docs/01 - Design/02 - UC as a Local Service.md` (what and why; lifecycle ids E1–E14 and G1–G15 are its §3), `docs/08 - Analysis/00 - Service Prototype Measurements.md` ("row N" is its §3), `docs/02 - Implementation/00 - Setup and Operations.md` (install, extension build, status server)

The design doc decides the shape. This doc is what to type: files, config, the wire contract, how the client library behaves, how to run it, and the order to move consumers.

## Hard constraints (non-negotiable)

1. A Playwright object is touched only from the thread whose Playwright instance produced it. Never from an HTTP handler.
2. The service binds `127.0.0.1`. So does every browser's debugging port.
3. The client library never closes a context, never adds a context-level init script, and never launches on a profile the service lists.
4. With `UC_SERVICE_URL` unset, `UCBrowser` behaves exactly as it does today.
5. The service never attaches to a browser it did not launch.

## 1. Layout

| File | New? | Holds |
|---|---|---|
| `uc_browser/service/__init__.py` | new | package marker, wire-contract version |
| `uc_browser/service/windows.py` | new | `WindowSpec`, `Window` (owner thread, control-worker pool, engine placement), `WindowManager` (config, launch, relaunch, list) |
| `uc_browser/service/leases.py` | new | clients, leases, reaper, revocation by label, sweep |
| `uc_browser/service/sessions.py` | new | `Session` (page + attachment + worker), `SessionStore` (cap, idle eviction, persisted `session_id → url` map) |
| `uc_browser/service/api.py` | new | FastAPI router for §3; turns worker futures into responses and streams |
| `uc_browser/service/client.py` | new | `ServiceClient` — plain HTTP, imports no browser library, usable on its own for fetch, cookies, snapshot, sessions and controls on service-made pages. Separately: target-id resolution, which imports Playwright and is loaded only by `UCBrowser` |
| `uc_browser/browser.py` | change | `UCBrowser` service mode (§4); launch paths refuse a served or held profile |
| `uc_browser/sites/grok.py`, `grok_fast.py`, `generic.py` | change | take their page from a `Session` and stop owning a browser and a lock |
| `uc_browser/llm_providers/uc.py` | change | calls the site routes; pinned executors and the in-memory session map removed |
| `uc_browser/mcp/grok_tools.py` | change | calls the site routes |
| `uc_browser/status_server.py` | change | `create_app` builds the managers and includes the router; shuts windows down in its lifespan |
| `pyproject.toml` | change | pixi task `service = "python -m uc_browser.status_server"` |
| `scripts/service_probe.py` | new | probes B and C of the Analysis doc, kept as the live acceptance check (§8) |
| `tests/test_service_*.py` | new | §8 |

Nothing in `src/` (the JS engine) changes.

## 2. Config

Read once at service start from `UC_WINDOWS_FILE` (default `data/windows.json`). Paths are relative to the service's working directory; absolute paths are allowed.

```json
{
  "windows": {
    "harvest": {"browser": "chromium", "engine": "extension", "profile": "data/.uc_chromium_profile", "state_file": "data/.playwright_state.json"},
    "grok":    {"browser": "chromium", "engine": "extension", "profile": "data/.grok/.uc_chromium_profile", "state_file": "data/.grok/state.json"},
    "native":  {"browser": "chrome",   "engine": "inject",    "profile": "data/.uc_chrome_profile"}
  },
  "sites": {
    "grok": {"window": "grok", "max_sessions": 4, "max_concurrent_sends": 1, "session_idle_s": 1800}
  }
}
```

**Windows**

| Field | Values | Meaning |
|---|---|---|
| `browser` | `chromium` \| `chrome` \| `firefox` \| `webkit` | Playwright's Chromium; installed Google Chrome started as a subprocess; or Playwright's Firefox or WebKit. The last two serve no CDP, so they offer everything except attach |
| `engine` | `extension` \| `inject` \| `none` | `extension`: loaded at launch (`chromium` only). `inject`: the bundle is evaluated in a page at that page's first control call (any browser; it is how the health monitor already probes Firefox and WebKit). `none`: controls answer 409 |
| `profile` | path | the window's `user_data_dir`; created if missing |
| `state_file` | path, optional | Playwright storage-state JSON whose cookies are added at launch |
| `stealth` | bool, default `true` for `chromium`, `false` for `chrome` | apply `playwright-stealth` to the context at launch |
| `control_workers` | int, default 2 | size of the control-worker pool |
| `lazy` | bool, default `true` | launch on first use rather than at service start |

**Sites**

| Field | Default | Meaning |
|---|---|---|
| `window` | — | which window the site's sessions live in |
| `max_sessions` | 4 | live sessions (each is a page, a thread and a Playwright driver) |
| `max_concurrent_sends` | 1 | G5: the account's limit, not a technical one |
| `session_idle_s` | 1800 | an idle session older than this is closed; its map entry stays |

The example is the `smallblackbox` deployment, written relative to `event_tool`'s checkout for brevity. The service runs from its own checkout (§7), so the deployed file gives these as absolute paths under `/home/rabbit/event_tool/`: the service then opens the profiles its consumers hold today and existing sign-ins carry over. With no file, the service defines one window, `default` (`chromium`, `extension`), on the profile `CHROMIUM_EXT` mode uses now, and no sites.

The session map is written to `UC_SESSIONS_FILE` (default `data/sessions.json`).

## 3. Wire contract (v0)

HTTP on the status server's port (`UC_STATUS_PORT`, default 4010). JSON in, JSON out. Errors are `{"error": "<code>", "detail": "<text>"}` with the status shown.

### Windows, clients, pages

| Method and path | Body | Returns | Errors |
|---|---|---|---|
| `GET /windows` | — | `{name: {browser, engine, running, offers, cdp, clients: n, pages: [{target_id, url, holder, age_s}]}}`. `offers` lists what this window supports, from `attach`, `control`, `fetch`, `cookies`, `snapshot`, `mhtml`. `cdp` is `ws://127.0.0.1:<port>/devtools/browser/<id>`, or `null` if not launched or if the browser serves none. `holder` is a client id, `session:<site>/<id>`, or `null` | — |
| `POST /windows/{w}/start` | — | the window's entry, launched if it was not | 404 `no_such_window`; 409 `profile_held` (a browser the service did not launch holds the profile); 500 `launch_failed` |
| `POST /clients` | `{pid, label, window}` | `{client_id}` | 400 `bad_pid`; 404 `no_such_window` |
| `DELETE /clients/{id}` | — | `{closed: n}` — closes every page leased to the client (E9) | 404 `no_such_client` |
| `DELETE /clients?label=<label>` | — | `{clients: n, closed: n}` — revocation (E10). Zero matches is not an error | — |
| `POST /windows/{w}/pages` | `{client_id, url?, wait_ms?}` | `{target_id}` — a page leased to the client; blank unless `url` is given, in which case the service navigates it. `url` exists for callers that hold no browser library | 404 `no_such_window` / `no_such_client`; 502 `navigation_failed` |
| `DELETE /windows/{w}/pages/{t}` | — | `{closed: true}` | 404 `no_such_target` |
| `POST /windows/{w}/cookies` | `{cookies: [...]}` in Playwright format, or `{from_chrome: ["grok.com", ...]}` | `{added: n}` (E13, G15) | 404 `no_such_window`; 501 `chrome_cookies_unavailable` if `rookiepy` is not installed |
| `GET /windows/{w}/cookies?domain=<d>` | — | `{cookies: [...]}` for that domain, for a caller that will make plain HTTP requests with the window's session | 404 `no_such_window`; 400 `domain_required` |
| `POST /windows/{w}/fetch` | `{url, method?, headers?, body?, from?, stream?}`. `from` is the page URL the request is made from; default is the origin of `url` | `{status, headers, body}`; with `stream: true`, newline-delimited `{"chunk": "<text>"}` lines then `{"status": n}`. The service keeps one page per (window, origin) for this, creating it on first use (G14; Instagram's replay) | 404 `no_such_window`; 502 `origin_unreachable` |
| `POST /windows/{w}/pages/{t}/snapshot` | `{format: "png" \| "html" \| "mhtml", full_page?}` | `{data}` — base64 for `png`, text otherwise | 404 `no_such_target`; 409 `format_unsupported` (`mhtml` outside Chromium and Chrome) |

### Controls

| Method and path | Body | Returns | Errors |
|---|---|---|---|
| `POST /windows/{w}/pages/{t}/controls/{name}` | `{args: [], kwargs: {}}` | `{result}` | 404 `no_such_target` / `no_such_control`; 409 `no_controls` (`engine: none`); 500 `control_failed`; 503 `busy` if no worker frees within the request's timeout |

### Sites

| Method and path | Body | Returns | Errors |
|---|---|---|---|
| `POST /sites/{site}/{op}` | the operation's keyword arguments, plus `session_id` (default `"default"`), and for `send`: `stream` (bool), `wait` (bool, default true) | `{result}`; with `stream: true`, newline-delimited JSON: `{"delta": "<text>"}` lines, then one `{"result": {...}}` or `{"error": ...}` line (G7) | 404 `no_such_site` / `no_such_op`; 401 `auth_required` (G9); 503 `busy`; 504 `timeout`, with any text that had rendered in `detail` |
| `GET /sites/{site}/sessions` | — | `{session_id: {url, live, idle_s}}` | 404 `no_such_site` |
| `DELETE /sites/{site}/sessions/{id}` | — | `{forgotten: true}` — drops the map entry and closes the page | 404 `no_such_session` |

Rules:

1. **Control names are an allowlist** built at import: the public `UCBrowser` methods whose first parameter after `self` is `page` (39 today). A leading underscore is never callable. Site operations are likewise the adapter's public methods.
2. **Arguments and results must be JSON.**
3. **A control runs on the page with that target id, whoever holds it.** Leases govern closing, not access.
4. **Pages are created blank unless the caller passes `url`.** A caller with an attachment navigates its own page; that is not the service's business.
5. **`stop` never queues behind a send.** It runs on a control worker against the session's page (G6; row 18).
6. **The contract is versioned by path only when it breaks.** v0 has no prefix.
7. **No route requires another.** Fetch, cookies, snapshot and sessions work for a caller that has never registered a client or attached. Registration exists for leases, and only pages are leased.

Existing routes (`/`, `/availability`, `/probe/{name}`, `/healthz`, `/mcp`) are unchanged.

## 4. `UCBrowser` in service mode

Selected by environment, so consumers change configuration and not code.

| Variable | Meaning |
|---|---|
| `UC_SERVICE_URL` | e.g. `http://127.0.0.1:4010`. Unset: today's in-process behaviour (constraint 4) |
| `UC_WINDOW` | window for a `UCBrowser` that does not ask for native Chrome. Default `default` |
| `UC_NATIVE_WINDOW` | window for `UCBrowser(native_chrome=True)` / `BrowserMode.NATIVE_CDP`. Default `native` |
| `UC_CLIENT_LABEL` | default label for clients this process registers. A caller may also pass `UCBrowser(label=...)` |

| Member | In service mode |
|---|---|
| `start()` | `POST /clients` with `os.getpid()`, the label and the window; `POST /windows/{w}/start`; `chromium.connect_over_cdp(cdp)`. Launches nothing and installs no init script |
| `new_page()` | **new public method, in both modes.** Service mode: `POST …/pages`, then resolve the target id to a local `Page` (E4). In-process: `self._context.new_page()` plus the stealth plugin, which is what consumers do by hand today |
| `_context` | the real `browser.contexts[0]`, unwrapped. A page a consumer makes with `_context.new_page()` works and is stealthed, but is unleased: revocation does not reach it and only the sweep closes it (§5) |
| `_stealth_plugin` | `None`. The window's context already has stealth (row 19) |
| `open(url, …)` | `self.new_page()` → attach the caller's tracer if given → `page.goto` → wait, exactly as now |
| `open_with_intercept(…)` | same page creation; routing and listeners stay local (E5) |
| the 39 page-taking controls | read the page's target id; `POST …/controls/{name}`; return `result` |
| a control on an `engine: none` window | on 409, return the value the method returns today when the engine is absent. Only `detect_all` → `{}` has been read; the other 38 fallbacks are `unverified` and must be tabulated before this row is built |
| `close()` | `DELETE /clients/{id}`; disconnect. Never `context.close()` |
| `client_id`, `label` | new read-only attributes |

**Resolving a target id to a `Page`.** After `POST …/pages`, poll the real context's `pages`; for each page not yet known, open a CDP session, send `Target.getTargetInfo`, detach, and cache `target_id → Page`. Stop when the wanted id appears. *Measured:* 0.16–0.17 s (row 6). Allow 10 s, then raise. The reverse lookup for controls uses the same cache.

**Errors.** A dead service surfaces as connection errors on HTTP and "target closed" errors on pages; revocation (E10) surfaces as "target closed" alone. Wrap the first as `UCServiceGone`. Leave the second as the Playwright error it is: the thread it interrupts was abandoned and should unwind.

## 5. Service internals

**Owner thread, one per window.** Starts `sync_playwright()`, launches the browser, then loops on a job queue. Jobs: create a page, close pages, add cookies, relaunch. Between jobs it wakes every few seconds and makes one cheap Playwright call, which both detects a dead browser and drains queued events (gotcha 8).

**Launching `chromium`.** The arguments `_start_chromium_ext` uses today, plus `--remote-debugging-port=0` and `--remote-debugging-address=127.0.0.1`. Then read `<profile>/DevToolsActivePort`: line 1 is the port, line 2 the browser path; the endpoint is `ws://127.0.0.1:<port><path>`. *Measured (row 1).* Read it with a short retry.

**Launching `chrome`.** The subprocess `_start_native_chrome` starts today, with port `0` in place of the fixed `9222`; the owner then attaches with `connect_over_cdp`. *Measured (row 20).* Before launching, check that the process the service started is the one answering: if the subprocess exits at once, another Chrome holds the profile — fail with `profile_held` (constraint 5; Analysis §5).

**Per-window setup**, on the owner, once, on the context: the API shim init script; `state_file` cookies; `Stealth().apply_stealth_sync(context)` if configured. *Measured (rows 3, 19):* both reach pages created later through any attachment.

**Control workers.** `control_workers` threads per window, each with its own `sync_playwright()` and its own `connect_over_cdp` to the window, each with an unstarted `UCBrowser` as the receiver for control methods (sound because no page-taking method reads `self._context` — Theory, Verification). A control request is queued to the pool; the worker resolves the target id on its own attachment, ensures the engine, runs the method.

**Ensuring the engine.** For `extension`: wait for `window.__UC.ready` as `_wait_ready` does. For `inject`: if `window.__UC` is absent, `page.evaluate(<bundle text>)`, then wait for ready. *Measured (row 22):* ready within 1.5 s; the check itself is one `evaluate`. Navigation discards the injected engine, so the check runs on every control call.

**Sessions.** A `Session` is created on first use of `(site, session_id)`: take a page from the window's owner, start a thread with its own attachment, resolve the page there, navigate to the mapped URL if one exists. All operations for the session run on that thread, in order. A per-site semaphore of `max_concurrent_sends` wraps `send`. Eviction: when `max_sessions` is reached, close the least recently used session that is not mid-operation; if all are busy, answer `busy`. A sweep closes sessions idle past `session_idle_s`. The map file is rewritten on every change.

**Long controls from library clients (E8).** `chat` is in a fixed set of long controls. The control route runs these on a temporary session worker bound to the caller's page instead of on the pool.

**Leases and the reaper.** `clients: {client_id: (pid, start_time, label, window)}`, `leases: {target_id: holder}`. Every 5 s: for each client whose process is gone, or whose start time differs (pid reuse), close its leased pages through the owner and drop it. Drop leases whose page has closed. For each window with zero registered clients, close pages that have no holder, keeping one blank page so the browser stays open.

**Relaunch.** If a window's browser disconnects: mark it not running, fail queued jobs, void its leases and sessions, stop its workers, relaunch on next use.

**Shutdown.** In the app lifespan's exit: stop sessions and workers, close each window on its owner, stop Playwright, join threads.

## 6. Gotchas (each from a probe or from running UC on Linux)

1. **A service-opened page is not on the client's connection yet when the call returns.** Always resolve by target id with a wait. *Measured (row 6).*
2. **`add_init_script` on an attached context affects every client of the window.** The client library must not call it. *Measured (row 8).*
3. **Disconnecting closes nothing.** `browser.close()` on a CDP attachment only detaches. *Measured (row 11).*
4. **Playwright's Chromium started by hand on Ubuntu dies with `No usable sandbox!`** (AppArmor restricts unprivileged user namespaces). Playwright passes `--no-sandbox` itself. Installed Google Chrome starts as a plain subprocess without it. *Measured (launch note, row 20).*
5. **The extension bundle is gitignored.** Check for `extension/dist/uc-extension.js` at service start; without it refuse `extension` and `inject` windows, since both need the file.
6. **A headed browser needs the desktop session.** Under systemd the unit needs `DISPLAY` and `XAUTHORITY`.
7. **UC's own pixi workspace lists `win-64` only** (`pyproject.toml`, `[tool.pixi.workspace]`). The service runs from UC's own checkout and environment (§7), so `linux-64` must be added and `pixi.lock` re-solved before anything else runs on Linux. Whether it solves is `unverified`: every Linux run of UC so far was inside `event_tool`'s environment (Python 3.12, Playwright 1.65.2, playwright-stealth 2.0.3). Building the extension bundle needs `node` and `npm`, which are not on `smallblackbox`'s PATH; `event_tool`'s pixi environment has them.
8. **A launching connection that makes no calls does not process events.** Sync Playwright dispatches incoming messages only inside a call, and the launching connection is told about every page in the window. An owner that slept for days would let them pile up. Reasoned, `unverified`; the periodic call in §5 costs nothing either way.
9. **Each service thread that touches a browser is a Playwright driver process, about 80 MB** (row 24). Three windows, each with an owner and two control workers, plus four Grok sessions, is thirteen drivers: about 1 GB before any browser. Size the pools deliberately.
10. **A second Chrome on a held profile exits immediately and the first one answers.** That is how four days of harvests attached to an abandoned browser (Analysis §5). Hence constraint 5.

## 7. Running it

The service is independent of every consumer: its own checkout of this repo on `main`, its own pixi environment, its own unit. It does not run from a consumer's submodule checkout. A consumer pins UC at a commit of its choosing, and the service must not move when that pin does, nor the pin when the service is deployed. The wire contract (§3) is all the two sides share, so they may run different UC commits and different Playwright versions (`unverified`, Analysis §6 item 6). *(Changed 2026-11-23 on the owner's instruction; the earlier draft ran the service from `event_tool`'s checkout and environment.)*

By hand, from the checkout:

```bash
UC_WINDOWS_FILE=data/windows.json pixi run python -m uc_browser.status_server
```

As a Linux user service (example for `smallblackbox`; the checkout path is a proposal; `not implemented`):

```ini
[Unit]
Description=Universal Controller service: browser windows, CDP, control API (127.0.0.1:4010)
After=graphical-session.target

[Service]
WorkingDirectory=/home/rabbit/universal_controller
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/rabbit/.Xauthority
Environment=UC_WINDOWS_FILE=/home/rabbit/universal_controller/data/windows.json
ExecStart=/home/rabbit/.pixi/bin/pixi run --frozen python -m uc_browser.status_server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Before first start on a host that has run the old arrangement, make sure no browser from it still holds a served profile; the service will refuse with `profile_held` if one does.

## 8. Tests and acceptance

| Level | What | Needs a browser? |
|---|---|---|
| Unit | control and site-operation allowlists; unknown and underscored names refused | no |
| Unit | routing and error codes of §3 against fake windows and sessions | no |
| Unit | reaper: dead pid, reused pid, live client; revocation by label; sweep only with zero clients | no |
| Unit | session store: cap, LRU eviction skips busy sessions, idle sweep, map survives a reload | no |
| Unit | `new_page()`: service mode posts to the pages route and resolves the target id; in-process mode creates on the local context and applies stealth | no |
| Unit | fetch: `from` defaults to the origin of `url`; one page kept per (window, origin); streamed and unstreamed shapes; `origin_unreachable` | no |
| Unit | cookies: add by list; `from_chrome` without `rookiepy` answers 501; read without `domain` answers 400 | no |
| Unit | snapshot: the three formats; `mhtml` on a window that is not Chromium or Chrome answers 409 | no |
| Unit | `UCBrowser` with `UC_SERVICE_URL` unset constructs and behaves as before (constraint 4) | no |
| Live | `scripts/service_probe.py`: rows 1–24 of the Analysis doc against the real service | yes |
| Live | revocation unwinds a stuck client: a client blocked in `page.wait_for_timeout(600000)` gets an error within seconds of `DELETE /clients?label=…` | yes — this is the one lifecycle claim (E10) no probe has exercised |
| Live | fetch, cookies and snapshot against a real window: an in-page request carries a cookie added through the cookies route; an MHTML snapshot is returned. None of these three routes was prototyped | yes |

Baseline before any change: on `main` at `5e3dd0a` the suite is **100 passed, 4 skipped** (run 2026-11-23 on Linux in `event_tool`'s environment).

## 9. Moving consumers

One at a time. Each has an observable check; do not start the next until it passes. Lifecycle ids say which rows of Design §3 the step puts on their pathway.

| Step | Change | Covers | Check |
|---|---|---|---|
| 0 | Stop any browser left over from the old arrangement | — | nothing listens on 9222; no Chromium holds a profile named in the config |
| 1 | Service up with `harvest` only; no consumer changed | — | `GET /windows` lists `harvest` with a `cdp` endpoint; `scripts/service_probe.py` passes |
| 2 | `event_tool`'s MCP service (the process that runs harvests) gets `UC_SERVICE_URL` and `UC_WINDOW=harvest`; it stops opening a `UCBrowser` for a group that holds only feed and API sources | E0–E6, E8, E9, E11, E12, E14 | a harvest logs no `Loading UC extension` line of its own; `Web sources: N messages from 50 sources` stays in the range of the last runs before the change (912–995 over 2026-11-18…23); after it, `GET /windows` shows `harvest` with no leased pages; a harvest restricted to the eight feed sources registers no client at all |
| 3 | `event_tool`'s job runner labels its `UCBrowser`s with the job id and calls `DELETE /clients?label=<job id>` when a job fails, times out or is cancelled | E10 | force a timeout on a test job: its pages are gone from `GET /windows` within the reaper interval |
| 4 | `native` window with `engine: inject`; native sources, Cloudflare retries and native explore sessions use it | E7, E11 | a harvest logs no `Launching native Chrome` line of its own; the four native sources still return messages; nothing listens on 9222 |
| 5 | Sessions and site routes; the grok adapter in-service on `grok`; the litellm provider and the Grok MCP tools call it | G1–G13 | the proxy's one-word test chat returns the word; `grok_list_conversations` through the MCP gateway returns the signed-in account's chats; a `stop` issued during a long reply returns while the send is still waiting |
| 6 | `event_tool`'s export script calls `POST /windows/grok/fetch` instead of opening a browser; its and the proxy's separate Grok profiles are removed | G14 | the export runs while the proxy answers a chat, and the script's only import from `uc_browser` is `ServiceClient` |
| 7 | Cookie import; `event_tool`'s `web login` opens a tab in `harvest` instead of launching Chrome; `event_tool` drops its direct `playwright-stealth` dependency and its one direct `playwright` import | E13, G15 | `grep -rn 'from playwright' src` in `event_tool` prints nothing |
| 8 | Health probes and the generic client become sessions; provider pinned executors removed | G10 | `grep -rn 'UCBrowser(' uc_browser` shows only the window manager and the client library |

Rollback at any step is unsetting `UC_SERVICE_URL` for that consumer (constraint 4).

## Verification

```bash
ls uc_browser/service 2>/dev/null || echo "not built"
```

`not built` is the expected output on 2026-11-23. Once the package exists, replace this block with: start the service, run `python scripts/service_probe.py`, and compare its rows with `docs/08 - Analysis/00 - Service Prototype Measurements.md` §3. A row that differs means either the build or this spec is wrong; decide which before moving a consumer.
