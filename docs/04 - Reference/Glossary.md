# Glossary

**Status**: Living Document
**Date**: 2026-11-23
**Applies to**: every doc under `docs/`; the service terms are defined in `docs/00 - Theory/00 - Windows, Transport and Control.md` §0 and repeated here for lookup

Terms the docs use with one fixed meaning. Where the code uses a different word for the same thing, the code's word is in the second column.

## Browser and service

| Term | In code | Means |
|---|---|---|
| **engine** | `src/`, built to `extension/dist/uc-extension.js`; plus its Python half in `uc_browser/` | The site-agnostic code that detects and actuates UI inside a page (`window.__UC_*`), with the classifier, adapters and wait loops that drive it |
| **engine placement** | *(not implemented)* | How the engine gets into a page: as an extension at launch, or injected over CDP at a page's first control call |
| **controller** | `UCBrowser` (`uc_browser/browser.py`) | The Python class that today launches a browser and wraps the engine as methods |
| **launch mode** | `BrowserMode` | How a `UCBrowser` gets its browser today: `CHROMIUM_EXT` (Playwright's Chromium with the engine as an extension), `NATIVE_CDP` (installed Google Chrome as a subprocess, attached over CDP, no engine), `CHROME`, `HEADLESS` |
| **profile** | `user_data_dir` | A browser's on-disk identity: cookies, storage, extensions. One process may hold it at a time |
| **window** | *(not implemented)* | One browser process with one profile, owned by the service. The unit of isolation |
| **target** | CDP `targetId` | One page inside a window. The id is the same string for every party attached to the window |
| **technique** | — | One way of getting a site's content: a feed, an API, an in-page request, page scripting, an engine control, an adapter, an application's files. No technique is the default (Design §1.1) |
| **pathway** | — | The ordered list of techniques one source or site uses, falling to the next when one fails |
| **need** | — | What a technique requires of a browser: *none*, the *identity* only, a live *page*, or the *engine* (Theory §1.4) |
| **offer** | *(not implemented)* | One thing the service provides on a window, matching one need: attach, control, fetch, cookies, snapshot, session |
| **CDP** | `connect_over_cdp` | Chrome DevTools Protocol: the wire protocol Chromium-family browsers serve for remote control. Firefox and WebKit do not serve it |
| **attachment** | `chromium.connect_over_cdp(url)` | One CDP connection to a window, with the thread that drives it. Costs milliseconds to make; launches nothing. The unit of concurrency, and the means behind the *attach* offer |
| **fetch** | `page.evaluate` of a `fetch`; `window.__UC_apiShim` | A request made from inside a page of a window, so it carries that window's session. Needs no page in the caller |
| **control** | `UCBrowser` methods that take a `page` | An operation that needs the engine: `detect_all`, `dismiss_cookies`, `chat`, … |
| **control plane** | *(not implemented)* | The service's HTTP API: controls on a target, and the other offers that are not attach |
| **client** / **consumer** | — | A *client* is one registered user of a window (in practice one `UCBrowser`); a process may hold several. A *consumer* is the project or feature a client belongs to (`event_tool`'s harvest, the litellm proxy) |
| **owner** | *(not implemented)* | The one service thread per window that launched its browser and creates and closes its pages |
| **control worker** | *(not implemented)* | A pooled service thread with its own attachment, running short controls |
| **session** | `session_id`, `session_key` | A conversation with a site that a caller names: one page, one attachment, one entry in the map from session id to conversation URL |
| **lease** | *(not implemented)* | The service's record of who is responsible for closing a page: a client or a session |
| **revocation** | *(not implemented)* | Releasing a client from outside, by label, so the service closes its pages even though the client is alive |
| **sweep** | *(not implemented)* | Closing pages nobody holds once a window has no registered clients |
| **service** | *(not implemented)* | The one long-running process that owns every window, serves their CDP endpoints and hosts the control plane and site routes |

## Sites and measurement

| Term | In code | Means |
|---|---|---|
| **adapter** | `uc_browser/sites/grok*.py` | A hand-tuned site client. An earned exception (`docs/01 - Design/00 - Chat-Window-to-API Design.md` §1) |
| **generic path** | `uc_browser/sites/generic.py` → `UCBrowser.chat()` | Driving a site with the engine alone |
| **signature** | `src/core/` | A cached resolution of a page's structure. A cache, never load-bearing |
| **oracle** | `bench/sites.py` | Ground-truth selectors used only to score the engine |
| **gate** | `score >= 4` | The engagement threshold an input candidate must clear |
| **login wall** | `has_login_wall` | A page state where the site demands sign-in before the content |

## Example

A harvest in `event_tool` reads its feed sources with no browser at all (**technique** needing *none*) and never contacts the **service** for them. For a calendar source a worker becomes a **client** of the `harvest` **window**: it registers under its job's label, makes an **attachment**, asks for a page, and receives a **target** id. It scripts the page itself and asks the service to run `detect_all` on that target (**control**); a **control worker** runs it. If a bot check appears, the source's **pathway** falls to the `native` window. If the job is abandoned, the job runner asks for **revocation** by its label and the service closes the pages. Meanwhile the Grok proxy, which holds no browser library, names a **session** and the service runs its send on that session's own attachment; and an export script reads the same account through **fetch**. None waits on another.
