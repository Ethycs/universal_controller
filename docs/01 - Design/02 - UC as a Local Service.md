# UC as a Local Service — one owner for every browser identity; every access technique offered on it, none required

**Status**: CURRENT — Grok/proxy/MCP migration is deployed. Native HTTP-only ownership and quiet reservations are implemented; consumer deployment evidence is in the status report. Standard harvest, generic/health and other consumer moves remain separate.
**Date**: 2026-11-24
**Applies to**: `uc_browser/browser.py`, `uc_browser/status_server.py`, `uc_browser/sites/`, `uc_browser/health.py`, `uc_browser/llm_providers/uc.py`, `uc_browser/mcp/`; consumers, chiefly `event_tool`
**Depends on**: `docs/00 - Theory/00 - Windows, Transport and Control.md` (the model), `docs/08 - Analysis/00 - Service Prototype Measurements.md` (the evidence; "row N" refers to its §3), `docs/03 - Architecture/00 - System Overview.md` (what exists)

## Hard constraints (non-negotiable)

1. **One sync Playwright instance per thread; one writer per browser profile** (System Overview, axiom 4). The design exists because of the second half and must never violate the first.
2. **No technique is privileged, and no consumer is made to change technique.** UC and its consumers reach sites by a composite of techniques (§1.1). The service must offer every one that needs a shared browser, stay out of the way of every one that needs none, and make no single one the price of entry. In particular `event_tool` drives raw Playwright pages in about twenty modules and must go on holding real `Page` objects.
3. **Loopback is always retained; additional HTTP interfaces are explicit.** The default is loopback only. The owner approved binding specific ZeroTier addresses without authentication on 2026-11-26: network membership is the trust boundary for the entire API, not just Grok. Wildcard listeners are forbidden. Browser debugging ports remain loopback-only; exposing HTTP does not enable remote CDP attachment.
4. **A served profile is opened by the service and by nothing else.** One stray `UCBrowser` launch on a served profile brings the lock failures back.
5. **Every lifecycle in §3 has a pathway.** A change to this design that strands one of those rows is not finished.
6. **Routing is explicit.** The caller names the window and offer; neither library nor service infers them. `UCBrowser(service=url, window=name)` requests service attachment; HTTP-only callers choose their routes. `cdp_endpoint` explicitly borrows an external browser. Omitting both means local launching, regardless of environment.

## 0. What is wrong now

Every user of UC constructs a `UCBrowser`, and every `UCBrowser` launches a browser.

| Owner | Process | Browser and profile |
|---|---|---|
| `event_tool` harvest, standard sources | `event-harvester-mcp` | Chromium + engine, `data/.uc_chromium_profile` |
| `event_tool` harvest, native sources and Cloudflare retries | `event-harvester-mcp` | Google Chrome on fixed CDP port 9222, `data/.uc_chrome_profile` |
| `event_tool` MCP `web_explore_*` / `web_diagnose`, CLI `web replay` / `web test` | `event-harvester-mcp`, or a CLI process | one more of either, per session |
| Grok MCP tools (`uc_browser/mcp/grok_tools.py`) | `event-harvester-mcp` | Chromium + engine, **the harvest's profile** |
| litellm proxy, `uc/grok` | proxy | Chromium + engine, its own profile |
| litellm proxy, `uc/<site>` generic | proxy, second pinned thread | Chromium + engine |
| health probes | status server | Chromium (or the engine under test), its own profile |
| `event_tool` `scripts/export_grok_history.py` | ad hoc | Chromium + engine, a third Grok profile |
| `event_tool` `web login` | CLI | Google Chrome launched by Playwright directly, a fourth profile |

*(Rows for `event_tool` are as of its commit `177c16c`; the others from `main` at `5e3dd0a`.)*

Five costs follow, each seen on `smallblackbox` in November 2026:

