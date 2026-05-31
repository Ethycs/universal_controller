"""Probe: find Grok's "generating" signal so send() can return early.

Sends a long-ish prompt and polls every 200ms, capturing which selectors
are present while generation is in flight vs after. The output tells us
the canonical "stop button" / "is generating" anchor.
"""
import json
import time

from uc_browser import BrowserMode, UCBrowser

PROMPT = "Write a haiku about persistent browsers."

uc = UCBrowser(mode=BrowserMode.CHROMIUM_EXT, timeout_ms=30000)
uc.start()
try:
    page = uc.open("https://grok.com/", wait_ms=4000)
    uc.dismiss_cookies(page)
    uc.close_modal(page)
    page.wait_for_selector("div.ProseMirror", timeout=10000)

    page.evaluate(
        "(a) => window.__UC_setText(a[0], a[1])",
        ["div.ProseMirror", PROMPT],
    )
    page.focus("div.ProseMirror")
    page.keyboard.press("Enter")

    print("polling for 25s — every poll dumps which 'busy' anchors are present")
    print()
    deadline = time.monotonic() + 25.0
    last_state = None
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        state = page.evaluate(
            """() => {
                const probes = {
                    stop_testid:    !!document.querySelector('[data-testid*="stop" i]'),
                    stop_aria:      !!document.querySelector('button[aria-label*="stop" i]'),
                    submit_aria:    !!document.querySelector('button[aria-label*="submit" i]'),
                    regenerate:     !!document.querySelector('button[aria-label*="regenerate" i]'),
                    asst_blocks:    document.querySelectorAll('div[data-testid="assistant-message"]').length,
                    last_text_len:  (() => {
                        const e = document.querySelectorAll('div[data-testid="assistant-message"]');
                        return e.length ? (e[e.length-1].innerText || '').length : 0;
                    })(),
                };
                // Bonus: list every button data-testid that's visible.
                const testids = new Set();
                document.querySelectorAll('button[data-testid]').forEach(b => {
                    const r = b.getBoundingClientRect();
                    if (r.width > 0) testids.add(b.getAttribute('data-testid'));
                });
                probes.button_testids = Array.from(testids);
                return probes;
            }"""
        )
        if state != last_state:
            t = time.strftime("%H:%M:%S")
            print(f"[{t}] {json.dumps(state, sort_keys=True)}")
            last_state = state
finally:
    page.close()
    uc.close()
