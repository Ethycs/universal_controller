"""Grok (grok.com) automation client.

Builds on UCBrowser primitives to expose a full-lifecycle API for a Grok
account: send, read, list, delete, rename, archive, regenerate, stop,
get_models, switch_model, new_chat.

Designed to be held as a long-lived singleton inside a process (e.g. the
MCP server) so the browser stays open across calls.

Auth:
    Relies on the persistent profile at ``data/.uc_chromium_profile/`` and
    saved cookies in ``data/.playwright_state.json``. If Grok shows a
    login wall on first call, raise ``GrokAuthRequired`` with a hint.

Resilience:
    Recipes use anchor-based selectors (role, aria-label, href patterns)
    rather than fragile class names. The ``_SELECTORS`` dict at the top
    of the module is the single point of tuning if Grok's DOM drifts.
"""

from __future__ import annotations

import atexit
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import urlparse

from uc_browser import BrowserMode, UCBrowser

logger = logging.getLogger("uc_browser.sites.grok")

_RECIPE_VERSION = 2  # bump when DOM recipes change

# Inline JS used by every caller that just typed into the composer.
# __UC_setText falls through to Method 3 (ClipboardEvent('paste')) when the
# text is long enough that Method 1/2 fail verify() — Grok intercepts that
# event and converts the paste into a 'pasted-text.txt' attachment chip
# WHILE Method 4 (directDOM) also writes the text into ProseMirror. Result
# is a double-input the user never asked for: typed text AND attached file.
# Strip the chips before submitting so only the PM text goes. Bounded loop
# because the chip remove button can chain in some layouts.
_REMOVE_ATTACHMENT_CHIPS_JS = r"""
(async () => {
  const sel = 'button[aria-label="Remove this attachment" i]';
  let removed = 0;
  for (let i = 0; i < 8; i++) {
    const btns = document.querySelectorAll(sel);
    if (!btns.length) break;
    for (const b of btns) { try { b.click(); removed++; } catch (e) {} }
    await new Promise(r => setTimeout(r, 80));
  }
  return {removed, remaining: document.querySelectorAll(sel).length};
})()
"""


# Anchor selectors verified against the live DOM. The chat-input/message
# anchors are stable test-ids; the menu/picker selectors are fuzzy and
# may need tuning when Grok's DOM drifts.
_SELECTORS: dict[str, str] = {
    # ── Core chat surface (DOM-anchored, verified) ───────────────────
    "chat_input": "div.ProseMirror",
    "user_message": 'div[data-testid="user-message"]',
    "assistant_message": 'div[data-testid="assistant-message"]',
    # ── Sidebar ──────────────────────────────────────────────────────
    "conversation_link": 'a[href*="/c/"]',
    # 3-dot / "more options" button on a conversation row in the sidebar
    "row_more_button": (
        'button[aria-label*="more" i], '
        'button[aria-label*="options" i], '
        'button[aria-haspopup="menu"], '
        'button[aria-haspopup="true"]'
    ),
    # Menu items inside an opened popover
    "menu_item": '[role="menuitem"], button',
    # Stop-generation button (shown while assistant is streaming)
    "stop_button": (
        '[data-testid*="stop" i], '
        'button[aria-label*="stop" i]'
    ),
    # Regenerate button at the bottom of an assistant message
    "regenerate_button": (
        'button[aria-label*="regenerate" i], '
        'button[aria-label*="try again" i]'
    ),
    # Model picker dropdown trigger (top header)
    "model_picker": (
        'button[aria-label*="model" i], '
        '[data-testid*="model" i] button, '
        'button[aria-haspopup="listbox"]'
    ),
    # New-chat button in the sidebar
    "new_chat_button": (
        'a[href="/"], '
        '[data-testid*="new-chat" i], '
        'button[aria-label*="new chat" i]'
    ),
    # Rename input field inside the rename modal/inline editor
    "rename_input": 'input[type="text"], textarea',
}


# ── Exceptions ───────────────────────────────────────────────────────


class GrokError(RuntimeError):
    """Base class for Grok-specific errors."""


class GrokAuthRequired(GrokError):
    """Login wall detected — user needs to run web-login first."""


# ── Helpers ──────────────────────────────────────────────────────────