1. **Sign-ins fragment.** The Grok account lives in the proxy's profile. The Grok MCP tools run in the harvest's profile, which has no Grok session, so they are signed out. The export script needed a third profile and a copy of the cookies.
2. **Profiles collide.** Two launches on one profile cannot coexist (`docs/02 - Implementation/00 - Setup and Operations.md`, gotcha 5, records what a held lock does to later launches). The Grok MCP tools and the harvest share a profile inside one process; the proxy had to be stopped before its profile could be opened to diagnose it.
3. **Every use pays a launch.** Seconds per browser, against 0.03–0.07 s to attach to a running one (row 2).
4. **Each owner needs a desktop.** A headed browser needs the desktop session's `DISPLAY` and `XAUTHORITY`, so every service that might launch one carries that configuration.
5. **Nobody can close a browser whose owner is gone.** A harvest abandoned at its 90-minute limit on 2026-11-19 left its native Chrome running for four days with 13 pages, and each later harvest attached to it without noticing (Analysis §5).

Inside UC the same pressure already produced machinery: the provider keeps two pinned threads so the adapter and the generic client can each own a Playwright instance, and health probing brings its own browser.

## 1. Decision

**One long-running process — the service — owns every browser and the signed-in identity inside it. It offers each access technique that needs that browser. Which techniques a pathway uses, and in what order it falls from one to the next, stays with the caller.**

- What is scarce and shared is the *identity*: a profile one process may hold. That is what the service owns.
- How a caller reaches a site through that identity is plural, and the service offers each way separately (§1.2).
- The engine's Python half — classifier, adapters, wait loops, signatures — runs only in the service.
- A technique that needs no browser never touches the service.

### 1.1 The techniques in use

Neither UC nor `event_tool` has one way of reading a site. Each pathway is a composite, usually an ordered fall from the cheapest technique that might work to the next. As built (`event_tool` at `177c16c`, UC `main` at `5e3dd0a`):

| # | Technique | Used today by | Needs a shared browser identity? |
|---|---|---|---|
| 1 | A published feed over plain HTTP (iCalendar, RSS) | `event_tool` `ics` (5 sources), `rss` (1) | no |
| 2 | A platform's own JSON over plain HTTP | `luma` (its discovery API), `partiful` (the props its pages are rendered from) | no |
| 3 | Probing a site for a feed before any browser is opened | `event_tool` onboarding and `feed_probe` | no |
| 4 | An application's data on disk, or a plugin inside the application | Discord's cache files and BetterDiscord exports; Signal's database | no |
| 5 | An official API | Telegram, Gmail; a conventional model API as `backup_model` when a chat site is down | no |
| 6 | Reading an image | OCR on event flyers; a screenshot sent to a vision model when rule-based hints are unsure | only for the screenshot |
| 7 | Engine detection and actuation in the page | `calendar`, `raw`, `feed`, `search` modes (41 sources); the generic chat path | yes |
| 8 | Scripting the page directly | every web mode: about 110 Playwright page calls in about twenty modules | yes |
| 9 | Watching the page's own network traffic | `feed` mode's response capture; Instagram's query recorder; the API shim's interceptors | yes |
| 10 | Fetching from inside the page, so the request carries the page's session | Instagram's replay of recorded GraphQL queries; the Grok reply read; the Grok history export; the API shim | yes |
| 11 | A site adapter riding the site's own submit path | grok | yes |
| 12 | A different browser when the first is refused | installed Chrome for 4 native sources and for automatic Cloudflare retries; real Chrome as the health probes' default; Firefox and WebKit in the probes and the browser matrix | yes |
| 13 | Placing the engine by evaluation where no extension can load | health probes on Firefox and WebKit; bench fixtures | yes |
| 14 | Carrying a session between browsers | cookies read from the user's own Chrome; a storage-state file; KeePassXC autofill in native Chrome | yes |
| 15 | Record once, replay later | Instagram queries; step routes; signatures; MHTML checkpoints and bench fixtures | to record |

The falls between them are part of the system, not exceptions to it:

| Pathway | Order of techniques |
|---|---|
| Adding a site | 3 → 1 or 2 if a feed validates → otherwise 7 and 8 |
| A standard web source | 7 and 8 in Chromium → 12 (installed Chrome) if a bot check appears |
| Instagram | 9 to record the page's queries → 10 to replay them → the first recorded page if the replay fails |
| A Grok send | 11 through the extension toolkit → 11 through the page's own submit, reply by 10 → reply read off the page by 8 |
| A chat site that is down | 5 (`backup_model`) |
| Diagnosing a page | rules on signals from 8 → 6 (screenshot to a vision model) |

