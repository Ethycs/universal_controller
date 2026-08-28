# UC Generalization Benchmark

Measures whether the **generic** chat-detection engine (`src/` + the ML
classifier) actually generalizes across real chat UIs — and doesn't
hallucinate chats on pages that have none. This is the development loop
for the "works on any chat UI" thesis: change the engine, re-run, watch
the table.

## How it works

```
capture.py  ──►  fixtures/<site>/page.mhtml   (self-contained snapshot)
                 fixtures/<site>/meta.json    (oracles, probe dump)
                 fixtures/<site>/screenshot.png

run_bench.py ──► loads each .mhtml OFFLINE, injects extension/dist/uc-extension.js
                 via CDP into every frame, scores honest top-1 metrics,
                 compares against results/baseline.json
```

MHTML snapshots preserve full CSS fidelity (scrollability, `position:
fixed`, ARIA — everything the structural scorer weighs), and although
Chromium blocks *page* scripts inside MHTML, CDP-injected scripts run
fine. So the benchmark is deterministic, offline, login-free, and CI-able,
while testing against the real DOM of real sites.

## Ground rules

- **Site-specific selectors live only in `bench/sites.py`** (the oracles)
  and are stamped into fixtures as `data-uc-truth` attributes at capture
  time. They are the measuring stick. Nothing in `src/` or `uc_browser/`
  may reference them.
- **Scoring is top-1 and truth-blind.** UC's #1 answer must be the truth
  element (or its wrapper/inner editable). There is no "prefer the hit
  that matches truth" reranking — that mistake is what made the old
  `scripts/benchmark_detection.py` numbers meaningless.
- **Negatives count.** Search pages, form docs, and feeds must not clear
  the same gates `chat()` uses to engage. A detector that says "chat"
  everywhere scores 100% on positives and is useless.

## Metrics

Positive fixtures (kind `chat` / `widget`):

| stage | question |
|---|---|
| `input_top1` | is `__UC_findInputs()[0]` the real chat input? |
| `input_gate` | …and does it clear the `score >= 4` gate `chat()` requires? |
| `input_top5` | is the real input anywhere in the top 5? (recall) |
| `input_pipeline` | full product path: heuristic gate, else ML fallback (`conf > 0.5`) |
| `send_top1` | given the *true* input, is `__UC_findButtons()[0]` the real send button? |
| `container_top1` | is `detectAll('STRUCTURAL').chat[0]` the real message stream? (scored only when the fixture holds messages) |

Negative fixtures: `fp_input` (top input scored ≥ 4 — `chat()` would
engage) and `fp_chat` (chat pattern ≥ 0.5 BEHAVIORAL confidence).

## Usage

```bash
pixi run bench                # score all fixtures, print the table
pixi run bench-check          # exit 1 on any regression vs baseline
pixi run bench-baseline       # accept current results as the new baseline
pixi run bench-capture        # recapture live sites (headful; minutes)
pixi run python bench/make_synthetic.py   # regen the synthetic smoke fixtures
```

Adding a site: add a `Site` entry with oracle candidates to
`bench/sites.py`, run `bench/capture.py --sites <name>`, check the
capture log said which truth roles resolved (if none, read the `probe`
dump in the fixture's `meta.json` and refine the oracles), then rerun the
bench and update the baseline.

Fixtures are snapshots in time; recapture when a site ships a redesign —
that's a feature: old fixtures keep old layouts in the corpus, so the
engine is scored against *generations* of UIs, not just today's.

## Caveats

- Empty-conversation captures can't score `container_top1` (no messages
  exist). Capturing an *active* conversation needs a logged-in or
  interactive capture session (`--include-login`).
- Login-walled sites (`login_required=True`) are skipped by default; the
  landing page they show logged-out is not their chat UI.
- Live-mode scoring (full `chat()` round trip against the real site) is
  intentionally out of scope here — that's what `tests/test_chatgpt.py`
  and the opt-in Grok live test do.
