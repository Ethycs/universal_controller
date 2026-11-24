# Engine-Absent Control Behavior

**Status**: CURRENT — §2's 39 return/exception outcomes measured on 2026-11-23 in the independent default pixi environment (Playwright 1.65.2, headless Chromium, no extension, `<body>hello</body>`). Source-derived explanations and the Grok diagnosis remain read-derived
**Date**: 2026-11-23
**Applies to**: the 39 page-taking `UCBrowser` controls (`docs/02 - Implementation/01 - UC Service — Build Spec.md` §3 rule 1, §4 row "a control on an `engine: none` window"); `has_login_wall` and Design G9
**Depends on**: `docs/08 - Analysis/00 - Service Prototype Measurements.md` ("row N"), `docs/01 - Design/02 - UC as a Local Service.md` (§2, G9), `docs/00 - Theory/00 - Windows, Transport and Control.md`

**Implementation resolution:** tasks 0–5 implement local fallback and callable orchestration. `UCBrowser` now checks the start response's `offers` first: absent `control` means no 409 round trip; an unexpected `no_controls` refusal still falls back. §4–7 preserve pre-build findings; callback/constant-fallback contradictions are resolved in `browser.py`, `client.py` and the Build Spec. Grok auth remains open for task 6. Live service mode verified `detect_all` and body-text fallbacks on an `engine: none` window.

## Verdict

1. **No control invents anything when the engine is absent; each swallows the Playwright error and returns its own default.** 33 of 39 return a constant (`None` 7, `[]` 10, `{}` 3, `False` 13). 3 return real page text (`get_feed_text`, `get_feed_items`, `scroll_feed`). 1 returns an error string (`set_text`). 2 raise (`ml_find_chat`, and `chat` through it). *Read.*
2. **16 of the 39 first wait five seconds** (`_wait_ready`, default `timeout_ms=5000`) and then return empty; the other 23 fail at once. `scroll_feed` with its default `seconds=15` takes about 22 s. *Read; arithmetic in §2 note 3.*
3. **An empty result is indistinguishable from "engine present, found nothing"** for 33 of 39 methods. `has_login_wall` returns `False`, which a caller must not read as "signed in" (§6).
4. **A 409 table in code is not implementable faithfully.** 6 outcomes need the page or the live error text. The faithful 409 behavior is: the client library runs the method's own in-process body on its local `Page`, and the library's service-less client raises instead of answering (§5).
5. **Generic HTTP dispatch on an unstarted `UCBrowser` is valid for all 39 at the attribute level**: no control reads or writes receiver state, only calls sibling methods. Two controls cannot cross HTTP because they take callables (`scan_action.action`, `scroll_feed.on_item`), so the Build Spec's "39 today" over-counts by one in practice (§4).
6. **`has_login_wall` can never return `True` on this tree, with or without the engine.** It filters hits on a `blocking` key that nothing in the engine emits and that no commit has ever emitted. "Sign up to continue" is not the cause; it is not reachable by any page (§6).

## 1. Conditions: what "absent" means, and what was measured

**Absent** means `window.__UC` and every `window.__UC_*` are `undefined` on the page. It occurs in four situations:

| Situation | Why | Basis |
|---|---|---|
| `engine: none` window | the window is configured without an engine; the control route answers 409 `no_controls` (Build Spec §2, §3) | spec |
| today's `HEADLESS`, `CHROME` and `NATIVE_CDP` modes | no extension is loaded; `start()` installs only the API shim | read, `browser.py:518-674` |
| a page the extension never ran on, in an `extension` window | `extension/manifest.json` matches `<all_urls>` with `run_at: document_idle`, `world: MAIN`, no `match_about_blank`, no `all_frames`. Service-created pages are **blank** (Build Spec §3 rule 4), and `about:blank`, `chrome-error://` pages and other non-matching URLs get no engine until navigated to a matching URL | read |
| an `inject` window before the first control call | the bundle is evaluated at that call, not before (Design §2) | spec; row 21 measured the undefined global |

The last two are **not** 409 cases: the worker runs the real method and the caller gets today's fallback as an ordinary `200 {result}`. §2 applies to them too.

**Measured earlier (Analysis §3), engine side only:**

