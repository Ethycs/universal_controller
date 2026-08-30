"""Tests for the find→reverse→strengthen loop: reversal against an
injected bundle on a synthetic chat page, corpus round-tripping, weak
negative labeling, and the strengthen (retrain) step."""

from __future__ import annotations

from pathlib import Path

import pytest

from uc_browser.reverser import (
    ReversedChat,
    emit,
    load_corpus,
    reverse,
)

BUNDLE = Path("extension/dist/uc-extension.js")

CHAT_HTML = """<!doctype html><html><head><style>
  body{margin:0;font-family:sans-serif}
  .log{height:300px;overflow-y:auto;border:1px solid #ccc}
  .composer{position:fixed;bottom:0;width:100%;display:flex}
  .composer [contenteditable]{flex:1;min-height:40px;border:1px solid #999}
  .search{position:absolute;top:0}
</style></head><body>
  <input class="search" type="search" placeholder="Search the site" aria-label="Search">
  <div class="log" role="log" aria-live="polite">
    <div>Hi, how can I help?</div><div>Tell me about X.</div></div>
  <div class="composer">
    <div contenteditable="true" role="textbox" aria-label="Message input"></div>
    <button aria-label="Send message">Send</button></div>
</body></html>"""


@pytest.mark.skipif(not BUNDLE.exists(), reason="bundle not built")
def test_reverse_produces_positive_and_negatives():
    from playwright.sync_api import sync_playwright

    js = BUNDLE.read_text(encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(CHAT_HTML, wait_until="load")
        page.evaluate(js)
        rc = reverse(page, source_url="synthetic://chat")
        b.close()

    assert rc is not None, "should find the contenteditable composer"
    assert rc.score >= 4
    assert rc.features, "positive feature vector extracted"
    rows = rc.to_rows()
    labels = [r["label"] for r in rows]
    assert labels[0] == "chat_input"
    # The search box should appear as a same-page hard negative.
    assert any(r["label"] in ("search", "non_chat", "form_field")
               for r in rows[1:]), labels


def test_corpus_roundtrip_and_load(tmp_path):
    corpus = tmp_path / "reversed.jsonl"
    rc = ReversedChat(
        source_url="s://a", selector="#c", frame_url="", vendor="crisp",
        score=6.0, features={n: 1 for n in __import__(
            "uc_browser.dom_classifier", fromlist=["CODE_FEATURE_NAMES"]
        ).CODE_FEATURE_NAMES},
        negatives=[{"selector": "#s", "label": "search",
                    "features": {n: 0 for n in __import__(
                        "uc_browser.dom_classifier",
                        fromlist=["CODE_FEATURE_NAMES"]).CODE_FEATURE_NAMES}}],
    )
    n = emit(rc, corpus_path=corpus)
    assert n == 2
    X, y, rows = load_corpus(corpus_path=corpus)
    assert rows == 2
    assert "chat_input" in y and "search" in y
    assert len(X[0]) == len(
        __import__("uc_browser.dom_classifier",
                   fromlist=["CODE_FEATURE_NAMES"]).CODE_FEATURE_NAMES)


def test_strengthen_trains_candidate(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    from uc_browser import loop as loop_mod
    from uc_browser.dom_classifier import CODE_FEATURE_NAMES

    corpus = tmp_path / "reversed.jsonl"
    # Fabricate a separable 2-class corpus: chat rows have kw_chat=1,
    # negatives have kw_search=1.
    import json
    with corpus.open("w", encoding="utf-8") as f:
        for i in range(30):
            pos = {n: 0 for n in CODE_FEATURE_NAMES}
            pos["kw_chat"] = 1
            pos["has_contenteditable"] = 1
            f.write(json.dumps({"label": "chat_input", "features": pos}) + "\n")
            neg = {n: 0 for n in CODE_FEATURE_NAMES}
            neg["kw_search"] = 1
            f.write(json.dumps({"label": "search", "features": neg}) + "\n")

    # loop.strengthen -> load_corpus() reads reverser.CORPUS_PATH.
    monkeypatch.setattr("uc_browser.reverser.CORPUS_PATH", corpus)
    monkeypatch.setattr(loop_mod, "CANDIDATE_MODEL", tmp_path / "cand.pkl")
    monkeypatch.setattr(loop_mod, "MODELS_DIR", tmp_path)

    out = loop_mod.ReverseLoop().strengthen()
    assert out.get("val_accuracy", 0) >= 0.9, out
    assert (tmp_path / "cand.pkl").exists()
