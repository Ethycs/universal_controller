"""Smoke test for the generalization bench runner.

Uses the synthetic fixtures (generated on the fly if missing) so it needs
no network and no live capture: the well-formed chat page must pass every
stage, and the search page must not false-positive. This keeps the bench
harness itself from silently rotting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "bench" / "fixtures"
BUNDLE = REPO / "extension" / "dist" / "uc-extension.js"


pytestmark = pytest.mark.skipif(
    not BUNDLE.exists(),
    reason="UC bundle not built (cd extension && npm install && npx rollup -c)",
)


def _ensure_synthetic_fixtures() -> None:
    if not (FIXTURES / "synthetic-chat" / "page.mhtml").exists():
        subprocess.run(
            [sys.executable, str(REPO / "bench" / "make_synthetic.py")],
            check=True, cwd=str(REPO), timeout=120,
        )


def test_synthetic_fixtures_score_correctly():
    _ensure_synthetic_fixtures()
    sys.path.insert(0, str(REPO))
    from bench._common import load_bundle
    from bench.run_bench import run_fixture

    from playwright.sync_api import sync_playwright

    bundle = load_bundle()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        chat = run_fixture(browser, bundle, FIXTURES / "synthetic-chat", False)
        assert chat["error"] is None
        for stage in ("input_top1", "input_gate", "input_pipeline",
                      "send_top1", "container_top1"):
            assert chat["stages"].get(stage) is True, (stage, chat["stages"])

        neg = run_fixture(browser, bundle, FIXTURES / "synthetic-search", False)
        assert neg["error"] is None
        assert neg["stages"]["fp_input"] is False, neg["stages"]
        assert neg["stages"]["fp_chat"] is False, neg["stages"]

        browser.close()