| What | Row |
|---|---|
| a native Chrome page before injection has `typeof window.__UC_detectAll === 'undefined'` | 21 |
| after `page.evaluate(bundle)`, `window.__UC.ready` is true within 1.5 s; `detect_all` returned its map in 0.09 s | 22 |
| `set_text`, `has_login_wall`, `find_inputs`, `detect_all` ran with the engine present on a never-started `UCBrowser` through the generic route | 4, 5 |

**Measured in this checkout:** all 39 outcomes in §2 matched the verification script. `chat` raised after 5.24 s; `ml_find_chat` raised immediately; both exceptions were `playwright._impl._errors.Error`. Readiness fallbacks took 5.00–5.01 s, `scan_action` 5.51 s, and `scroll_feed(seconds=1)` 10.82 s. Body fallbacks returned `"hello"` / `["hello"]`. No live account or profile was used.

**Read from the installed library** (`playwright/_impl/_errors.py`, 1.65.2): `TimeoutError(Error)` and `TargetClosedError(Error)`. So `except PlaywrightError` also swallows a closed page and a wait timeout. A page-evaluate that throws in the page (`window.__UC_x is not a function`) surfaces as `Error`; that is the assumption every row relies on, and §8 checks it.

## 2. The 39 controls with the engine absent

`W` = `_wait_ready(page)`: `page.wait_for_function("window.__UC && window.__UC.ready === true", timeout=5000)`; on timeout or closed page it returns `False`. `E` = a bare `page.evaluate(...)` of a missing global; it raises `Error` immediately (no wait).

| # | Method | Path taken | Returns | Cost | Notes |
|---|---|---|---|---|---|
| 1 | `first_scan` | W fails | `None` | 5 s | no log above debug |
| 2 | `next_scan` | E, `except Exception` | `None` | ~0 | logs `ERROR next_scan failed` |
| 3 | `auto_detect` | E, `except Exception` | `[]` | ~0 | logs `ERROR auto_detect failed` |
| 4 | `scan_action` | `first_scan` (W) → **`action(page)` runs** → `wait_for_timeout(500)` → `next_scan` → `auto_detect` | `[]` | 5.5 s + action | the caller's action still executes; `action` is a callable (§4) |
| 5 | `detect` | W fails | `[]` | 5 s | |
| 6 | `detect_all` | W fails | `{}` | 5 s | the one the handoff had read |
| 7 | `get_patterns` | `evaluate("window.__UC")` → `undefined` → `None`; `(None or {}).get(...)` | `{}` | ~0 | **no error at all**; same value as an engine that has not run `detect` yet |
| 8 | `dismiss_cookies` | `detect` (W) → E caught | `False` | 5 s | |
| 9 | `search` | `detect` (W) → no hits | `False` | 5 s | logs `WARNING No search bar detected`; never reaches `fill` |
| 10 | `get_feed_text` | `detect` (W) → E caught → **`document.body.innerText`** | `str`, the page text | 5 s | a real fallback. The second evaluate is not wrapped: a closed or navigating page, or no `<body>`, raises `Error` |
| 11 | `get_feed_items` | `detect` (W) → no hits → **`[document.body.innerText or ""]`** | `list[str]`, one item | 5 s | same unwrapped evaluate |
| 12 | `scroll_feed` | `detect` (W), then a loop: `scrollBy` (**scrolls the page**), `wait_for_timeout(800)`, `get_feed_items` (5 s + body text) | `list[str]` of distinct body-text snapshots, at least one | ≈ 5 + 5.8·⌈`seconds`/5.8⌉ s (22.4 s at 15) | `on_item` is called per new snapshot (a callable, §4); `scrollBy` and the second evaluate are unwrapped; with `seconds ≤ 0` it is `[]` after 5 s |
| 13 | `fill_form` | `detect` (W) → no hits | `False` | 5 s | logs `WARNING No form detected` |
| 14 | `submit_form` | `detect` (W) → no hits | `False` | 5 s | |
| 15 | `close_modal` | `detect` (W) → no hits | `False` | 5 s | |
| 16 | `has_login_wall` | `detect` (W) → `any([])` | `False` | 5 s | **do not read as "signed in"** (§6) |
| 17 | `bind` | E caught | `None` | ~0 | |
| 18 | `chat_send` | E caught | `False` | ~0 | |
| 19 | `chat_get_messages` | E caught | `[]` | ~0 | |
| 20 | `form_fill_uc` | E caught | `False` | ~0 | with the engine present, a `null` result is `True` (`is not False`) |
| 21 | `form_get_values` | E caught | `{}` | ~0 | |
| 22 | `dropdown_toggle` | E caught | `False` | ~0 | |
| 23 | `dropdown_select` | E caught | `False` | ~0 | |
| 24 | `modal_close_uc` | E caught | `False` | ~0 | |
| 25 | `get_llm_context` | E caught | `None` | ~0 | |
| 26 | `heap_scan` | E caught | `None` | ~0 | |
| 27 | `scan_framework` | E caught | `None` | ~0 | |
| 28 | `start_passive` | E caught | `False` | ~0 | |
| 29 | `stop_passive` | E caught | `False` | ~0 | |
| 30 | `get_passive_results` | E caught | `[]` | ~0 | |
| 31 | `save_signature` | E caught | `None` | ~0 | |
| 32 | `load_signatures` | E caught | `[]` | ~0 | |
| 33 | `auto_bind_signatures` | E caught | `[]` | ~0 | |
| 34 | `find_inputs` | W fails | `[]` | 5 s | |
| 35 | `find_buttons` | W fails | `[]` | 5 s | |
| 36 | `set_text` | E caught | `{"success": False, "error": str(e)}` | ~0 | `str(e)` is Playwright's text, `Page.evaluate: TypeError: window.__UC_setText is not a function` plus a call log. Version-dependent; cannot be tabulated |
| 37 | `find_new_content` | E caught | `[]` | ~0 | |
| 38 | `ml_find_chat` | `from uc_browser.dom_classifier import …`; then **unwrapped** `page.evaluate("window.__UC_findChatCandidates()")` | **raises `playwright.sync_api.Error`**; returns `None` only if the import raises `ImportError` | ~0 | depends on the environment: `numpy`, `joblib`, `sklearn` are in this checkout's pixi envs, `models/dom_classifier/*.pkl` and `ml/rasterizer.js` exist, so the import succeeds and the call raises. `dom_classifier` reads `ml/rasterizer.js` at import: a missing file raises `FileNotFoundError`, not `ImportError` |
| 39 | `chat` | `start_passive` (E, `False`) → `find_inputs` (W, `[]`) → `inputs` empty → `ml_find_chat` | **raises `Error`** (as 38); `None` + `WARNING No interactive input found on page.` if the import raises `ImportError` | 5 s | **nothing typed, nothing sent**; it fails before any page write |