_UUID_PATTERN = (
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_PATH_UUID_RE = re.compile(rf"/c/{_UUID_PATTERN}")
_QUERY_UUID_RE = re.compile(rf"[?&]chat={_UUID_PATTERN}")
# Strip Grok's "Thought for Ns" reasoning header from streamed responses.
_THOUGHT_PREFIX_RE = re.compile(r"^Thought for [0-9]+\s*s\s*\n+", flags=re.IGNORECASE)


def _conv_id_from_url(url: str) -> Optional[str]:
    """Extract the conversation UUID from any of Grok's URL shapes.

    Recognised forms:
        ``/c/<uuid>``                      — classic chat URL
        ``/project/<uuid>?chat=<uuid>``    — chat lives inside a "project" workspace
        ``...?chat=<uuid>``                — short link form
    """
    if not url:
        return None
    match = _PATH_UUID_RE.search(url) or _QUERY_UUID_RE.search(url)
    return match.group(1) if match else None


def _strip_thought_prefix(text: str) -> str:
    """Drop Grok's ``Thought for Ns`` reasoning prefix if present."""
    return _THOUGHT_PREFIX_RE.sub("", text or "", count=1)


def _normalize_url(url_or_id: str) -> str:
    """Accept either a full URL or a bare conversation id; return a URL."""
    if not url_or_id:
        return "https://grok.com/"
    if url_or_id.startswith("http"):
        return url_or_id
    return f"https://grok.com/c/{url_or_id}"


# ── GrokClient ───────────────────────────────────────────────────────


class GrokClient:
    """Stateful Grok automation client.

    Holds a single UCBrowser instance plus a per-session map of long-lived
    Pages. Each ``session_key`` owns its own tab, so *switching* between
    conversations is free — no navigation cost when jumping between
    session A's chat and session B's chat. Callers that don't care about
    session boundaries transparently share the ``_default`` page;
    sidebar-level operations (read / list / delete) always use ``_default``.

    Construct once (e.g. lazily inside the MCP server), reuse across calls.

    Threadsafety
    ------------
    All Playwright access serialises through a single client-wide lock.
    Playwright's *sync* API binds its greenlet to one thread and crashes
    if a different thread re-enters; the single lock prevents that. As
    a consequence, multi-threaded callers do *not* get true parallelism
    here — only the no-navigation tab-switch win. True parallel session
    sends require migrating UCBrowser + GrokClient to Playwright's async
    API.
    """

    GROK_HOME = "https://grok.com/"
    DEFAULT_SESSION = "_default"

    def __init__(
        self,
        *,
        mode: BrowserMode = BrowserMode.CHROMIUM_EXT,
        timeout_ms: int = 30000,
    ) -> None:
        self.mode = mode
        self.timeout_ms = timeout_ms
        self._uc: UCBrowser | None = None
        # session_key -> Page. Each session owns its own tab so switching
        # between conversations costs no navigation.
        self._pages: dict[str, object] = {}
        # Single client-wide lock. Serialises ALL Playwright access
        # (open / navigate / evaluate / etc.) because sync Playwright's
        # greenlet is thread-bound.
        self._lock = threading.RLock()
        # Kept as an attribute solely so existing tests can introspect
        # which session_keys have ever been touched. Mapped to the same
        # RLock instance; no per-session granularity is possible without
        # async Playwright.
        self._page_locks: dict[str, threading.RLock] = {}

    # ── Lifecycle ────────────────────────────────────────────────────

    def __enter__(self) -> "GrokClient":
        self._ensure_uc()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure_uc(self) -> UCBrowser:
        if self._uc is None:
            logger.info("Starting UCBrowser for Grok (mode=%s).", self.mode)
            self._uc = UCBrowser(mode=self.mode, timeout_ms=self.timeout_ms)
            self._uc.start()
        return self._uc

    def close(self) -> None:
        with self._lock:
            pages = list(self._pages.values())
            self._pages.clear()
            self._page_locks.clear()
            for p in pages:
                try:
                    p.close()
                except Exception:
                    pass
            if self._uc is not None:
                try:
                    self._uc.close()
                except Exception as e:  # pragma: no cover
                    logger.debug("UCBrowser close failed: %s", e)
                self._uc = None

    # ── Internal: per-session pages ──────────────────────────────────

    def _get_session_page(self, session_key: str):
        """Return the long-lived Page for ``session_key``, opening on first use.

        Cold start (open + extension boot + sidebar hydrate) happens once
        per session_key. Subsequent calls reuse the tab.

        Caller MUST hold ``self._lock`` (Playwright sync is thread-bound).
        """
        page = self._pages.get(session_key)
        if page is not None and not page.is_closed():
            return page
        uc = self._ensure_uc()
        logger.info("Opening Grok page for session %r (cold start).", session_key)
        # No fixed sleep: open the page and immediately wait on the
        # ProseMirror chat input. As soon as the input is attached we
        # know the bundle has booted enough to interact. Warm callers
        # return effectively instantly.
        page = uc.open(self.GROK_HOME, wait_ms=0)
        uc.dismiss_cookies(page)
        uc.close_modal(page)
        if uc.has_login_wall(page):
            current = page.url
            try:
                page.close()
            except Exception:
                pass
            raise GrokAuthRequired(
                f"Grok shows a login wall at {current}. "
                "Run `pixi run event-harvester web-login --urls https://grok.com` "
                "to log in, then retry."
            )
        try:
            page.wait_for_selector(
                _SELECTORS["chat_input"], state="attached", timeout=15000,
            )
        except Exception:
            # Some entry pages (e.g. the project workspace landing) may
            # not have the standard input. Fall through; downstream
            # recipes will surface a clearer error if interaction fails.
            pass
        self._pages[session_key] = page
        # Track the key even though all keys share the same underlying
        # RLock — useful for introspection.
        self._page_locks.setdefault(session_key, self._lock)
        return page

    @contextmanager
    def _session(self, session_key: str) -> Iterator[object]:
        """Yield this session's Page with exclusive Playwright access.

        All sessions share one client-wide lock — sync Playwright can't be
        re-entered from a different thread. Distinct sessions still win
        because their tabs persist, so we never pay navigation cost when
        we switch between them.
        """
        with self._lock:
            page = self._get_session_page(session_key)
            yield page

    @staticmethod
    def _same_path(a: str, b: str) -> bool:
        """True when two grok URLs point at the same place (ignoring ?rid=)."""
        return a.split("?", 1)[0].rstrip("/") == b.split("?", 1)[0].rstrip("/")

    def _navigate_in(self, page, url: str, *, wait_ms: int = 1200):
        """Navigate the given (already-locked) page if it isn't there yet."""
        if not self._same_path(page.url, url):
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        return page

    @contextmanager
    def _open(
        self,
        url: str,
        *,
        wait_ms: int = 1200,
        session_key: Optional[str] = None,
    ) -> Iterator[object]:
        """Acquire the client lock, navigate the session's page, yield it.

        Replaces the older "return a page; caller does try/finally" pattern.
        Holding the lock for the recipe's whole duration is required —
        Playwright sync is thread-bound and re-entry from elsewhere crashes
        the greenlet. Sidebar recipes default to ``DEFAULT_SESSION``;
        ``send()`` may pass through any session_key.
        """
        sk = session_key or self.DEFAULT_SESSION
        with self._session(sk) as page:
            self._navigate_in(page, url, wait_ms=wait_ms)
            yield page

    # Backwards-compat for any caller still doing ``self._ensure_page()``
    # without a session_key. Returns the default-session page; you still
    # need to hold the lock yourself to do any Playwright work on it.
    def _ensure_page(self):
        with self._lock:
            return self._get_session_page(self.DEFAULT_SESSION)

    # ── Core ─────────────────────────────────────────────────────────

    def send(
        self,
        message: str,
        *,
        conversation_url: Optional[str] = None,
        timeout_s: int = 60,
        wait_for_response: bool = True,
        session_key: Optional[str] = None,
    ) -> dict:
        """Send a message; return ``{response, url, conversation_id}``.

        Grok-specific recipe (bypasses the generic UCBrowser.chat heuristic
        which mis-scores grok's input wrapper). Types into the ProseMirror
        editor and submits via Enter.

        ``session_key`` selects which Page (browser tab) drives this call.
        Distinct ``session_key`` values get their own tabs and can run in
        parallel; the same ``session_key`` serialises (one chat at a time).
        Defaults to ``GrokClient.DEFAULT_SESSION``.

        With ``wait_for_response=True`` (default) we wait for a new
        ``div[data-testid="assistant-message"]`` to appear and the
        ``button[aria-label="Stop ..."]`` to disappear, then return the
        response.

        With ``wait_for_response=False`` (blind / fire-and-forget) we
        return as soon as the URL settles to ``/c/<uuid>`` (or immediately
        if we're already on a chat URL and ``conversation_url`` was given).
        ``response`` is an empty string in that case.

        If ``conversation_url`` is None, posts to the Grok root (which
        creates a new conversation and redirects to ``/c/<uuid>``).
        """
        url = _normalize_url(conversation_url) if conversation_url else self.GROK_HOME
        sk = session_key or self.DEFAULT_SESSION
        with self._session(sk) as page:
            self._navigate_in(page, url, wait_ms=1200)
            input_sel = _SELECTORS["chat_input"]
            assistant_sel = _SELECTORS["assistant_message"]
            page.wait_for_selector(input_sel, timeout=15000)

            # Snapshot the count of assistant blocks so we can detect a new one.
            # (Only matters when we're going to wait for it.)
            prev_count = 0
            if wait_for_response:
                prev_count = page.evaluate(
                    "(sel) => document.querySelectorAll(sel).length",
                    assistant_sel,
                )

            # Type via the extension's framework-aware setText (ProseMirror).
            typed = page.evaluate(
                """(args) => {
                    if (!window.__UC_setText) return {success: false, error: 'no __UC_setText'};
                    return window.__UC_setText(args[0], args[1]);
                }""",
                [input_sel, message],
            ) or {}
            if not typed.get("success"):
                logger.warning("Grok: __UC_setText failed (%s); using keyboard.type fallback", typed.get("error"))
                page.focus(input_sel)
                page.keyboard.type(message, delay=10)

            # Long pastes get split-input: text in PM, identical text as an
            # attachment chip. Drop any chips so the submit carries only the
            # editor content; otherwise Grok replies about the file.
            chips = page.evaluate(_REMOVE_ATTACHMENT_CHIPS_JS) or {}
            if chips.get("removed"):
                logger.debug(
                    "Grok: removed %d attachment chip(s) before submit (remaining=%d)",
                    chips.get("removed"), chips.get("remaining"),
                )

            # Submit on Enter — Grok has no aria-labelled send button.
            page.focus(input_sel)
            page.keyboard.press("Enter")

            # Fire-and-forget: only wait long enough to capture the URL.
            if not wait_for_response:
                # Optimisation (#2): if we were already on a /c/<uuid> page,
                # Enter doesn't change the URL — return immediately and skip
                # the up-to-5s settle loop entirely. Drops blind same-chat
                # sends from ~1.6s to <0.4s.
                already_on_chat = "/c/" in page.url or "?chat=" in page.url
                if not (conversation_url and already_on_chat):
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        page.wait_for_timeout(200)
                        if "/c/" in page.url or "?chat=" in page.url:
                            break
                result_url = page.url
                return {
                    "response": "",
                    "url": result_url,
                    "conversation_id": _conv_id_from_url(result_url),
                }

            # Optimisation (#1): instead of waiting for text to "stabilise"
            # (which paid a 2.1s tax per send) we watch Grok's own
            # generating signal: ``button[aria-label*="stop"]`` is present
            # while streaming and disappears when the response is complete.
            # Fall back to text-stability detection if the stop button never
            # appears at all (e.g. cached responses that complete instantly).
            stop_sel = _SELECTORS["stop_button"]
            deadline = time.monotonic() + timeout_s
            last_text = ""
            stable = 0
            seen_generating = False
            while time.monotonic() < deadline:
                page.wait_for_timeout(250)
                state = page.evaluate(
                    """(args) => {
                        const asstSel = args[0];
                        const stopSel = args[1];
                        const prev = args[2];
                        const els = document.querySelectorAll(asstSel);
                        const stopBtn = document.querySelector(stopSel);
                        const text = els.length > prev
                            ? (els[els.length - 1].innerText || '').trim()
                            : '';
                        return {
                            ready: els.length > prev,
                            text: text,
                            generating: !!stopBtn,
                        };
                    }""",
                    [assistant_sel, stop_sel, prev_count],
                ) or {}
                if not state.get("ready"):
                    continue
                cur = state.get("text") or ""
                if state.get("generating"):
                    # Streaming in progress — capture text, keep waiting.
                    seen_generating = True
                    last_text = cur
                    stable = 0
                    continue
                if seen_generating:
                    # Stop button vanished after we saw it — done.
                    last_text = cur
                    break
                # Stop button never appeared (instant/cached response).
                # Fall back to short stability check: 2 polls × 250ms = 500ms.
                if cur and cur == last_text:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                    last_text = cur

            result_url = page.url
            return {
                "response": _strip_thought_prefix(last_text),
                "url": result_url,
                "conversation_id": _conv_id_from_url(result_url),
            }

    def read(self, conversation_url: str) -> dict:
        """Return the full transcript: ``{url, conversation_id, messages}``.

        Walks ``div[data-testid="user-message"]`` and
        ``div[data-testid="assistant-message"]`` blocks in document order.
        Assistant messages have any ``Thought for Ns`` reasoning prefix
        stripped.
        """
        url = _normalize_url(conversation_url)
        with self._open(url, wait_ms=3500) as page:
            messages = page.evaluate(
                """(args) => {
                    const userSel = args[0];
                    const asstSel = args[1];
                    const all = document.querySelectorAll(
                        `${userSel}, ${asstSel}`
                    );
                    const out = [];
                    for (const el of all) {
                        const tid = el.getAttribute('data-testid');
                        out.push({
                            role: tid === 'user-message' ? 'user' : 'assistant',
                            text: (el.innerText || '').trim(),
                        });
                    }
                    return out;
                }""",
                [_SELECTORS["user_message"], _SELECTORS["assistant_message"]],
            ) or []
            # Strip Grok's reasoning prefix from assistant blocks.
            cleaned = [
                {**m, "text": _strip_thought_prefix(m["text"])} if m.get("role") == "assistant"
                else m
                for m in messages
            ]
            return {
                "url": page.url,
                "conversation_id": _conv_id_from_url(page.url),
                "messages": cleaned,
            }

    def list_conversations(self) -> list[dict]:
        """Scrape the sidebar for ``[{id, title, url}, ...]``."""
        with self._open(self.GROK_HOME, wait_ms=4500) as page:
            items = page.evaluate(
                """(sel) => {
                    const links = document.querySelectorAll(sel);
                    const seen = new Set();
                    const out = [];
                    for (const a of links) {
                        const href = a.href;
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        // Try to grab a title from the link's text or its closest row
                        let title = (a.innerText || '').trim();
                        if (!title) {
                            const row = a.closest('li, [role="listitem"], div');
                            title = (row?.innerText || '').trim();
                        }
                        let m = null;
                        try { m = new URL(href).pathname.match(/\\/c\\/([^/?#]+)/); }
                        catch (e) {}
                        out.push({
                            id: m ? m[1] : null,
                            title: title.slice(0, 200) || null,
                            url: href,
                        });
                    }
                    return out;
                }""",
                _SELECTORS["conversation_link"],
            )
            return items or []

    def _open_row_menu(self, page, conv_id: str) -> bool:
        """Hover the sidebar row for ``conv_id`` and open its Options menu.

        Grok hides the 3-dot button until the row is hovered (synthetic
        ``mouseenter`` events aren't enough — Playwright's real ``.hover()``
        is required). Returns True if the popover opened.
        """
        row_loc = page.locator(f'a[href*="/c/{conv_id}"]').first
        try:
            if row_loc.count() == 0:
                logger.warning("Grok: row not found for %s", conv_id)
                return False
            row_loc.scroll_into_view_if_needed()
            row_loc.hover()
            page.wait_for_timeout(300)
            li_loc = row_loc.locator("xpath=ancestor::li[1]")
            opts_loc = li_loc.locator('button[aria-label="Options" i]')
            if opts_loc.count() == 0:
                logger.warning("Grok: Options button not found for %s", conv_id)
                return False
            # force=True because the button has width/height before hover
            # animation settles, even though it's now in the DOM.
            opts_loc.click(force=True)
            page.wait_for_timeout(500)
            return True
        except Exception as e:
            logger.warning("Grok: _open_row_menu failed: %s", e)
            return False

    def _click_menu_item(self, page, target_label: str) -> bool:
        """Click the ``<div role="menuitem">`` whose text starts with ``target_label``."""
        return bool(
            page.evaluate(
                """(label) => {
                    const want = label.toLowerCase();
                    for (const el of document.querySelectorAll('[role="menuitem"]')) {
                        const t = (el.innerText || '').trim().toLowerCase();
                        if (t === want || t.startsWith(want)) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                target_label,
            )
        )

    def delete(self, conversation_url: str) -> bool:
        """Delete a conversation. Returns True if the row disappears.

        Grok's current flow: hover sidebar row → click 3-dot → click "Delete"
        in the popover. No confirm dialog appears in the standard flow; we
        still best-effort click any destructive button if one shows up.
        """
        conv_id = _conv_id_from_url(conversation_url) or conversation_url
        with self._open(self.GROK_HOME, wait_ms=4000) as page:
            if not self._open_row_menu(page, conv_id):
                return False
            if not self._click_menu_item(page, "delete"):
                logger.warning("Grok delete: 'Delete' menu item not found")
                return False
            page.wait_for_timeout(800)
            # Best-effort confirm dialog handling — currently a no-op, but
            # leaves us covered if Grok adds a confirmation step back.
            page.evaluate(
                """() => {
                    const dlgs = document.querySelectorAll(
                        '[role="alertdialog"], [role="dialog"]'
                    );
                    for (const d of dlgs) {
                        const r = d.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;
                        for (const b of d.querySelectorAll('button')) {
                            const t = (b.innerText || '').trim().toLowerCase();
                            if (t === 'delete' || t === 'confirm'
                                || t === 'yes' || t === 'remove') {
                                b.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }"""
            )
            page.wait_for_timeout(1200)
            # Verify the row no longer exists in the sidebar.
            gone = page.evaluate(
                "(id) => !document.querySelector('a[href*=\"/c/' + id + '\"]')",
                conv_id,
            )
            return bool(gone)

    # ── Lifecycle helpers ────────────────────────────────────────────

    def new_chat(self) -> dict:
        """Create a new chat; return ``{url, conversation_id}``.

        Grok's root URL is the new-chat surface, but we need it to commit
        (the URL doesn't change to /c/<uuid> until the first message).
        For predictability we send a no-op probe — actually no, we just
        return the home URL; callers should use ``send`` to materialize.
        """
        return {"url": self.GROK_HOME, "conversation_id": None}

    def rename(self, conversation_url: str, new_title: str) -> bool:
        """Rename a conversation via its sidebar row's menu."""
        conv_id = _conv_id_from_url(conversation_url) or conversation_url
        with self._open(self.GROK_HOME, wait_ms=4000) as page:
            if not self._open_row_menu(page, conv_id):
                return False
            if not self._click_menu_item(page, "rename"):
                return False
            page.wait_for_timeout(400)
            # Fill the rename input — Grok may use inline edit or a modal
            typed = page.evaluate(
                """(args) => {
                    const sel = args[0];
                    const title = args[1];
                    const input = document.querySelector(sel);
                    if (!input) return false;
                    input.focus();
                    if ('value' in input) input.value = title;
                    else input.textContent = title;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                [_SELECTORS["rename_input"], new_title],
            )
            if not typed:
                return False
            # Submit via Enter or a Save button
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            return True

    def archive(self, conversation_url: str) -> bool:
        """Archive a conversation via its sidebar row's menu (best-effort).

        Grok's current popover offers Open/Rename/Pin/Delete — no Archive
        item. This call returns False unless Grok adds it back later, at
        which point the same hover+menu-item flow will pick it up.
        """
        conv_id = _conv_id_from_url(conversation_url) or conversation_url
        with self._open(self.GROK_HOME, wait_ms=4000) as page:
            if not self._open_row_menu(page, conv_id):
                return False
            clicked = self._click_menu_item(page, "archive")
            page.wait_for_timeout(800)
            return clicked

    def regenerate(self, conversation_url: str, *, timeout_s: int = 60) -> dict:
        """Click "Regenerate" on the last assistant message and capture the new response."""
        url = _normalize_url(conversation_url)
        uc = self._ensure_uc()
        with self._open(url, wait_ms=3500) as page:
            # Snapshot the last assistant block's text as the "old"
            old = page.evaluate(
                """() => {
                    const els = document.querySelectorAll(
                        '[data-message-author-role="assistant"]'
                    );
                    if (!els.length) return '';
                    return (els[els.length - 1].innerText || '').trim();
                }""",
            )
            # Capture a trigram baseline so __UC_extractFromContainer can score
            uc._wait_ready(page)
            page.evaluate("window.__UC_captureBaseline && window.__UC_captureBaseline()")
            clicked = page.evaluate(
                """(sel) => {
                    const btns = document.querySelectorAll(sel);
                    if (!btns.length) return false;
                    btns[btns.length - 1].click();  // last regenerate = most recent reply
                    return true;
                }""",
                _SELECTORS["regenerate_button"],
            )
            if not clicked:
                return {"ok": False, "reason": "regenerate-not-found", "response": None}
            # Poll for the assistant block to differ from `old` and stabilise
            deadline = time.monotonic() + timeout_s
            last = ""
            stable = 0
            while time.monotonic() < deadline:
                page.wait_for_timeout(500)
                cur = page.evaluate(
                    """() => {
                        const els = document.querySelectorAll(
                            '[data-message-author-role="assistant"]'
                        );
                        if (!els.length) return '';
                        return (els[els.length - 1].innerText || '').trim();
                    }""",
                )
                if cur and cur != old:
                    if cur == last:
                        stable += 1
                        if stable >= 3:
                            return {"ok": True, "response": cur}
                    else:
                        stable = 0
                        last = cur
            return {"ok": bool(last), "response": last, "reason": "timeout"}

    def stop(self, conversation_url: str) -> bool:
        """Click the stop button if the assistant is currently generating."""
        url = _normalize_url(conversation_url)
        with self._open(url, wait_ms=2000) as page:
            clicked = page.evaluate(
                """(sel) => {
                    const btn = document.querySelector(sel);
                    if (!btn) return false;
                    btn.click();
                    return true;
                }""",
                _SELECTORS["stop_button"],
            )
            return bool(clicked)

    # ── Models ───────────────────────────────────────────────────────

    def get_models(self) -> list[str]:
        """Open the model picker and return the menu options as labels."""
        with self._open(self.GROK_HOME, wait_ms=3500) as page:
            names = page.evaluate(
                """(sel) => {
                    const trigger = document.querySelector(sel);
                    if (!trigger) return [];
                    trigger.click();
                    return new Promise((resolve) => {
                        setTimeout(() => {
                            const opts = document.querySelectorAll(
                                '[role="option"], [role="menuitem"]'
                            );
                            const out = [];
                            for (const o of opts) {
                                const t = (o.innerText || '').trim();
                                if (t) out.push(t.split('\\n')[0]);
                            }
                            // Close the picker so we leave the page clean
                            document.body.click();
                            resolve(out);
                        }, 400);
                    });
                }""",
                _SELECTORS["model_picker"],
            )
            return list(dict.fromkeys(names or []))  # de-dupe preserving order

    def switch_model(self, model_name: str) -> bool:
        """Open the picker and click the option matching ``model_name``."""
        with self._open(self.GROK_HOME, wait_ms=3500) as page:
            chose = page.evaluate(
                """(args) => {
                    const trigger = document.querySelector(args[0]);
                    if (!trigger) return false;
                    trigger.click();
                    return new Promise((resolve) => {
                        setTimeout(() => {
                            const target = args[1].toLowerCase();
                            const opts = document.querySelectorAll(
                                '[role="option"], [role="menuitem"]'
                            );
                            for (const o of opts) {
                                const t = (o.innerText || '').trim().toLowerCase();
                                if (t === target || t.startsWith(target)) {
                                    o.click();
                                    resolve(true);
                                    return;
                                }
                            }
                            document.body.click();
                            resolve(false);
                        }, 400);
                    });
                }""",
                [_SELECTORS["model_picker"], model_name],
            )
            return bool(chose)


# ── Process-level singleton ──────────────────────────────────────────
#
# Both the MCP server and the litellm CustomLLM provider want a single
# long-lived GrokClient (one persistent browser, shared sessions). Keep
# the singleton here so neither caller spins up its own competing one.

_singleton: GrokClient | None = None
_singleton_lock = threading.Lock()


def get_grok_client() -> GrokClient:
    """Return the process-wide GrokClient, constructing it on first call."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            logger.info("Creating GrokClient singleton.")
            _singleton = GrokClient()
            atexit.register(_close_singleton)
        return _singleton


def _close_singleton() -> None:
    global _singleton
    with _singleton_lock:
        client = _singleton
        _singleton = None
    if client is not None:
        try:
            client.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("GrokClient singleton close failed.", exc_info=True)


def reset_grok_singleton() -> None:
    """Drop the cached client (tests only). Closes the browser if open."""
    _close_singleton()
