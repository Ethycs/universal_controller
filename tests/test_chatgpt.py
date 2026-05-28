"""Incremental test: UC extension on ChatGPT.

Run: pixi run python tests/test_uc_chatgpt.py [step]

Steps:
  1  Verify extension loads + find inputs
  2  Verify setText works on the input
  3  Verify button discovery + click
  4  Verify scan-diff detects response
  5  Full chat() pipeline test
"""

import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)

from uc_browser import UCBrowser

CHATGPT_URL = "https://chatgpt.com"


def step1_find_inputs(uc, page):
    """Step 1: Verify extension loads + find inputs."""
    print("\n=== STEP 1: Extension load + input discovery ===\n")

    ready = page.evaluate("window.__UC && window.__UC.ready")
    print(f"UC ready: {ready}")
    if not ready:
        print("FAIL: Extension not loaded.")
        return None

    version = page.evaluate("window.__UC.version")
    print(f"UC version: {version}")

    inputs = page.evaluate("window.__UC_findInputs()")
    print(f"\nFound {len(inputs)} input(s):")
    for i, inp in enumerate(inputs[:5]):
        print(f"  [{i}] selector={inp['selector']}")
        print(f"      tag={inp['tag']} ce={inp['contentEditable']} score={inp['score']}")
        print(f"      placeholder={inp.get('placeholder', '')}")
        print(f"      ariaLabel={inp.get('ariaLabel', '')}")
        print(f"      rect={inp.get('rect', {})}")

    if not inputs:
        print("FAIL: No inputs found. Check if ChatGPT requires login.")
        return None

    print(f"\nBest input: {inputs[0]['selector']} (score={inputs[0]['score']})")
    return inputs[0]


def step2_set_text(uc, page, input_info):
    """Step 2: Verify setText works on the input."""
    print("\n=== STEP 2: setText on input ===\n")

    selector = input_info["selector"]
    result = page.evaluate(
        "(args) => window.__UC_setText(args[0], args[1])",
        [selector, "hello test from UC"],
    )
    print(f"setText result: {json.dumps(result)}")

    if result and result.get("success"):
        print(f"SUCCESS: Text set via method '{result.get('method')}'")
    else:
        print(f"FAIL: setText failed — {result.get('error', 'unknown')}")
        print("Trying Playwright type() fallback...")
        try:
            page.click(selector)
            page.type(selector, "hello test from UC", delay=20)
            print("Playwright type() succeeded")
        except Exception as e:
            print(f"Playwright type() also failed: {e}")

    # Verify text is in the element
    page.wait_for_timeout(500)
    actual = page.evaluate(f"""() => {{
        const el = document.querySelector('{selector}');
        return el ? (el.textContent || el.value || '') : 'NOT FOUND';
    }}""")
    print(f"Actual text in element: '{actual[:100]}'")

    # Clear it for next steps
    page.evaluate(f"""() => {{
        const el = document.querySelector('{selector}');
        if (el) {{
            if (el.contentEditable === 'true') {{
                el.focus();
                document.execCommand('selectAll');
                document.execCommand('delete');
            }} else {{
                el.value = '';
            }}
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
        }}
    }}""")


def step3_find_buttons(uc, page, input_info):
    """Step 3: Verify button discovery."""
    print("\n=== STEP 3: Button discovery ===\n")

    selector = input_info["selector"]
    buttons = page.evaluate("(s) => window.__UC_findButtons(s)", selector)
    print(f"Found {len(buttons)} button(s):")
    for i, btn in enumerate(buttons[:5]):
        print(f"  [{i}] selector={btn['selector']}  score={btn['score']}  label='{btn['label'][:40]}'")

    if not buttons:
        print("WARN: No buttons found via UC. Checking for data-testid...")
        fallback = page.evaluate("""() => {
            const btn = document.querySelector('[data-testid="send-button"]');
            return btn ? { found: true, tag: btn.tagName, text: btn.innerText } : { found: false };
        }""")
        print(f"  Fallback check: {json.dumps(fallback)}")

    return buttons[0] if buttons else None


def step4_scan_diff(uc, page, input_info, btn_info):
    """Step 4: Verify scan-diff detects response."""
    print("\n=== STEP 4: Scan-diff response detection ===\n")

    selector = input_info["selector"]

    # Type FIRST — send button only appears after text is entered
    test_msg = "Say hello in exactly 3 words. Nothing else."
    result = page.evaluate(
        "(args) => window.__UC_setText(args[0], args[1])",
        [selector, test_msg],
    )
    print(f"setText: {json.dumps(result)}")

    if not (result and result.get("success")):
        page.click(selector)
        page.type(selector, test_msg, delay=10)

    page.wait_for_timeout(500)

    # Now find buttons (send button appears after text entry)
    buttons = page.evaluate("(s) => window.__UC_findButtons(s)", selector)
    print(f"Buttons after text: {len(buttons)}")
    for b in buttons[:3]:
        print(f"  {b['selector']} score={b['score']} label='{b['label'][:40]}'")
    btn_sel = buttons[0]["selector"] if buttons else None

    # Baseline AFTER typing, BEFORE sending
    scan = uc.first_scan(page)
    print(f"\nBaseline scan: {scan}")

    # Click send
    if btn_sel:
        print(f"Clicking: {btn_sel}")
        try:
            page.click(btn_sel)
        except Exception:
            page.press(selector, "Enter")
    else:
        print("No button found, pressing Enter")
        page.press(selector, "Enter")

    print("Message sent. Waiting 8 seconds for response...")
    page.wait_for_timeout(8000)

    # Diff
    diff = uc.next_scan(page)
    print(f"\nDiff summary: {json.dumps(diff)}")

    # Find new content
    new_content = page.evaluate("window.__UC_findNewContent()")
    print(f"\nNew content blocks: {len(new_content)}")
    for i, nc in enumerate(new_content[:5]):
        print(f"  [{i}] change={nc['change']} textLen={nc['textLength']}")
        print(f"      selector={nc['selector']}")
        print(f"      text={nc['text'][:120]}...")

    return new_content


def step5_full_chat(uc, page):
    """Step 5: Full chat() pipeline test."""
    print("\n=== STEP 5: Full chat() pipeline ===\n")

    response = uc.chat(page, "Say hello in exactly 3 words. Nothing else.")
    print(f"\nResponse ({len(response) if response else 0} chars):")
    print(response if response else "(None — no response detected)")
    return response


def main():
    step = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    print(f"Running steps 1-{step} against ChatGPT...")
    print("(Make sure you're logged in — run web_login first if needed)\n")

    with UCBrowser() as uc:
        page = uc.open(CHATGPT_URL, wait_ms=5000)
        print(f"Opened: {page.url}")

        # Check for login wall
        if uc.has_login_wall(page):
            print("\nWARN: Login wall detected. You may need to run web_login() first.")

        # Step 1
        input_info = step1_find_inputs(uc, page)
        if not input_info or step < 2:
            page.close()
            return

        # Step 2
        if step >= 2:
            step2_set_text(uc, page, input_info)

        # Step 3
        btn_info = None
        if step >= 3:
            btn_info = step3_find_buttons(uc, page, input_info)

        # Step 4
        if step >= 4:
            step4_scan_diff(uc, page, input_info, btn_info)

        # Step 5 — opens a NEW page so previous test messages don't interfere
        if step >= 5:
            page.close()
            page = uc.open(CHATGPT_URL, wait_ms=5000)
            step5_full_chat(uc, page)

        page.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
