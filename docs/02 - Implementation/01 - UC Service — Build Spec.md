# UC Service — Build Spec

**Status**: CURRENT for tasks 0–6 and the independent UC/Grok user units. Grok profile ownership, NSFW chat/list/read and restart lifecycle are live-verified; both the proxy and event_tool's Grok MCP gateway use the service. Generic/health migration and non-Grok consumer moves remain pending.
**Date**: 2026-11-24
**Applies to**: new package `uc_browser/service/`; changes to `uc_browser/browser.py`, `uc_browser/status_server.py`, `uc_browser/sites/`, `uc_browser/llm_providers/uc.py`, `uc_browser/mcp/grok_tools.py`, `pyproject.toml`; consumer changes in `event_tool`
**Depends on**: `docs/01 - Design/02 - UC as a Local Service.md` (what and why; lifecycle ids E1–E14 and G1–G15 are its §3), `docs/08 - Analysis/00 - Service Prototype Measurements.md` ("row N" is its §3), `docs/02 - Implementation/00 - Setup and Operations.md` (install, extension build, status server)

The design doc decides the shape. This doc is what to type: files, config, the wire contract, how the client library behaves, how to run it, and the order to move consumers.

## Hard constraints (non-negotiable)

1. A Playwright object is touched only from the thread whose Playwright instance produced it. Never from an HTTP handler.
2. The service binds `127.0.0.1`. So does every browser's debugging port.
3. In service mode the client library never closes a context, adds a context-level init script or launches a browser. An in-process launch refuses a profile already held by a live browser.
4. Routing is explicit. `UCBrowser(service=url, window=name)` selects a service window; the caller names both and neither side infers. `service` omitted or `None` means in-process, regardless of `UC_SERVICE_URL`, `UC_WINDOW`, `UC_NATIVE_WINDOW` or `UC_CLIENT_LABEL`, which no longer exist as configuration. Intentional safety exception: native launch uses a dynamically selected explicit loopback port and refuses held profiles instead of attaching to fixed port 9222.
5. The service never attaches to a browser it did not launch.

## 1. Layout

