"""The reverser — turn a found chat window into reusable intelligence.

This is the "reverse-engineer the sample into a signature" stage of the
Signature Lab (the AV analogy's malware-analysis tier). Given a page
where the engine found a composer, it extracts:

  * a POSITIVE labeled example — the composer's DOM feature vector,
    labeled ``chat_input`` (the label is free: the detector + vendor
    signature already told us it's a chat);
  * HARD NEGATIVES — the other inputs on the same page that did NOT win
    (search boxes, email fields, etc.), labeled by their own weak class.
    Same-page negatives are the examples the classifier most needs — they
    are what the target/`#email-address` false-near-miss was made of;
  * a SIGNATURE — selector + vendor + frame, for warm-start autobind.

The emitted rows accumulate in ``data/training/reversed_chats.jsonl``,
which the classifier retrains from (see uc_browser.loop). Self-
supervising: the scanner labels its own training data, so the corpus
grows every time the loop runs — that is what strengthens the scanner.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from uc_browser._paths import _SUBMODULE_ROOT
from uc_browser.dom_classifier import CODE_FEATURE_NAMES

logger = logging.getLogger("uc_browser.reverser")

CORPUS_PATH = _SUBMODULE_ROOT / "data" / "training" / "reversed_chats.jsonl"


@dataclass
class ReversedChat:
    source_url: str
    selector: str
    frame_url: str
    vendor: Optional[str]
    score: float
    features: dict = field(default_factory=dict)   # CODE_FEATURE_NAMES -> value
    negatives: list = field(default_factory=list)  # [{selector, features, label}]
    at: float = 0.0

    def to_rows(self) -> list[dict]:
        """Flatten into labeled training rows (1 positive + N negatives)."""
        rows = [{
            "label": "chat_input", "source": self.source_url,
            "selector": self.selector, "vendor": self.vendor,
            "score": self.score, "features": self.features, "at": self.at,
        }]
        for neg in self.negatives:
            rows.append({
                "label": neg.get("label", "non_chat"),
                "source": self.source_url, "selector": neg.get("selector"),
                "vendor": self.vendor, "features": neg.get("features", {}),
                "at": self.at,
            })
        return rows


def _features(frame, selector: str) -> Optional[dict]:
    """CODE_FEATURE_NAMES vector for one element, via the bundle helper."""
    try:
        has = frame.evaluate(
            "() => typeof window.__UC_extractCodeFeatures === 'function'")
        if not has:
            return None
        return frame.evaluate("(s) => window.__UC_extractCodeFeatures(s)",
                              selector)
    except Exception:
        return None


def _weak_label(feat: dict) -> str:
    """Cheap heuristic label for a same-page non-winning input, so the
    negative is typed (search vs form vs generic) rather than just 'not
    chat'. The classifier learns the boundary from these."""
    if not feat:
        return "non_chat"
    if feat.get("role_search") or feat.get("kw_search") or feat.get("has_search_text"):
        return "search"
    if feat.get("kw_login") or feat.get("has_login_text"):
        return "login_form"
    if feat.get("n_input", 0) >= 2 or feat.get("kw_form"):
        return "form_field"
    return "non_chat"


def reverse(page, *, source_url: str) -> Optional[ReversedChat]:
    """Reverse the chat currently detectable on ``page`` into a
    ReversedChat. Returns None if no composer clears the gate.

    Runs the generic finder across frames, takes the top-scoring input as
    the positive, and the rest as hard negatives from the SAME page."""
    from uc_browser import vendor_signatures as vs

    best = None  # (score, frame, selector)
    per_frame_candidates: list[tuple] = []  # (frame, [inputs])
    for frame in page.frames:
        try:
            inputs = frame.evaluate("window.__UC_findInputs && window.__UC_findInputs()")
        except Exception:
            inputs = None
        if not inputs:
            continue
        per_frame_candidates.append((frame, inputs))
        top = inputs[0]
        if top.get("score", 0) >= 4 and (best is None or top["score"] > best[0]):
            best = (top["score"], frame, top["selector"])
    if best is None:
        return None

    score, win_frame, win_sel = best
    pos_feat = _features(win_frame, win_sel) or {}
    vendors = []
    try:
        vendors = vs.identify(page)
    except Exception:
        pass

    # Hard negatives: every other candidate input across all frames.
    negatives = []
    for frame, inputs in per_frame_candidates:
        for cand in inputs:
            sel = cand.get("selector")
            if frame is win_frame and sel == win_sel:
                continue
            feat = _features(frame, sel)
            if feat is None:
                continue
            negatives.append({"selector": sel, "features": feat,
                              "label": _weak_label(feat)})
            if len(negatives) >= 12:
                break
        if len(negatives) >= 12:
            break

    return ReversedChat(
        source_url=source_url, selector=win_sel,
        frame_url=win_frame.url if win_frame is not page.main_frame else "",
        vendor=vendors[0] if vendors else None, score=round(float(score), 2),
        features=pos_feat, negatives=negatives, at=time.time(),
    )


_emit_lock = threading.Lock()


def emit(reversed_chat: ReversedChat, corpus_path: Optional[Path] = None) -> int:
    """Append the reversed chat's labeled rows to the training corpus.
    Returns the number of rows written (1 positive + negatives)."""
    path = corpus_path or CORPUS_PATH
    rows = [r for r in reversed_chat.to_rows() if r.get("features")]
    with _emit_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    return len(rows)


def load_corpus(corpus_path: Optional[Path] = None):
    """Load the reversed corpus as (X, y) using CODE_FEATURE_NAMES order.
    Returns (list[list[float]], list[str], n_rows)."""
    path = corpus_path or CORPUS_PATH
    X, y = [], []
    if not path.exists():
        return X, y, 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            feat = row.get("features") or {}
            if not feat:
                continue
            X.append([float(feat.get(n, 0) or 0) for n in CODE_FEATURE_NAMES])
            y.append(row.get("label", "non_chat"))
    return X, y, len(y)


def descriptor_dict(reversed_chat: ReversedChat) -> dict:
    return asdict(reversed_chat)
