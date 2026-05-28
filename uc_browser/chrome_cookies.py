"""Chrome cookie extraction helpers.

Decrypts cookies from the user's installed Chrome (handling Windows app-bound
encryption via a UAC-elevated subprocess) and converts them to Playwright
format. Used by UCBrowser._inject_chrome_cookies to seed a Playwright context
with the user's existing Chrome session cookies.

Requires the optional `rookiepy` extra:

    pip install "uc-browser[chrome-cookies]"

Public API:
    get_chrome_cookies(domains=None) -> list[dict]
    cookies_to_playwright(cookies) -> list[dict]
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("uc_browser.chrome_cookies")


def get_chrome_cookies(domains: list[str] | None = None) -> list[dict]:
    """Extract cookies from Chrome using rookiepy.

    Chrome v130+ uses app-bound encryption requiring admin on Windows.
    Falls back to spawning an elevated subprocess if needed.
    """
    try:
        import rookiepy

        if domains:
            cookies = rookiepy.chrome(domains)
        else:
            cookies = rookiepy.chrome()
        logger.info("Extracted %d cookies from Chrome.", len(cookies))
        return cookies
    except Exception as e:
        if "admin" not in str(e).lower() and "appbound" not in str(e).lower():
            logger.error("Failed to extract Chrome cookies: %s", e)
            return []

    logger.info("Chrome requires admin for cookie decryption. Requesting elevation...")
    return _get_cookies_elevated(domains)


def _get_cookies_elevated(domains: list[str] | None = None) -> list[dict]:
    """Spawn an elevated Python subprocess to extract Chrome cookies.

    Shows a UAC prompt. The elevated process writes cookies to a temp file.
    """
    import subprocess
    import sys
    import tempfile

    cookie_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="chrome_cookies_", delete=False,
    )
    cookie_file.close()

    domain_arg = json.dumps(domains) if domains else "null"
    script = f'''
import json, sys, os
with open(r"{cookie_file.name}", "w") as f:
    json.dump({{"status": "running"}}, f)
try:
    sys.path.insert(0, os.path.dirname(r"{sys.executable}") + r"\\..\\Lib\\site-packages")
    import rookiepy
    domains = {domain_arg}
    cookies = rookiepy.chrome(domains) if domains else rookiepy.chrome()
    with open(r"{cookie_file.name}", "w") as f:
        json.dump({{"status": "done", "cookies": cookies}}, f)
except Exception as e:
    with open(r"{cookie_file.name}", "w") as f:
        json.dump({{"status": "error", "error": str(e)}}, f)
'''

    script_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="extract_cookies_", delete=False,
    )
    script_file.write(script)
    script_file.close()

    try:
        if sys.platform == "win32":
            import ctypes
            import time

            python_exe = sys.executable
            params = (
                f'/c title Event Harvester - Cookie Access '
                f'&& "{python_exe}" "{script_file.name}"'
            )
            print("  [UAC] Requesting admin access for Chrome cookie decryption...")
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", params, None, 1,
            )
            if ret <= 32:
                logger.warning("UAC elevation was denied or failed (code %d).", ret)
                return []

            for i in range(30):
                time.sleep(1)
                try:
                    data = json.loads(Path(cookie_file.name).read_text())
                    if data.get("status") == "done":
                        cookies = data.get("cookies", [])
                        logger.info("Elevated extraction: %d cookies.", len(cookies))
                        return cookies
                    elif data.get("status") == "error":
                        logger.error(
                            "Elevated extraction error: %s", data.get("error"),
                        )
                        return []
                except (json.JSONDecodeError, FileNotFoundError):
                    continue
            logger.warning("Elevated cookie extraction timed out.")
            return []
        else:
            subprocess.run(
                [sys.executable, script_file.name],
                capture_output=True, text=True, timeout=30,
            )
            cookies = json.loads(Path(cookie_file.name).read_text())
            logger.info("Extracted %d cookies.", len(cookies))
            return cookies

    except Exception as e:
        logger.error("Elevated cookie extraction failed: %s", e)
        return []
    finally:
        Path(script_file.name).unlink(missing_ok=True)
        Path(cookie_file.name).unlink(missing_ok=True)


def cookies_to_playwright(cookies: list[dict]) -> list[dict]:
    """Convert rookiepy cookies to Playwright format."""
    pw_cookies = []
    for c in cookies:
        cookie = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
        }
        if c.get("expires"):
            cookie["expires"] = c["expires"]
        if c.get("secure"):
            cookie["secure"] = True
        if c.get("httponly"):
            cookie["httpOnly"] = True
        cookie["sameSite"] = "Lax"
        pw_cookies.append(cookie)
    return pw_cookies