### Notes

1. **Page-visible side effects with the engine absent** are only three: `scan_action` runs the caller's `action`; `scroll_feed` scrolls and calls `on_item`; everything else only reads.
2. **Log noise:** `next_scan` and `auto_detect` (and so `scan_action`) log at `ERROR` for an absent engine. The rest log at `debug` or `warning`.
3. **`scroll_feed` arithmetic.** `end_time` is set after the first `detect` (5 s). Each loop iteration is 0.8 s + one `get_feed_items` (`detect` 5 s) ≈ 5.8 s, and the loop tests the clock only at the top: iterations start at 0, 5.8, 11.6 s, then 17.4 s ≥ 15 ends it. Total ≈ 5 + 17.4 = 22.4 s. *Arithmetic, not a measurement.*
4. **Outcome classes** (rows above): `None` — 1, 2, 17, 25, 26, 27, 31 (7). `[]` — 3, 4, 5, 19, 30, 32, 33, 34, 35, 37 (10). `{}` — 6, 7, 21 (3). `False` — 8, 9, 13–16, 18, 20, 22, 23, 24, 28, 29 (13). Page text — 10, 11, 12 (3). Error dict — 36 (1). Raises — 38, 39 (2). Total 39.
5. **A closed page looks the same.** `TargetClosedError` is an `Error`, so every wrapped control also returns its default when the page is gone. Only unwrapped calls (`page.wait_for_timeout`, the bare evaluates in rows 10–12, 38) surface a closed page. This matters for revocation (E10): a stuck client unwinds only if it is parked in one of those.