| File | New? | Holds |
|---|---|---|
| `uc_browser/service/__init__.py` | new | package marker, wire-contract version |
| `uc_browser/service/windows.py` | new | `WindowSpec`, `Window` (owner thread, control-worker pool, engine placement), `WindowManager` (config, launch, relaunch, list) |
| `uc_browser/service/workers.py`, `profiles.py` | new | bounded thread-affine runtimes, target enumeration, profile guards and native process ownership |
| `uc_browser/service/operations.py`, `errors.py` | new | fetch/cookies/snapshot jobs and browser-independent wire errors |
| `uc_browser/service/targets.py` | new | client-side CDP target cache and delayed-page resolution |
| `uc_browser/service/leases.py` | new | clients, leases, reaper, revocation by label, sweep |
| `uc_browser/service/sessions.py`, `site_api.py` | new (task 6) | bounded session workers, atomic persisted URL map, account send admission, independent stop, site router |
| `uc_browser/service/api.py` | new | FastAPI router for window/client operations; turns worker futures into responses and streams |
| `uc_browser/service/client.py` | new | stdlib-only `ServiceClient`; importing it does not load Playwright, including through package exports. Window and site HTTP helpers |
| `uc_browser/browser.py` | change | explicit `UCBrowser` service mode (§4); in-process launch paths refuse a held profile |
| `uc_browser/sites/grok_service.py`, `grok.py`, `grok_fast.py` | change (task 6) | service-owned borrowed-page Grok adapter with configured project boundary; standalone local API remains explicit |
| `uc_browser/llm_providers/uc.py` | change (task 6) | Grok uses `ServiceClient` and the service map; generic routes retain their own executor pending task 8 |
| `uc_browser/mcp/grok_tools.py` | change (task 6) | HTTP-only Grok tools sharing the service map with the provider |
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
    "grok":    {"browser": "chromium", "engine": "extension", "profile": "data/.grok/.uc_chromium_profile"},
    "native":  {"browser": "chrome",   "engine": "inject",    "profile": "data/.uc_chrome_profile"}
  },
  "sites": {
    "grok": {"window": "grok", "folder": "NSFW", "folder_url": "https://grok.com/project/<project-id>", "max_sessions": 4, "max_concurrent_sends": 1, "session_idle_s": 1800}
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
| `control_workers` | int, default 2 | maximum workers in each of the separate short and long lanes; workers start lazily |
| `queue_timeout_s` | positive finite number, default 30 | worker admission deadline in seconds for each lane; exposed in window discovery so clients can include it in finite read budgets |
| `lazy` | bool, default `true` | launch on first use rather than at service start |

**Sites**

| Field | Default | Meaning |
|---|---|---|
| `window` | — | which window the site's sessions live in |
| `max_sessions` | 4 | live sessions (each is a page, a thread and a Playwright driver) |
| `max_concurrent_sends` | 1 | G5: the account's limit, not a technical one |
| `session_idle_s` | 1800 | an idle session older than this is closed; its map entry stays |
| `folder`, `folder_url` | unset | optional Grok project restriction; the deployed Grok adapter requires the existing `NSFW` project's name and canonical URL. Verify identity and membership rather than silently using the account home |

The example is not permission to open arbitrary existing profiles. Tasks 0–5 used fresh profiles. For the authorized Grok migration the service config lives at `~/.config/universal-controller/windows.json` and keeps the existing signed-in profile at `/home/rabbit/event_tool/data/.grok/.uc_chromium_profile`. Stop its old owner before launch. Do not supply a stale `state_file` over the current account cookies. Native Chrome and port 9222 are not part of this migration.

Only `grok` is currently a supported configured site. Persistent named-session URLs and last-use times live in `UC_SESSIONS_FILE` (default `data/sessions.json`); the deployed unit selects `~/.local/state/universal-controller/sessions.json`. No messages or cookies are stored in this map.

## 3. Wire contract (v0)

HTTP on the status server's port (`UC_STATUS_PORT`, default 4010). JSON in, JSON out. Errors are `{"error": "<code>", "detail": "<text>"}` with the status shown.

### Windows, clients, pages

| Method and path | Body | Returns | Errors |
|---|---|---|---|
| `GET /windows` | — | `{name: {browser, engine, running, offers, cdp, queue_timeout_s, clients: n, pages: [{target_id, url, holder, age_s}]}}`. `offers` lists `attach`, `control`, `fetch`, `cookies`, `snapshot`, `mhtml` as supported. `cdp` is the browser websocket endpoint or `null`. `holder` is a client id, `session:<site>/<id>` for site pages, or `null` | — |
| `POST /windows/{w}/start` | — | the window's entry, launched if it was not | 404 `no_such_window`; 409 `profile_held` (a browser the service did not launch holds the profile); 500 `launch_failed` |
| `POST /clients` | `{pid, label, window}` | `{client_id}` | 400 `bad_pid`; 404 `no_such_window` |
| `DELETE /clients/{id}` | — | `{closed: n}` — closes every page leased to the client (E9) | 404 `no_such_client` |
| `DELETE /clients?label=<label>` | — | `{clients: n, closed: n}` — revocation (E10). Zero matches is not an error | — |
| `POST /windows/{w}/pages` | `{client_id, url?, wait_ms?}` | `{target_id}` — a page leased to the client; blank unless `url` is given, in which case the service navigates it. `url` exists for callers that hold no browser library | 404 `no_such_window` / `no_such_client`; 502 `navigation_failed` |
| `DELETE /windows/{w}/pages/{t}` | — | `{closed: true}` | 404 `no_such_target`; 500 `close_failed` if the browser explicitly refuses target closure |
| `POST /windows/{w}/cookies` | `{cookies: [...]}` in Playwright format, or `{from_chrome: ["grok.com", ...]}` | `{added: n}` (E13, G15) | 404 `no_such_window`; 501 `chrome_cookies_unavailable` if `rookiepy` is not installed |
| `GET /windows/{w}/cookies?domain=<d>` | — | `{cookies: [...]}` for that domain, for a caller that will make plain HTTP requests with the window's session | 404 `no_such_window`; 400 `domain_required` |
| `POST /windows/{w}/fetch` | `{url, method?, headers?, body?, from?, stream?}`. Method defaults to GET. One retained page per (window, origin of `from` or `url`); first navigation uses `from`, else the origin root, and waits for `domcontentloaded`. Later same-origin `from` values become Fetch `referrer`, without navigation. Requests always use `credentials: 'include'`, subject to browser cookie/CORS/referrer policy | `{status, headers, body}`; with `stream: true`, newline-delimited `{"chunk": "<text>"}` lines then `{"status": n}`. Buffered and streamed requests share the retained page's exclusive lock; a changed `from` cannot navigate away from an in-flight request | 404 `no_such_window`; 502 `origin_unreachable` |
| `POST /windows/{w}/pages/{t}/snapshot` | `{format: "png" \| "html" \| "mhtml", full_page?}` | `{data}` — base64 for `png`, text otherwise | 404 `no_such_target`; 409 `format_unsupported` (`mhtml` outside Chromium and Chrome) |

### Controls

| Method and path | Body | Returns | Errors |
|---|---|---|---|
| `POST /windows/{w}/pages/{t}/controls/{name}` | `{args: [], kwargs: {}}` | `{result}` | 404 `no_such_target` / `no_such_control`; 409 `no_controls` (`engine: none`); 500 `control_failed`; 503 `busy` if no worker frees within the window's `queue_timeout_s` |

### Sites

| Method and path | Body | Returns | Errors |
|---|---|---|---|
| `POST /sites/{site}/{op}` | operation keyword arguments plus `session_id` (default `"default"`), optional `ephemeral` (default false), `stream` (bool) and `wait` (queue admission, default true) | `{result}`; with `stream: true`, newline-delimited JSON: `{"delta": "<text>"}` then one `{"result": {...}}` or error line (G7) | 404 `no_such_site` / `no_such_op`; 401 `auth_required` (G9); `folder_mismatch` / `page_deviation` for failed project/page expectations; 503 `busy`; 504 `timeout` |
| `GET /sites/{site}/sessions` | — | `{session_id: {url, live, idle_s, ephemeral?}}`; only live temporary sessions carry `ephemeral: true` | 404 `no_such_site` |
| `DELETE /sites/{site}/sessions/{id}` | — | `{forgotten: true}` — drops the map entry and closes the page | 404 `no_such_session` |

Rules:

1. **Control names are an allowlist** built at import: the public `UCBrowser` methods whose first parameter after `self` is `page` (39 today). A leading underscore is never callable. Site operations are likewise the adapter's public methods.
2. **Arguments and results must be JSON.** `scan_action(action=callable)` and `scroll_feed(on_item=callable)` are local client orchestration; nested controls still dispatch remotely. HTTP rejects non-null callable parameters with 400 `bad_arguments`. All 39 names remain allowlisted, but only 38 have a JSON-callable signature.
3. **A control runs on the page with that target id, whoever holds it.** Leases govern closing, not access.
4. **Pages are created blank unless the caller passes `url`.** A caller with an attachment navigates its own page; that is not the service's business.
5. **`stop` never queues behind a send.** It runs on a control worker against the session's page (G6; row 18).
6. **The contract is versioned by path only when it breaks.** v0 has no prefix.
7. **No browser attachment is required.** Fetch, cookies and snapshot **of an existing target** need no client registration. Creating a leased page requires `client_id`; registration is plain HTTP with the caller's pid/label/window. Site operations create service-held session pages without client registration or attachment.
8. Worker admission uses the window's `queue_timeout_s` (default 30 s); a full/expired queue returns 503 `busy`. This is separate from operation duration. Browser shutdown fails queued work with 503 `window_stopped`. Bad request schemas/signatures return 400 `bad_arguments`.

Existing routes (`/`, `/availability`, `/probe/{name}`, `/healthz`, `/mcp`) are unchanged.

**Fetch identity and redirects.** Cookie inclusion is explicit, but does not bypass SameSite, CORS or the page's referrer policy. Before Fetch, the service rejects an explicit `from` whose origin differs from the loaded document (for example after a cross-origin redirect), surfacing `origin_unreachable`. Chromium otherwise accepts that input and can substitute its own referrer; the guard prevents silent origin changes. A closed/restarted fetch page is recreated; a page moved off its landed origin is re-established under the same exclusive lock.

**Client read budgets.** `ServiceClient.control_timeout` and `control(..., timeout=...)` are optional **whole-operation** budgets in seconds. For finite budgets, the helper discovers the current `queue_timeout_s` and uses `queue + operation budget + 5 s` as its HTTP read timeout. Default `None` stays unbounded, including `UCBrowser` calls: a page evaluate or repeated locator actions has no universal wall-clock bound. In particular, `chat(timeout_s)` bounds only response polling, not setup or the entire control. A read timeout does not cancel server work; do not retry a send blindly.

**Site lifetime and expectations.** `wait` governs admission, not whether the adapter waits for a reply (`wait_for_response`). Named sessions retain their conversation URL after idle eviction and service restart. `ephemeral: true` marks a temporary session whose URL is never persisted and is discarded on eviction; it is not inferred from a naming convention. A waited-for stateless consumer explicitly forgets its session afterward. A blind send leaves its page alive until idle eviction instead of assuming generation survives tab closure. Reusing an id with conflicting persistent/ephemeral lifetime is an error.

The Grok adapter owns the expectation: the configured project and an enabled composer for a send. Missing expected state is a deviation; login affordances can diagnose `auth_required`, otherwise `page_deviation`. No generic engine login-wall predicate or phrase list decides this. Project-restricted reads/mutations reject conversations outside that project, and cleanup never ranges across the account.

The live Grok project renders conversation cards without anchors. Membership comes from authenticated `GET /rest/app-chat/conversations?pageSize=60&workspaceId=…`, following `nextPageToken` with `pageToken`. Every row must carry the configured `workspaceId`; malformed pages, repeated tokens and foreign rows fail rather than returning a partial or account-wide list. `GET /rest/workspaces/{id}` verifies both id and configured name before driving the page. Send continuations, reads, regeneration and destructive operations refresh membership; listing and cleanup never enumerate the account. New chats were observed at `/project/{id}?chat={conversation_id}`. A canonical `/c/` result is accepted only after backend membership verification, without navigating away from a potentially still-generating blind send.

Additional lifecycle errors: 409 `session_conflict` / `session_closed` / `cancelled`; 503 `session_lost` / `session_unavailable`; 500 `site_failed` / `session_store_failed`. A map-write failure can follow a completed operation and must not cause a retry. A closing worker retains its capacity slot until it really exits. Disconnect cancels queued work, never an operation already inside the adapter; stream cleanup also runs when disconnect precedes the first body yield.

**Thin Grok consumers.** The provider takes `service_url` (or an injected `ServiceClient`); deployed `uc_handler` obtains that explicit address from consumer configuration `UC_GROK_SERVICE_URL`. MCP registration takes `service_url` or the same consumer setting. Neither falls back to a local Grok browser. The proxy and MCP tools share named sessions through the service, not through a process singleton. Generic models remain separate in-process adapters until task 8.

The local generic health store cannot gate the service's Grok identity; the Grok adapter diagnoses that window directly. Production `grok-proxy.config.yaml` sets router `num_retries: 0` and `max_fallbacks: 0`: a timeout may follow an accepted prompt, so the proxy must not automatically replay it. Callers must likewise treat uncertain delivery as uncertain, not as permission to resend.

## 4. `UCBrowser` in service mode

Selected by constructor arguments; no environment variable participates. This is a breaking migration (branch `feat/local-service`).

`UCBrowser(service=url, window=name, label=None, timeout_ms=30000)`:

| Argument | Rule |
|---|---|
| `service` | Service address, e.g. `http://127.0.0.1:4010`. Omitted or `None`: local launching (constraint 4), unless `cdp_endpoint` explicitly selects external attachment (§4.1); never environment-selected |
| `window` | Window name. Required and non-empty when `service` is supplied; supplying `window` without `service` raises `ValueError` |
| `label` | Optional client label; never derived from the environment |
| `timeout_ms` | Allowed in all paths; also bounds direct CDP connection establishment |
| `mode` (non-`None`), `headless`, `use_extension`, `native_chrome` (non-`None`, including `False`), explicitly passed `channel` or `stealth` (even at their default values) | Mixed with `service`: `ValueError` at construction. `channel` and `stealth` use sentinels so local mode keeps its old defaults and explicit local values |

`start()` first does `GET /windows` and checks that the named window exists and offers attach. A missing window or a window without attach raises `ValueError` before registration, service start or Playwright. Only then the sequence below runs. A CDP endpoint missing after start is unexpected: `RuntimeError`, with cleanup.

`ServiceClient(url)` requires an explicit address: a missing argument is `TypeError`, an empty or invalid URL `ValueError`. There is no environment fallback.

| Member | In service mode |
|---|---|
| `start()` | `GET /windows` validation, then `POST /clients` with `os.getpid()`, the label and the window; `POST /windows/{w}/start`; `chromium.connect_over_cdp(cdp)`. Launches nothing and installs no init script |
| `new_page()` | **new public method, in both modes.** Service mode: `POST …/pages`, then resolve the target id to a local `Page` (E4). In-process: `self._context.new_page()` plus the stealth plugin, which is what consumers do by hand today |
| `_context` | the real `browser.contexts[0]`, unwrapped. A page a consumer makes with `_context.new_page()` works and is stealthed, but is unleased: revocation does not reach it and only the sweep closes it (§5) |
| `_stealth_plugin` | `None`. The window's context already has stealth (row 19) |
| `open(url, …)` | `self.new_page()` → attach the caller's tracer if given → `page.goto` → wait. If any step fails before returning the page, close that page best-effort and re-raise the original exception, without releasing the client or closing its context |
| `open_with_intercept(…)` | same page creation and failure cleanup; routing and listeners stay local (E5). Both opening methods retain their public signatures; cleanup also applies in-process |
| the 39 page-taking controls | read the page's target id; `POST …/controls/{name}`; return `result`, except callable orchestration described in §3 |
| a control on an `engine: none` window | `UCBrowser` reads `offers` from the start response and runs the original body locally when `control` is absent, without a refusal round trip. Unexpected 409 `no_controls` still triggers and remembers the same fallback. Nested controls stay local under a scoped recursion guard. `ServiceClient` instead raises `ControlsUnavailable`. All 39 original outcomes were measured: `docs/08 - Analysis/01 - Engine-Absent Control Behavior.md` |
| `close()` | `DELETE /clients/{id}`; disconnect. Never `context.close()` |
| `client_id`, `label` | new read-only attributes |

**Resolving a target id to a `Page`.** After `POST …/pages`, poll the real context's `pages`; for each page not yet known, open a CDP session, send `Target.getTargetInfo`, detach, and cache `target_id → Page`. Stop when the wanted id appears. *Measured:* 0.16–0.17 s (row 6). Allow 10 s, then raise. The reverse lookup for controls uses the same cache.

Pump Playwright events **before** snapshotting `context.pages`; reading that property alone can omit a newly created target. The independent probe measured 20 ms propagation and matched owner/client/Chrome target sets after this correction. This remains necessary for Page resolution and page-taking operations; CDP lease enumeration/closure does not depend on the Page cache.

**Context-independence audit.** All 39 original page-taking control bodies were inspected for `self._context`: zero accesses. Retain the Theory doc's grep audit when adding controls; local fallback and the unstarted worker receiver depend on this. Behavioral regressions check real caller-page text and raw Playwright errors rather than pinning source strings.

**Errors.** A dead service surfaces as connection errors on HTTP and "target closed" errors on pages; revocation (E10) surfaces as "target closed" alone. Wrap the first as `UCServiceGone`. Leave the second as the Playwright error it is: the thread it interrupts was abandoned and should unwind.

### 4.1 Explicit attachment outside the service

`UCBrowser(cdp_endpoint=url, label=None, timeout_ms=30000)` borrows the first context at the named HTTP or browser-WebSocket CDP endpoint. Empty/non-string endpoints, `service`, `window` and any explicit in-process launch arguments are rejected at construction. There is no endpoint discovery, environment selection, profile access or launch fallback. This restores intentional attachment to an operator-cleared browser without restoring the old implicit fixed-port behavior.

No stealth, cookies, API shim, engine or context init scripts are installed. Page controls retain their local implementations. The client tracks only pages created by its public page-opening methods and their popups; failed navigation closes its new page. Teardown closes tracked pages best-effort, then stops its driver to disconnect. Neither the borrowed context nor browser is closed; pre-existing and independently opened tabs remain. Repeated close is safe; repeated start while attached is an error. A connection failure is `RuntimeError` naming the endpoint.

This path has no service registration, lease, sweep or revocation. Its label is caller metadata only. The service still refuses profiles it does not own; no service window brokering an external browser is implemented here. Verification: `tests/test_cdp_attachment.py` exercises real disposable Chromium, unrelated-tab survival, popups, navigation failure, interception, local text control and reattachment. Operator-cleared native-site access remains unverified until the consumer's separate live check.

## 5. Service internals

**Owner thread, one per window.** Starts `sync_playwright()`, launches the browser, then loops on a job queue. Jobs: create a page, close pages, add cookies, relaunch. Between jobs it wakes every few seconds and makes one cheap Playwright call, which both detects a dead browser and drains queued events (gotcha 8).

**Launching `chromium`.** The arguments `_start_chromium_ext` uses today, plus `--remote-debugging-port=0` and `--remote-debugging-address=127.0.0.1`. Then read `<profile>/DevToolsActivePort`: line 1 is the port, line 2 the browser path; the endpoint is `ws://127.0.0.1:<port><path>`. *Measured (row 1).* Read it with a short retry.

**Launching `chrome`.** Use the shared `profiles.launch_native` subprocess path for both the service and in-process native mode. Select an available nonzero loopback port and pass it explicitly; port `0` makes installed Chrome report `navigator.webdriver=true` (PBX 210-1001, Chrome 157). Explicit-port Chrome does not write `DevToolsActivePort`: discover its browser websocket URL through `/json/version`, with positive proof that the endpoint belongs to this launch before attaching. A live child plus any answering URL is insufficient: another process can win the port-allocation/bind race. Keep the profile launch lock, prelaunch held-profile check and failure cleanup. A foreign listener must fail, never become the service's browser. Playwright `chromium` keeps its existing port-0 launch and stealth setup.

The native proof is the exact random browser path announced by the launched child's own stderr (`DevTools listening on ws://127.0.0.1:<port>/devtools/browser/<id>`), matched by `/json/version`, with the child alive before and after discovery. Linux additionally checks socket ownership. A bounded-memory reader drains stderr for the browser's lifetime so logging cannot fill its pipe. A foreign address/path or bind failure returns `launch_failed` and stops this launch; there is no automatic port retry. This proof does not depend on `/proc`, but **Windows runtime remains unverified**.

**Per-window setup**, on the owner, once, on the context: the API shim init script; `state_file` cookies; `Stealth().apply_stealth_sync(context)` if configured. *Measured (rows 3, 19):* both reach pages created later through any attachment.

**Control workers.** Each window has independent bounded short and long lanes, each capped by `control_workers`; every worker owns its Playwright/CDP attachment and unstarted local `UCBrowser` receiver. Jobs use the configured `queue_timeout_s` as an admission deadline, not an operation limit. Internal callers may explicitly pass `queue_timeout=None` to disable it. Firefox/WebKit have no CDP and execute jobs on their owner, without lane deadlines; long work there delays other operations. Those binaries were not installed or live-tested in this build.

**Ensuring the engine.** For `extension`: wait for `window.__UC.ready` as `_wait_ready` does. For `inject`: if `window.__UC` is absent, `page.evaluate(<bundle text>)`, then wait for ready. *Measured (row 22):* ready within 1.5 s; the check itself is one `evaluate`. Navigation discards the injected engine, so the check runs on every control call.

Blank/non-http extension pages are not injected or waited on by the preparation step: the control's existing absent-engine behavior runs once. Injection errors return `control_failed`, not `no_controls`.

**Sessions.** One thread-affine attachment and leased page per active `(site, session_id)`, with serialized operations, a bounded live-session cap, idle/LRU eviction that skips busy sessions, and a shared account send cap (default one). Named conversation mappings survive page eviction and service restart; ephemeral mappings do not. `stop` targets the active page through an independent control worker, not the send queue. Reconnection never automatically resends an uncertain prompt.

**Long operations (E8).** `chat`, `scroll_feed` and fetch use the separate long lane, so short controls are not queued behind their waits. Site sends instead use their dedicated bounded session workers.

**Leases and the reaper.** `clients: {client_id: (pid, start_time, label, window)}`, `leases: {(window, target_id): holder}`. Every 5 s, reap exited/reused pids and closed targets. On CDP windows, the owner enumerates page targets using browser-level `Target.getTargets` and closes them with `Target.closeTarget`, regardless of whether its Playwright context knows the Page. Non-page targets are excluded. Firefox/WebKit retain owner-thread Page enumeration/closure. The reaper uses fresh owner inventory, never `Window.info()`'s cached timeout/error fallback; failed enumeration preserves leases for a later pass. Cleanup jobs use `launch=False` so they cannot relaunch a stopped window. With zero registered clients, close unheld raw pages, preserving one blank page and service-owned fetch pages. Fetch pages display `holder: null` but are exempt from the raw-page sweep. Creation and lease assignment are atomic against revocation.

**Relaunch.** If a window's browser disconnects: mark it not running, fail queued jobs, void its leases and sessions, stop its workers, relaunch on next use.

**Shutdown.** Stop the reaper, close browser contexts on their owner threads to interrupt worker waits, stop thread-owned Playwright instances, then join the bounded lanes. Startup rejects missing engine bundles and eager launch failures and cleans up previously started windows. Graceful HTTP service shutdown was verified with no profile processes left; systemd restart remains task 7.

## 6. Gotchas (each from a probe or from running UC on Linux)

1. **A service-opened page is not on the client's connection yet when the call returns.** Always resolve by target id with a wait. *Measured (row 6).*
2. **`add_init_script` on an attached context affects every client of the window.** The client library must not call it. *Measured (row 8).*
3. **Disconnecting closes nothing.** `browser.close()` on a CDP attachment only detaches. *Measured (row 11).*
4. **Playwright's Chromium started by hand on Ubuntu dies with `No usable sandbox!`** (AppArmor restricts unprivileged user namespaces). Playwright passes `--no-sandbox` itself. Installed Google Chrome starts as a plain subprocess without it. *Measured (launch note, row 20).*
5. **The extension bundle is gitignored.** Check for `extension/dist/uc-extension.js` at service start; without it refuse `extension` and `inject` windows, since both need the file.
6. **A headed browser needs the desktop session.** Under systemd the unit needs `DISPLAY` and `XAUTHORITY`.
7. **Independent Linux solve is verified.** `linux-64` and Node.js 22 are in the UC pixi workspace. The adapter's PyPI stealth extra replaces redundant conda stealth; the initial UC solve selected an incompatible Playwright 1.60 combination. This is not a universal conda-stealth pin: `event_tool` reportedly solves conda Playwright 1.65.2 with stealth 2.0.3; the exact constraint interaction was not investigated. The project requires Playwright >=1.65.2. Shared dependency changes also re-solved **win-64, but Windows was not run**; carry this caveat into PR review. Build with `pixi run npm --prefix extension ci` and `pixi run npm --prefix extension run build`.
8. **A launching connection that makes no calls does not process events.** This produced the apparent missing page in row 11: a pump before enumeration fixed the exact target sets. Page-taking APIs still need pumping. Lease maintenance now uses browser-authoritative CDP target enumeration/closure, not the owner's Page list or cached status. Multi-day idle behavior remains unverified.
9. **Each browser-touching thread costs a Playwright driver.** Measured PSS: 117 MB in the independent baseline and 77 MB in the live-service run. At default caps, up to five drivers per active window (owner + two short + two long), before future site sessions. Size pools deliberately.
10. **A second Chrome on a held profile can open a tab in the first and exit.** Reject the live profile lock before launch. Installed Chrome rewrites `/proc/<pid>/cmdline` into one process-title string here; missing NUL-separated arguments must not be treated as pid reuse. A narrow live check verified refusal without changing the first window's endpoint file or page count.
11. **Native port 0 changes browser-visible behavior.** Earlier native probes verified CDP and engine injection, not `navigator.webdriver`. Native acceptance now requires `False` before injection on a headed fresh-profile page. That value alone is not proof of passing a real site's bot checks.

## 7. Running it

The service is independent of every consumer: its own checkout of this repo on `main`, its own pixi environment, its own unit. It does not run from a consumer's submodule checkout. A consumer pins UC at a commit of its choosing, and the service must not move when that pin does, nor the pin when the service is deployed. The wire contract (§3) is all the two sides share, so they may run different UC commits and different Playwright versions (`unverified`, Analysis §6 item 6). *(Changed 2026-11-23 on the owner's instruction; the earlier draft ran the service from `event_tool`'s checkout and environment.)*

By hand, from the checkout:

```bash
UC_WINDOWS_FILE=data/windows.json pixi run python -m uc_browser.status_server
```

Health scheduling is **off by default**, both in the module entrypoint and `create_app()`. The command above and the unit below do not automatically open health-probe browsers. To opt in explicitly, append `--run-scheduler` (library equivalent: `create_app(run_scheduler=True)`). It may open registered chat sites in headed windows; manual `POST /probe/{name}` and MCP probes remain available without it. `UC_AUTO_BOOTSTRAP=0` and an empty sites file do not remove the built-in registry seeds and are not scheduler controls.

Linux user-unit templates are versioned in `systemd/uc-service.service` and `systemd/uc-grok-proxy.service`. Install them in `~/.config/systemd/user/` after stopping the previous profile owner; run `systemctl --user daemon-reload`, then enable/start them. Setup §6 records the profile handoff and private config/state paths.

Both run from the independent checkout and pixi environment. The UC unit owns the headed browser and binds 4010; the Grok-only proxy binds 4000 and uses `grok-proxy.config.yaml`, with `UC_GROK_SERVICE_URL=http://127.0.0.1:4010`. Proxy restart must not close the service browser. UC restart closes its process group; the profile retains sign-in and the URL map reconstructs named sessions lazily. The proxy has a `Wants`/ordering dependency, not a stop-propagating dependency on UC.

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
| Unit | `UCBrowser` with `service` omitted behaves as before whatever the environment holds; mixed-mode and window-without-service arguments raise `ValueError`; `start` validates `GET /windows` before registering; `ServiceClient` rejects a missing or invalid address (constraint 4) | no |
| Unit | default app/CLI startup schedules no health probes; explicit opt-in does; manual probing works with scheduling off | no |
| Live | `scripts/service_probe.py`: rows 1–24 of the Analysis doc against the real service | yes |
| Live | revocation unwinds `page.wait_for_timeout(600000)` after `DELETE /clients?label=…` | verified: `TargetClosedError` in 0.04 s |
| Live | no-registration cookie-bearing fetch, retained origin page, incremental UTF-8 stream, HTML/PNG/MHTML snapshots | verified against real Chromium; native first-control injection and second-launch `profile_held` also verified |
| Live | native service row 20 requires `navigator.webdriver is False` before engine injection; measure the unchanged Playwright Chromium value separately; explicit native port discovery succeeds without `DevToolsActivePort` and a held profile is refused | yes, headed disposable profiles only |

Historical baseline reported by PBX 210-1001: on `main` at `5e3dd0a`, **100 passed, 4 skipped**, run 2026-11-23 on Linux in `event_tool`'s environment **without a built extension bundle**. Three skips were bundle-dependent checks in `test_bench_runner.py`, `test_reverse_loop.py` and `test_vendor_signatures.py`; the fourth was Grok sign-in. The synthetic fixture test did not run in that baseline, so its later failure is not a regression against a passing Linux result.

Independent baseline **with the bundle built**: **102 passed, 1 skipped, 1 failed**, specifically `tests/test_bench_runner.py::test_synthetic_fixtures_score_correctly`. Chromium loaded the synthetic `about:blank` MHTML as an empty document; capturing a routed real URL and regenerating the fixtures fixed the targeted test (**1 passed**; Setup §3). The full baseline suite was not repeated afterward. Integrated service/client/window/health tests: **115 passed**. Startup-failure and flattened-Chrome-title regressions: **3 passed** separately. Real native engine injection and the service probe passed: this was a fixture-capture problem, not an unresolved injected-engine failure. Probe rows 13 (shutdown) and 19 (before/after stealth) remain baseline-only; graceful shutdown was exercised separately, and the live client saw `navigator.webdriver=False`.

## 9. Moving consumers

One at a time. Each has an observable check; do not start the next until it passes. Lifecycle ids say which rows of Design §3 the step puts on their pathway.

| Step | Change | Covers | Check |
|---|---|---|---|
| 0 | Stop the previous owner of each profile actually being migrated | — | no foreign process holds that profile; unrelated native Chrome and port 9222 remain untouched |
| 1 | Service up with `harvest` only; no consumer changed | — | `GET /windows` lists `harvest` with a `cdp` endpoint; `scripts/service_probe.py` passes |
| 2 | `event_tool`'s MCP service passes `service=<url>, window="harvest"` explicitly for browser sources; feed/API-only groups open no browser | E0–E6, E8, E9, E11, E12, E14 | compare per-source success/deviation/failure states with the pre-change run, not aggregate message totals; no consumer-owned extension launch; no leased pages remain afterward; feed-only runs register no client |
| 3 | `event_tool`'s job runner labels its `UCBrowser`s with the job id and calls `DELETE /clients?label=<job id>` when a job fails, times out or is cancelled | E10 | force a timeout on a test job: its pages are gone from `GET /windows` within the reaper interval |
| 4 | `native` window with `engine: inject`; `event_tool` routes standard sources to `harvest` and native or retry work to `native`; plain HTTP never opens a browser; Grok future site routes are unchanged. Native sources, Cloudflare retries and native explore sessions use it | E7, E11 | a harvest logs no `Launching native Chrome` line of its own; the four native sources still return messages; nothing listens on 9222 |
| 5 | Sessions and site routes; Grok adapter in-service on `grok`; provider and Grok MCP tools call it | G1–G13 | one-word proxy test reaches the verified NSFW project; both consumers share the service session map; project-scoped listing never returns unrelated account chats; independent stop remains callable during a send; proxy restart preserves the window and UC restart cleans up its old browser |
| 6 | `event_tool`'s export script calls `POST /windows/grok/fetch` instead of opening a browser; its and the proxy's separate Grok profiles are removed | G14 | the export runs while the proxy answers a chat, and the script's only import from `uc_browser` is `ServiceClient` |
| 7 | Cookie import; `event_tool`'s `web login` opens a tab in `harvest` instead of launching Chrome; `event_tool` drops its direct `playwright-stealth` dependency and its one direct `playwright` import | E13, G15 | `grep -rn 'from playwright' src` in `event_tool` prints nothing |
| 8 | Health probes and the generic client become sessions; provider pinned executors removed | G10 | `grep -rn 'UCBrowser(' uc_browser` shows only the window manager and the client library |

Browser-consumer rollback omits `service` (constraint 4). Grok thin consumers have no in-process fallback: rollback requires stopping the new owner, restoring the previous proxy unit/client revision, and letting only that owner open the preserved profile.

## Verification

```bash
pixi run -e dev pytest -q tests/test_service_api.py tests/test_service_client.py tests/test_service_windows_core.py tests/test_service_fetch.py tests/test_health.py
DISPLAY=:0 XAUTHORITY=/home/rabbit/.Xauthority pixi run python scripts/service_probe.py
# Against an already-running service configured with DISPOSABLE a/b/native profiles:
pixi run python scripts/service_probe.py --service-url http://127.0.0.1:4010 \
  --window-a a --window-b b --native-window native --destructive --settle-s 8
```

The destructive probe adds context scripts/cookies and closes its disposable targets; never aim it at signed-in production windows. A model assertion failure stops migration. Before task 6, coordinate live Grok access and profile ownership with the owner and PBX 210-1001.