### 1.2 What the service offers

One offer per technique that needs the browser. Each stands alone; a caller takes the ones its pathway uses.

| Offer | Serves technique | The caller needs | Available on |
|---|---|---|---|
| **attach** — the window's CDP endpoint | 8, 9, and 15 where recording is done client-side | a CDP client (Playwright, in every consumer today) | Chromium and Chrome |
| **control** — run an engine control on a page named by target id | 7, 13 | HTTP | any engine |
| **fetch** — run a request inside a page of the window | 10 | HTTP | any engine |
| **cookies** — read or add to the window's cookie jar | 14; and plain HTTP with a borrowed session where a site allows it | HTTP | any engine |
| **snapshot** — screenshot, HTML, or MHTML of a page | 6, 15 | HTTP | any engine (MHTML: Chromium and Chrome) |
| **session** — a site adapter's operations | 11 | HTTP | per site |
| **pages** — create (optionally at a URL) and close | lets an HTTP-only caller use control, fetch and snapshot with no browser library | HTTP | any engine |
| *nothing* | 1–5 | — | — |

Only **attach** asks the caller to hold a browser library. A caller that falls from one technique to another (the tables above) does so across these offers and across windows, on one identity where they share a window.

*Rejected: one technique for everything.* The first draft of this design made "attach over CDP and hold a Playwright page" the only way in and told the reader to design nothing else. That serves technique 8 and strands most of the table: 10 and 11 need no page in the caller, Firefox and WebKit serve no CDP, and 1–5 should not meet the service at all.

*Rejected: replacing direct page scripting with an RPC API* (`click`, `evaluate` as service calls). Technique 8 is open-ended; an API over it re-implements CDP and forces every consumer module to change. Constraint 2 rules it out. **pages** with an optional URL is the whole of what the service does on a caller's behalf.

*Rejected: a shared browser and nothing else*, callers evaluating `window.__UC_*` themselves. The in-page functions are reachable that way (probe A), but the Python half of the engine would then load in every caller, and nobody would be left to close pages.

*Rejected: status quo plus conventions* ("don't run the Grok tools during a harvest"). The current state is what conventions produce.

## 2. Windows

A **window** is one browser process on one profile. It is the unit of isolation: clients of one window share everything, clients of different windows share nothing (rows 8–10; Theory §1.2).

Windows are declared in a config file; UC ships none beyond a single default so that existing single-process use keeps working. The deployment this design was written for needs three:

| Window | Browser | Engine | Profile holds | Clients |
|---|---|---|---|---|
| `harvest` | Playwright's Chromium | extension, at launch | Instagram and event-site sign-ins | `event_tool` harvest workers, `web_explore_*`, `web_diagnose`, CLI |
| `grok` | Playwright's Chromium | extension, at launch | the Grok account | grok adapter sessions (in-service); through them the proxy, the Grok MCP tools and the export script |
| `native` | installed Google Chrome, HTTP-only owner | none in the measured deployment; optional page-local injection | native-site cookies and temporary clearance | native sources, retries and native explore sessions |

Why three and not one: the harvest and Grok need different tolerance (the adapter must not have harvest tabs appear in its context), and native Chrome is a different binary. Why not more: the harvest's parallel workers want the *same* sign-ins, so they are tabs in one window, not windows.

**Native loading must be possible without any CDP connection.** Delayed engine injection alone was insufficient: peer measurements found a check spinning with Playwright attached, and passing when loaded unattached before a later client attached. An `unattached: true` native window therefore has an HTTP-only owner and no startup shim, cookie import or stealth. The caller reserves a quiet interval, opens/polls/closes a leased page through HTTP, decides readiness itself, then releases and attaches. Native deployment uses `engine: none`, preserving local absent-engine fallbacks. Optional `inject` controls attach transiently and inject only on the addressed page; their effect on real bot checks remains unverified.

**Other engines.** UC `main` already probes with real Chrome, Firefox and WebKit, placing the engine by evaluation where no extension loads. A window on Firefox or WebKit offers everything in §1.2 except attach, which only Chromium-family browsers serve. Only in-service code (probes, the browser matrix) uses those engines today, so nothing is lost. Neither is installed on `smallblackbox`.