## 3. Engine-present contrast (why the defaults are ambiguous)

| Method | Engine absent | Engine present, nothing found |
|---|---|---|
| `detect`, `find_inputs`, `find_buttons`, `find_new_content` | `[]` | `[]` |
| `detect_all` | `{}` | a map of eight empty lists (`__UC_detectAll` returns all eight pattern names) |
| `has_login_wall`, `search`, `fill_form`, `close_modal`, … | `False` | `False` |
| `get_patterns` | `{}` | `{}` before any detect |

Only `detect_all` differs in shape (`{}` against a map of eight keys). Nothing in the return values tells a caller which case it got; the debug line `UC extension not ready on <url>` is the only trace.

## 4. Is generic HTTP dispatch on an unstarted receiver valid?

**Yes for the receiver; no for two arguments.**

**Receiver.** `UCBrowser.__init__` only assigns attributes (no launch, no I/O). Over lines 826–1796, which hold all 39 methods and their private callees (`_wait_ready`, `_lock_response_via_anchor`, `_wait_chat_response`):

| Check | Result (read) |
|---|---|
| `self.` attributes used | only sibling methods: `detect`, `_wait_ready`, `get_feed_text`, `first_scan`, `next_scan`, `detect_all`, `dismiss_cookies`, `close_modal`, `set_text`, `search`, `scroll_feed`, `save_signature`, `ml_find_chat`, `has_login_wall`, `get_passive_results`, `get_feed_items`, `find_new_content`, `find_inputs`, `find_buttons`, `bind`, `auto_detect`, `start_passive`, `_wait_chat_response`, `_lock_response_via_anchor` (+ `open`, used only by `navigate_and_*`, which are not page-first) |
| reads of `_context`, `_browser`, `_pw`, `_stealth_plugin`, `_chrome_proc`, `mode`, `channel`, `use_stealth`, `timeout_ms` | none after line 825 (`grep -n 'self\._context'` prints 442–722, 743, 790: `__init__`, `start`, launch, `is_alive`, `close`, `open`, `open_with_intercept`) |
| assignments to `self.` | none after line 825 |
| count | 49 public `def`s: 39 page-first + `start`, `is_alive`, `close`, `open`, `open_with_intercept`, `navigate_and_search`, `navigate_and_scrape` + 3 legacy properties (`headless`, `use_extension`, `native_chrome`) |

So the receiver is stateless, one instance can serve every worker thread, and the Theory doc's verification passes. **Measured** for four of the 39 only (rows 4, 5).

**Qualifications:**

| # | Qualification | Consequence |
|---|---|---|
| 1 | `scan_action(page, action)` and `scroll_feed(page, seconds, on_item)` take callables. Build Spec §3 rule 2 says arguments must be JSON. | `scan_action` cannot be called over HTTP at all (`action` is required). `scroll_feed` can, with `on_item=None`. Both must run their body in the client (§5), where their inner controls dispatch. 38, not 39, are dispatchable as written |
| 2 | The worker's receiver must be a pristine in-process `UCBrowser`. If service mode wraps the public methods based on ambient configuration at import, a service process that also had it set would dispatch to itself | build the worker receiver from the unwrapped method bodies, independent of the environment (superseded in part: routing is now the explicit `service` argument; omitted means local) |
| 3 | `ml_find_chat` and `chat` import `uc_browser.dom_classifier`: `numpy`, `joblib`, `sklearn`, the two `.pkl` models, and a file read at import. Its model cache is a module global filled lazily | the service's own environment must carry them (they are in `pyproject.toml`); the first call pays the load; two workers racing the lazy load is benign but duplicates it |
| 4 | Worker occupancy. Rows 1, 4–6, 8–16, 34, 35, 39 hold a worker 5 s or more when the engine is absent; `scroll_feed` holds one for `seconds` even with the engine; `search` and `submit_form` add 2 s; `chat` up to `timeout_s` | the spec names only `chat` as a long control. With `control_workers = 2`, three absent-engine `detect_all` calls at once give the third a 503 `busy`. Add `scroll_feed` to the long set, or run it in the client (§5) |
| 5 | A control that raises today raises a Playwright `Error`; over HTTP it becomes 500 `control_failed` | the type changes for engine-present failures (`chat` on a closed page, rows 10–12 bare evaluates). The client wrapper should say so in its exception |
| 6 | Results come from `page.evaluate`, so they are JSON-shaped by construction | no conversion needed; `None` travels as `{"result": null}` |

