"""Vendor-signature identification against synthetic pages that embed a
known loader. Offline; no live vendor sites."""

from __future__ import annotations

import pytest

from uc_browser import vendor_signatures as vs


def test_signature_table_wellformed():
    names = [s.name for s in vs.SIGNATURES]
    assert len(names) == len(set(names)), "duplicate vendor names"
    for s in vs.SIGNATURES:
        assert s.fingerprints, f"{s.name} has no fingerprints"
        assert s.launcher, f"{s.name} has no launcher selectors"


@pytest.mark.parametrize("vendor,src", [
    ("intercom", "https://widget.intercom.io/widget/abc123"),
    ("crisp", "https://client.crisp.chat/l.js"),
    ("zendesk", "https://static.zdassets.com/ekr/snippet.js?key=x"),
    ("ada", "https://static.ada.support/embed2.js"),
    ("gorgias", "https://config.gorgias.chat/bundle-loader/1"),
    ("drift", "https://js.driftt.com/include/x/abc.js"),
])
def test_identify_by_script_src(vendor, src):
    from playwright.sync_api import sync_playwright

    html = f'<html><head><script src="{src}"></script></head><body>x</body></html>'
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(html)
        found = vs.identify(page)
        b.close()
    assert vendor in found, f"expected {vendor} in {found}"


def test_identify_by_window_global():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content("<html><body>x</body></html>")
        page.evaluate("window.fcWidget = { init: () => {} }")  # freshchat global
        found = vs.identify(page)
        b.close()
    assert "freshchat" in found


def test_no_false_positive_on_plain_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content("<html><body><h1>hello</h1>"
                         "<script src='https://example.com/app.js'></script>"
                         "</body></html>")
        found = vs.identify(page)
        b.close()
    assert found == []


def test_inpage_scanner_catches_lazy_and_global(tmp_path):
    """With the bundle injected, __UC_scanVendors identifies engines via
    resource timing (a script fetched AFTER load) and window globals —
    the recall the static DOM scan misses."""
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    bundle = Path("extension/dist/uc-extension.js")
    if not bundle.exists():
        pytest.skip("bundle not built")
    js = bundle.read_text(encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content("<html><body>x</body></html>")
        page.evaluate(js)
        # A widget global that appears post-load (loader ran late).
        page.evaluate("window.$crisp = []")
        found = page.evaluate("window.__UC_scanVendors()")
        b.close()
    assert "crisp" in found


def test_open_widget_prefers_js_api():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content("<html><body>x</body></html>")
        # Fake the Intercom global with an observable open call.
        page.evaluate("""() => { window.__opened = false;
            window.Intercom = (cmd) => { if (cmd === 'show') window.__opened = true; }; }""")
        assert vs.open_widget(page, "intercom") is True
        assert page.evaluate("window.__opened") is True
        b.close()