## 3. Lifecycles the design must carry

Each row is something a consumer does over time, what happens today, and the pathway in the service. Constraint 5 makes this table normative.

### 3.1 `event_tool`

| # | Lifecycle | Today | Pathway |
|---|---|---|---|
| E0 | Browser-free sources (`ics`, `rss`, `luma`, `partiful`: 8 enabled) run in the same loop as browser sources | a browser is allocated that they never touch; `luma.py` says so in its docstring | techniques 1 and 2 need nothing from the service and must not register with it. `event_tool` should stop opening a `UCBrowser` for a group that holds only these. The service makes that waste cheap (no launch); removing it is a consumer change |
| E1 | Long-lived host: `event-harvester-mcp` runs for days and performs every harvest | each harvest launches and should close two browsers | the process is a client; it launches nothing. Each `UCBrowser` it creates registers and attaches (§5, §6) |
| E2 | A harvest splits standard sources over `WEB_PARALLEL` threads plus native work | one browser per thread | standard workers attach to `harvest`; native work uses quiet preparation and an attachment per source. A native quiet reservation cannot overlap another client |
| E3 | Per source: open → Cloudflare wait → controls → raw page work → close | controls run in-process | standard windows use ordinary attachment. Native sources first reserve quiet loading, poll title/URL without CDP using the consumer's predicate, close/release preparation, then attach for that source alone. Failure to prepare never triggers attachment |
| E4 | Detail pages: `uc._context.new_page()` in batches of five, then `uc._stealth_plugin.apply_stealth_sync(page)` (calendar, feed, search, steps; Instagram likewise) | the client makes and stealths its own pages | `UCBrowser.new_page()`, a new public method, asks the service for a leased page; stealth is already on the window's context (row 19). `event_tool` moves these call sites to it when it consolidates its browser access in one module. Until then they keep working: `uc._context` is the real context and `uc._stealth_plugin` is `None`, which all three call sites guard. Such pages are unleased, so revocation (E10) does not reach them and only the sweep closes them (§5) |
| E5 | Network observers: `open_with_intercept`, Instagram's request/response recorder, navigation tracer, screenshots | attached to the client's page | unchanged — all transport, on the client's own attachment |
| E6 | MHTML checkpoints: a per-page CDP session calls `Page.captureSnapshot`; replay uses `set_content` | client-side | unchanged — transport |
| E7 | A standard source hits Cloudflare and is retried in native Chrome | another native browser | the same explicit native service route and per-source quiet preparation as E3; no implicit local-launch fallback |
| E8 | A chat-mode source calls `uc.chat(page, query, timeout_s=45)` (none configured today) | long control in-process | a long control from a library client: the service runs it on a worker of its own for its duration, so short controls on the window are not delayed (§6) |
| E9 | The group ends; the `with` block exits | `close()` shuts the browser | `close()` releases the client: the service closes its pages; the window stays |
| E10 | **The job times out and abandons a thread it cannot kill** | the thread keeps its browser indefinitely | revoke clients by job label and close their pages. Native cleanup uses HTTP, not an attachment. If the revoked thread still holds its browser websocket, new quiet reservations fail busy until it disconnects |
| E11 | Explore sessions hold a browser across MCP calls, optionally native | each owns a browser | each is a registered client. A long-lived native attachment blocks quiet preparations; busy errors identify its label/pid. Consumer cleanup disconnects before releasing the lease |
| E12 | CLI `web replay` / `web test` from a second process while harvesting | another launch can collide with the harvest's profile | standard CLI clients attach to `harvest`; native callers explicitly choose the native service route and honor quiet admission. Their leases belong to the CLI process |
| E13 | `web login`: a separate Chrome whose only product is a state file the next harvest reads | fourth profile; sign-in happens in a browser nothing else uses | sign-in happens to the window (§8); the command becomes "open a tab in `harvest` and wait" |
| E14 | The consumer service process (`event-harvester-mcp`) restarts (deploy) | its browsers die with it | nothing happens to UC's windows; the old consumer's clients are reaped by pid (§5). Restarting UC itself instead closes its windows (§11) |

### 3.2 uc-grok

