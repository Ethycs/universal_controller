# What a controlled browser shares is an identity held by one process. The ways of reaching a site through it are many and are composed per pathway; none of them is the browser's one interface. Its unit of isolation is the browser process, not the tab and not the client.

**Status**: DRAFT (theory; the service it argues for is not implemented)
**Date**: 2026-11-23
**Applies to**: `uc_browser/browser.py` (`UCBrowser`), every runtime browser owner in `uc_browser/` (`sites/grok.py`, `sites/generic.py`, `health.py`), and consumers such as `event_tool`
**Depends on**: `docs/03 - Architecture/00 - System Overview.md` (design axiom 4), `docs/08 - Analysis/00 - Service Prototype Measurements.md` (every claim marked *measured*; row numbers refer to its §3)

**Executive summary:** Today each consumer of UC constructs its own `UCBrowser`, which launches its own browser. Axiom 4 says a profile has one writer, so two consumers that need the same signed-in identity cannot both run. This document derives the fix from five facts. (1) A profile belongs to exactly one process, so sharing an identity means sharing a process, and a shared process that outlives its users is a service. (2) Everything attached to one browser process sees all of it, so the process is the only real isolation boundary. (3) Access is plural: sites are reached by a composite of techniques that differ in what they need — nothing from a browser, the identity only, a live page, or UC's engine — and a pathway falls from one to the next. An interface built around any one of them taxes all the others, so each need is served separately and none is required. (4) Work on a browser is concurrent per attachment, so the service is itself just another client of its windows. (5) The engine is placed in a page, at launch or on demand; it is not a property of the browser.

---

## 0. Preliminaries / definitions