## 5. Recommended 409 behavior (client service mode)

**Rule: a 409 `no_controls` makes the client run the method itself, on its own `Page`. It never answers from a table.**

| Where | Behavior |
|---|---|
| Server, `engine: none` | answer `409 {"error": "no_controls", "detail": "window <w> has engine none"}` before queueing a worker, resolving the target or touching the page. No wait, no evaluate |
| `UCBrowser` service mode, on 409 `no_controls` | call the method's **undecorated in-process body** with the caller's `page` and arguments. Its result, exception, 5 s waits, log lines and page effects are by construction what the method does today when the engine is absent (§2). Mark a thread-local "local control" flag for the call so that nested `self.detect(...)` inside `search`, `chat`, … run in-process and do not POST and re-409. Log one `WARNING` per window the first time: `controls unavailable on window <w> (engine none); running <name> locally`. Remember the 409 per window for the life of the client so the next call skips the POST |
| `UCBrowser` service mode, any other status | unchanged: 404 → target gone, 409 other codes, 500 → `control_failed`, 503 → `busy` |
| `ServiceClient` (no browser import) | raise `ControlsUnavailable(window, name)` carrying status 409 and `no_controls`. **Return no value** |
| `scan_action`, `scroll_feed(on_item=...)` | always run the body locally, whatever the window's engine; their inner controls dispatch (§4 qualification 1) |

**Why not a table of 39 defaults:**

1. Six outcomes need the page or a live error: 10, 11, 12 return the page's text, 36 returns Playwright's message, 38 and 39 raise. A table would have to invent them or drop them.
2. The 33 constants would change what callers observe in time: a table skips the 5 s waits, and `scroll_feed`'s loop count and text depend on that time.
3. It would answer `False`/`[]` for a window that never had an engine with no trace, where the in-process path leaves at least a debug line.
4. A table is a second copy of the methods' defaults, and drifts as they change. The local body is the only copy.

**Option B, not recommended.** If a fast path is wanted, 32 of the 39 (the constants except `scan_action`) could be answered from a table, accepting the latency change in rows 1, 5, 6, 8, 9, 13–16, 34, 35 and a loss of the `ERROR` log in rows 2 and 3. The other 7 still need the local body. Decide this only when a consumer's 5 s waits are shown to matter; none is known to.

**Test oracle (no browser).** Unit-test the local fallthrough with a fake `Page` whose `evaluate` raises `playwright.sync_api.Error`, whose `wait_for_function` raises `playwright.sync_api.TimeoutError`, and whose `wait_for_timeout` records its argument. Assert each row of §2 for return value and exception class; patch `time` for `scroll_feed`. The fake pins the error classes; §8 pins the real library.

## 6. `has_login_wall` and Grok's signed-out page

`has_login_wall` is `any(h.get("blocking") for h in self.detect(page, "login"))` (`browser.py:1147-1150`). The handoff and Design G9 describe a missing phrase. The gap is wider.