| # | Lifecycle | Today | Pathway |
|---|---|---|---|
| G1 | Proxy starts; no browser until the first completion | lazy launch on a pinned thread | the proxy holds no browser. `grok` launches lazily in the service at first use |
| G2 | First use of a `session_id` opens a tab: load grok.com, dismiss, check sign-in, wait for the composer (hydration runs 15–25 s) | tab kept in `GrokClient._pages` forever; nothing evicts | a **session** in the service owns the tab and an attachment. Sessions are capped; the least recently used idle one is closed (§7) |
| G3 | `session_id` → conversation URL map | in memory, in whichever process hosts the provider; the proxy and the MCP server each have their own | one map, in the service, written to disk. A session evicted or lost in a restart is rebuilt from its URL on next use |
| G4 | Send: type, submit, wait up to `UC_GROK_TIMEOUT_S` (300 s floor) for the reply | holds the adapter's one lock for the whole wait | runs on the session's attachment. Other sessions and other operations are not behind it (rows 14–17) |
| G5 | One account, many sessions | serialized by the single lock; the code notes true parallelism needs async Playwright | parallel by construction; **how many sends at once** becomes a per-site setting, default 1, because it is one account's rate limit and not a technical limit (Open question 2) |
| G6 | Stop generation | waits for the lock held by the send it is meant to stop | acts on the busy page through a different attachment (row 18) |
| G7 | Streaming: `on_delta` feeds a queue the provider drains | in-process callback | the send route streams: one line per delta, then the result (§7) |
| G8 | List, read, delete, rename, archive, regenerate, model picker; `cleanup_threads`; delete-after-capture | run on the default session's tab under the same lock | adapter operations on the default session's attachment, concurrent with other sessions' sends |
| G9 | The programmed Grok page differs from the adapter's expectation | `has_login_wall` reads a `blocking` field the engine never emits, so its generic predicate is dead | the Grok adapter expects its configured project and an enabled composer before sending. It diagnoses an expectation failure as `auth_required` when login controls establish that, otherwise `page_deviation`. This is site-client logic, not a generic engine/service detector; cookie import can restore the session, and the next operation checks again |
| G10 | Availability gate reads health data written by probes | probes run in the health monitor's own browser and profile, and its scheduler skips sites marked `login_required` | a probe is a short-lived session in the site's own window, so a signed-in site can be probed and the probe sees what a send would see |
| G11 | The caller gives up (`event_tool`'s litellm config sets a 180 s per-model timeout; the browser wait floor is 300 s) | the send runs on, holding the lock | the send runs on, holding only its session. The caller may issue stop (G6) |
| G12 | MCP `chat`, `grok_*`, `list_chat_sessions`, `forget_chat_session` | operate on the hosting process's own browser and session map | thin clients of the same service routes the provider uses: one browser, one map |
| G13 | Proxy stops or restarts | its browser closes; tabs and the session map are lost | nothing happens to `grok`. Sessions and the map persist |
| G14 | The export script | separate profile and a cookie copy; a Playwright page of its own to fetch from | a caller of **fetch** on `grok`: no profile, no cookies, no browser library |
| G15 | The Grok session expires | re-copy cookies into a state file; restart the proxy | cookie import into the running window (§8); no restart |

## 4. How a caller reaches a window

```
 caller                                              service
 ────────────────────────────────────────           ───────────────────────────────────────
 techniques 1–5: no browser              ─────────►  (not involved)

 HTTP only        fetch · cookies        ─────────►  a page of the window · its cookie jar
                  session operations     ─────────►  site adapter on a session worker
                  snapshot · control     ─────────►  control worker, on a page the service made

 holds pages      control (target id)    ─────────►  control worker, on the caller's page
                  ═══ attach (CDP) ══════════════►   the window itself   (Chromium, Chrome)

                                                     window = browser + profile        × N
                                                     launched and closed by the service only
```

**Attach.** Each Chromium-family window serves CDP on loopback. Playwright Chromium selects its port; installed native Chrome receives an explicit dynamically selected port to preserve `navigator.webdriver=false`, with launched-process endpoint proof before attachment (Build Spec §5). The service lists the endpoint. A caller attaches with any CDP client; UC's own client library uses Playwright's `connect_over_cdp`.

**The HTTP offers.** A small API in the service. Control is one generic route — window, target id, control name, arguments — dispatching to the existing `UCBrowser` methods (rows 4–5: four different controls through one route, no per-control code, 0.03–0.08 s each). Fetch, cookies, snapshot and session operations have the same shape: name the window or site, pass arguments, get JSON.

**The target id joins them.** A page a caller holds over attach and a page the service acts on are named by the same string, read by either side from its own handle (Theory §1.3). That is what lets one pathway use several offers on one page: script it, ask for a control, fetch through it, snapshot it.

## 5. Pages, leases and revocation

**The service creates pages.** A client asks for a page and receives a target id; it then finds its own `Page` object for that target and navigates it. `UCBrowser.open()` and `UCBrowser.new_page()` both do this. A consumer that reaches past the controller to `uc._context.new_page()` (E4) gets a page the service did not create: it works, but it has no holder.

Reasons the service creates the page rather than the client:

1. **The lease starts at birth.** A page the service created is known to it from the first moment.
2. **Per-window setup is already there for ordinary windows.** Shim and stealth reach later pages (rows 3, 19). Native unattached windows deliberately have neither; page creation, metadata and lease cleanup are HTTP-only.

The client still navigates, so observers that hook the page (E5) attach before the first request, as now. A client must wait for the new target to appear on its own connection (row 6: 0.16–0.17 s); the client library hides this.

**Leases.** Service-created pages belong to a registered client, an in-service operation/session, or explicitly to the current window.

- A client registers once per `UCBrowser` with its process id and a label, and gets a client id. A long-lived process holds many clients over time (E1).
- `close()` releases the client; the service closes its pages (E9).
- **Death:** a reaper closes the pages of any client whose process has exited (E12, E14). No heartbeat: the service is local, so process liveness is exact.
- **Revocation:** release by label closes the client's leased pages. Native cleanup uses HTTP; ordinary owners use their browser connection. Explicitly window-owned tabs survive and cannot be used as an interruptible work page. A still-connected client continues to block quiet preparation even after revocation.
- **Sweep:** a page with no holder — made by a raw client over CDP — is left alone while any client is registered on its window, and closed once none is.
- **Quiet loading:** a native preparer registers `unattached: true` only when there are no other clients, service workers or existing endpoint connections. Metadata alone does not determine page readiness; the consumer does. Ordinary cleanup disconnects before releasing registration. Raw clients bypassing registration and connecting after a reservation remain outside this coordination guarantee.
- **Optional window lifetime:** the default remains client ownership. A caller may create or promote a tab to `window`; it survives release, process death and sweep, but ends on explicit close or window/browser termination. No stored URL or restart restoration. It adds no client or connection. Any actual Playwright attachment still reaches every page, including retained tabs.

*Rejected: idle timeouts on client pages.* A harvest worker can hold a page for many minutes making only transport calls, which the service does not see. Sessions are different: the service runs every operation on them and knows exactly when they are idle (§7).

## 6. Concurrency

Constraint 1 and Theory §1.5 fix the shape. The unit is *(thread, Playwright instance, attachment)*.

| Thread | Per window | Does |
|---|---|---|
| **Owner** | one | launches/owns the process and maintains pages. Ordinary windows retain Playwright; unattached native windows use HTTP only and perform no context setup |
| **Control workers** | bounded short/long pools | ordinary windows retain idle attachments; native unattached windows attach per operation and disconnect before completion |
| **Session workers** | one per live session, capped | each holds its own attachment and its session's page; runs that session's sends and waits (G4), and long controls borrowed by library clients (E8) |

- A long operation occupies one session worker and nothing else (rows 14–17).
- Controls on different windows never wait on each other.
- Transport is unaffected by any of it: it travels on the client's own connection.
- Each worker costs one Playwright driver, about 80 MB (row 24). Pools are bounded by config; a request beyond the bound waits.

The provider's Grok pinned thread has no purpose once Grok uses site HTTP operations. The generic client's pinned thread remains until its separate task-8 migration; it must not be silently redirected by this Grok cutover.

## 7. Sites: adapters, sessions and streaming

The grok adapter and the generic client stop constructing `UCBrowser`. They run inside the service on session workers.

**Session.** The unit a caller names with `session_id`. It owns one page in the site's window, one attachment, and one entry in the persisted map `session_id → conversation URL` (G2, G3). Sessions are created on first use, closed when idle past a limit or when the cap is reached (least recently used first), and rebuilt from the map when next used. `forget` removes the map entry and the page.

**Routes.** One generic route per site operation, mirroring the control route: site, operation name, arguments. The operations are the adapter's public methods (for grok: send, read, list, delete, rename, archive, regenerate, stop, models, switch model, new chat, cleanup).

**Streaming.** A send may ask for a stream. The response is then newline-delimited JSON: a line per delta as the reply renders, and a final line with the result (G7). Without it, the route answers once with the result.

**Errors with meaning.** `auth_required` (G9), `busy` when the site's concurrent-send limit is reached and the caller asked not to wait, `timeout` with whatever text had rendered. The provider maps these to the litellm exceptions it raises today.

**Account folder boundary.** A configured Grok folder/project is an explicit part of that adapter's program. New conversations originate there; subsequent operations preserve and verify their project/conversation identity. The deployment uses `NSFW`. A caller-supplied conversation outside the configured project must fail closed, not move or modify it. Bulk cleanup is project-scoped, never account-wide. Folder placement organizes the account; it is not a separate login or security principal.

**Client configuration.** Provider and Grok MCP consumers pass their configured service address to `ServiceClient`; deployment uses `UC_GROK_SERVICE_URL`. Missing configuration is an error, never permission to launch a fallback browser. A named `session_id` belongs to the service map and survives consumer restarts. Stateless provider calls use disposable service sessions so independent prompts cannot silently continue one shared default conversation.

**What becomes a thin client:**

| Today | After |
|---|---|
| litellm provider owns browsers on pinned threads | provider calls the site routes; holds no browser |
| Grok MCP tools run in the hosting process's browser and session map | tools call the site routes (G12) |
| health monitor launches a probe browser | a probe is a short-lived session in the site's window (G10) |
| `status_server` serves status only | the same process hosts windows, the control plane and the site routes |

`status_server` is the host because it is already UC's long-running process, already a FastAPI app on loopback, and already mounts an MCP endpoint.

## 8. Signing in

A window is headed, persistent and long-lived, so signing in is something done *to a window*:

1. **By hand in the window**, for sites that allow it (E13).
2. **By cookie import**, for sites that refuse sign-in in an automated browser (observed for Grok's Google and X sign-in, 2026-11-17). The service accepts a cookie list, or a list of domains to copy from the user's normal Chrome (G15). `uc_browser/chrome_cookies.py` already reads those cookies; `browser.py` has an `_inject_chrome_cookies` helper that nothing calls.
3. **From the state file at window start**, as `CHROMIUM_EXT` mode does today.

## 9. Where Playwright lives

Playwright is a driver. It is not the access technique, and the service's contract does not name it: the contract is HTTP plus, for Chromium-family windows, a CDP endpoint.

| Use | Lives | Why |
|---|---|---|
| **Driving the service's own browsers** — launch, Chromium build (`playwright install`), flags, stealth, the service's attachments | in the UC service, and only there | constraint 4. It also removes browser binaries and desktop configuration from every consumer |
| **A consumer's CDP client for attach** | in a consumer exactly as far as it uses technique 8 or 9 | constraint 2 |

By consumer:

| Consumer | Techniques it uses on a window | Needs Playwright? |
|---|---|---|
| `event_tool` harvest and explore | 7, 8, 9, 10, 12 | yes — for 8 and 9. It comes through `uc_browser`; the one direct launch (`web login`) and the direct `playwright-stealth` dependency go (E13, E4) |
| `event_tool` browser-free sources | 1, 2 | no, and no service either (E0) |
| litellm proxy, Grok MCP tools | 11 | no |
| Grok export script | 10 | no |
| health probes, generic client, grok adapter | 7, 10, 11, 12, 13 | they run inside the service |

The pin belongs to UC: the service's Playwright version determines the Chromium build. A consumer attaching with a different Playwright version, or with a different CDP client altogether, is how the protocol is meant to be used, but it is `unverified` here because both sides have always shared one environment.

## 10. What this fixes, and what it does not

| Fixes | Does not fix |
|---|---|
| Profile collisions between consumers | Interference between clients *inside* one window — that is the model (Theory §2) |
| Sign-ins split across profiles | Sites that sign a session out on their own schedule |
| A launch per use | Grok's own rate limits on one account |
| `DISPLAY` / `XAUTHORITY` setup in every consumer service | The need for a desktop session at all: the service still runs headed browsers |
| Browsers and tabs that outlive an abandoned owner (E10, G2) | A consumer thread that is stuck somewhere other than a page call |
| `stop` queued behind the send it should stop (G6) | Recognising Grok's signed-out page (G9) |
| No controls in native Chrome | — |

## 11. Failure modes to design for

1. **UC itself restarts.** Its browsers go with it, so every client's pages die mid-use. Sign-ins survive in profiles; named site sessions rebuild lazily from the persisted URL map (G3). Ephemeral sessions do not survive. HTTP connection failures surface as `UCServiceGone`; raw page operations retain Playwright's target-closed errors. An interrupted send is never replayed automatically.
2. **A window's browser crashes.** The service relaunches that window; other windows are untouched; that window's leases and sessions are void.
3. **A consumer bypasses the service** and launches on a served profile (constraint 4). There is no implicit service default: a consumer that wants the service passes `service` and `window`, and the in-process launch path still refuses a profile the service lists (held-profile guard retained).
4. **A browser the service did not launch already holds a profile**, as the four-day-old Chrome did (Analysis §5). The service must detect a held profile at launch and refuse loudly, not attach to whatever answers.
5. **A control arrives for a target that just closed**, including one closed by revocation. Return a distinct not-found error.
6. **Every session worker is busy.** A new session waits or gets `busy`; it must not evict a session that is mid-send.

## 12. Sequencing

Each step leaves the system working and is verified before the next.

1. Land the probes as `scripts/service_probe.py` and re-run them on `main`.
2. Window manager, CDP endpoint listing, the control route with a worker pool, one Chromium window. No consumer changes.
3. `UCBrowser` service mode: registration, `new_page()`, leases, reaper, revocation.
4. Move `event_tool`'s standard harvest to `harvest`; add the revocation call to its job runner (E10).
5. The `native` window with HTTP-only ownership and quiet loading; move native sources, retries, explore and CLI routing. Use no engine initially to preserve the measured access method; injection is optional and requires separate site verification.
6. Sessions and site routes; move the grok adapter in-service; make the provider, the Grok MCP tools and the export script clients. Retire the extra Grok profiles.
7. Cookie import; retire the separate login browser.
8. Health probes and the generic client become sessions; remove the provider's pinned threads.

Steps 4–6 each move one consumer and can stop there if a later step slips.

## Open questions

1. **Does the injected engine trip a bot check when injected after the check has passed?** Rows 21–23 used a page with no check. If it does for some site, that source keeps a window with no engine; the config allows it.
2. **How many sends may run at once on one Grok account?** The technical limit is gone (G5); the account's is unknown. Default 1.
3. **Should the default window exist when a config file declares others?** Proposed: no.
4. **For which signed-in sources is plain HTTP with borrowed cookies enough?** It is the cheapest technique on a signed-in site when it works. Not for Grok sends: `grok_api.py` records that Grok's send endpoint rejects even an in-page fetch that bypasses the site's own request wrapper. `unverified` everywhere else.

*Closed on 2026-11-23:* "can a native window have controls" — yes, by injection (rows 21–23). "Is one worker per window enough for long controls" — no, and it need not be: long work takes its own attachment (rows 14–18).

## Verification

Tasks 0–5 were verified with fresh profiles: 115 integrated service/client/window/health tests, three additional startup/profile regressions, all baseline probe rows and the real-service model assertions. Revocation interrupted a 600-second page wait in 0.04 seconds.

```bash
pixi run -e dev pytest -q tests/test_service_api.py tests/test_service_client.py tests/test_service_windows_core.py tests/test_health.py
pixi run python scripts/service_probe.py
```

See Build Spec §8 and the dated status report for current deployment evidence. The original tasks 0–5 used fresh profiles only; later authorized Grok deployment and native migration are recorded separately rather than inferred from those tests.
