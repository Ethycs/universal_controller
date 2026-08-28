"""Shared plumbing for the benchmark: paths, bundle injection, truth
tagging, and the in-page scoring harness."""

from __future__ import annotations

import json
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_DIR = BENCH_DIR.parent
FIXTURES_DIR = BENCH_DIR / "fixtures"
RESULTS_DIR = BENCH_DIR / "results"
BUNDLE_PATH = REPO_DIR / "extension" / "dist" / "uc-extension.js"


def load_bundle() -> str:
    if not BUNDLE_PATH.exists():
        raise SystemExit(
            f"UC bundle not built: {BUNDLE_PATH}\n"
            "Run: cd extension && npm install && npx rollup -c"
        )
    return BUNDLE_PATH.read_text(encoding="utf-8")


def inject_bundle_all_frames(page, bundle_js: str) -> int:
    """Evaluate the UC bundle in every frame of the page. Returns the
    number of frames successfully injected. MHTML pages block their own
    scripts, but CDP evaluation still executes."""
    ok = 0
    for frame in page.frames:
        try:
            frame.evaluate(bundle_js)
            ok += 1
        except Exception:
            pass
    return ok


def read_meta(fixture_dir: Path) -> dict:
    return json.loads((fixture_dir / "meta.json").read_text(encoding="utf-8"))


