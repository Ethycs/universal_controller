# Universal Controller

**Turn any LLM chat UI on the web into a callable Python API.**

Drop UC in as a git submodule. Get a controlled Chromium that finds the chat input, identifies the message stream, and lets you do this from Python:

```python
page.evaluate("window.__UC_chatSend('Summarize the last paper I sent.')")
messages = page.evaluate("window.__UC_chatGetMessages()")
```

Works on ChatGPT, Claude.ai, Gemini, Perplexity, Pi, and arbitrary chat widgets — without bespoke selectors per site. UC detects the chat structurally (DOM patterns + ML classifier), so it's resilient to layout changes that would break CSS-selector-based scrapers.

## Why this exists

Every LLM provider gives you their own SDK. But people want to:

- **Orchestrate multiple providers** — send the same prompt to ChatGPT, Claude, Gemini, compare responses
- **Use accounts that have no API** — most consumer LLM accounts (free tier ChatGPT, Pi, niche providers) don't expose API access at all
- **Drive chat UIs in production research** — automated red-teaming, eval pipelines, agent-of-agents systems
- **Use the *same* code against any chat UI** — no per-site selector maintenance

UC binds to chat UIs the way browsers bind to forms: structurally, via patterns, with an ML fallback for ambiguous DOM.

## How it works

```
                Python (your code)
                       │
                       │  page.evaluate("window.__UC_chatSend(...)")
                       ▼
       ┌─────────────────────────────────────────┐
       │  Chromium + UC extension                │
       │                                         │
       │   ┌──────────────────────────────┐     │
       │   │  detection/  + ml/+models/   │ ──► identify chat input,
       │   │  (find the chat structurally) │     message stream, send button
       │   └──────────────────────────────┘     │
       │                                         │
       │   ┌──────────────────────────────┐     │
       │   │  actions/chat-api.js          │ ──► chatSend, chatGetMessages,
       │   │  (the API surface)            │     chatOnMessage
       │   └──────────────────────────────┘     │
       │                                         │
       │   ┌──────────────────────────────┐     │
       │   │  llm/state-machine.js         │ ──► verify the message went,
       │   │  (verification)               │     wait for response
       │   └──────────────────────────────┘     │
       │                                         │
       │   ┌──────────────────────────────┐     │
       │   │  iframe/                      │ ──► reach chats inside iframes
       │   │  (cross-frame RPC)            │     (Intercom, embedded widgets)
       │   └──────────────────────────────┘     │
       └─────────────────────────────────────────┘
                       │
                       ▼
                lu.ma, ChatGPT, Pi,
                Claude.ai, etc. — any
                page with a chat UI
```

Every module is in service of the chat-to-API mission:

| Module | Role |
|---|---|
| `src/detection/` | Find the chat input, message stream, and send button on an unknown page |
| `ml/` + `models/dom_classifier/` | Classify DOM regions when structure alone is ambiguous (`chat_input` vs `search` vs `form_field`) |
| `src/actions/chat-api.js` | The API itself: `chatSend`, `chatGetMessages`, `chatOnMessage` |
| `src/iframe/` | Cross-frame RPC for chats embedded in iframes |
| `src/llm/heap-scanner.js`, `state-machine.js` | Verify the message was sent and a response actually came back |
| `src/llm/context-extractor.js` | Structured conversation snapshot for downstream LLMs |
| `extension/` | MV3 packaging so the above runs in real Chromium with `window.__UC_*` callable from Playwright |
| `scripts/` | Train the DOM classifier so `chat_input` labels stay accurate across new UI frameworks |

---

## Getting started

### Prerequisites