| # | Finding | Evidence (read) |
|---|---|---|
| 1 | **The predicate is dead.** `blocking` is not set by `_serializeResult` (`extension/src/extension-entry.js:1220-1295`), which builds every hit the engine returns. `rg 'blocking' src extension/src` finds only a comment in `src/core/passive-detector.js`. `git log --all -S'blocking' -- '*.js'` lists one commit, `9a7ce47`, whose only match is that comment. `git log -S'h.get("blocking")'` finds the Python line in `92056c0` ("Move UC"), the first commit holding it. A producer has never existed | `has_login_wall` returns `False` on every page, engine present or not |
| 2 | **Even with a producer, this page would not match.** The login pattern's structural rules are `has-password: 4`, `form-tag: 2`, `has-button: 1` (`src/detection/patterns.js`); the behavioral check is "has a password field" (`src/detection/universal-controller.js`); and the phrasal scanner lists `sign up` as a **negative** for `login` (`src/core/phrasal-scanner.js`, `login.negative: ['create account', 'sign up', 'register']`). A "Sign up to continue" dialog has no password field and its phrase lowers the score | `unverified` against the real page, which was not loaded |
| 3 | **The check runs once per tab.** `GrokClient._get_session_page` (`sites/grok.py:277-318`) calls it on a tab's cold open, after `dismiss_cookies` and `close_modal`, then caches the tab. A session that expires later, or a wall that appears only on the first send, is never checked | `grok.py:297` is the only caller in the adapter |
| 4 | **The send loop turns a wall into silence.** `GrokClient.send` waits `timeout_s` for a new `assistant-message` block and, on the deadline, falls out of the loop and returns `{"response": "", ...}` with no error (`grok.py:483-535`). A signed-out page that shows a dialog instead of a reply therefore ends as an empty reply after the full wait. The handoff's "hangs until its timeout" is this | consistent with the 2026-11-17 observation |
| 5 | **A different heuristic exists and does count `sign up`.** `uc_browser/health.py` `_LOGIN_WALL_JS` counts `sign in`, `log in`, `login`, `sign up` and reports a wall when there is a password field, or two or more of those with no input (`health.py:450`). It is the only working login-wall test in the tree, and it needs two hits | read |
| 6 | **With the engine absent the answer is `False` for the same reason as everywhere else**, after a 5 s wait (§2 row 16) | read |

**Consequence for the service.** `auth_required` (G9) cannot be derived from `has_login_wall`, from its 409 fallback, or from a phrase added to the scanner alone. The service needs a signed-out test that:

