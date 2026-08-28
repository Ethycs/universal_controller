"""Generic site client — drives ANY registered chat site through the
generic engine (``UCBrowser.chat()``): scored input discovery, ML
fallback, framework-aware setText, proximity button finding, trigram-diff
response extraction. No per-site selectors.

This is what turns a bench-passing site into a callable litellm model
without writing an adapter. Contrast with ``grok_fast.py``, the
hand-tuned fast path for grok.com — sites earn one of those when they
need it; everything else goes through here.

Threading model: one process-wide client, one browser, one send at a
time (a lock serializes). Each (site, session) pair keeps its own tab so
conversation state lives in the page across calls — same continuity
contract as the Grok driver's session_key.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("uc_browser.sites.generic")

_DEFAULT_TIMEOUT_S = 60


class GenericSiteError(RuntimeError):
    """Send failed: no composer found, or no response extracted."""


class GenericClient:
    """Browser-driven client for registry sites via the generic engine."""

    def __init__(self):
        self._uc = None
        self._pages: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    # ── browser lifecycle ───────────────────────────────────────────

    def _ensure_browser(self):
        if self._uc is None:
            from uc_browser.browser import BrowserMode, UCBrowser

            uc = UCBrowser(mode=BrowserMode.CHROMIUM_EXT)
            try:
                uc.start()
            except Exception:
                # A failed launch (e.g. profile lock held by a zombie
                # Chromium) must not leave a half-started sync Playwright
                # bound to this thread — only one instance can ever start
                # per thread, so a poisoned thread kills every later send.
                try:
                    uc.close()
                except Exception:
                    pass
                raise
            self._uc = uc
            logger.info("GenericClient: browser started (CHROMIUM_EXT).")
        return self._uc

    def close(self) -> None:
        with self._lock:
            if self._uc is not None:
                try:
                    self._uc.close()
                except Exception:
                    pass
                self._uc = None
                self._pages.clear()

    # ── sending ─────────────────────────────────────────────────────

    @staticmethod
    def _resolve_target(site: str, url: str) -> tuple[str, list[dict]]:
        """Warm start from the Signature Lab feed: a fresh site profile
        supplies the real chat page and its pre-steps. Cache, not
        dependency — no profile (or an expired one) means the registered
        URL and pure generic discovery."""
        try:
            from uc_browser.site_profiles import get_profile_store

            profile = get_profile_store().fresh(site)
            if profile:
                return profile.chat_page_url, list(profile.pre_steps)
        except Exception:
            pass
        return url, []

    def _page_for(self, site: str, url: str, session_key: Optional[str]):
        key = (site, session_key or "default")
        page = self._pages.get(key)
        if page is not None:
            try:
                if not page.is_closed():
                    return page, False
            except Exception:
                pass
        uc = self._ensure_browser()
        target, pre_steps = self._resolve_target(site, url)
        page = uc.open(target, wait_ms=4000)
        if pre_steps:
            from uc_browser.health import HealthMonitor

            for step in pre_steps:
                try:
                    if step.get("type") == "launcher-auto":
                        HealthMonitor._try_open_widget(page)
                    elif step.get("type") == "click" and step.get("selector"):
                        page.locator(step["selector"]).first.click(timeout=3000)
                except Exception:
                    continue
            page.wait_for_timeout(2000)
        self._pages[key] = page
        return page, True

    def send(self, site: str, url: str, message: str, *,
             session_key: Optional[str] = None,
             timeout_s: int = _DEFAULT_TIMEOUT_S) -> dict:
        """Send ``message`` on ``site`` and return the extracted response.

        Returns {"response": str, "site": str, "page_url": str}.
        Raises GenericSiteError when the engine can't engage the page or
        no response could be extracted before the timeout.
        """
        with self._lock:
            started = time.time()
            uc = self._ensure_browser()
            page, fresh = self._page_for(site, url, session_key)
            if fresh:
                logger.info("GenericClient[%s]: new tab → %s", site, url)

            try:
                text = uc.chat(page, message, timeout_s=timeout_s)
            except Exception as e:
                # A dead tab (site navigated/crashed) gets one retry on a
                # fresh page before we give up.
                logger.warning("GenericClient[%s]: chat() raised %s — "
                               "retrying on a fresh tab", site, e)
                self._pages.pop((site, session_key or "default"), None)
                try:
                    page.close()
                except Exception:
                    pass
                page, _ = self._page_for(site, url, session_key)
                text = uc.chat(page, message, timeout_s=timeout_s)

            if not text:
                raise GenericSiteError(
                    f"{site}: engine engaged the page but extracted no "
                    f"response within {timeout_s}s "
                    f"(elapsed {time.time() - started:.0f}s).")
            return {"response": text, "site": site, "page_url": page.url}


_client: Optional[GenericClient] = None
_client_lock = threading.Lock()


def get_generic_client() -> GenericClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = GenericClient()
            atexit.register(_client.close)
        return _client
