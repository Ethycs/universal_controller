"""Probe: what happens when Grok converts a long paste into a file attachment?

Symptom: setting a long string via ``__UC_setText`` (whose contenteditable
path falls through to ``ClipboardEvent('paste')``) makes Grok extract the
text into an attachment chip — the ProseMirror editor goes empty, the send
control re-enables, and clicking it sends the *file*, not the prompt. From
the outside the composer looks "submitted" so ``__UC_grokComposerEmpty``
reports success; the reply is "I see you attached…" instead of an answer.

This script:

1. Opens grok.com on a fresh chat.
2. Snapshots the composer DOM state BEFORE typing.
3. Types a LONG string via ``__UC_setText`` (forcing the paste fallback).
4. Snapshots state AFTER typing — looking for any attachment chip, any
   disabled state on the send control, and whether the ProseMirror is
   empty.
5. (Optional, with ``--send``) Clicks the send control and watches the
   FIRST user-message block that appears, so we can see whether the
   actual prompt text or the attachment got sent.

Run::

    pixi run -e dev python scripts/grok_probe_attachment.py
    pixi run -e dev python scripts/grok_probe_attachment.py --send
    pixi run -e dev python scripts/grok_probe_attachment.py --chars 12000

You must already be logged into Grok in the persistent profile
(``data/.uc_chromium_profile/``). Run ``pixi run event-harvester
web-login --urls https://grok.com`` first if not.
"""

from __future__ import annotations

import argparse
import json
import sys

from uc_browser.sites.grok_fast import _INSTALL_JS
from uc_browser import BrowserMode, UCBrowser


def make_text(n_chars: int) -> str:
    """A long but recognizable string. Uses a sentinel so we can grep for it."""
    sentinel = "GROK_PROBE_ATTACHMENT "
    body = "lorem ipsum dolor sit amet consectetur adipiscing elit " * 400
    out = (sentinel + body)[:n_chars]
    return out


def snapshot(page, label: str) -> dict:
    """Dump everything that could tell us 'is this an attachment yet'."""
    state = page.evaluate(
        r"""() => {
            const pm = document.querySelector('div.ProseMirror');
            // The up-arrow path that __UC_grokFindSendButton anchors on.
            const arrowPath = 'M6 11L12 5M12 5L18 11M12 5V19';
            const arrow = document.querySelector(
                'svg path[d="' + arrowPath + '"]'
            );
            const sendCtl = arrow
                ? (arrow.closest('button, [role="button"]')
                   || arrow.closest('svg')?.parentElement)
                : null;

            // Candidate attachment-chip selectors — we don't know which one
            // Grok uses; capture ALL hits so we can pick the stable anchor.
            const chipSelectors = [
                '[data-testid*="attach" i]',
                '[data-testid*="file" i]',
                '[aria-label*="attach" i]',
                '[aria-label*="file" i]',
                'button[aria-label*="remove" i]',
                'button[aria-label*="delete attachment" i]',
                '[class*="attachment" i]',
                '[class*="chip" i]',
                '[class*="file-preview" i]',
            ];
            const chips = {};
            for (const sel of chipSelectors) {
                const hits = document.querySelectorAll(sel);
                if (hits.length) {
                    chips[sel] = Array.from(hits).slice(0, 3).map(el => ({
                        tag: el.tagName,
                        testid: el.getAttribute('data-testid'),
                        aria: el.getAttribute('aria-label'),
                        cls: (el.className || '').toString().slice(0, 120),
                        text: (el.innerText || '').trim().slice(0, 80),
                    }));
                }
            }

            // Also: look for elements whose innerText contains "txt" or our
            // sentinel — these often surface as the file-name label.
            const fnameHits = [];
            const sentinel = 'GROK_PROBE_ATTACHMENT';
            const all = document.querySelectorAll(
                'div, span, p, button, [role="button"]'
            );
            for (const el of all) {
                const t = (el.innerText || '').trim();
                if (!t || t.length > 200) continue;
                if (t.includes(sentinel) || /\.txt$/i.test(t)
                    || /^paste\b/i.test(t)) {
                    fnameHits.push({
                        tag: el.tagName,
                        cls: (el.className || '').toString().slice(0, 120),
                        testid: el.getAttribute('data-testid'),
                        text: t.slice(0, 120),
                    });
                    if (fnameHits.length >= 5) break;
                }
            }

            return {
                pm_present: !!pm,
                pm_text_len: pm ? (pm.textContent || '').length : -1,
                pm_text_head: pm ? (pm.textContent || '').slice(0, 80) : null,
                send_present: !!sendCtl,
                send_tag: sendCtl ? sendCtl.tagName : null,
                send_role: sendCtl ? sendCtl.getAttribute('role') : null,
                send_aria_disabled: sendCtl
                    ? sendCtl.getAttribute('aria-disabled') : null,
                send_disabled_attr: sendCtl ? !!sendCtl.disabled : null,
                send_html_head: sendCtl
                    ? (sendCtl.outerHTML || '').slice(0, 220) : null,
                attachment_chip_hits: chips,
                filename_or_sentinel_hits: fnameHits,
                user_message_count: document.querySelectorAll(
                    'div[data-testid="user-message"]'
                ).length,
                asst_message_count: document.querySelectorAll(
                    'div[data-testid="assistant-message"]'
                ).length,
                url: location.href,
            };
        }"""
    )
    print(f"\n──── {label} ────")
    print(json.dumps(state, indent=2, sort_keys=True))
    return state


