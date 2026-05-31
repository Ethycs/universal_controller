"""Test KeePassXC-Browser extension in Playwright Chrome.

Verifies that:
1. The extension loads in Playwright's Chrome
2. Native messaging connects to KeePassXC desktop app
3. Auto-fill works on a login form

Prerequisites:
- KeePassXC desktop running with browser integration enabled
- KeePassXC-Browser extension installed in Chrome
- KeePassXC database unlocked

Usage:
    pixi run python scripts/test_keepassxc.py
"""

import os
import time
from pathlib import Path


def _ensure_chromium_native_messaging():
    """Register KeePassXC native messaging host for Chromium (not just Chrome).

    Playwright uses bundled Chromium which looks up native messaging hosts
    under HKCU\\Software\\Chromium\\NativeMessagingHosts, not Chrome's key.
    """
    import winreg

    manifest = (
        Path(os.environ["LOCALAPPDATA"])
        / "KeePassXC/org.keepassxc.keepassxc_browser_chromium.json"
    )
    if not manifest.exists():
        print(f"Chromium manifest not found: {manifest}")
        return False

    key_path = r"Software\Chromium\NativeMessagingHosts\org.keepassxc.keepassxc_browser"
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
        winreg.CloseKey(key)
        print(f"Registered native messaging host: {key_path} -> {manifest}")
        return True
    except Exception as e:
        print(f"Failed to register native messaging: {e}")
        return False


def main():
    from playwright.sync_api import sync_playwright

    # Register native messaging host for Chromium if needed
    _ensure_chromium_native_messaging()

    # Find the KeePassXC-Browser extension on disk
    ext_dir = (
        Path(os.environ["LOCALAPPDATA"])
        / "Google/Chrome/User Data/Default/Extensions"
        / "oboonakemofpalcgghocfoadofidjkkk"
    )
    if not ext_dir.exists():
        print("KeePassXC-Browser extension not found in Chrome extensions.")
        return

    versions = sorted(ext_dir.iterdir())
    if not versions:
        print("No version directory found for KeePassXC-Browser.")
        return

    source = versions[-1]  # latest version

    # Copy to clean path without spaces or Chrome-internal metadata
    import shutil
    ext_path = str(Path("data/.keepassxc_extension").resolve())
    if Path(ext_path).exists():
        shutil.rmtree(ext_path)
    Path(ext_path).mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        # Skip only Chrome-internal metadata (keep managed_storage.json —
        # it's referenced by the manifest)
        if item.name == "_metadata":
            continue
        dst = Path(ext_path) / item.name
        if item.is_dir():
            shutil.copytree(str(item), str(dst))
        else:
            shutil.copy2(str(item), str(dst))
    print(f"KeePassXC-Browser extension copied to: {ext_path}")

    # Use a persistent profile so the extension can save its association
    profile_dir = str(Path("data/.keepassxc_test_profile").resolve())
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    # Launch Playwright's Chromium manually with CDP port (not pipe)
    # because Playwright's default --remote-debugging-pipe conflicts with extensions.
    import subprocess

    chromium_path = (
        Path(os.environ["LOCALAPPDATA"])
        / "ms-playwright/chromium-1208/chrome-win64/chrome.exe"
    )
    if not chromium_path.exists():
        # Try other versions
        pw_dir = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
        matches = list(pw_dir.glob("chromium-*/chrome-win64/chrome.exe"))
        if matches:
            chromium_path = matches[-1]
        else:
            print(f"Playwright Chromium not found in {pw_dir}")
            return

    port = 9223
    cmd = [
        str(chromium_path),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--load-extension={ext_path}",
        f"--disable-extensions-except={ext_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "about:blank",
    ]
    print(f"Launching Chromium: {chromium_path.name}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)

    if proc.poll() is not None:
        print(f"Chromium exited immediately with code {proc.returncode}")
        return

    with sync_playwright() as p:
        browser = None
        for _ in range(15):
            time.sleep(1)
            try:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
                break
            except Exception:
                continue

        if not browser:
            print(f"Failed to connect to Chromium on port {port}")
            proc.kill()
            return

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        print(f"\nChromium launched with KeePassXC-Browser extension.")

        # Wait for MV3 service worker to register
        sw = None
        if context.service_workers:
            sw = context.service_workers[0]
            print(f"Service worker already registered: {sw.url[:100]}")
        else:
            print("Waiting for service worker to register...")
            try:
                sw = context.wait_for_event("serviceworker", timeout=30000)
                print(f"Service worker registered: {sw.url[:100]}")
            except Exception as e:
                print(f"No service worker appeared: {e}")

        # Also check background pages (MV2) just in case
        bgs = context.background_pages
        if bgs:
            print(f"Background pages: {len(bgs)}")
            for p_ in bgs:
                print(f"  {p_.url[:100]}")

        if not sw and not bgs:
            print("\nExtension did NOT load. Check manifest + Playwright version.")
            proc.kill()
            return

        # Open a test site with a login form
        test_page = context.new_page()
        test_page.goto("https://lu.ma/signin", timeout=30000, wait_until="domcontentloaded")
        time.sleep(3)

        print("\nTest page opened (lu.ma/signin).")
        print("If KeePassXC is unlocked and has credentials for lu.ma,")
        print("you should see the KeePassXC auto-fill icon in the form fields.\n")
        print("Close the browser when done testing.")

        # Wait for Chromium to close
        proc.wait()

    print("\nTest complete.")


if __name__ == "__main__":
    main()