- Python 3.10+
- Node.js 20+
- Playwright (`pip install playwright && playwright install chromium`)
- A package manager — examples below use [pixi](https://pixi.sh) but `pip + venv` works fine

### 1. Install UC as a submodule

In your project:

```bash
git submodule add https://github.com/Ethycs/universal_controller.git ext/universal_controller
git commit -m "Add universal_controller submodule"
```

### 2. Build the Chrome extension

```bash
cd ext/universal_controller/extension
npm install
npx rollup -c
```

This produces `ext/universal_controller/extension/dist/uc-extension.js` (~225 KB), which is what gets loaded into Chromium.

### 3. Drive a chat from Python

UC requires a specific browser launch pattern — Playwright's standard `launch_persistent_context` doesn't load extensions reliably with branded Chrome, and bundled Chromium with `--remote-debugging-pipe` crashes when extensions are loaded. Instead, launch Chromium yourself and connect Playwright via CDP:

```python
import subprocess
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

UC = Path("ext/universal_controller").resolve()
CHROMIUM = Path.home() / "AppData/Local/ms-playwright/chromium-1208/chrome-win64/chrome.exe"
EXTENSION = UC / "extension"
PROFILE = Path("data/.uc_profile").resolve()
PROFILE.mkdir(parents=True, exist_ok=True)

# Launch Chromium with UC extension loaded
proc = subprocess.Popen([
    str(CHROMIUM),
    "--remote-debugging-port=9222",
    f"--user-data-dir={PROFILE}",
    f"--load-extension={EXTENSION}",
    f"--disable-extensions-except={EXTENSION}",
    "--no-first-run",
    "--no-default-browser-check",
    "about:blank",
])
time.sleep(3)

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]

    # Wait for UC's service worker to register
    if not context.service_workers:
        context.wait_for_event("serviceworker", timeout=15000)

    # Open a chat site
    page = context.new_page()
    page.goto("https://chatgpt.com/")
    page.wait_for_timeout(3000)

    # Detect the chat structurally + via ML
    page.evaluate("window.__UC_detectAll()")

    # Send a message via the universal API
    page.evaluate("window.__UC_chatSend('Hello from Python!')")

    # Wait for response, then read the conversation
    page.wait_for_timeout(8000)
    messages = page.evaluate("window.__UC_chatGetMessages()")
    for m in messages:
        print(f"[{m.get('role')}] {m.get('text', '')[:200]}")

    browser.close()

proc.terminate()
```

That same code works on `claude.ai`, `gemini.google.com`, `pi.ai`, etc. — the detection adapts.

### 4. Authenticate (optional)

For accounts that need a logged-in session: open Chrome manually, sign into the site once, then either:

- Reuse the same `--user-data-dir` (Chromium remembers cookies between launches), **or**
- Export cookies from your real Chrome via [rookiepy](https://github.com/thewh1teagle/rookie) and `context.add_cookies(...)`

**Google SSO note:** Google's OAuth iframe blocks Chromium when `--no-sandbox` is in the launch args. Drop that flag if you need to log in via Google.

---

## API surface (`window.__UC_*`)

The MV3 extension exposes these on the page's window:

### Chat API

```js
window.__UC_chatSend(text)             // → Promise<{success, method}>
window.__UC_chatGetMessages()          // → [{role, text, timestamp}]
window.__UC_chatOnMessage(handler)     // subscribe to new-message events
```

### Detection

```js
window.__UC_firstScan()                // baseline DOM snapshot
window.__UC_nextScan()                 // diff against baseline
window.__UC_autoDetect()               // infer patterns from what changed
window.__UC_detect("chat")             // structural + phrasal + semantic
window.__UC_detectAll()                // all pattern types at once
```

### ML classifier

```js
window.__UC_loadWeights()              // load models/dom_classifier/weights.json
window.__UC_classify(element)          // → {label, confidence, scores}
```

### Auxiliary action APIs (forms, modals, dropdowns)

```js
window.__UC_formFill({email, password})
window.__UC_modalClose()
window.__UC_dropdownSelect(label)
```

### LLM reasoning helpers

```js
window.__UC_extractLLMContext()        // structured page summary
window.__UC_fullHeapScan()             // framework state inspection
```

---

## Training the classifier (optional)

UC ships with pre-trained `models/dom_classifier/{raster,code}_classifier.pkl`. Retrain only when:

- New UI framework appears that current models misclassify (e.g., a brand-new chat widget)
- You want to add a label class beyond the default 8

```bash
cd ext/universal_controller
pixi run python scripts/scrape_storybooks.py            # → data/training/storybook_samples.json
pixi run python scripts/train_dom_classifier.py        # → models/dom_classifier/raster_classifier.pkl
pixi run python scripts/train_code_classifier.py       # → models/dom_classifier/code_classifier.pkl
pixi run python scripts/benchmark_detection.py         # eval against ChatGPT, Bing, Google AI Studio
```

UC doesn't ship a Python runtime — your project supplies Python. Required deps are declared in `pyproject.toml` under `[project.optional-dependencies].training`.

---

## Layout

```
universal_controller/
├── src/                       Engine
│   ├── core/                    Signature store, framework helpers
│   ├── detection/               Pattern detection, scan-diff workflows
│   ├── actions/                 chatSend, chatGetMessages, formFill, modalClose, etc.
│   ├── iframe/                  Cross-frame RPC
│   ├── llm/                     Context extraction, heap scanner, state-machine
│   └── ui/, styles.js           Pattern library
│
├── extension/                 Chrome MV3 packaging
│   ├── manifest.json
│   ├── src/extension-entry.js     Imports engine, exposes window.__UC_*
│   ├── rollup.config.js           Bundles with GM_*/unsafeWindow shims
│   └── dist/uc-extension.js       Built bundle (regenerable; gitignored)
│
├── ml/                        In-browser ML runtime
│   ├── rasterizer.js              DOM bbox → 32×32×4 spatial feature grid
│   └── dom_inference.js           Pure-JS forward pass (Scaler → PCA → Dense)
│
├── models/dom_classifier/     Trained weights
│   ├── raster_classifier.pkl    Stage 1: spatial MLP
│   ├── code_classifier.pkl      Stage 2: structural RandomForest
│   ├── weights.json             Raster weights for in-browser inference
│   └── labels.json              Component label set (chat_input, search, ...)
│
├── scripts/                   Python training pipeline
│   ├── scrape_storybooks.py     Crawl design systems → labelled raster dataset
│   ├── train_dom_classifier.py  Stage 1 training
│   ├── train_code_classifier.py Stage 2 training
│   └── benchmark_detection.py   Live evaluation against benchmark sites
│
├── pyproject.toml             Declares training-script Python deps
└── README.md
```

---

## Caveats

- **First run is exploratory.** UC's detection works on most chat UIs out of the box, but exotic widgets may need a hint — call `__UC_detect("chat")` with a name argument to narrow the search.
- **Verification is best-effort.** `chatSend` returns when it has *submitted* the message, not when the LLM has finished responding. Use `chatOnMessage` or poll `chatGetMessages` for completion.
- **Native messaging** (e.g., KeePassXC-Browser auto-fill) requires the host registered under `HKCU\Software\Chromium\NativeMessagingHosts`, not Chrome's key.
- **MV3 service worker timing.** After launching Chromium, wait for the `serviceworker` event before calling `__UC_*` functions.

## License

ISC