| Term | Meaning here |
|---|---|
| **profile** *P* | A browser's persistent identity on disk: cookies, storage, extension state. |
| **window** *W* | One running browser process bound to one profile: *W = (P, b)*, where *b* is the browser binary (Playwright's Chromium, or installed Google Chrome). |
| **target** *t* | One page in a window. Named by its CDP target id, a string every attached party reads identically. |
| **attachment** *a* | One CDP connection to a window, with the thread that drives it. |
| **client** *c* | A process holding one or more attachments. The service is a client of its own windows. |
| **transport operation** τ | An operation on a target expressible in CDP alone: navigate, evaluate, click, read, intercept. |
| **engine** | UC's in-page code (`window.__UC_*`) together with its Python half: classifier, adapters, wait loops, signatures. |
| **control** κ | An operation on a target that needs the engine. |
| **scope** | The set of things one party's actions can observe or alter. |
| **lease** | The association of a target with the party responsible for closing it. |

"Service" below means only: a process that holds windows open independently of any client. No API is implied until §1.4.

---

## 1. Objects and operations

### 1.1 The single-writer law forces sharing into one process

A profile directory may be held by one browser process at a time (System Overview, axiom 4: *one writer per browser profile*). Let *need(c)* be the identity a client requires. If two clients have *need(c₁) = need(c₂) = P*, there are three options:

1. Run them at different times. This is today's arrangement and it is fragile: each consumer assumes it is alone.
2. Copy the identity into a second profile. Sign-ins then diverge, and sites that refuse automated sign-in must be re-seeded per copy.
3. Let both use the one process that holds *P*.

Only option 3 keeps a single identity single. It requires the process holding *P* to exist before the first client and survive the last, which is the definition of a service given above. **The service is not a convenience; it is what the single-writer law leaves once two consumers share an identity.**

### 1.2 Scope is the window

A CDP attachment is to the browser, not to a tab. *Measured (rows 3, 8, 9, 10, 19):* each attachment saw every page in the window, including pages other clients created; an init script added through one attachment ran inside pages created through another; cookies were common to all. Across two windows, none of these crossed.

So within a window there is one scope and all attachments are in it. Between windows scopes are disjoint. Tabs give no isolation and client identity gives none. Stated as a rule:

> Two clients may share a window **iff** they need the same profile **and** can tolerate each other's context-level effects. Otherwise they need two windows.

The number of windows a deployment needs is therefore the number of distinct (profile, tolerance) classes among its consumers, and not the number of consumers.

The same fact has a useful side: whatever must hold for *every* page — the API shim, stealth — is installed once on the window's context and reaches pages no matter who creates them.

### 1.3 The target id is the shared name

Two parties attached to one window hold different objects for the same page. They need a name both can produce without exchanging objects. The CDP target id is that name: either side obtains it from its own handle (`Target.getTargetInfo`), and it is stable for the page's life. *Measured (rows 4, 6, 7, 18):* a service located a client-created page by target id and operated on it; a client located a service-created page the same way; one attachment located another's busy page.

This is what lets several means of access address one page (§2).

### 1.4 Access is plural; techniques differ in what they need

Neither UC nor its consumers read a site one way. The design doc lists fifteen techniques in use (Design §1.1). Classify each by the least it needs from a browser:

| Need | The technique requires | Examples in use |
|---|---|---|
| **none** | no browser | a published feed, a platform's JSON, an official API, an application's files |
| **identity** | the signed-in session, and nothing of the caller's in the browser | a request made from inside a page; cookies carried to or from another browser; a site adapter's operations |
| **page** | a live page the caller scripts or observes | direct page scripting; watching the page's network traffic; snapshots |
| **engine** | UC's engine in the page | detection and actuation controls; adapters built on them |

A **pathway** is an ordered list of techniques, and it falls to the next when one fails: a feed probe before any browser, bundled Chromium before installed Chrome, a reply read by in-page request before a reply read off the DOM. The order is each pathway's own, set by what its author found cheap and reliable for that site. There is no global ranking: a new site is tried without a browser first, while a chat site falls *from* its adapter *to* a conventional API.

Two properties of the classes decide how each is served.

- **Page scripting is open-ended.** `event_tool` alone makes about 110 direct Playwright page calls of 13 kinds. An API over it would re-implement the browser's own protocol. So the *page* need is served by that protocol.
- **Everything else is closed and small.** `UCBrowser` has 39 public methods that take a page; none touches browser-level state; most are one call into `window.__UC_*`. An in-page request is one operation. An adapter has about a dozen. These are served by name, with arguments.

What does not follow is that one of these is *the* interface. Suppose the page need were made the way in. Then a technique that needs only the identity — a request from inside a page — must first acquire a page and a browser library in the caller; a browser that serves no CDP cannot be used at all; and a technique that needs nothing is routed past a browser for no reason. The same holds for any other choice. So:

> Serve each need by its own means: nothing for *none*; named operations for *identity* and *engine*; the browser's own protocol for *page*. Require none of them. A pathway composes them on one identity because they are offered on one window.

### 1.5 Concurrency is per attachment

A browser serves each CDP connection independently. *Measured (rows 14–18):* while one attachment thread sat in a six-second polling loop on its page, a second attachment thread's calls completed in a median of 2 ms, and the second thread read and wrote the first thread's page.

Two things follow.

1. **A long operation occupies an attachment, not a window.** How many things a window can do at once is the number of attachments on it. This holds for the service as much as for anyone: to run a long control without stalling short ones, the service takes another attachment.
2. **A busy page is still reachable.** Whatever is waiting on a page through one attachment cannot stop another attachment acting on it. An operation that must interrupt another (stopping a generation mid-reply) is expressible.

Combined with axiom 4's other half — one sync Playwright instance per thread — the unit of concurrency in an implementation is *(thread, Playwright instance, attachment)*, taken together. Each costs a driver process (*measured, row 24:* about 80 MB), so they are pooled, not spawned freely.

### 1.6 The engine is placed, not built in

A control needs the engine present in the page. There are two ways for it to get there:

- **at launch**, as a browser extension, which requires a browser that will load one;
- **on demand**, by evaluating the engine bundle in the page over CDP. *Measured (rows 21–23):* in installed Google Chrome, which runs without the extension, the injected bundle became ready and `detect_all` and `set_text` worked.

So whether a window has controls is not a property of its browser. It is a choice of *when* the engine enters the page: before any site script (extension, or injection as an init script) or only once someone asks for a control. The second matters where a page should stay untouched until it has loaded, such as a page behind a bot check.

---

## 2. The model in one statement

For a window *W = (P, b)* with targets *T(W)* and attachments *A(W)*:

```
pathway:      an ordered list of techniques; each needs one of {none, identity, page, engine}
none:         performed by the caller alone; W is not involved
identity:     request(W, r), cookies(W)   performed by the service inside W;
                            the caller holds nothing in the browser
page:         τ(t)          performed through any a ∈ A(W), for any t ∈ T(W),
                            where the browser serves a remote protocol
engine:       κ(t, args)    performed by the service on t, named by target id,
                            through one of the service's own attachments;
                            requires the engine in t, placed at launch or on demand
scope:        scope(a) = W  for every a ∈ A(W)        (nothing narrower exists)
concurrency:  operations through different attachments do not wait on each other
lifetime:     t lives until its lease-holder releases it, is observed dead,
              or is revoked from outside
```

Consequences:

1. **Isolation is bought per window.** Since `scope(a) = W`, no API design can isolate two clients inside one window. Anything that must not see or affect a neighbour goes in another window, at a cost of one browser process (*measured, row 12:* about 200 MB near idle).
2. **Concurrency is bought per attachment**, at a cost of one driver process each. Isolation and concurrency are separate purchases: more attachments do not isolate, and more windows are not needed for parallelism.
3. **Lifetime cannot be delegated to clients.** *Measured (row 11):* a client that disconnected left its pages open. *Observed in the field (Analysis §5):* a browser whose owning job was abandoned ran for four days and later jobs attached to it unawares. With clients able to die or hang at any point, only a party that outlives them can guarantee a page is ever closed. That party is the service, and the lease is how it knows whose page it is.
4. **Revocation works from outside.** Because a busy page is reachable through another attachment (§1.5), the service can close a page out from under a client that is stuck. The stuck client's next call fails, which is how a thread that cannot be killed gets unwound.
5. **Transport and control do not contend for the page.** *Measured (row 4):* the service typed into a page while the client that created it stayed attached, and the client read the result.

---

## 3. Reduction to concrete families

### 3.1 Windows

| Browser | Engine placement | Transport | Control | Used for |
|---|---|---|---|---|
| Playwright's Chromium | extension, at launch | yes | yes | everything that detects or actuates through the engine |
| installed Google Chrome | injected on demand | yes | yes, from the first control call on a page | sites that block automated Chromium (Cloudflare) |

Both rows are one kind of object. They differ in binary and in when the engine arrives.

### 3.2 Client families

| Family | Example | Process | Its attachment | Needs it uses |
|---|---|---|---|---|
| In-service session | grok adapter, generic client, health probe | the service | one per session, from a bounded pool | identity, page, engine — in-process |
| Library client | `event_tool` harvest through `UCBrowser` | the consumer | one per `UCBrowser` | page over its attachment; engine and identity over the API |
| HTTP-only caller | the litellm proxy, the Grok MCP tools, an export script | the consumer | none | identity, and engine on pages the service makes |
| Raw client | a one-off script, a debugger | any | its own | page only |
| Not a client | `event_tool`'s feed and API sources | the consumer | — | none |

The families differ in which needs they draw on, in whether a process boundary sits between them and the service, and in who holds the lease: a session, a registered client, or nobody.

### 3.3 Assigning consumers to windows

Apply the rule of §1.2. For the known consumers:

| Consumer | Needs identity | Tolerates neighbours? | Window |
|---|---|---|---|
| `event_tool` harvest, standard sources | harvest sign-ins (Instagram, event sites) | yes, among its own workers | `harvest` |
| `event_tool` harvest, Cloudflare-gated sources | none persistent | yes | `native` |
| grok adapter (proxy, MCP tools, export) | the Grok account | no | `grok` |

Three classes, three windows. The harvest's parallel workers are one class and share one window as tabs. Sessions of the grok adapter are one class and share one window, each on its own attachment.

---

## 4. What this predicts, and how it would be falsified

| Prediction | Falsified if |
|---|---|
| Attaching costs milliseconds and is independent of profile size | attach time grows with the profile or with open tabs |
| A control through the service costs one local round trip more than in-process (*measured:* 0.03–0.08 s per call) | per-call overhead approaches the page-load times it accompanies |
| Profile-lock failures vanish, because exactly one process ever opens each profile | any consumer still launches a browser on a served profile |
| Two clients in one window will interfere through context-level hooks | *already observed* — this is the model, not a risk to it |
| Nothing a client does in one window is visible in another | a cookie, script or page crosses windows |
| A long control delays nothing on other attachments (*measured* with a polling loop, row 15) | a real `chat` against a model slows short controls on the same window by more than its own page load |
| Closing a page from outside unwinds a client stuck on it | the stuck call does not fail (`unverified`) |

The fifth row is the one the isolation argument rests on, and the sixth the one the concurrency argument rests on. If either fails, the corresponding section must be re-derived.

---

## Minimal starter set

The smallest thing that exercises every claim above:

1. One window that the service launches and whose CDP endpoint it publishes.
2. Attach from a second process (*page*).
3. One generic control route: (window, target id, control name, arguments) → result, served through a pool of the service's own attachments (*engine*).
4. One in-page request route: (window, request) → response (*identity*), exercised by a caller with no browser library.
5. A lease per service-created page, a reaper that closes pages whose holder is gone, and revocation by the holder's label.

A second window, another browser binary, in-service sessions and cookie import add reach, not new theory.

## Verification

```bash
grep -c -E '^    def [a-z][a-z_]*\(' uc_browser/browser.py     # public methods on UCBrowser
grep -n 'self\._context' uc_browser/browser.py                  # where browser-level state is touched
```

Every line the second command prints should fall inside a launch, close or page-opening method. If a page-taking control turns out to use `self._context`, §1.4's claim that controls are target-scoped is false for that control, and it cannot be served by target id as specified.