def write_meta(fixture_dir: Path, meta: dict) -> None:
    (fixture_dir / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


# ── In-page JS: tag ground-truth elements from oracle candidates ──────
# Runs per-frame at capture time. Stamps data-uc-truth="<role>" so the
# annotation survives inside the MHTML snapshot.
TAG_TRUTH_JS = r"""(truth) => {
  const report = {};
  for (const [role, candidates] of Object.entries(truth)) {
    report[role] = null;
    for (const sel of candidates || []) {
      let el = null;
      try { el = document.querySelector(sel); } catch (e) { continue; }
      if (!el) continue;
      // Require visibility — a display:none template node is not truth.
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      el.setAttribute('data-uc-truth', role);
      report[role] = { selector: sel, tag: el.tagName,
                       rect: { w: Math.round(r.width), h: Math.round(r.height) } };
      break;
    }
  }
  return report;
}"""


# ── In-page JS: probe dump for oracle refinement ──────────────────────
PROBE_JS = r"""() => {
  const brief = (el) => ({
    tag: el.tagName,
    id: el.id || undefined,
    cls: (typeof el.className === 'string' ? el.className : '').slice(0, 80) || undefined,
    placeholder: el.getAttribute('placeholder') || undefined,
    ariaLabel: el.getAttribute('aria-label') || undefined,
    testid: el.getAttribute('data-testid') || undefined,
    ce: el.contentEditable === 'true' || undefined,
    visible: (() => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })(),
  });
  const inputs = [...document.querySelectorAll(
    'textarea, input[type="text"], input:not([type]), '
    + '[contenteditable]:not([contenteditable="false"]), [role="textbox"]'
  )].slice(0, 20).map(brief);
  const buttons = [...document.querySelectorAll('button, [role="button"]')]
    .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .slice(0, 30).map(brief);
  return { url: location.href, title: document.title.slice(0, 120), inputs, buttons };
}"""


# ── In-page JS: best-effort consent-banner dismissal ──────────────────
DISMISS_CONSENT_JS = r"""() => {
  const words = /^(accept|accept all|accept all cookies|allow all|allow all cookies|agree|i agree|i accept|confirm|got it|ok|reject all|agree and close)$/i;
  let clicked = 0;
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const t = (b.innerText || '').trim().toLowerCase();
    if (t && t.length < 30 && words.test(t)) { try { b.click(); clicked++; } catch (e) {} }
    if (clicked >= 2) break;
  }
  return clicked;
}"""


# ── In-page JS: the scoring harness ───────────────────────────────────
# Runs per-frame at bench time, after the UC bundle is injected. Scores
# honest top-1: UC's #1 answer either is the truth element or shares a
# containment relation with it (wrapper vs inner editable). No
# truth-aware reranking anywhere.
SCORE_JS = r"""(meta) => {
  const resolve = (sel) => { try { return sel ? document.querySelector(sel) : null; } catch (e) { return null; } };
  const rel = (a, b) => !!(a && b && (a === b || a.contains(b) || b.contains(a)));
  const truthEl = (role) => {
    let el = document.querySelector('[data-uc-truth="' + role + '"]');
    if (!el && meta.truth && meta.truth[role]) {
      for (const sel of meta.truth[role]) { el = resolve(sel); if (el) break; }
    }
    return el;
  };

  const out = { frameUrl: location.href, hasTruth: {}, stages: {}, detail: {} };
  if (typeof window.__UC_findInputs !== 'function') {
    out.error = 'UC not injected in this frame';
    return out;
  }

  const tIn = truthEl('input');
  const tSend = truthEl('send');
  const tMsgs = truthEl('messages');
  out.hasTruth = { input: !!tIn, send: !!tSend, messages: !!tMsgs };

  // S1 — input discovery (top-1 and top-5 recall)
  let inputs = [];
  try { inputs = window.__UC_findInputs() || []; } catch (e) { out.error = String(e); }
  out.detail.inputs = inputs.slice(0, 3).map(i => ({ selector: i.selector, score: i.score }));
  const topInputEl = inputs.length ? resolve(inputs[0].selector) : null;
  const topScore = inputs.length ? (inputs[0].score || 0) : 0;
  if (tIn) {
    out.stages.input_top1 = rel(topInputEl, tIn);
    // The gate chat() actually applies: heuristic wins only at score >= 4.
    out.stages.input_gate = out.stages.input_top1 && topScore >= 4;
    out.stages.input_top5 = inputs.slice(0, 5).some(i => rel(resolve(i.selector), tIn));
  }

  // S2 — send-button discovery, isolated: given the TRUE input, does UC
  // find the true send button? (Not conditioned on S1 passing.)
  if (tIn && tSend) {
    tIn.setAttribute('data-uc-bench-anchor', '1');
    let btns = [];
    try { btns = window.__UC_findButtons('[data-uc-bench-anchor="1"]') || []; } catch (e) {}
    tIn.removeAttribute('data-uc-bench-anchor');
    out.detail.buttons = btns.slice(0, 3).map(b => ({ selector: b.selector, score: b.score, label: (b.label || '').slice(0, 40) }));
    const topBtnEl = btns.length ? resolve(btns[0].selector) : null;
    out.stages.send_top1 = rel(topBtnEl, tSend);
  }

  // S3 — chat-container detection (only scorable when the fixture holds
  // a message stream).
  let det = null;
  try { det = window.__UC_detectAll('STRUCTURAL'); } catch (e) {}
  const chatHits = (det && det.chat) || [];
  out.detail.chat = chatHits.slice(0, 3).map(h => ({ selector: h.selector, confidence: h.confidence }));
  if (tMsgs) {
    const topC = chatHits.length ? resolve(chatHits[0].selector) : null;
    out.stages.container_top1 = rel(topC, tMsgs);
  }

  // Negative-control metrics (recorded for every frame; judged per-site).
  out.neg = {
    top_input_score: topScore,
    top_input_selector: inputs.length ? inputs[0].selector : null,
    chat_behavioral: (() => {
      // detectAll above ran at STRUCTURAL (thr 0.2); a false positive is
      // only "engaged" at the BEHAVIORAL threshold chat() would trust.
      const hs = chatHits.filter(h => h.confidence >= 0.5);
      return hs.length ? hs[0].confidence : 0;
    })(),
  };
  return out;
}"""


# ── In-page JS: candidate discovery for the ML fallback stage ─────────
ML_CANDIDATES_JS = "window.__UC_findChatCandidates ? window.__UC_findChatCandidates() : []"
ML_CLEANUP_JS = "window.__UC_clearChatCandidates && window.__UC_clearChatCandidates()"
