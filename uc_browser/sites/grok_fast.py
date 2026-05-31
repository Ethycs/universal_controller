"""Grok fast path: trigger submit in-page + capture reply at the network layer.

Two halves:

1. **Trigger submit in-page.** ``__UC_grokTriggerSubmit`` walks a chain —
   click Grok's div-shaped send control → invoke React's form
   ``onSubmit`` from the fiber tree → dispatch a keyboard Enter — and
   returns on the first method that empties the composer. Going through
   Grok's own click/submit path keeps the ``x-statsig-id`` anti-bot
   signing intact: the headers are composed inside the apiClient wrapper
   on the way out, so we have to ride that wrapper.

2. **Capture the reply via** :meth:`page.expect_response`. The matcher
   fires on either ``/conversations/new`` (fresh chat) or
   ``/conversations/<uuid>/responses`` (continue) and we read the body
   directly. Network-level capture sees the call whether Grok used
   ``fetch`` or ``XMLHttpRequest`` and survives Sentry's ``window.fetch``
   wrap, unlike an in-page hook.

If ``expect_response`` misses *after* the submit is confirmed (large
contexts can outlast the listener window), :meth:`_poll_dom_response`
reads the assistant block from the DOM rather than letting the caller
retype the message. Genuine pre-submit failures (no editor, setText
broke, trigger chain exhausted) still raise ``RuntimeError`` so
``send_with_fallback`` can drop to ``GrokClient.send``.

Expected warm overhead per send: < 0.5s + Grok's actual generation
time, vs ~2.4s overhead on the DOM-only path.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

from uc_browser.sites.grok import (
    GrokClient,
    _SELECTORS,
    _conv_id_from_url,
    _strip_thought_prefix,
)
# The JSON-lines parsers live with the (currently-not-wired) shim-path
# module so we don't duplicate them.
from uc_browser.sites.grok_api import (
    _extract_assistant_text,
    _extract_conversation_id,
)

logger = logging.getLogger("uc_browser.sites.grok_fast")

# Match both ``/conversations/new`` (new chats) and
# ``/conversations/<uuid>/responses`` (continuing existing chats).
_CHAT_URL_RE = re.compile(
    r"/rest/app-chat/conversations(/new|/[0-9a-fA-F-]+/responses)\b"
)


# ── JS installed once per page ─────────────────────────────────────


_INSTALL_JS = r"""
(() => {
  if (window.__UC_GROK_FAST_INSTALLED) return;
  window.__UC_GROK_FAST_INSTALLED = true;

  // ── Response storage (polled by wait_for_function, NOT expose_function) ──
  // Sync Playwright can't deliver expose_function callbacks while Python
  // is blocked on the same thread, so we write to a window slot and let
  // Python poll via wait_for_function (which yields the greenlet).
  window.__GROK_LAST_RESPONSE = null;
  window.__GROK_RESPONSE_VERSION = 0;
  window.__UC_grokDeliver = function(payload) {
    try {
      window.__GROK_LAST_RESPONSE = payload;
      window.__GROK_RESPONSE_VERSION++;
    } catch (e) {}
  };

  // ── React onSubmit invoker ────────────────────────────────
  // Walks up from div.ProseMirror to the closest fiber, then up through
  // fibers looking for the form's onSubmit. Caches the last hit per
  // discovery; if it 404's (component remounted) we re-walk.
  function getFiber(el) {
    if (!el) return null;
    for (const k of Object.keys(el)) {
      if (k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')) {
        return el[k];
      }
    }
    return null;
  }
  function nearestFiberUp(el, maxUp) {
    maxUp = maxUp || 25;
    let cur = el;
    for (let i = 0; i < maxUp && cur; i++) {
      const f = getFiber(cur);
      if (f) return f;
      cur = cur.parentElement;
    }
    return null;
  }
  window.__UC_grokFindOnSubmit = function() {
    const input = document.querySelector('div.ProseMirror');
    if (!input) return {ok: false, why: 'no-prosemirror'};
    let f = nearestFiberUp(input, 25);
    if (!f) return {ok: false, why: 'no-fiber-above-input'};
    let depth = 0;
    while (f && depth < 40) {
      try {
        const props = f.memoizedProps || f.pendingProps;
        if (props && typeof props.onSubmit === 'function') {
          // Stash on window so the trigger function can call it without
          // re-walking. Stale references are caught by callers — if it
          // throws, we re-walk.
          window.__UC_grokOnSubmit = props.onSubmit;
          return {
            ok: true,
            depth,
            type: (typeof f.type === 'function'
                    ? (f.type.displayName || f.type.name || '(anon-fn)')
                    : String(f.type)),
            fn_name: props.onSubmit.name || '(anon)',
          };
        }
      } catch (e) {}
      f = f.return;
      depth++;
    }
    return {ok: false, why: 'no-onSubmit-in-trail', walked: depth};
  };

  // Grok's send control has been BOTH a <button> (current build) and a
  // <div>+SVG (older). Anchor on the stable testid first, then aria-label,
  // then the up-arrow icon's structural ancestor as a layout-agnostic
  // fallback. Avoid styling/Tailwind classes — they churn.
  window.__UC_grokSendArrowPath = 'M6 11L12 5M12 5L18 11M12 5V19';
  window.__UC_grokFindSendButton = function() {
    // 1) Stable data-testid (current build).
    const byTestid = document.querySelector('button[data-testid="chat-submit"]');
    if (byTestid) return byTestid;
    // 2) Semantic aria-label fallback.
    const byAria = document.querySelector(
      'button[type="submit"][aria-label="Submit" i]'
    );
    if (byAria) return byAria;
    // 3) Older <div>+SVG layout: anchor on the up-arrow path.
    const path = document.querySelector(
      'svg path[d="' + window.__UC_grokSendArrowPath + '"]'
    );
    if (path) {
      const semantic = path.closest('button, [role="button"]');
      if (semantic) return semantic;
      const svg = path.closest('svg');
      if (svg && svg.parentElement) return svg.parentElement;
    }
    // 4) Composer-scoped last resort.
    const input = document.querySelector('div.ProseMirror');
    const form = input ? input.closest('form') : null;
    const scope = form || document;
    return scope.querySelector('button[type="submit"]');
  };

  // Some layouts use a <div> with no `.disabled`; treat aria-disabled as
  // not-clickable, otherwise assume ready (we setText first, which enables
  // the control).
  window.__UC_grokIsClickable = function(el) {
    if (!el) return false;
    if (el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  };

  // Count attachment chips currently in the composer. One Remove-this-
  // attachment button per chip is the stable anchor (we verified
  // aria-label="Remove this attachment" against the live DOM).
  window.__UC_grokAttachmentCount = function() {
    return document.querySelectorAll(
      'button[aria-label="Remove this attachment" i]'
    ).length;
  };

  // Click every Remove-this-attachment button until none remain (bounded).
  // Returned synchronously by the await — the caller can decide what to do
  // if `remaining > 0` (extremely unlikely under current DOM).
  window.__UC_grokRemoveAttachments = async function() {
    let removed = 0;
    for (let i = 0; i < 8; i++) {
      const btns = document.querySelectorAll(
        'button[aria-label="Remove this attachment" i]'
      );
      if (!btns.length) break;
      for (const b of btns) { try { b.click(); removed++; } catch (e) {} }
      await new Promise(r => setTimeout(r, 80));
    }
    return {removed, remaining: window.__UC_grokAttachmentCount()};
  };

  // "Composer empty" gates the submit-chain success. Empty means BOTH the
  // editor cleared AND no attachment chips are staged — a chip-only state
  // is NOT a successful send (would send the file instead of the prompt).
  window.__UC_grokComposerEmpty = function() {
    const input = document.querySelector('div.ProseMirror');
    if (!input) return false;
    if ((input.textContent || '').trim().length !== 0) return false;
    if (window.__UC_grokAttachmentCount() > 0) return false;
    return true;
  };

  window.__UC_grokTriggerSubmit = async function() {
    // Submit is fragile on a single path: the React onSubmit handler can
    // 404 (remounted form) or resolve as a silent no-op (detached form),
    // leaving the typed message sitting in the box. Try the real send
    // control first (what a user clicks), then React onSubmit, then a
    // keyboard Enter — returning on the first method that produces an
    // observable delivery signal.
    //
    // Delivery signals (any one is sufficient):
    //   (a) user-message block count grew    — authoritative
    //   (b) PM unmounted + URL is /c/<uuid>  — new-chat navigation just
    //                                            finished; the send won
    //                                            and the editor was
    //                                            remounted on the new page
    //   (c) PM exists, is empty, and has no  — same-chat continue case;
    //       attachment chip                    composer cleared in place
    //
    // The pre-fix version polled only (c) and reported false-negative on
    // new-chat sends, which made callers re-type the same message.
    const attempts = [];
    const userMsgSel = 'div[data-testid="user-message"]';
    const prevMsgCount = document.querySelectorAll(userMsgSel).length;

    async function pollSubmitted(maxMs) {
      const deadline = Date.now() + maxMs;
      while (Date.now() < deadline) {
        if (document.querySelectorAll(userMsgSel).length > prevMsgCount) {
          return {ok: true, reason: 'user-message-appeared'};
        }
        const pm = document.querySelector('div.ProseMirror');
        if (!pm && location.pathname.startsWith('/c/')) {
          return {ok: true, reason: 'navigated-to-chat'};
        }
        if (pm
            && (pm.textContent || '').trim().length === 0
            && window.__UC_grokAttachmentCount() === 0) {
          return {ok: true, reason: 'composer-cleared'};
        }
        await new Promise(r => setTimeout(r, 50));
      }
      return {ok: false};
    }

    // 1) Click the actual send control (most reliable on current UI).
    try {
      const btn = window.__UC_grokFindSendButton();
      if (btn && window.__UC_grokIsClickable(btn)) {
        btn.click();
        const r = await pollSubmitted(1500);
        if (r.ok) return {ok: true, method: 'div-click', reason: r.reason};
        attempts.push({method: 'div-click', cleared: false});
      } else {
        attempts.push({method: 'div-click', skipped: btn ? 'not-clickable' : 'not-found'});
      }
    } catch (e) { attempts.push({method: 'div-click', err: String(e)}); }

    // 2) React onSubmit (original fast path).
    try {
      const find = window.__UC_grokFindOnSubmit();
      if (find.ok) {
        await window.__UC_grokOnSubmit({preventDefault: () => {}, persist: () => {}});
        const r = await pollSubmitted(1500);
        if (r.ok) return {ok: true, method: 'react-onsubmit', picked: find, reason: r.reason};
        attempts.push({method: 'react-onsubmit', cleared: false, picked: find});
      } else {
        attempts.push({method: 'react-onsubmit', skipped: find.why});
      }
    } catch (e) { attempts.push({method: 'react-onsubmit', err: String(e)}); }

    // 3) Keyboard Enter on the composer.
    try {
      const pm = document.querySelector('div.ProseMirror');
      if (pm) {
        pm.focus();
        const opts = {key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                      bubbles: true, cancelable: true};
        pm.dispatchEvent(new KeyboardEvent('keydown', opts));
        pm.dispatchEvent(new KeyboardEvent('keypress', opts));
        pm.dispatchEvent(new KeyboardEvent('keyup', opts));
        const r = await pollSubmitted(1500);
        if (r.ok) return {ok: true, method: 'enter-key', reason: r.reason};
        attempts.push({method: 'enter-key', cleared: false});
      }
    } catch (e) { attempts.push({method: 'enter-key', err: String(e)}); }

    return {ok: false, why: 'all-submit-methods-failed', attempts};
  };
})();
"""


# ── Client ─────────────────────────────────────────────────────────


class GrokFastClient:
    """Fast-path Grok client. Reuses GrokClient as the auth oracle.

    The response-delivery mechanism is *polled*, not bridged: the install
    script's interceptor writes the response onto
    ``window.__GROK_LAST_RESPONSE`` and bumps
    ``window.__GROK_RESPONSE_VERSION``. The Python ``send()`` uses
    :func:`page.wait_for_function` to wait for the version counter to
    advance — that yields Playwright's greenlet between checks (unlike a
    Python-side ``queue.get`` which would block the whole thread and
    starve the bridge).
    """

    def __init__(self, grok_client: Optional[GrokClient] = None) -> None:
        from uc_browser.sites.grok import get_grok_client
        self._client = grok_client or get_grok_client()
        # session_key -> True once the install script + interceptor
        # registration is in place for that session's page.
        self._installed: dict[str, bool] = {}
        self._lock = threading.Lock()

    # ── Internal: install per session page ──────────────────

    def _ensure_installed(self, page, session_key: str) -> None:
        """Install the React onSubmit invoker on the page.

        Always run the JS — page.goto for a new URL resets window globals
        so a previously-installed walker disappears. The install script
        guards itself with ``__UC_GROK_FAST_INSTALLED``, so re-running on
        the same warm page is a cheap no-op.

        Response capture is done at the Playwright network layer via
        ``page.expect_response`` — that sees the call regardless of
        whether Grok used ``fetch`` or ``XMLHttpRequest``, and Sentry's
        ``window.fetch`` wrap can't bypass it.
        """
        page.evaluate(_INSTALL_JS)
        with self._lock:
            self._installed[session_key] = True

    # ── Public API ──────────────────────────────────────────

    def send(
        self,
        message: str,
        *,
        conversation_url: Optional[str] = None,
        session_key: Optional[str] = None,
        timeout_s: int = 120,
    ) -> dict:
        """Send a message via React-onSubmit + capture via fetch hook.

        Raises ``RuntimeError`` on any structural failure (hook didn't
        install, onSubmit not found, response timeout). Callers should
        catch and fall back to ``GrokClient.send``.
        """
        sk = session_key or self._client.DEFAULT_SESSION
        url_target = (
            conversation_url
            if conversation_url
            else self._client.GROK_HOME
        )
        with self._client._session(sk) as page:
            # Navigate to the target chat (or stay on home for new chat).
            self._client._navigate_in(page, url_target, wait_ms=0)
            page.wait_for_selector("div.ProseMirror", timeout=15000)
            self._ensure_installed(page, sk)

            # Snapshot assistant-block count so the DOM poll (below) can tell
            # a NEW response from any already on the page.
            assistant_sel = _SELECTORS["assistant_message"]
            prev_count = page.evaluate(
                "(sel) => document.querySelectorAll(sel).length", assistant_sel,
            )

            # ``submitted`` gates the failure handling: once the message is
            # confirmed sent (composer cleared), a later timeout must NOT
            # bubble up as a RuntimeError — that would make send_with_fallback
            # re-type the whole message (the double-paste). We poll the DOM
            # for the reply instead.
            submitted = False
            try:
                # Set up the network listener BEFORE triggering — Playwright
                # buffers matching responses from the moment the `with` opens.
                with page.expect_response(
                    lambda r: bool(_CHAT_URL_RE.search(r.url)),
                    timeout=timeout_s * 1000,
                ) as resp_info:
                    typed = page.evaluate(
                        "(args) => window.__UC_setText && window.__UC_setText(args[0], args[1])",
                        ["div.ProseMirror", message],
                    ) or {}
                    if not typed.get("success"):
                        raise RuntimeError(
                            f"GrokFastClient: setText failed ({typed.get('error')})"
                        )
                    # Long pastes get split-input: text in PM, identical text
                    # as a 'pasted-text.txt' chip. Strip the chips so the
                    # submit carries only the editor content; otherwise Grok
                    # replies about the file.
                    chips = page.evaluate(
                        "() => window.__UC_grokRemoveAttachments && window.__UC_grokRemoveAttachments()"
                    ) or {}
                    if chips.get("removed"):
                        logger.debug(
                            "[grok_fast] removed %d attachment chip(s) before submit (remaining=%d)",
                            chips.get("removed"), chips.get("remaining"),
                        )
                    # Trigger submit (button → React onSubmit → Enter), verified
                    # by the composer clearing. Only then is the send real.
                    trig = page.evaluate("() => window.__UC_grokTriggerSubmit()") or {}
                    if not trig.get("ok"):
                        raise RuntimeError(f"GrokFastClient: trigger failed: {trig}")
                    submitted = True
                response = resp_info.value
            except RuntimeError:
                # setText / trigger genuinely failed: nothing was sent, so the
                # caller may safely fall back and re-send.
                raise
            except Exception as e:
                # Network capture missed/timed out. If we already submitted,
                # the message is in flight — read the reply from the DOM
                # rather than letting the caller re-type it.
                if not submitted:
                    raise RuntimeError(f"GrokFastClient: pre-submit failure: {e}")
                logger.warning(
                    "[grok_fast] network capture missed after confirmed submit "
                    "(%s); polling DOM for response (no re-send)", e,
                )
                return self._poll_dom_response(page, assistant_sel, prev_count, timeout_s)

            try:
                body = response.text()
            except Exception as e:
                raise RuntimeError(f"GrokFastClient: response.text() failed: {e}")
            logger.debug(
                "[grok_fast] matched response: url=%s status=%s body_len=%d",
                response.url[:140], response.status, len(body),
            )
            text = _extract_assistant_text(body)
            conv_id = _extract_conversation_id(body) or _conv_id_from_url(response.url)
            # Falling back to the page URL covers continue-chat where the
            # response body's conversation block isn't always present.
            if not conv_id:
                conv_id = _conv_id_from_url(page.url)
            final_url = (
                f"https://grok.com/c/{conv_id}"
                if conv_id
                else page.url
            )
            return {
                "response": text,
                "url": final_url,
                "conversation_id": conv_id,
            }

    def _poll_dom_response(
        self, page, assistant_sel: str, prev_count: int, timeout_s: int
    ) -> dict:
        """Read the assistant reply from the DOM after a confirmed submit.

        Used when the network-level ``expect_response`` capture misses (slow
        large-context renders can exceed the listener window even though the
        send succeeded). Watches Grok's ``stop`` button as the generating
        signal — present while streaming, gone when complete — then returns
        the last assistant block. Never re-types: the message is already in
        flight, so this path exists precisely to avoid the double-paste.
        """
        stop_sel = _SELECTORS["stop_button"]
        deadline = time.monotonic() + timeout_s
        last_text = ""
        stable = 0
        seen_generating = False
        while time.monotonic() < deadline:
            page.wait_for_timeout(250)
            state = page.evaluate(
                """(args) => {
                    const asstSel = args[0], stopSel = args[1], prev = args[2];
                    const els = document.querySelectorAll(asstSel);
                    const stopBtn = document.querySelector(stopSel);
                    const text = els.length > prev
                        ? (els[els.length - 1].innerText || '').trim()
                        : '';
                    return {ready: els.length > prev, text, generating: !!stopBtn};
                }""",
                [assistant_sel, stop_sel, prev_count],
            ) or {}
            if not state.get("ready"):
                continue
            cur = state.get("text") or ""
            if state.get("generating"):
                seen_generating = True
                last_text = cur
                stable = 0
                continue
            if seen_generating:
                last_text = cur
                break
            if cur and cur == last_text:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
                last_text = cur

        final_url = page.url
        return {
            "response": _strip_thought_prefix(last_text),
            "url": final_url,
            "conversation_id": _conv_id_from_url(final_url),
        }


# ── Top-level orchestrator: fast → DOM fallback ───────────────────


_fast_client_singleton: Optional[GrokFastClient] = None
_fast_client_lock = threading.Lock()


def _get_fast_client() -> GrokFastClient:
    """Return the process-wide GrokFastClient singleton (shares GrokClient)."""
    global _fast_client_singleton
    with _fast_client_lock:
        if _fast_client_singleton is None:
            _fast_client_singleton = GrokFastClient()
        return _fast_client_singleton


def reset_grok_fast_singleton() -> None:
    """Drop the cached fast client. Tests only."""
    global _fast_client_singleton
    with _fast_client_lock:
        _fast_client_singleton = None


def send_with_fallback(
    message: str,
    *,
    conversation_url: Optional[str] = None,
    session_key: Optional[str] = None,
    timeout_s: int = 60,
    wait_for_response: bool = True,
) -> dict:
    """Send a message via the fast path, falling back to DOM on any error.

    Production entry point used by the litellm provider and the MCP
    ``chat`` tool. The fast path (``GrokFastClient``) is tried first for
    full-wait sends; if it raises ``RuntimeError`` (e.g. corner-case
    timeout, missing React onSubmit on a non-standard layout) we
    transparently fall back to ``GrokClient.send`` (DOM-driven) so the
    caller still gets a response.

    Blind-mode (``wait_for_response=False``) bypasses the fast path
    entirely — the DOM path already returns in ~1.3 s for that case,
    and the fast path's ``expect_response`` wait isn't useful when we
    don't care about the reply.
    """
    from uc_browser.sites.grok import get_grok_client
    if not wait_for_response:
        return get_grok_client().send(
            message,
            conversation_url=conversation_url,
            timeout_s=timeout_s,
            wait_for_response=False,
            session_key=session_key,
        )
    try:
        return _get_fast_client().send(
            message,
            conversation_url=conversation_url,
            session_key=session_key,
            timeout_s=timeout_s,
        )
    except RuntimeError as fast_err:
        logger.warning(
            "GrokFastClient.send failed (%s); falling back to GrokClient.send",
            fast_err,
        )
        return get_grok_client().send(
            message,
            conversation_url=conversation_url,
            timeout_s=timeout_s,
            wait_for_response=True,
            session_key=session_key,
        )
