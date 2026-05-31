"""Visual confirmation test for KeePassXC-Browser in Playwright Chromium.

Reuses BC-010's launch path. Opens chrome://extensions/, then a chosen login
page, scans the DOM for KeePassXC-injected indicators (kpxc-* class names,
icons, data attributes), and keeps the browser open until you press Enter.

Usage:
    pixi run python scripts/test_keepassxc_visual.py [URL]

If URL is omitted, defaults to https://lu.ma/signin
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser_configs import get_config  # noqa: E402
from browser_test import (  # noqa: E402
    _build_launch_args,
    _prepare_profile,
    _register_chromium_native_messaging,
)


def main() -> int:
    # Args: [URL] [CONFIG_ID]
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://lu.ma/signin"
    config_id = sys.argv[2] if len(sys.argv) > 2 else "BC-010"
    cfg = get_config(config_id)
    if cfg is None:
        print(f"{config_id} config not found")
        return 1
    print(f"Using config: {cfg.id} — {cfg.description}")

    profile_path, prof_msg = _prepare_profile(cfg)
    if profile_path is None:
        print(f"Profile setup failed: {prof_msg}")
        return 1
    print(f"  {prof_msg}")

    if cfg.register_native_messaging:
        nm = _register_chromium_native_messaging()
        print(f"  native messaging: {nm}")

    binary = cfg.resolve_binary()
    if not binary:
        print("Binary not found")
        return 1

    args = _build_launch_args(cfg, profile_path)
    cmd = [binary] + args + ["about:blank"]
    print(f"\nLaunching: {Path(binary).name}")
    print(f"  port: {cfg.cdp_port}")
    print(f"  extension: {cfg.extension_path}")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    if proc.poll() is not None:
        print(f"  Chromium exited immediately, code={proc.returncode}")
        return 1

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = None
        for _ in range(15):
            time.sleep(1)
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{cfg.cdp_port}")
                break
            except Exception:
                continue

        if not browser:
            print(f"  Could not connect to CDP on port {cfg.cdp_port}")
            proc.kill()
            return 1
        print("  Connected via CDP")

        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # Wait for service worker
        sw = None
        if context.service_workers:
            sw = context.service_workers[0]
        else:
            try:
                sw = context.wait_for_event("serviceworker", timeout=15000)
            except Exception:
                pass
        if sw:
            print(f"  Service worker: {sw.url}")
        else:
            print("  No service worker detected")

        # 1. chrome://extensions/ — visual confirmation
        print("\n=== Step 1: chrome://extensions/ ===")
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("chrome://extensions/", timeout=10000)
            time.sleep(2)
            ext_count = page.evaluate("""() => {
                const mgr = document.querySelector('extensions-manager');
                if (!mgr || !mgr.shadowRoot) return -1;
                const list = mgr.shadowRoot.querySelector('extensions-item-list');
                if (!list || !list.shadowRoot) return -1;
                return list.shadowRoot.querySelectorAll('extensions-item').length;
            }""")
            if ext_count >= 0:
                print(f"  Visible extensions in chrome://extensions/: {ext_count}")
            else:
                print("  Could not enumerate extensions via DOM (Shadow DOM access blocked)")
        except Exception as e:
            print(f"  chrome://extensions/ error: {e}")

        # 2. Navigate to a login form and scan for KeePassXC injection
        print(f"\n=== Step 2: {target_url} form scan ===")
        page2 = context.new_page()
        try:
            page2.goto(
                target_url,
                timeout=30000,
                wait_until="domcontentloaded",
            )
            time.sleep(4)  # let extension content scripts inject

            findings = page2.evaluate("""() => {
                const result = {
                    kpxc_class_count: document.querySelectorAll('[class*="kpxc"]').length,
                    kpxc_id_count: document.querySelectorAll('[id*="kpxc"]').length,
                    kpxc_data_attrs: 0,
                    kpxc_div_count: 0,
                    icon_examples: [],
                    extension_meta: null,
                };
                for (const el of document.querySelectorAll('*')) {
                    const attrs = el.getAttributeNames();
                    for (const a of attrs) {
                        if (a.startsWith('data-kpxc')) result.kpxc_data_attrs++;
                    }
                }
                const kpxcDivs = document.querySelectorAll('div[class*="kpxc"]');
                result.kpxc_div_count = kpxcDivs.length;
                for (const d of Array.from(kpxcDivs).slice(0, 5)) {
                    result.icon_examples.push({
                        cls: d.className,
                        rect: d.getBoundingClientRect().toJSON(),
                    });
                }
                // Look for inline manifest reference
                for (const m of document.querySelectorAll('meta')) {
                    if ((m.name||'').toLowerCase().includes('keepass')) {
                        result.extension_meta = m.outerHTML;
                    }
                }
                return result;
            }""")
            print(f"  Elements with 'kpxc' in class: {findings['kpxc_class_count']}")
            print(f"  Elements with 'kpxc' in id:    {findings['kpxc_id_count']}")
            print(f"  data-kpxc-* attributes:        {findings['kpxc_data_attrs']}")
            print(f"  div.kpxc-* nodes:              {findings['kpxc_div_count']}")
            for ex in findings["icon_examples"]:
                print(f"    - {ex['cls']} at ({ex['rect']['x']:.0f},{ex['rect']['y']:.0f})")
            if any([
                findings["kpxc_class_count"],
                findings["kpxc_id_count"],
                findings["kpxc_data_attrs"],
                findings["kpxc_div_count"],
            ]):
                print("\n  KeePassXC content scripts ARE injecting into the page.")
            else:
                print("\n  No KeePassXC injection markers found yet.")
                print("  Possible reasons:")
                print("    - KeePassXC desktop not running or not unlocked")
                print("    - Extension not yet associated with KeePassXC database")
                print("    - Form fields not yet recognized as login fields")
        except Exception as e:
            print(f"  Page eval error: {e}")

        print()
        print("Browser is open. Visually inspect:")
        print("  - Toolbar: KeePassXC icon should be present (may be in puzzle menu)")
        print("  - Click the icon: it should show 'Connect' if not yet associated,")
        print("    or list credentials if already associated.")
        print(f"  - On {target_url}: username field should show a small")
        print("    KeePassXC icon if credentials are available for the domain.")
        print()
        print("Press Enter in this terminal to close the browser...")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

        try:
            browser.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
