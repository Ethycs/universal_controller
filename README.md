# Universal Controller

Controlled-browser engine + Chrome MV3 extension + ML classifier for UI detection.

A reusable library for Python projects doing browser automation. Drop UC in as a git submodule and you get the full stack: a generic UI detection engine, a packaged Chrome extension exposing it to Playwright, an in-browser ML classifier for component recognition (chat windows, login forms, search bars, etc.), trained model weights, and the training pipeline that produced them.

## What's in here

```
universal_controller/
├── src/                       Engine — UI pattern detection, scan-diff workflows, action APIs
│   ├── core/                    Signature store, framework helpers
│   ├── detection/               UniversalController, scan-diff, pattern matching
│   ├── actions/                 chat, form, dropdown, modal, text-input APIs
│   ├── iframe/                  Cross-frame RPC for Shadow DOM / nested frames
│   ├── llm/                     Context extraction, heap scanner, state-machine verifier
│   └── ui/, styles.js           UC pattern library
│
├── extension/                 Chrome MV3 packaging
│   ├── manifest.json
│   ├── src/
│   │   ├── extension-entry.js     Imports engine, exposes window.__UC*
│   │   ├── background.js
│   │   └── storage-adapter.js     localStorage shim for GM_* APIs
│   ├── rollup.config.js         Bundles with GM_* + unsafeWindow shims
│   └── dist/uc-extension.js     Built bundle (regenerable)
│
├── ml/                        In-browser ML runtime (loaded via page.evaluate)
│   ├── rasterizer.js            DOM bbox → 32×32×4 spatial feature grid
│   └── dom_inference.js         Pure-JS forward pass (Scaler → PCA → Dense)
│
├── models/dom_classifier/     Trained weights
│   ├── raster_classifier.pkl    Stage 1: spatial MLP
│   ├── code_classifier.pkl      Stage 2: structural RandomForest
│   ├── weights.json             Raster weights for in-browser inference
│   └── labels.json              Component label set
│
└── scripts/                   Python training pipeline
    ├── scrape_storybooks.py     Crawl design systems → labelled raster dataset
    ├── train_dom_classifier.py  Stage 1 training
    ├── train_code_classifier.py Stage 2 training
    └── benchmark_detection.py   Live evaluation against benchmark sites
```

## Use as a submodule

In a downstream project:

```bash
git submodule add https://github.com/Ethycs/universal_controller.git ext/universal_controller
cd ext/universal_controller/extension
npm install && npx rollup -c          # builds dist/uc-extension.js
```

Then load the extension via Playwright with the [BC-010 launch pattern](https://github.com/Ethycs/event_tool/blob/main/docs/browser_attempts.md):

```python
import subprocess
subprocess.Popen([
    chromium_binary,
    "--remote-debugging-port=9223",
    "--user-data-dir=path/to/profile",
    f"--load-extension={path_to}/ext/universal_controller/extension",
    f"--disable-extensions-except={path_to}/ext/universal_controller/extension",
    "--no-first-run", "--no-default-browser-check",
    "about:blank",
])
# Connect via CDP from Playwright, then call window.__UC_detectAll() etc.
```

## Training the DOM classifier

```bash
cd ext/universal_controller
pixi run python scripts/scrape_storybooks.py            # builds data/training/storybook_samples.json
pixi run python scripts/train_dom_classifier.py        # → models/dom_classifier/raster_classifier.pkl
pixi run python scripts/train_code_classifier.py       # → models/dom_classifier/code_classifier.pkl
pixi run python scripts/benchmark_detection.py         # evaluates against live sites
```

UC doesn't ship a Python runtime — your project supplies Python (pixi, venv, poetry). Required deps are declared in [pyproject.toml](pyproject.toml) under `[project.optional-dependencies].training`.

## Engine surface (window.__UC*)

The MV3 extension exposes the engine on the page's window object:

```js
// Scan-diff (Cheat Engine style)
window.__UC_firstScan()                  // baseline DOM snapshot
// (interact with the page)
window.__UC_nextScan()                   // diff against baseline
window.__UC_autoDetect()                 // infer patterns from what changed

// Static detection (no interaction)
window.__UC_detect("search")             // structural + phrasal + semantic + behavioral
window.__UC_detectAll()                  // all pattern types at once

// Action APIs (post-detection)
window.__UC_chatSend(text)
window.__UC_formFill({email: ..., password: ...})
window.__UC_modalClose()

// ML classifier
window.__UC_loadWeights()                // load models/dom_classifier/weights.json
window.__UC_classify(element)            // → label + confidence

// LLM context
window.__UC_extractLLMContext()          // structured page summary
window.__UC_fullHeapScan()               // framework state inspection
```

## License

ISC
