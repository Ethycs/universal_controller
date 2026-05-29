"""Programmatic browser API powered by Universal Controller + Playwright.

Provides a high-level Python interface for interacting with any website
using auto-detected UI patterns. The UC Chrome extension runs in two modes:

**Scan-diff-bind** (Cheat Engine style) — interaction-based:
    1. first_scan(page)        — baseline DOM snapshot
    2. (perform action via Playwright)
    3. next_scan(page)         — diff against baseline
    4. auto_detect(page)       — infer patterns from what changed

**Static detect** (three-signal scoring) — no interaction needed:
    detect(page, "search")     — structural + phrasal + semantic + behavioral
    detect_all(page)           — detect all pattern types at once

Usage::

    from uc_browser import UCBrowser, BrowserMode

    with UCBrowser(mode=BrowserMode.CHROMIUM_EXT) as uc:
        page = uc.open("https://lu.ma/discover")

        # Static detect — quick, no interaction required
        uc.detect_all(page)
        uc.search(page, "AI meetup San Francisco")

        # Scan-diff — interaction-based, higher confidence
        uc.first_scan(page)
        page.click("button.filter")           # perform an action
        diff = uc.next_scan(page)             # see what changed
        patterns = uc.auto_detect(page)       # infer what pattern it was
"""

import logging
import os
import shutil
import sys
import time
import warnings
from enum import Enum
from pathlib import Path
from typing import Optional

from playwright.sync_api import Error as PlaywrightError

from uc_browser import _paths

# Optional integration: event_harvester's navigation_trace.attach (used to
# attach observability spans when the harvest pipeline is in use). When the
# package is used standalone, the soft import is a no-op.
try:
    from event_harvester.navigation_trace import attach as _nav_trace_attach
except ImportError:  # pragma: no cover - exercised when standalone
    _nav_trace_attach = None

logger = logging.getLogger("uc_browser")


class BrowserMode(str, Enum):
    """Browser launch mode for UCBrowser.

    - CHROME: Installed Chrome via Playwright channel="chrome" + persistent
      profile at data/.chrome_profile. Best for logged-in scraping
      (Instagram, LinkedIn, etc.).
    - CHROMIUM_EXT: Playwright's bundled Chromium with the UC extension
      loaded. Best for chat automation, pattern detection, anchor-locking.
    - NATIVE_CDP: Launch the user's actual Chrome via subprocess + CDP.
      No automation flags, real profile, real extensions. Best for
      Cloudflare-protected sites.
    - HEADLESS: Headless Chrome, no extension. Best for CI/batch scraping
      where saved storage_state covers auth.
    """

    CHROME = "chrome"
    CHROMIUM_EXT = "chromium-ext"
    NATIVE_CDP = "native-cdp"
    HEADLESS = "headless"

# Extension dir defaults to ../extension/ (sibling of this package inside the
# universal_controller submodule); override via UC_EXTENSION_DIR.
_EXT_DIR = _paths.ext_dir()

# Profile + state paths default to CWD-relative (so event_harvester's data/
# dir keeps working). Each is overridable via env var for standalone use.
_CHROME_PROFILE = Path(
    os.environ.get("UC_CHROME_PROFILE", "data/.chrome_profile"),
).resolve()
# Clean profile dir for native_chrome=True mode. We deliberately do NOT
# copy the user's full Chrome User Data into here any more — only the
# KeePassXC-Browser extension is loaded via --load-extension. The old
# data/.native_chrome_profile path is left untouched for backwards
# compatibility; users can delete it manually.
_NATIVE_PROFILE = Path(
    os.environ.get("UC_NATIVE_PROFILE", "data/.uc_chrome_profile"),
).resolve()
_STATE_FILE = Path(
    os.environ.get("UC_STATE_FILE", "data/.playwright_state.json"),
)


# ── Generic API shim ────────────────────────────────────────────────
#
# Installed via context.add_init_script so every page in every tab gets
# ``window.__UC_apiShim`` available before any site JS runs.
#
# The point: when a site decorates its own ``fetch`` (rotating auth
# tokens, CSRF headers, anti-bot signing, etc.), calling fetch from
# *inside* the page picks up all of that for free. Site-specific Python
# clients hand us a URL + JSON body; we POST it through the page's
# context; the site's JS handles the rotating/signing automatically.
#
# Site recipes can also stream via ``__UC_apiShim.stream(url, opts, callbackName)``
# — Playwright's ``page.expose_function()`` is the bridge that delivers
# each chunk back to Python as the response arrives.

_UC_API_SHIM_JS = r"""
(() => {
  if (window.__UC_apiShim) return;

  // ── Interceptor registry ────────────────────────────────────
  // Site recipes call ``__UC_apiShim.addInterceptor(regexSrc, cbName)``
  // to register a URL pattern + the name of a window-bound function
  // (typically created via Playwright's ``page.expose_function``).
  // When ``window.fetch`` is called and the URL matches, we clone the
  // response and deliver ``{url, status, body, done: true}`` to the
  // callback. The site recipe parses ``body`` (often newline-JSON).
  //
  // The hook is installed HERE — inside the init script — so it's in
  // place before any page bundle has a chance to capture a local
  // reference to ``window.fetch``.
  const _interceptors = [];
  const _origFetch = window.fetch;
  window.fetch = function(input, init) {
    let url = '';
    try {
      url = typeof input === 'string' ? input : (input?.url || String(input));
    } catch (e) {}
    const p = _origFetch.apply(this, arguments);
    if (_interceptors.length === 0) return p;
    for (const ic of _interceptors) {
      let match = false;
      try { match = ic.regex.test(url); } catch (e) {}
      if (!match) continue;
      p.then(async r => {
        let clone;
        try { clone = r.clone(); } catch (e) { return; }
        let body = '';
        try { body = await clone.text(); } catch (e) {}
        const cb = window[ic.callbackName];
        if (typeof cb !== 'function') return;
        try { await cb({url, status: r.status, body, done: true}); } catch (e) {}
      }).catch(() => {});
    }
    return p;
  };

  window.__UC_apiShim = {
    /**
     * Register a URL-pattern interceptor. ``regexSrc`` is a string
     * RegExp body (no slashes); ``callbackName`` is the global window
     * function to invoke with the response payload.
     */
    addInterceptor(regexSrc, callbackName) {
      _interceptors.push({regex: new RegExp(regexSrc), callbackName});
      return _interceptors.length;
    },

    /**
     * Drop interceptors whose callback name matches.
     */
    removeInterceptor(callbackName) {
      for (let i = _interceptors.length - 1; i >= 0; i--) {
        if (_interceptors[i].callbackName === callbackName) {
          _interceptors.splice(i, 1);
        }
      }
    },

    /**
     * One-shot fetch via the page's own context. Returns
     * {status, ok, headers, body}. Body is text — caller parses.
     * Note: only works on sites whose auth machinery decorates
     * ``window.fetch`` (or none) — sites with apiClient-level
     * signing (e.g. Grok's ``x-statsig-id``) reject these calls.
     */
    async fetch(url, opts) {
      opts = opts || {};
      const init = {
        method: opts.method || 'POST',
        headers: Object.assign(
          {'Content-Type': 'application/json'},
          opts.headers || {}
        ),
        credentials: opts.credentials || 'include',
      };
      if (opts.body !== undefined) {
        init.body = (typeof opts.body === 'string')
          ? opts.body
          : JSON.stringify(opts.body);
      }
      // Use the original fetch so we don't recursively re-hook.
      const r = await _origFetch.call(window, url, init);
      let bodyText = '';
      try { bodyText = await r.text(); } catch (e) { bodyText = '[body read failed]'; }
      return {
        status: r.status,
        ok: r.ok,
        headers: Object.fromEntries(r.headers),
        body: bodyText,
      };
    },

    /**
     * Streaming fetch: invokes window[callbackName](chunkStr) for each
     * decoded chunk as it arrives. The callback must be bound via
     * Playwright's page.expose_function() before this is called.
     * Returns {status, ok, headers} once the stream is fully drained.
     */
    async stream(url, opts, callbackName) {
      opts = opts || {};
      const cb = window[callbackName];
      if (typeof cb !== 'function') {
        throw new Error('__UC_apiShim.stream: callback not bound: ' + callbackName);
      }
      const init = {
        method: opts.method || 'POST',
        headers: Object.assign(
          {'Content-Type': 'application/json'},
          opts.headers || {}
        ),
        credentials: opts.credentials || 'include',
      };
      if (opts.body !== undefined) {
        init.body = (typeof opts.body === 'string')
          ? opts.body
          : JSON.stringify(opts.body);
      }
      const r = await _origFetch.call(window, url, init);
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        const chunk = dec.decode(value, {stream: true});
        try { await cb(chunk); } catch (e) {}
      }
      return {
        status: r.status,
        ok: r.ok,
        headers: Object.fromEntries(r.headers),
      };
    },
  };
})();
"""