1. is Grok-specific, in the adapter, not in the generic engine (the engine's `login` pattern is about forms with passwords);
2. runs at tab open **and again whenever a send or read returns no assistant block** (finding 4), so a wall that appears late and an expired session both map to `401 auth_required` and mark the window signed-out;
3. prefers a **positive** signed-in marker (what `list_conversations` already relies on) over matching wall copy, with the copy `Sign up to continue` as a corroborating signal. Both are `unverified` until a signed-out page is captured.

To capture the signed-out DOM without touching a live account, open `https://grok.com` in a **fresh** profile through the service's `grok` window pointed at an empty `profile` path (no cookies imported, no message sent), read `document.body.innerText` and the composer's presence, and record them here. That run was not made.

Fixing the generic predicate is separate and cheap: either have `_serializeResult` emit `blocking` (a pattern-specific rule: a `login` hit that is fixed-position or overlays the viewport) or delete `has_login_wall`'s claim and its two callers' reliance on it (`sites/grok.py:297`, `navigate_and_search`). Until then it returns `False` always.

## 7. Contradictions and gaps that touch the build

| # | Where | Contradiction or gap | Affects |
|---|---|---|---|
| 1 | Build Spec §3 rules 1 and 2 | The allowlist counts 39 controls and requires JSON arguments; `scan_action` needs a callable and `scroll_feed` takes one. 38 are dispatchable | tasks 3–4 (`UCBrowser` wrapper, allowlist test) |
| 2 | Build Spec §4 row "engine: none" | It says to return "the value the method returns today". The table is now made (§2) but six rows cannot be returned without the page; §5 replaces "return the value" with "run the body" | task 3 |
| 3 | Handoff task 5 "Done when" | A control on a native page returns a pattern map. That exercises `engine: inject`, never the 409. No window in the example config has `engine: none`, so the 409 path has no live test; the absent-engine fallback is reachable through `200` on blank pages (§1) | task 5 |
| 4 | Build Spec §5 "Ensuring the engine" | For `extension` it waits for `__UC.ready`; the method then waits again in `_wait_ready`. On a blank page that is 10 s before an empty result, and the call looks successful | tasks 2, 5 |
| 5 | Build Spec §3 rule 4 | Pages are created blank. In an `extension` window the engine is absent on them until a matching navigation (manifest has no `match_about_blank`). A client that calls a control straight after `new_page()` gets §2's defaults after 5 s with status 200 | tasks 3–4 |
| 6 | Build Spec §5 "Control workers" | Only `chat` is a long control. `scroll_feed` (≥ `seconds`), and every absent-engine wait (§4 qualification 4), occupy one of two workers | tasks 2–4 |
| 7 | Handoff task 5 and bug 2: `_start_native_chrome` | It must not be reused as written. It returns with `_context` still `None` when Chrome is not found (it falls back to Playwright `channel="chrome"`, which has no CDP) or when 15 connect attempts fail (`browser.py:605-612`, `664-670`), and `start()` carries on. Its `connect_over_cdp("http://localhost:9222")` attaches to whatever answers. The service launcher must raise `launch_failed` on both and attach only to the port its own subprocess announced | task 5 |
| 8 | Build Spec §5 "Launching chrome" | `DevToolsActivePort` may be a stale file from an earlier run of the same profile; read it only after the launched process is seen alive, or delete it first. Detect a held profile before launch from Chromium's `SingletonLock` symlink (`<host>-<pid>`) as well as by a quick exit of the launched process. `unverified` on this host: neither was run | task 5 |
| 9 | Design G9, handoff bug 1 | The bug is not a missing phrase (§6 finding 1). The estimate "a code fix this design does not supply" holds, but its size is a new adapter-level auth test plus a producer or removal for `blocking` | task 6 |
| 10 | Build Spec §4 | `UCBrowser.start()` in service mode installs no init script and never closes a context; the `inject` and 409 paths add no new rule for that. No contradiction, recorded so the 409 path is not given a `close()`/`add_init_script` shortcut | task 3 |

## 8. What was not measured

1. Default-duration `scroll_feed` was not timed; the measured run used `seconds=1`. Its default-duration cost remains arithmetic.
2. Missing-global Playwright errors and all return shapes were measured; behavior across other Playwright versions remains unverified.
3. The signed-out Grok page (§6 findings 2, 4): never loaded.
4. `inject` failure modes (CSP, a navigating page mid-evaluate). Build Spec §5 does not say what the route answers; **recommended**: 500 `control_failed` with the evaluate's message, not 409, because 409 means the window was configured without an engine.
5. Whether `SingletonLock` exists and is readable for installed Chrome here (§7 row 8).

## Verification

```bash
cd /home/rabbit/universal_controller
pixi run python - <<'PY'
import inspect, time
from playwright.sync_api import sync_playwright
from uc_browser.browser import UCBrowser

uc = UCBrowser()                       # never started: also checks the receiver claim (§4)
names = sorted(
    n for n, f in inspect.getmembers(UCBrowser, inspect.isfunction)
    if not n.startswith("_") and list(inspect.signature(f).parameters)[1:2] == ["page"]
)
print(len(names), "page-first public methods")        # expect 39
ARGS = {"detect": ("search",), "search": ("q",), "fill_form": ({},), "bind": ("chat",),
        "chat_send": ("hi",), "form_fill_uc": ({},), "dropdown_select": ("x",),
        "set_text": ("#a", "b"), "save_signature": ("chat",), "chat": ("hi",),
        "scan_action": (lambda p: None,)}
KW = {"scroll_feed": {"seconds": 1}, "chat": {"timeout_s": 2}}
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    page = b.new_page()
    page.set_content("<body>hello</body>")              # no extension: engine absent
    for n in names:
        t = time.time()
        try:
            out = repr(getattr(uc, n)(page, *ARGS.get(n, ()), **KW.get(n, {})))[:70]
        except Exception as e:
            out = f"RAISED {type(e).__module__}.{type(e).__name__}"
        print(f"{n:22s} {time.time() - t:6.2f}s  {out}")
    b.close()
PY
```

Decision from the output:

- 39 names, and every row's return and cost matching §2 (`scroll_feed(seconds=1)` ≈ 10.8 s): this doc moves from *read-derived* to *measured*. Edit the Status line and add the run date. The page has no extension, so this also confirms the unstarted-receiver claim for all 39 (§4).
- `ml_find_chat` and `chat` printing `RAISED playwright._impl._errors.Error` confirms §2 rows 38–39. `None` instead means `dom_classifier` raised `ImportError` in that environment; record which environment and keep §5 unchanged.
- Any row that differs: fix §2 and §5 together before building the 409 path, since §5 rests on the claim that the local body is the faithful answer.

For §6, after a signed-out capture exists: `grep -rn 'blocking' extension/src src` should print the producer, or `grep -n 'has_login_wall' -r uc_browser` should print no caller that treats `False` as "signed in".