def read_user_messages(page) -> list[str]:
    return page.evaluate(
        """() => {
            const els = document.querySelectorAll('div[data-testid="user-message"]');
            return Array.from(els).map(e => (e.innerText || '').trim().slice(0, 200));
        }"""
    ) or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chars", type=int, default=6000,
                    help="length of the synthetic test string (default 6000)")
    ap.add_argument("--file", type=str, default=None,
                    help="path to a real prompt file to use as the input (overrides --chars)")
    ap.add_argument("--send", action="store_true",
                    help="actually click send after typing and observe what gets sent")
    ap.add_argument(
        "--wait-after-send", type=float, default=4.0,
        help="seconds to wait after clicking send before reading user-messages",
    )
    args = ap.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
        print(f"probe: typing {len(text)} chars from {args.file!r}")
    else:
        text = make_text(args.chars)
        print(f"probe: typing {len(text)} chars (sentinel='GROK_PROBE_ATTACHMENT…')")

    uc = UCBrowser(mode=BrowserMode.CHROMIUM_EXT, timeout_ms=30000)
    uc.start()
    page = uc.open("https://grok.com/", wait_ms=4000)
    try:
        uc.dismiss_cookies(page)
        uc.close_modal(page)
        page.wait_for_selector("div.ProseMirror", timeout=15000)

        # Install the same JS production uses — exposes
        # __UC_grokRemoveAttachments / __UC_grokTriggerSubmit /
        # __UC_grokComposerEmpty on the page. Without this, the probe is
        # observing the page but not exercising any of our helpers.
        page.evaluate(_INSTALL_JS)

        snapshot(page, "BEFORE setText")

        typed = page.evaluate(
            "(a) => window.__UC_setText && window.__UC_setText(a[0], a[1])",
            ["div.ProseMirror", text],
        ) or {}
        print(f"\n__UC_setText returned: {typed}")

        # Give Grok's paste-handler a moment to convert.
        page.wait_for_timeout(800)
        after_typing = snapshot(page, "AFTER setText (long paste)")

        # Run the production chip-strip and observe whether it actually clears
        # the chip. The 'remaining' count is the load-bearing signal.
        chips = page.evaluate(
            "() => window.__UC_grokRemoveAttachments && window.__UC_grokRemoveAttachments()"
        )
        print(f"\n__UC_grokRemoveAttachments returned: {chips}")
        page.wait_for_timeout(400)
        after_strip = snapshot(page, "AFTER chip-strip")

        # Decisive signal: did the strip actually win?
        chip_count_after_strip = sum(
            len(v) for v in (after_strip.get("attachment_chip_hits") or {}).values()
            if isinstance(v, list)
        )
        print(
            f"\nverdict: pm_text_len={after_strip.get('pm_text_len')}, "
            f"chip-related-hits-remaining={chip_count_after_strip}"
        )

        if not args.send:
            print("\n(skip --send to stop here. Pass --send to click and see what arrives.)")
            return 0

        # Click the send control via the same path our production code uses.
        trig = page.evaluate("() => window.__UC_grokTriggerSubmit && window.__UC_grokTriggerSubmit()")
        print(f"\n__UC_grokTriggerSubmit returned: {trig}")

        page.wait_for_timeout(int(args.wait_after_send * 1000))
        snapshot(page, "AFTER trigger (post-wait)")

        msgs = read_user_messages(page)
        print("\nuser-message blocks that appeared:")
        for i, m in enumerate(msgs):
            print(f"  [{i}] {m!r}")

        # Anchor verdict on the first ~60 chars of what we typed — works for
        # both synthetic and file inputs.
        prompt_prefix = text[:60].strip()
        if not msgs:
            print("  (none — submit may have failed silently OR Grok rejected it)")
        else:
            last = msgs[-1]
            if prompt_prefix and prompt_prefix[:30] in last:
                print("\n  → prompt text SENT verbatim (no attachment conversion this run)")
            elif any(suf in last.lower() for suf in (".txt", "paste", "attached")):
                print("\n  → looks like an ATTACHMENT was sent, not the typed text")
            else:
                print("\n  → ambiguous — inspect the snapshot above for context")
        return 0
    finally:
        try:
            page.close()
        finally:
            uc.close()


if __name__ == "__main__":
    sys.exit(main())