def _find_real_chrome_profile() -> Path | None:
    """Find the user's real Chrome profile directory."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            p = Path(local) / "Google" / "Chrome" / "User Data"
            if p.exists():
                return p
    else:
        p = Path.home() / ".config" / "google-chrome"
        if p.exists():
            return p
    return None


def _is_chrome_running() -> bool:
    """Check if Chrome is already running."""
    import subprocess as sp

    try:
        if sys.platform == "win32":
            result = sp.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return "chrome.exe" in result.stdout.lower()
        else:
            result = sp.run(["pgrep", "-x", "chrome"], capture_output=True, timeout=5)
            return result.returncode == 0
    except Exception as e:
        logger.debug("_is_chrome_running check failed: %s", e)
        return False


_SKIP_DIRS = frozenset({
    "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "Service Worker", "blob_storage", "File System", "IndexedDB",
    "Session Storage", "Local Storage", "GCM Store",
    "BrowserMetrics", "CdmStorage", "optimization_guide_prediction_model_downloads",
})

# Official KeePassXC-Browser extension ID (Chrome Web Store)
_KEEPASSXC_EXT_ID = "oboonakemofpalcgghocfoadofidjkkk"
_KEEPASSXC_DST = Path("data/.keepassxc_extension").resolve()


def _ensure_keepassxc_extension() -> Path | None:
    """Locate the KeePassXC-Browser extension in the user's installed
    Chrome and stage it as a clean unpacked copy under
    ``data/.keepassxc_extension`` for ``--load-extension``.

    Returns the staged path on success, or None if the extension isn't
    installed locally. The copy strips Chrome's ``_metadata`` directory
    (which can confuse unpacked-extension loading) but keeps everything
    else.
    """
    real_profile = _find_real_chrome_profile()
    if real_profile is None:
        return None
    ext_root = real_profile / "Default" / "Extensions" / _KEEPASSXC_EXT_ID
    if not ext_root.exists():
        return None
    versions = sorted(p for p in ext_root.iterdir() if p.is_dir())
    if not versions:
        return None
    source = versions[-1]  # latest version directory

    try:
        if _KEEPASSXC_DST.exists():
            shutil.rmtree(_KEEPASSXC_DST)
        _KEEPASSXC_DST.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.name == "_metadata":
                continue
            dst = _KEEPASSXC_DST / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dst))
            else:
                shutil.copy2(str(item), str(dst))
        logger.info("Staged KeePassXC extension from %s -> %s", source, _KEEPASSXC_DST)
        return _KEEPASSXC_DST
    except Exception as e:
        logger.warning("Failed to stage KeePassXC extension: %s", e)
        return None


def _copy_chrome_auth(src_profile: Path, dst_dir: Path) -> None:
    """Copy Chrome profile to a working directory for CDP use.

    Copies the full Default profile minus large cache/storage directories.
    Chrome needs a consistent profile state to start — partial copies crash.
    """
    # Top-level files (Local State has the encryption key)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_profile.iterdir():
        if item.is_file():
            try:
                shutil.copy2(str(item), str(dst_dir / item.name))
            except Exception as e:
                logger.debug("Skip top-level file %s: %s", item.name, e)

    # Default profile — full copy minus caches
    default_src = src_profile / "Default"
    default_dst = dst_dir / "Default"
    if not default_src.exists():
        return

    default_dst.mkdir(parents=True, exist_ok=True)
    for item in default_src.iterdir():
        dst_item = default_dst / item.name
        if item.name in _SKIP_DIRS:
            continue
        try:
            if item.is_dir():
                if not dst_item.exists():
                    shutil.copytree(str(item), str(dst_item), dirs_exist_ok=True)
            else:
                shutil.copy2(str(item), str(dst_item))
        except Exception as e:
            # Locked/inaccessible files are expected (Chrome may hold handles)
            logger.debug("Skip locked profile item %s: %s", item.name, e)

    logger.info("Copied Chrome profile to %s", dst_dir)


def _inject_chrome_cookies(context) -> int:
    """Inject cookies from the user's real Chrome into a Playwright context.

    Uses rookiepy to decrypt Chrome cookies, converts to Playwright format,
    and adds them to the context. Returns number of cookies injected.
    """
    try:
        from uc_browser.chrome_cookies import (
            cookies_to_playwright,
            get_chrome_cookies,
        )

        raw = get_chrome_cookies()
        if not raw:
            return 0
        pw_cookies = cookies_to_playwright(raw)
        context.add_cookies(pw_cookies)
        logger.info("Injected %d cookies from Chrome.", len(pw_cookies))
        return len(pw_cookies)
    except Exception as e:
        logger.debug("Cookie injection skipped: %s", e)
        return 0


class UCBrowser:
    """High-level browser automation using UC pattern detection + Playwright.

    Manages browser lifecycle and provides methods to interact with
    auto-detected UI patterns on any website.
    """

    def __init__(
        self,
        mode: Optional[BrowserMode] = None,
        *,
        channel: str = "chrome",
        stealth: bool = True,
        timeout_ms: int = 30000,
        # Legacy flags — deprecated, prefer `mode` instead.
        headless: Optional[bool] = None,
        use_extension: Optional[bool] = None,
        native_chrome: Optional[bool] = None,
    ):
        # Resolve mode from explicit param or legacy flags
        if mode is None:
            mode = self._infer_mode_from_legacy_flags(
                headless, use_extension, native_chrome,
            )
        elif any(x is not None for x in (headless, use_extension, native_chrome)):
            warnings.warn(
                "UCBrowser: legacy flags (headless/use_extension/native_chrome) "
                "are ignored when `mode` is given; pass only one.",
                DeprecationWarning, stacklevel=2,
            )
        self.mode = mode
        self.channel = channel
        self.use_stealth = stealth
        self.timeout_ms = timeout_ms
        self._pw = None
        self._context = None
        self._browser = None
        self._stealth_plugin = None
        self._chrome_proc = None

    @staticmethod
    def _infer_mode_from_legacy_flags(
        headless: Optional[bool],
        use_extension: Optional[bool],
        native_chrome: Optional[bool],
    ) -> BrowserMode:
        """Map the old boolean flags to a BrowserMode."""
        # Default precedence (matches the old start() conditional order):
        #   native_chrome > headless > use_extension > chrome
        if native_chrome:
            return BrowserMode.NATIVE_CDP
        if headless and not use_extension:
            return BrowserMode.HEADLESS
        # Default ON: use_extension was True by default in the old API,
        # so anyone not passing flags lands on CHROMIUM_EXT.
        if use_extension is None or use_extension:
            return BrowserMode.CHROMIUM_EXT
        return BrowserMode.CHROME

    # ── Legacy flag compatibility shims (deprecated) ────────────────────

    @property
    def headless(self) -> bool:
        return self.mode == BrowserMode.HEADLESS

    @property
    def use_extension(self) -> bool:
        return self.mode == BrowserMode.CHROMIUM_EXT

    @property
    def native_chrome(self) -> bool:
        return self.mode == BrowserMode.NATIVE_CDP

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        """Launch the browser according to self.mode."""
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()

        starters = {
            BrowserMode.NATIVE_CDP: self._start_native_chrome,
            BrowserMode.HEADLESS: self._start_headless,
            BrowserMode.CHROMIUM_EXT: self._start_chromium_ext,
            BrowserMode.CHROME: self._start_chrome,
        }
        try:
            starters[self.mode]()
        except KeyError:
            raise ValueError(f"Unknown BrowserMode: {self.mode}")

        if self.use_stealth and self.mode != BrowserMode.NATIVE_CDP:
            self._init_stealth()

        # Install the generic API shim so site recipes can call the page's
        # own ``fetch`` from Python — inheriting whatever auth / token
        # rotation / anti-bot decoration the site's JS does. Site-agnostic;
        # site recipes supply the endpoint, body, and response parser.
        try:
            if self._context is not None:
                self._context.add_init_script(_UC_API_SHIM_JS)
                logger.info("Installed __UC_apiShim into context.")
        except PlaywrightError as e:
            logger.debug("add_init_script(api shim) failed: %s", e)

    def _start_headless(self) -> None:
        """Headless Chrome with saved storage_state for auth."""
        self._browser = self._pw.chromium.launch(
            headless=True, channel=self.channel,
        )
        if _STATE_FILE.exists():
            self._context = self._browser.new_context(
                storage_state=str(_STATE_FILE),
            )
        else:
            self._context = self._browser.new_context()

    def _start_chromium_ext(self) -> None:
        """Playwright Chromium + UC extension (branded Chrome blocks
        --load-extension, so use bundled Chromium)."""
        ext_args = []
        if _EXT_DIR.is_dir() and (_EXT_DIR / "manifest.json").exists():
            ext_path = str(_EXT_DIR.resolve())
            ext_args = [
                f"--load-extension={ext_path}",
                f"--disable-extensions-except={ext_path}",
            ]
            logger.info("Loading UC extension from %s", ext_path)
        else:
            logger.warning("UC extension not found at %s", _EXT_DIR)
        profile_dir = str((_CHROME_PROFILE.parent / ".uc_chromium_profile").resolve())
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            channel=None,  # Playwright's bundled Chromium
            args=[
                "--disable-blink-features=AutomationControlled",
                *ext_args,
            ],
        )
        # Inject saved cookies from web_login (stored in .playwright_state.json)
        if _STATE_FILE.exists():
            try:
                import json
                state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                cookies = state.get("cookies", [])
                if cookies:
                    self._context.add_cookies(cookies)
                    logger.info("Injected %d cookies from saved session.", len(cookies))
            except Exception as e:
                logger.debug("Cookie injection from state file skipped: %s", e)

    def _start_chrome(self) -> None:
        """Installed Chrome + logged-in persistent profile."""
        _CHROME_PROFILE.parent.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            str(_CHROME_PROFILE),
            headless=False,
            channel=self.channel,
            args=["--disable-blink-features=AutomationControlled"],
        )

    def _init_stealth(self) -> None:
        """Apply playwright-stealth plugin if available."""
        try:
            from playwright_stealth import Stealth
            self._stealth_plugin = Stealth()
        except ImportError:
            logger.warning("playwright-stealth not installed, skipping stealth")

    def _start_native_chrome(self) -> None:
        """Launch actual Chrome via subprocess, connect Playwright via CDP.

        No automation flags, real user profile, real extensions — Chrome
        runs exactly as the user would launch it, plus a debug port.
        """
        import subprocess
        import time as _time

        chrome_path = shutil.which("chrome") or shutil.which("google-chrome")
        if not chrome_path:
            # Windows default paths
            for candidate in [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
            ]:
                if candidate.exists():
                    chrome_path = str(candidate)
                    break

        if not chrome_path:
            logger.error("Chrome not found. Falling back to Playwright channel='chrome'.")
            _CHROME_PROFILE.parent.mkdir(parents=True, exist_ok=True)
            self._context = self._pw.chromium.launch_persistent_context(
                str(_CHROME_PROFILE), headless=False, channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            )
            return

        port = 9222
        # Chrome requires a non-default --user-data-dir for remote
        # debugging. We use a fresh, otherwise-empty profile and only
        # load the KeePassXC-Browser extension (if it's installed in
        # the user's real Chrome) so logins can autofill from the
        # KeePassXC desktop database. Skipping the full Default-profile
        # copy avoids the "Failed to load extension from <data/...>"
        # noise that came from Chrome trying to re-resolve every other
        # extension referenced in the copied Preferences.
        profile_dir = str(_NATIVE_PROFILE)
        Path(profile_dir).mkdir(parents=True, exist_ok=True)

        cmd = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        keepass_path = _ensure_keepassxc_extension()
        if keepass_path is not None:
            cmd.extend([
                f"--load-extension={keepass_path}",
                f"--disable-extensions-except={keepass_path}",
            ])
            logger.info("Loading KeePassXC extension from %s", keepass_path)
        else:
            logger.info(
                "KeePassXC extension not found in installed Chrome; "
                "launching without autofill support.",
            )

        logger.info("Launching native Chrome: %s", " ".join(cmd[:2]))
        self._chrome_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Wait for CDP to be ready (retry until Chrome's debug port comes up)
        last_err = None
        for _ in range(15):
            _time.sleep(1)
            try:
                self._browser = self._pw.chromium.connect_over_cdp(
                    f"http://localhost:{port}",
                )
                break
            except Exception as e:
                last_err = e
                continue
        else:
            if last_err:
                logger.debug("CDP connect retries exhausted, last error: %s", last_err)
            logger.error("Failed to connect to native Chrome on port %d", port)
            self._chrome_proc.kill()
            self._chrome_proc = None
            return

        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        logger.info("Connected to native Chrome via CDP (port %d).", port)

    def close(self) -> None:
        """Shut down browser and Playwright."""
        if self._context:
            try:
                self._context.close()
            except Exception as e:
                logger.debug("context.close() failed during teardown: %s", e)
        if self._browser:
            try:
                self._browser.close()
            except Exception as e:
                logger.debug("browser.close() failed during teardown: %s", e)
        if self._chrome_proc:
            try:
                self._chrome_proc.terminate()
                self._chrome_proc.wait(timeout=5)
            except Exception as e:
                logger.debug("chrome_proc.terminate() failed, killing: %s", e)
                self._chrome_proc.kill()
            self._chrome_proc = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception as e:
                logger.debug("playwright.stop() failed during teardown: %s", e)
        self._context = self._browser = self._pw = None

    # ── Page management ─────────────────────────────────────────────────

    def open(
        self,
        url: str,
        wait_ms: int = 2000,
        *,
        source_name: str | None = None,
        tracer: "NavigationTracer | None" = None,
        capture: "ScreenshotCapture | None" = None,
    ) -> "Page":
        """Open a URL in a new tab, wait for load, return the Playwright Page.

        If ``tracer`` is provided, navigation events are recorded to a
        per-(source, run) JSONL file. ``source_name`` is accepted for
        symmetry with ``open_with_intercept`` but the tracer carries its
        own source_name internally. When ``capture`` is also provided,
        per-step PNG screenshots are written alongside each JSONL row.
        """
        page = self._context.new_page()
        if self._stealth_plugin:
            self._stealth_plugin.apply_stealth_sync(page)
        if tracer is not None and _nav_trace_attach is not None:
            _nav_trace_attach(tracer, page, capture=capture)
            # Initial "open" record happens BEFORE goto — no DOM yet to
            # snapshot, so no image even when capture is non-None.
            tracer.record(kind="open", url=url, parent=None)
        page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        # After goto+settle, take a "loaded" screenshot when capture is on
        # so the trace has at least one image even if no framenavigated
        # event fires (single-page apps, hash routers, etc.).
        if tracer is not None and capture is not None:
            tracer.record_with_screenshot(
                kind="framenavigated", url=page.url, page=page,
                capture=capture, parent=1, label="initial-load",
            )
        return page

    def open_with_intercept(
        self,
        url: str,
        api_pattern: str,
        wait_ms: int = 2000,
        *,
        source_name: str | None = None,
        tracer: "NavigationTracer | None" = None,
        capture: "ScreenshotCapture | None" = None,
    ) -> tuple:
        """Open URL with API response interception.

        Registers a response handler *before* navigation so responses
        are captured from the first load. Returns (page, captured) where
        captured is a mutable list of (url, body) tuples that fills as
        matching responses arrive.

        If ``tracer`` is provided, navigation events (open/framenavigated/
        popup/close) are recorded to a per-(source, run) JSONL file.
        XHR records are emitted by the caller after the scroll loop —
        the tracer is not bound into ``_on_response`` here to keep this
        method's response handler independent. When ``capture`` is also
        provided, per-step PNG screenshots are written alongside each
        JSONL row.
        """
        import re as _re

        page = self._context.new_page()
        if self._stealth_plugin:
            self._stealth_plugin.apply_stealth_sync(page)

        if tracer is not None and _nav_trace_attach is not None:
            _nav_trace_attach(tracer, page, capture=capture)
            tracer.record(kind="open", url=url, parent=None)

        captured: list[tuple[str, str]] = []
        pattern = _re.compile(api_pattern)

        def _on_response(response):
            try:
                if not pattern.search(response.url):
                    return
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type:
                    return
                body = response.text()
                if len(body) > 100:
                    captured.append((response.url, body))
            except Exception as e:
                logger.debug("response handler error for %s: %s", response.url, e)

        page.on("response", _on_response)
        page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(wait_ms)
        # See open(): post-goto snapshot guarantees one image per session.
        if tracer is not None and capture is not None:
            tracer.record_with_screenshot(
                kind="framenavigated", url=page.url, page=page,
                capture=capture, parent=1, label="initial-load",
            )
        return page, captured

    def _wait_ready(self, page, timeout_ms: int = 5000) -> bool:
        """Wait for the UC extension to be loaded on this page."""
        try:
            page.wait_for_function(
                "window.__UC && window.__UC.ready === true",
                timeout=timeout_ms,
            )
            return True
        except PlaywrightError as e:
            logger.debug("UC extension not ready on %s: %s", page.url, e)
            return False

    # ── Scan-diff-bind workflow (interaction-based) ─────────────────────

    def first_scan(self, page) -> Optional[dict]:
        """Take a baseline DOM snapshot. Call before performing an action.

        Returns scan summary: {elements, timestamp} or None.
        """
        if not self._wait_ready(page):
            return None
        try:
            result = page.evaluate("window.__UC_firstScan()")
            logger.info("First scan: %d elements captured", result.get("elements", 0))
            return result
        except Exception as e:
            logger.error("first_scan failed: %s", e)
            return None

    def next_scan(self, page) -> Optional[dict]:
        """Diff against the baseline after an action was performed.

        Returns diff summary: {changed, added, removed, increased, decreased} or None.
        """
        try:
            result = page.evaluate("window.__UC_nextScan()")
            logger.info(
                "Next scan: %d changed, %d added, %d removed",
                result.get("changed", 0), result.get("added", 0), result.get("removed", 0),
            )
            return result
        except Exception as e:
            logger.error("next_scan failed: %s", e)
            return None

    def auto_detect(self, page) -> list[dict]:
        """Infer patterns from the last diff (Cheat Engine style).

        Must be called after first_scan() → action → next_scan().
        Returns list of detected patterns: [{pattern, confidence, proof, selector}].
        """
        try:
            results = page.evaluate("window.__UC_autoDetect()")
            for r in results:
                logger.info(
                    "Auto-detected %s (%.0f%%): %s",
                    r.get("pattern"), r.get("confidence", 0) * 100, r.get("proof"),
                )
            return results
        except Exception as e:
            logger.error("auto_detect failed: %s", e)
            return []

    def scan_action(self, page, action: callable) -> list[dict]:
        """Convenience: first_scan → action → next_scan → auto_detect.

        Args:
            page: Playwright page.
            action: Callable that performs the interaction (receives page as arg).

        Returns:
            List of detected patterns from the diff.

        Example::

            patterns = uc.scan_action(page, lambda p: p.click("button.filter"))
        """
        self.first_scan(page)
        action(page)
        page.wait_for_timeout(500)
        self.next_scan(page)
        return self.auto_detect(page)

    # ── Static detection (three-signal, no interaction needed) ──────────

    def detect(self, page, pattern_name: str, guarantee: str = "BEHAVIORAL") -> list[dict]:
        """Detect a specific pattern type using three-signal scoring.

        Args:
            page: Playwright page.
            pattern_name: One of: search, feed, form, modal, login, cookie, chat, dropdown.
            guarantee: Confidence level: STRUCTURAL, SEMANTIC, BEHAVIORAL, VERIFIED.

        Returns:
            List of detected candidates sorted by confidence.
        """
        if not self._wait_ready(page):
            return []
        try:
            results = page.evaluate(
                "(args) => window.__UC_detect(args[0], args[1])",
                [pattern_name, guarantee],
            )
            if results:
                logger.info(
                    "Detected %d %s candidate(s), best=%.2f",
                    len(results), pattern_name, results[0].get("confidence", 0),
                )
            return results or []
        except Exception as e:
            logger.error("detect(%s) failed: %s", pattern_name, e)
            return []

    def detect_all(self, page, guarantee: str = "BEHAVIORAL") -> dict:
        """Detect all pattern types at once using three-signal scoring.

        Returns dict mapping pattern name → list of candidates.
        """
        if not self._wait_ready(page):
            return {}
        try:
            results = page.evaluate(
                "(g) => window.__UC_detectAll(g)", guarantee,
            )
            for ptype, hits in (results or {}).items():
                if hits:
                    logger.info(
                        "Detected %s: %d hit(s), best=%.2f",
                        ptype, len(hits), hits[0].get("confidence", 0),
                    )
            return results or {}
        except Exception as e:
            logger.error("detect_all failed: %s", e)
            return {}

    def get_patterns(self, page) -> dict:
        """Read current detected patterns from window.__UC (no new detection)."""
        try:
            uc = page.evaluate("window.__UC")
            return (uc or {}).get("patterns", {})
        except PlaywrightError as e:
            logger.debug("get_patterns failed: %s", e)
            return {}

    # ── Pattern-driven actions ──────────────────────────────────────────

    def dismiss_cookies(self, page) -> bool:
        """Detect and click the cookie consent accept button."""
        self.detect(page, "cookie")
        try:
            return page.evaluate("window.__UC_dismiss()") is True
        except PlaywrightError as e:
            logger.debug("dismiss_cookies failed: %s", e)
            return False

    def search(self, page, query: str, submit: bool = True) -> bool:
        """Type a query into the detected search bar.

        Runs static detection for search first, then fills the best match.
        """
        hits = self.detect(page, "search")
        if not hits:
            logger.warning("No search bar detected on %s", page.url)
            return False

        best = hits[0]
        selector = best.get("input_selector") or best["selector"]
        logger.info("Filling search: %s (confidence=%.2f)", selector, best["confidence"])

        try:
            page.fill(selector, query)
            if submit:
                page.press(selector, "Enter")
                page.wait_for_timeout(2000)
            return True
        except PlaywrightError as e:
            logger.debug("Playwright fill failed (%s), trying JS", e)
            try:
                filled = page.evaluate("(q) => window.__UC_fillSearch(q)", query)
                if filled and submit:
                    page.press(selector, "Enter")
                    page.wait_for_timeout(2000)
                return bool(filled)
            except PlaywrightError as e2:
                logger.debug("JS-fallback fill failed: %s", e2)
                return False

    def get_feed_text(self, page) -> str:
        """Extract text from the detected feed container, or full page."""
        self.detect(page, "feed")
        try:
            return page.evaluate("window.__UC_getVisibleText()") or ""
        except PlaywrightError as e:
            logger.debug("get_feed_text via UC failed, falling back: %s", e)
            return page.evaluate("document.body.innerText") or ""

    def get_feed_items(self, page) -> list[str]:
        """Extract individual feed item texts using detected item selector."""
        hits = self.detect(page, "feed")
        if not hits:
            return [page.evaluate("document.body.innerText") or ""]

        item_sel = hits[0].get("item_selector")
        if not item_sel:
            return [self.get_feed_text(page)]

        try:
            return page.evaluate(
                """(sel) => {
                    const items = document.querySelectorAll(sel);
                    return Array.from(items).map(el => el.innerText.trim()).filter(t => t.length > 10);
                }""",
                item_sel,
            )
        except PlaywrightError as e:
            logger.debug("get_feed_items via item_selector failed: %s", e)
            return [self.get_feed_text(page)]

    def scroll_feed(self, page, seconds: int = 15, on_item: callable = None) -> list[str]:
        """Scroll the detected feed container, collecting item texts.

        Uses static detection to find the feed, then scrolls it.
        """
        hits = self.detect(page, "feed")
        feed_selector = hits[0]["selector"] if hits else None

        seen_texts = set()
        all_items = []
        end_time = time.time() + seconds

        while time.time() < end_time:
            if feed_selector:
                page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (el) el.scrollTop += el.clientHeight * 2;
                        else window.scrollBy(0, window.innerHeight * 2);
                    }""",
                    feed_selector,
                )
            else:
                page.evaluate("window.scrollBy(0, window.innerHeight * 2)")

            page.wait_for_timeout(800)

            current = self.get_feed_items(page)
            for text in current:
                if text not in seen_texts:
                    seen_texts.add(text)
                    all_items.append(text)
                    if on_item:
                        on_item(text)

        logger.info("Scroll complete: %d items in %ds", len(all_items), seconds)
        return all_items

    def fill_form(self, page, fields: dict[str, str]) -> bool:
        """Fill a detected form with field values.

        Keys are matched against input name, type, or placeholder.
        """
        hits = self.detect(page, "form")
        if not hits:
            logger.warning("No form detected on %s", page.url)
            return False

        form_fields = hits[0].get("fields", [])
        filled_any = False

        for key, value in fields.items():
            key_lower = key.lower()
            matched = None
            for f in form_fields:
                if (
                    key_lower in (f.get("name") or "").lower()
                    or key_lower in (f.get("type") or "").lower()
                    or key_lower == f.get("name", "").lower()
                ):
                    matched = f
                    break
            if matched:
                try:
                    page.fill(matched["selector"], value)
                    filled_any = True
                except Exception as e:
                    logger.debug("Failed to fill %s: %s", key, e)

        return filled_any

    def submit_form(self, page) -> bool:
        """Click the submit button on the detected form."""
        hits = self.detect(page, "form")
        if not hits:
            return False
        try:
            form_sel = hits[0]["selector"]
            btn = page.query_selector(
                f"{form_sel} button[type='submit'], {form_sel} button, {form_sel} [type='submit']"
            )
            if btn:
                btn.click()
                page.wait_for_timeout(2000)
                return True
        except PlaywrightError as e:
            logger.debug("submit_form click failed: %s", e)
        return False

    def close_modal(self, page) -> bool:
        """Dismiss the detected modal/dialog."""
        hits = self.detect(page, "modal")
        if not hits:
            return False
        dismiss_sel = hits[0].get("dismiss_selector")
        if dismiss_sel:
            try:
                page.click(dismiss_sel)
                page.wait_for_timeout(500)
                return True
            except PlaywrightError as e:
                logger.debug("close_modal click failed: %s", e)
        return False

    def has_login_wall(self, page) -> bool:
        """Check if the page has a blocking login wall."""
        hits = self.detect(page, "login")
        return any(h.get("blocking") for h in hits)

    # ── Full UC action APIs (requires bind) ───────────────────────────

    def bind(self, page, pattern_name: str) -> Optional[dict]:
        """Detect and bind a pattern, creating a UC action API for it."""
        try:
            return page.evaluate("(p) => window.__UC_bind(p)", pattern_name)
        except PlaywrightError as e:
            logger.debug("bind(%s) failed: %s", pattern_name, e)
            return None

    def chat_send(self, page, text: str) -> bool:
        """Send a chat message using UC's framework-aware setText."""
        try:
            return page.evaluate("(t) => window.__UC_chatSend(t)", text) is True
        except PlaywrightError as e:
            logger.debug("chat_send failed: %s", e)
            return False

    def chat_get_messages(self, page) -> list:
        """Get all visible chat messages."""
        try:
            return page.evaluate("window.__UC_chatGetMessages()") or []
        except PlaywrightError as e:
            logger.debug("chat_get_messages failed: %s", e)
            return []

    def form_fill_uc(self, page, data: dict) -> bool:
        """Fill form using UC's priority-based field matching (name > id > type > placeholder)."""
        try:
            return page.evaluate("(d) => window.__UC_formFill(d)", data) is not False
        except PlaywrightError as e:
            logger.debug("form_fill_uc failed: %s", e)
            return False

    def form_get_values(self, page) -> dict:
        """Get current form field values."""
        try:
            return page.evaluate("window.__UC_formGetValues()") or {}
        except PlaywrightError as e:
            logger.debug("form_get_values failed: %s", e)
            return {}

    def dropdown_toggle(self, page) -> bool:
        """Toggle detected dropdown."""
        try:
            return page.evaluate("window.__UC_dropdownToggle()") is True
        except PlaywrightError as e:
            logger.debug("dropdown_toggle failed: %s", e)
            return False

    def dropdown_select(self, page, value: str) -> bool:
        """Select a dropdown option by text."""
        try:
            return page.evaluate("(v) => window.__UC_dropdownSelect(v)", value) is True
        except PlaywrightError as e:
            logger.debug("dropdown_select failed: %s", e)
            return False

    def modal_close_uc(self, page) -> bool:
        """Close modal using UC's method (button click + Escape fallback)."""
        try:
            return page.evaluate("window.__UC_modalClose()") is True
        except PlaywrightError as e:
            logger.debug("modal_close_uc failed: %s", e)
            return False

    # ── Advanced: LLM context, heap scan, passive, signatures ──────────

    def get_llm_context(self, page, pattern_name: str = "search") -> Optional[str]:
        """Extract LLM-formatted context for a detected pattern."""
        try:
            return page.evaluate("(p) => window.__UC_getLLMContext(p)", pattern_name)
        except PlaywrightError as e:
            logger.debug("get_llm_context failed: %s", e)
            return None

    def heap_scan(self, page, pattern_name: str = None) -> Optional[dict]:
        """Scan React/Vue/Angular internals for the detected element."""
        try:
            return page.evaluate("(p) => window.__UC_heapScan(p)", pattern_name)
        except PlaywrightError as e:
            logger.debug("heap_scan failed: %s", e)
            return None

    def scan_framework(self, page) -> Optional[dict]:
        """Detect which frontend framework the page uses."""
        try:
            return page.evaluate("window.__UC_scanFramework()")
        except PlaywrightError as e:
            logger.debug("scan_framework failed: %s", e)
            return None

    def start_passive(self, page) -> bool:
        """Start passive detection (MutationObserver + event correlation)."""
        try:
            return page.evaluate("window.__UC_startPassive()") is True
        except PlaywrightError as e:
            logger.debug("start_passive failed: %s", e)
            return False

    def stop_passive(self, page) -> bool:
        """Stop passive detection."""
        try:
            return page.evaluate("window.__UC_stopPassive()") is True
        except PlaywrightError as e:
            logger.debug("stop_passive failed: %s", e)
            return False

    def get_passive_results(self, page) -> list:
        """Get patterns inferred by passive detection."""
        try:
            return page.evaluate("window.__UC_getPassiveResults()") or []
        except PlaywrightError as e:
            logger.debug("get_passive_results failed: %s", e)
            return []

    def save_signature(self, page, pattern_name: str) -> Optional[dict]:
        """Save a confirmed-working pattern binding for future auto-bind."""
        try:
            return page.evaluate("(p) => window.__UC_saveSignature(p)", pattern_name)
        except PlaywrightError as e:
            logger.debug("save_signature(%s) failed: %s", pattern_name, e)
            return None

    def load_signatures(self, page) -> list:
        """Load saved signatures for the current site."""
        try:
            return page.evaluate("window.__UC_loadSignatures()") or []
        except PlaywrightError as e:
            logger.debug("load_signatures failed: %s", e)
            return []

    def auto_bind_signatures(self, page) -> list:
        """Auto-bind patterns from saved signatures."""
        try:
            return page.evaluate("window.__UC_autoBindSignatures()") or []
        except PlaywrightError as e:
            logger.debug("auto_bind_signatures failed: %s", e)
            return []

    # ── Generic input/button discovery ────────────────────────────────

    def find_inputs(self, page) -> list[dict]:
        """Find all interactive inputs on the page, scored by chat-likelihood."""
        if not self._wait_ready(page):
            return []
        try:
            return page.evaluate("window.__UC_findInputs()") or []
        except PlaywrightError as e:
            logger.debug("find_inputs failed: %s", e)
            return []

    def find_buttons(self, page, input_selector: str = None) -> list[dict]:
        """Find submit/send buttons near an input element."""
        if not self._wait_ready(page):
            return []
        try:
            return page.evaluate("(s) => window.__UC_findButtons(s)", input_selector) or []
        except PlaywrightError as e:
            logger.debug("find_buttons failed: %s", e)
            return []

    def set_text(self, page, selector: str, text: str) -> dict:
        """Set text using UC's framework-aware setText (handles React/Slate/etc)."""
        try:
            return page.evaluate(
                "(args) => window.__UC_setText(args[0], args[1])", [selector, text],
            ) or {"success": False}
        except PlaywrightError as e:
            logger.debug("set_text failed: %s", e)
            return {"success": False, "error": str(e)}

    def find_new_content(self, page) -> list[dict]:
        """After a scan-diff, find where new content appeared (children-added, text-grew)."""
        try:
            return page.evaluate("window.__UC_findNewContent()") or []
        except PlaywrightError as e:
            logger.debug("find_new_content failed: %s", e)
            return []

    # ── ML-enhanced detection ────────────────────────────────────────────

    def ml_find_chat(self, page) -> dict | None:
        """Use ML classifier to find a chat input UC's heuristics missed.

        Returns the best chat_input candidate as:
          {"selector": str, "confidence": float, "label": "chat_input"}
        or None if no chat input found.

        If found, also binds it via __UC_bindBySelector so UC's chat API
        (__UC_chatSend, __UC_chatGetMessages) can use it.
        """
        try:
            from uc_browser.dom_classifier import classify_code, extract_code_features
        except ImportError:
            return None

        # Discover candidate containers (extension tags them with data-ml-id)
        candidates = page.evaluate("window.__UC_findChatCandidates()")

        if not candidates:
            return None

        best = None
        for sel in candidates:
            result = classify_code(page, sel)
            if not result:
                continue
            if result.get("label") == "chat_input":
                if not best or result["confidence"] > best["confidence"]:
                    best = {
                        "selector": sel,
                        "confidence": result["confidence"],
                        "label": "chat_input",
                    }

        # Clean up data-ml-id tags
        page.evaluate("window.__UC_clearChatCandidates()")

        if best:
            # Bind via UC so chat API works
            try:
                page.evaluate(
                    "(args) => window.__UC_bindBySelector(args[0], args[1])",
                    ["chat", best["selector"]],
                )
                logger.info(
                    "ML found chat_input (conf=%.2f) at %s — bound to UC chat API.",
                    best["confidence"], best["selector"],
                )
            except PlaywrightError as e:
                logger.debug("ml_find_chat bind failed: %s", e)

        return best

    # ── Dynamic chat interaction (full UC toolbox) ──────────────────────

    def chat(
        self, page, message: str, timeout_s: int = 30,
    ) -> Optional[str]:
        """Send a message to any chat interface and return the response.

        Uses the full UC toolbox:
        - __UC_findInputs (scored input discovery)
        - __UC_setText (framework-aware: React, Slate, ProseMirror)
        - __UC_findButtons (proximity-based button finding)
        - __UC_startPassive (background MutationObserver correlation)
        - __UC_captureBaseline + __UC_firstScan (trigram + scan-diff baselines)
        - __UC_watchContainer (real-time MutationObserver on response area)
        - __UC_extractResponse (trigram set difference for filtering)
        - __UC_findNewContent (scan-diff for container discovery)
        - __UC_saveSignature (persist working patterns per domain)
        """
        # ── Start passive detection in background ────────────────
        self.start_passive(page)

        # ── Find the input ───────────────────────────────────────
        inputs = self.find_inputs(page)

        # If UC's heuristic found a high-confidence input, use it.
        # Otherwise, try ML classifier to find chat inputs UC missed.
        if inputs and inputs[0].get("score", 0) >= 4:
            best_input = inputs[0]
            selector = best_input["selector"]
        else:
            ml_result = self.ml_find_chat(page)
            if ml_result and ml_result["confidence"] > 0.5:
                logger.info("ML override: using ML-detected chat input over UC heuristic.")
                inner_input = page.evaluate(
                    "(s) => window.__UC_resolveInnerInput(s)",
                    ml_result["selector"],
                )
                selector = inner_input or ml_result["selector"]
                best_input = {"selector": selector, "score": ml_result["confidence"] * 10,
                              "contentEditable": True, "placeholder": ""}
            elif inputs:
                best_input = inputs[0]
                selector = best_input["selector"]
            else:
                logger.warning("No interactive input found on page.")
                return None
        logger.info(
            "Input: %s (score=%.1f, ce=%s, ph='%s')",
            selector, best_input["score"],
            best_input["contentEditable"],
            best_input.get("placeholder", "")[:30],
        )

        # ── Capture baselines BEFORE typing ──────────────────────
        # Trigram baseline (for text filtering)
        try:
            bl = page.evaluate("window.__UC_captureBaseline()")
            logger.info("Trigram baseline: %d trigrams", bl.get("trigrams", 0))
        except PlaywrightError as e:
            logger.debug("captureBaseline failed: %s", e)
        # Scan-diff baseline (for container discovery)
        self.first_scan(page)

        # ── Type with UC's setText (handles contenteditable/React) ─
        result = self.set_text(page, selector, message)
        if not result.get("success"):
            logger.warning("setText failed (%s), Playwright fallback", result.get("error"))
            try:
                page.click(selector)
                if best_input.get("contentEditable"):
                    page.type(selector, message, delay=10)
                else:
                    page.fill(selector, message)
            except PlaywrightError as e:
                logger.error("Failed to type via Playwright fallback: %s", e)
                return None
        else:
            logger.info("setText OK (method=%s)", result.get("method"))

        # ── Find send button (only appears after text entry on some sites) ─
        page.wait_for_timeout(300)
        buttons = self.find_buttons(page, selector)
        btn_sel = buttons[0]["selector"] if buttons else None
        if btn_sel:
            logger.info("Send button: %s (score=%.1f)", btn_sel, buttons[0]["score"])

        # ── Set up real-time response watcher before submitting ──
        watch_started = page.evaluate(
            "(s) => window.__UC_setupResponseWatcher(s)", selector,
        )
        if watch_started:
            logger.info("Response watcher active")

        # ── Submit ───────────────────────────────────────────────
        if btn_sel:
            try:
                page.click(btn_sel)
            except PlaywrightError as e:
                logger.debug("Send button click failed (%s), pressing Enter", e)
                page.press(selector, "Enter")
        else:
            page.press(selector, "Enter")

        # ── Verify send: check if input cleared (postcondition) ──
        page.wait_for_timeout(500)
        input_cleared = page.evaluate(
            "(s) => window.__UC_isInputCleared(s)", selector,
        )
        if input_cleared:
            logger.info("Send verified: input cleared")
        else:
            logger.warning("Input not cleared — send may have failed")

        # ── Wait for response ────────────────────────────────────
        response = self._wait_chat_response(page, timeout_s, sent_message=message)

        # ── Save working pattern for this domain ─────────────────
        if response and len(response) > 10:
            try:
                # Try to bind and save signature for next visit
                self.bind(page, "chat")
                self.save_signature(page, "chat")
                logger.info("Signature saved for %s", page.url)
            except PlaywrightError as e:
                logger.debug("Signature save failed: %s", e)

        # ── Check passive detection results ──────────────────────
        try:
            passive = self.get_passive_results(page)
            if passive:
                logger.info("Passive detected %d pattern(s)", len(passive))
        except PlaywrightError as e:
            logger.debug("get_passive_results failed: %s", e)

        return response

    def _lock_response_via_anchor(
        self, page, sent_message: str,
    ) -> Optional[str]:
        """Find the AI response by anchoring on the user's message.

        Algorithm:
        1. Find DOM elements containing sent_message (the anchors).
        2. For each anchor, find the response candidate that follows it.
        3. Score each candidate via trigram newness against baseline.
        4. Tag the highest-scoring candidate with data-uc-response.

        Returns the locked selector (e.g. '[data-uc-response="1"]') or
        None if no anchor produces a high-newness response.
        """
        if not sent_message:
            return None
        try:
            anchors = page.evaluate(
                "(m) => window.__UC_findAnchorCandidates(m, 5)", sent_message,
            ) or []
        except PlaywrightError as e:
            logger.debug("findAnchorCandidates failed: %s", e)
            return None

        if not anchors:
            logger.debug("Anchor: 0 anchors found for %r", sent_message[:40])
            return None

        best = None
        best_ratio = 0.0
        msg_low = sent_message.lower().strip()
        scored_count = 0
        rejected_low_ratio = 0
        no_response = 0

        for a in anchors:
            try:
                resp = page.evaluate(
                    "(s) => window.__UC_findResponseAfterAnchor(s)",
                    a.get("selector"),
                )
            except PlaywrightError as e:
                logger.debug("findResponseAfterAnchor failed for %r: %s",
                             a.get("selector", "")[:40], e)
                continue
            if not resp:
                no_response += 1
                continue
            cand_text = (resp.get("text") or "").strip()
            if not cand_text:
                no_response += 1
                continue
            scored_count += 1
            # Reject candidates that are echoes of the sent message
            if msg_low in cand_text.lower() and len(cand_text) <= len(sent_message) + 5:
                continue
            # Score by trigram newness against the RAW baseline (do NOT add
            # sent_message trigrams — the response may legitimately echo
            # words from the prompt, e.g. user asks "say pineapple" and the
            # response is "pineapple"). Echo-rejection is handled above.
            try:
                ratio = page.evaluate(
                    """(text) => {
                        if (!window._baselineTrigrams) return 1.0;
                        const _tri = (t) => {
                            const s = new Set();
                            const lc = t.toLowerCase();
                            for (let i = 0; i <= lc.length - 3; i++) s.add(lc.slice(i, i + 3));
                            return s;
                        };
                        const bl = window._baselineTrigrams;
                        const tris = _tri(text);
                        if (!tris.size) return 0;
                        let n = 0;
                        for (const t of tris) if (!bl.has(t)) n++;
                        return n / tris.size;
                    }""",
                    cand_text,
                )
            except PlaywrightError as e:
                logger.debug("trigram ratio eval failed: %s", e)
                ratio = 0
            if ratio is None:
                ratio = 0
            # Tightened threshold: disclaimers/footers score ~0.0-0.65,
            # genuine AI responses score ~0.85-1.0. 0.7 cleanly separates.
            if ratio < 0.7:
                rejected_low_ratio += 1
                logger.debug("Anchor candidate ratio=%.2f rejected (text=%r)",
                             ratio, cand_text[:60])
                continue
            if ratio > best_ratio:
                best_ratio = ratio
                best = resp

        if not best:
            logger.debug(
                "Anchor: %d anchors, %d scored, %d rejected, %d no-response",
                len(anchors), scored_count, rejected_low_ratio, no_response,
            )
            return None

        try:
            locked = page.evaluate(
                "(s) => window.__UC_lockResponse(s)", best.get("selector"),
            )
        except PlaywrightError as e:
            logger.debug("lockResponse failed: %s", e)
            locked = None
        if locked:
            logger.info(
                "Anchor-locked response: %s (newness=%.2f)", locked, best_ratio,
            )
        return locked

    def _wait_chat_response(
        self, page, timeout_s: int = 30, sent_message: str = "",
    ) -> Optional[str]:
        """Wait for response using anchor-lock primary + legacy fallback.

        Primary path:
          1. Find user-message anchors in the DOM.
          2. For each, look at its next sibling for a response candidate.
          3. Score with trigram newness; lock the best one with data-uc-response.
          4. Poll the locked element's innerText until stable.

        Fallback path (anchor-lock fails to find a candidate):
          - Real-time observer (__UC_getObserved) for streaming text
          - Scan-diff (__UC_findNewContent) to find the conversation container
          - Trigram extraction (__UC_extractFromContainer) within the container

        Response is complete when text stabilises for 3 polls (1.5s) or
        streaming indicators disappear after 5s.
        """
        logger.info("Waiting for response (up to %ds)...", timeout_s)

        locked_selector = None
        response_selector = None  # legacy scan-diff container
        last_text = ""
        stable_count = 0
        poll_ms = 500

        for tick in range(timeout_s * 2):  # 2 ticks per second
            page.wait_for_timeout(poll_ms)

            # ── Primary: try to anchor-lock every poll until locked ──
            if not locked_selector:
                locked_selector = self._lock_response_via_anchor(page, sent_message)

            current_text = ""

            if locked_selector:
                # ── Locked: read only the tagged element ─────────
                try:
                    current_text = page.evaluate(
                        "window.__UC_readLocked()",
                    ) or ""
                except PlaywrightError as e:
                    logger.debug("readLocked at tick %d failed: %s", tick, e)
            else:
                # ── Fallback Layer 1: real-time observer ─────────
                try:
                    observed = page.evaluate("window.__UC_getObserved()")
                    if observed:
                        parts = []
                        for obs in observed:
                            t = obs.get("text", "").strip()
                            if not t or len(t) < 5:
                                continue
                            if sent_message and sent_message.lower()[:40] in t.lower():
                                continue
                            parts.append(t)
                        if parts:
                            current_text = max(parts, key=len)
                except PlaywrightError as e:
                    logger.debug("getObserved at tick %d failed: %s", tick, e)

                # ── Fallback Layer 2: scan-diff container discovery ──
                if not response_selector and tick < 20:
                    try:
                        self.next_scan(page)
                        new_content = self.find_new_content(page)
                        if new_content:
                            response_selector = new_content[0].get("selector")
                            if response_selector:
                                logger.info("Container (scan-diff): %s", response_selector)
                                page.evaluate(
                                    "(s) => window.__UC_watchContainer(s)",
                                    response_selector,
                                )
                    except PlaywrightError as e:
                        logger.debug("scan-diff discovery at tick %d failed: %s", tick, e)

                # ── Fallback Layer 3: trigram extraction in container ──
                if response_selector and not current_text:
                    try:
                        current_text = page.evaluate(
                            "(args) => window.__UC_extractFromContainer(args[0], args[1])",
                            [response_selector, sent_message],
                        ) or ""
                    except PlaywrightError as e:
                        logger.debug("extractFromContainer at tick %d failed: %s", tick, e)

            # ── Stability check ──────────────────────────────────
            min_len = 1 if locked_selector else 15  # locked = trust short answers
            if not current_text or len(current_text) < min_len:
                continue

            if current_text == last_text:
                stable_count += 1
                if stable_count >= 3:
                    logger.info("Response stabilised (%d chars).", len(current_text))
                    return current_text
            else:
                stable_count = 0
                last_text = current_text

            # ── Streaming indicator check (after 5s minimum) ─────
            if tick >= 10:
                streaming = page.evaluate("""() => {
                    return document.querySelectorAll(
                        '[class*="streaming"], [class*="typing"], '
                        + '[class*="loading"], [data-testid*="stop"]'
                    ).length > 0;
                }""")
                if not streaming and last_text and stable_count >= 2:
                    logger.info("Streaming done (%d chars).", len(last_text))
                    return last_text

        # Stop observer
        try:
            page.evaluate("window.__UC_stopWatching()")
        except PlaywrightError as e:
            logger.debug("stopWatching during teardown failed: %s", e)

        if last_text:
            logger.warning("Timed out, partial response (%d chars).", len(last_text))
            return last_text
        logger.warning("No response detected after %ds.", timeout_s)
        return None

    # ── Convenience: open + detect + act ────────────────────────────────

    def navigate_and_search(self, url: str, query: str) -> tuple:
        """Open URL → detect → clear obstacles → search → return (page, text).

        High-level convenience method that handles the full workflow.
        """
        page = self.open(url)
        self.detect_all(page)
        self.dismiss_cookies(page)
        self.close_modal(page)

        if self.has_login_wall(page):
            logger.warning("Login wall detected on %s", url)

        self.search(page, query)
        page.wait_for_timeout(1000)
        # Re-detect after search results load
        self.detect(page, "feed")
        text = self.get_feed_text(page)
        return page, text

    def navigate_and_scrape(self, url: str, scroll_seconds: int = 15) -> tuple:
        """Open URL → detect → clear obstacles → scroll feed → return (page, items).

        High-level convenience method for feed scraping.
        """
        page = self.open(url)
        self.detect_all(page)
        self.dismiss_cookies(page)
        self.close_modal(page)
        items = self.scroll_feed(page, seconds=scroll_seconds)
        return page, items
