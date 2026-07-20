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
import os
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


def _is_reasoning_banner(text: str) -> bool:
    """True when ``text`` is only grok's reasoning banner, not a real reply.

    Grok shows a reasoning header before the answer in several forms:
      - ``Thinking about your request — Ns`` — transient, streamed char-by-char
        and later REPLACED by the answer (no newline terminator while forming),
      - ``Thoughts`` / ``Thought for Ns`` — collapsed header (handled by
        ``_strip_thought_prefix``; this catches the bare header before its
        newline arrives).
    Such text must never be emitted as a delta nor mistaken for a finished
    answer. The phrase match is exact-prefix so a real reply that merely starts
    with "Think…" (but isn't the banner) still streams.
    """
    t = (text or "").strip().lower()
    if not t:
        return True
    if t.startswith("thought"):  # "Thoughts", "Thought for Ns"
        return True
    banner = "thinking about your request"
    # partial-while-forming ("thi", "thinking about your") OR the full banner.
    return banner.startswith(t) or t.startswith(banner)


def _stream_delta(cur: str, emitted: str) -> tuple[str, str]:
    """Incremental text to stream from a growing assistant block.

    Strips grok's reasoning header so it never leaks into the stream, and only
    emits forward (append-only) growth. Returns ``(delta_to_emit, new_emitted)``;
    ``delta_to_emit`` may be empty.
    """
    visible = _strip_thought_prefix(cur)
    # Hold back the forming reasoning banner until real answer text appears
    # (it gets replaced by the answer, at which point `visible` is no longer a
    # banner and the normal forward-diff below emits the reply).
    if emitted == "" and _is_reasoning_banner(visible):
        return "", emitted
    if visible.startswith(emitted):
        return visible[len(emitted):], visible
    # Non-monotonic (markdown reflow, or the banner just got replaced) — resync
    # silently to the current visible text without re-emitting.
    return "", visible


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

  // Track the file chips that pop up on big pastes. Grok turns a paste it
  // deems too large into a 'pasted-text.txt' attachment — the message then
  // lives in a file, the editor goes empty, and a naive submit sends nothing
  // (or sends the file). This returns structured info per chip so callers can
  // detect that case and recover (e.g. re-type the text into the editor).
  //
  // Selectorless by preference: anchor on the semantic Remove button (stable),
  // walk up to the chip container, read its visible label + the editor state.
  window.__UC_grokAttachmentInfo = function() {
    // The authoritative chip container is Grok's attachments list, found
    // semantically (role=list + aria-label), NOT by the Remove button (which
    // only exists on hover) or styling classes. Each direct child is a chip.
    const list = document.querySelector(
      '[role="list"][aria-label*="attachment" i]'
    );
    const chips = [];
    if (list) {
      // listitem children if present, else direct element children.
      let items = list.querySelectorAll(':scope [role="listitem"]');
      if (!items.length) items = list.children;
      Array.prototype.forEach.call(items, (el, i) => {
        const label = (el.innerText || el.getAttribute('aria-label') || '')
          .trim().replace(/\s+/g, ' ').slice(0, 120);
        chips.push({
          index: i,
          label: label,
          pasted_text: /pasted[-_ ]?text|\.txt\b/i.test(label),
        });
      });
    }
    // Fallback: count Remove buttons too, in case the list is structured
    // differently on some layout.
    const removeBtns = document.querySelectorAll(
      'button[aria-label="Remove this attachment" i]'
    ).length;
    const pm = document.querySelector('div.ProseMirror');
    const editor_chars = pm ? (pm.textContent || '').trim().length : -1;
    return {
      count: Math.max(chips.length, removeBtns),
      list_chips: chips.length,
      remove_buttons: removeBtns,
      pasted_text_count: chips.filter(c => c.pasted_text).length,
      editor_chars: editor_chars,
      list_hidden: list ? list.classList.contains('hidden') : null,
      chips: chips,
    };
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

  // Shared "did the submit land?" poll, hoisted to window so both the bespoke
  // trigger chain and the toolkit submit path share one delivery-signal
  // definition (user-message-appeared / navigated-to-/c/ / composer-cleared).
  window.__UC_grokPollSubmitted = async function(prevMsgCount, maxMs) {
    const userMsgSel = 'div[data-testid="user-message"]';
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
  };

  // Is the extension's selectorless structural button finder present? That is
  // the defining capability of the toolkit submit path; capture degrades
  // gracefully (stable assistant testid + optional __UC_readLocked), so we
  // gate only on the one function we cannot substitute. If the extension build
  // is stale / not loaded we drop to the bespoke chain rather than crash.
  window.__UC_grokToolkitReady = function() {
    return typeof window.__UC_findButtons === 'function';
  };

  // Selectorless submit: take the best-scored send control from the toolkit's
  // structural button finder (no arrow-svg-path / Tailwind anchors), click it,
  // confirm with the shared delivery poll. On any miss fall through to the
  // bespoke trigger chain (icon-anchored div-click → React onSubmit → Enter) so
  // we never lose the statsig-signed submit ride.
  window.__UC_grokToolkitSubmit = async function() {
    const userMsgSel = 'div[data-testid="user-message"]';
    const prevMsgCount = document.querySelectorAll(userMsgSel).length;
    try {
      const btns = window.__UC_findButtons('div.ProseMirror') || [];
      if (btns.length) {
        const el = document.querySelector(btns[0].selector);
        if (el && window.__UC_grokIsClickable(el)) {
          el.click();
          const r = await window.__UC_grokPollSubmitted(prevMsgCount, 1500);
          if (r.ok) {
            return {ok: true, method: 'toolkit-findButtons',
                    selector: btns[0].selector, score: btns[0].score,
                    reason: r.reason};
          }
        }
      }
    } catch (e) { /* fall through to the bespoke chain */ }
    const fallback = await window.__UC_grokTriggerSubmit();
    if (fallback && typeof fallback === 'object') fallback.via = 'bespoke-fallback';
    return fallback;
  };

  // Selectorless reply capture: lock the toolkit's response-watch attribute
  // (data-uc-response="1", read back by __UC_readLocked) onto the NEWEST
  // assistant reply block. Anchored on grok's stable SEMANTIC testid — not the
  // rotating Tailwind classes, not a substring match of the echoed prompt
  // (grok normalizes/truncates it, so that never matches) and explicitly NOT
  // the trigram extractor (which latched onto page chrome — a cookie-consent
  // banner — and produced a false-positive render). Idempotent + lazy: safe to
  // call every poll; locks as soon as a fresh reply block exists past `prev`.
  window.__UC_grokAnchorLock = function(prevCount) {
    const els = document.querySelectorAll('div[data-testid="assistant-message"]');
    if (els.length <= (prevCount || 0)) return {ok: false, why: 'no-new-assistant-block'};
    const target = els[els.length - 1];
    if (!target.hasAttribute('data-uc-response')) {
      document.querySelectorAll('[data-uc-response]')
        .forEach(e => e.removeAttribute('data-uc-response'));
      target.setAttribute('data-uc-response', '1');
    }
    return {ok: true, lockedSel: '[data-uc-response="1"]', via: 'last-assistant-testid'};
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

    // 1) Click the actual send control (most reliable on current UI).
    try {
      const btn = window.__UC_grokFindSendButton();
      if (btn && window.__UC_grokIsClickable(btn)) {
        btn.click();
        const r = await window.__UC_grokPollSubmitted(prevMsgCount, 1500);
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
        const r = await window.__UC_grokPollSubmitted(prevMsgCount, 1500);
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
        const r = await window.__UC_grokPollSubmitted(prevMsgCount, 1500);
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
        on_delta=None,
    ) -> dict:
        """Send a message via React-onSubmit + capture via fetch hook.

        ``on_delta``: optional callable ``(text: str) -> None`` invoked with each
        incremental chunk of the reply as it renders (real streaming). Only the
        DOM-poll capture paths stream; the one-shot network-capture path returns
        whole (the caller reconciles any un-streamed remainder).

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

            # ── Preferred path: the selectorless extension toolkit ──────────
            # Use the extension's structural button finder + anchor/baseline
            # capture instead of grok-specific selectors. Only taken when the
            # toolkit functions are actually present on the page (extension
            # loaded + a build that exposes them); otherwise we drop to the
            # bespoke network path below. ``UC_GROK_DISABLE_TOOLKIT=1`` forces
            # the bespoke path (escape hatch for regressions).
            toolkit_ready = bool(page.evaluate(
                "() => typeof window.__UC_grokToolkitReady === 'function' "
                "&& window.__UC_grokToolkitReady()"
            ))
            if toolkit_ready and os.environ.get("UC_GROK_DISABLE_TOOLKIT") != "1":
                try:
                    return self._send_toolkit(
                        page, message, assistant_sel, prev_count, timeout_s,
                        on_delta=on_delta,
                    )
                except RuntimeError as e:
                    # Pre-submit failure ⇒ nothing was sent; safe to fall through
                    # to the bespoke path (its setText replaces, not appends).
                    logger.warning(
                        "[grok_fast] toolkit path pre-submit failure (%s); "
                        "falling back to bespoke network path", e,
                    )
            elif not toolkit_ready:
                logger.info(
                    "[grok_fast] selectorless toolkit not present on page; "
                    "using bespoke path"
                )

            # ``submitted`` gates the failure handling: once the message is
            # confirmed sent (composer cleared), a later timeout must NOT
            # bubble up as a RuntimeError — that would make send_with_fallback
            # re-type the whole message (the double-paste). We poll the DOM
            # for the reply instead.
            submitted = False
            # The network response (if it matches _CHAT_URL_RE at all) fires
            # within seconds of submit. If the matcher misses — e.g. a
            # logged-in continue-chat whose response URL doesn't match — there
            # is no point waiting the full generation budget here; cap this
            # phase short and let _poll_dom_response carry the long wait. The
            # DOM poll watches the actual generating signal, so capping the
            # network phase costs nothing but saves the wasted minutes.
            net_capture_s = min(timeout_s, int(os.environ.get("UC_GROK_NET_CAPTURE_S", "45")))
            try:
                # Set up the network listener BEFORE triggering — Playwright
                # buffers matching responses from the moment the `with` opens.
                with page.expect_response(
                    lambda r: bool(_CHAT_URL_RE.search(r.url)),
                    timeout=net_capture_s * 1000,
                ) as resp_info:
                    typed = page.evaluate(
                        "(args) => window.__UC_setText && window.__UC_setText(args[0], args[1])",
                        ["div.ProseMirror", message],
                    ) or {}
                    if not typed.get("success"):
                        raise RuntimeError(
                            f"GrokFastClient: setText failed ({typed.get('error')})"
                        )
                    # Settle grok's async paste->file conversion and recover if
                    # it swallowed the whole message into a chip (kayla/ceraph
                    # empty-render trap). Shared with the toolkit path.
                    self._handle_chips(page, message)
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
                return self._poll_dom_response(
                    page, assistant_sel, prev_count, timeout_s, on_delta=on_delta
                )

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

    # ── Selectorless toolkit path ───────────────────────────────────

    def _handle_chips(self, page, message: str) -> None:
        """Settle grok's async paste->file conversion; recover a chip-only msg.

        Big pastes spill into a ``pasted-text.txt`` attachment chip that
        materialises a beat AFTER ``setText``. Poll for a settled state (editor
        has text OR a chip showed) so we don't sample the empty in-between gap,
        then strip the chips so the submit carries editor content (Grok replies
        *about* the file otherwise). If the message went ENTIRELY into a chip
        (editor empty), re-insert it so there is something real to send. Shared
        by the bespoke and toolkit send paths.
        """
        info: dict = {}
        for _ in range(14):  # up to ~3.5s
            page.wait_for_timeout(250)
            info = page.evaluate(
                "() => window.__UC_grokAttachmentInfo && window.__UC_grokAttachmentInfo()"
            ) or {}
            if (info.get("editor_chars") or 0) > 0 or info.get("count"):
                break
        logger.info(
            "[grok_fast] post-setText settled: chips=%d (list=%s remove=%s) "
            "editor_chars=%s labels=%s",
            info.get("count"), info.get("list_chips"),
            info.get("remove_buttons"), info.get("editor_chars"),
            [c.get("label") for c in (info.get("chips") or [])][:3],
        )
        # Text vanished (not in editor, no chip caught) → dump composer markup
        # so we can find the real file-icon element and build a correct detector.
        if (info.get("editor_chars") or 0) == 0 and not info.get("count"):
            dump = page.evaluate(
                """() => {
                    const pm = document.querySelector('div.ProseMirror');
                    const form = pm ? pm.closest('form') : null;
                    const root = form || (pm ? pm.parentElement : document.body);
                    if (!root) return null;
                    const html = (root.outerHTML || '').replace(/\\s+/g, ' ');
                    return html.slice(0, 2200);
                }"""
            )
            logger.warning("[grok_fast] composer dump (text vanished): %s", dump)
        # ── Chip-submit: the DEFAULT for large contexts ────────────────
        # When grok files a large paste into a pasted-text.txt chip, that file
        # carries the FULL payload (verified end-to-end: a 56K probe round-
        # tripped with start/mid/end sentinels intact and an exact char count)
        # and grok renders the scene faithfully FROM the file. The inherited
        # claim that "grok replies about the file" was tested and REFUTED
        # (scene02 chip-only, editor empty, produced a full in-character render).
        # So submit the FILE instead of fighting grok's conversion with the old
        # strip/re-insert dance — that dance re-formed a chip and left the
        # visible text+chip "double". Clear any leftover editor text so the
        # composer is a single clean attachment (file only), not text+chip.
        # Escape hatch: UC_GROK_STRIP_CHIP=1 forces the old editor-text path.
        strip_mode = os.environ.get("UC_GROK_STRIP_CHIP") == "1"
        if info.get("count") and not strip_mode:
            if (info.get("editor_chars") or 0) > 0:
                page.evaluate(
                    "() => window.__UC_setText && window.__UC_setText('div.ProseMirror', '')"
                )
                page.wait_for_timeout(200)
            after = page.evaluate(
                "() => window.__UC_grokAttachmentInfo && window.__UC_grokAttachmentInfo()"
            ) or {}
            logger.info(
                "[grok_fast] chip-submit: sending the pasted-text.txt file "
                "(chips=%s editor_chars=%s).",
                after.get("count"), after.get("editor_chars"),
            )
            # Safety: if clearing the editor also dropped the chip, we'd send
            # nothing — re-insert the message so there is a real payload.
            if not after.get("count") and (after.get("editor_chars") or 0) == 0:
                logger.error(
                    "[grok_fast] chip-submit lost both chip and editor text; "
                    "re-inserting message as a fallback."
                )
                page.evaluate(
                    "(args) => window.__UC_setText && window.__UC_setText(args[0], args[1])",
                    ["div.ProseMirror", message],
                )
            return

        # ── Editor-text path: no chip formed, OR UC_GROK_STRIP_CHIP=1 ───
        # No chip → the text is already in the editor; the strip below is a
        # harmless no-op. With the escape hatch + a chip present, run the old
        # recover-to-editor dance: re-insert BEFORE stripping (re-inserting
        # re-triggers grok's paste->file, so strip AFTER to leave one editor
        # copy).
        text_in_chip_only = (
            info.get("count") and (info.get("editor_chars") or 0) == 0
        )
        if text_in_chip_only:
            retyped = page.evaluate(
                "(args) => window.__UC_setText && window.__UC_setText(args[0], args[1])",
                ["div.ProseMirror", message],
            ) or {}
            for _ in range(8):  # up to ~2s for the async chip to materialise
                page.wait_for_timeout(250)
                mid = page.evaluate(
                    "() => window.__UC_grokAttachmentInfo && window.__UC_grokAttachmentInfo()"
                ) or {}
                if (mid.get("editor_chars") or 0) > 0 and mid.get("count"):
                    break
            logger.info(
                "[grok_fast] (strip mode) chip-only message re-inserted (setText ok=%s).",
                retyped.get("success"),
            )
        chips = page.evaluate(
            "() => window.__UC_grokRemoveAttachments && window.__UC_grokRemoveAttachments()"
        ) or {}
        if chips.get("removed"):
            logger.debug(
                "[grok_fast] (strip mode) removed %d attachment chip(s) (remaining=%d)",
                chips.get("removed"), chips.get("remaining"),
            )
        if text_in_chip_only:
            after = page.evaluate(
                "() => window.__UC_grokAttachmentInfo && window.__UC_grokAttachmentInfo()"
            ) or {}
            logger.warning(
                "[grok_fast] (strip mode) post-recovery composer: editor_chars=%s "
                "chips=%s (want text>0, chips=0).",
                after.get("editor_chars"), after.get("count"),
            )
            if (after.get("editor_chars") or 0) == 0:
                logger.error(
                    "[grok_fast] (strip mode) recovery FAILED: editor empty after "
                    "chip strip — the chip WAS the only copy; submit sends nothing."
                )

    def _send_toolkit(
        self, page, message: str, assistant_sel: str,
        prev_count: int, timeout_s: int, on_delta=None,
    ) -> dict:
        """Send + capture using the extension's selectorless toolkit.

        Insertion still rides ``__UC_setText`` (the toolkit's framework-aware
        setText). Submit is sourced from the structural button finder
        (``__UC_findButtons``) rather than a grok arrow-svg-path; capture locks
        the toolkit response-watch attribute onto the stable ``assistant-message``
        testid and reads via ``__UC_readLocked`` rather than the network
        ``expect_response``.

        Contract mirrors the bespoke path: ``RuntimeError`` means a pre-submit
        failure (nothing sent — caller may safely re-send); a confirmed-submit
        miss returns a (possibly empty) result rather than raising.
        """
        typed = page.evaluate(
            "(args) => window.__UC_setText && window.__UC_setText(args[0], args[1])",
            ["div.ProseMirror", message],
        ) or {}
        if not typed.get("success"):
            raise RuntimeError(
                f"GrokFastClient(toolkit): setText failed ({typed.get('error')})"
            )
        self._handle_chips(page, message)
        trig = page.evaluate("() => window.__UC_grokToolkitSubmit()") or {}
        if not trig.get("ok"):
            raise RuntimeError(f"GrokFastClient(toolkit): submit failed: {trig}")
        logger.info(
            "[grok_fast] toolkit submit ok via %s (reason=%s via=%s)",
            trig.get("method"), trig.get("reason"), trig.get("via"),
        )
        return self._toolkit_capture(
            page, message, assistant_sel, prev_count, timeout_s, on_delta=on_delta
        )

    def _toolkit_capture(
        self, page, message: str, assistant_sel: str,
        prev_count: int, timeout_s: int, on_delta=None,
    ) -> dict:
        """Capture the reply selectorlessly after a confirmed toolkit submit.

        Each poll lazily locks the toolkit response-watch attribute onto the
        newest ``assistant-message`` block (stable semantic testid) and reads it
        via ``__UC_readLocked`` — falling back to the block's ``innerText``.
        There is NO trigram fallback (it grabbed page chrome — a cookie banner —
        and faked a render). Completion = reply present + grok's ``stop`` button
        gone + text settled across two reads. An idle-activity timer
        (``UC_GROK_EARLY_BAIL_S``) bails on a genuine non-response while
        surviving grok's stop-button FLICKER (it blips on submit, vanishes, then
        returns when generation actually starts).
        """
        stop_sel = _SELECTORS["stop_button"]
        early_bail_s = int(os.environ.get("UC_GROK_EARLY_BAIL_S", "60"))
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        last_text = ""
        stable = 0
        emitted = ""  # text already streamed via on_delta (prefix-stripped)
        last_activity = t0  # last poll that saw reply text OR a generating signal
        bailed = False
        while time.monotonic() < deadline:
            page.wait_for_timeout(250)
            state = page.evaluate(
                """(args) => {
                    const stopSel = args[0], asstSel = args[1], prev = args[2];
                    const stopBtn = document.querySelector(stopSel);
                    // Lazily lock the toolkit response-watch attribute onto the
                    // newest assistant block (stable testid) and read it via
                    // __UC_readLocked. NO trigram/extractResponse fallback — it
                    // grabbed page chrome (a cookie-consent banner) and faked a
                    // render. Only ever read from assistant-message blocks past
                    // `prev`, never arbitrary page text.
                    let text = '';
                    const lock = window.__UC_grokAnchorLock(prev);
                    if (lock && lock.ok && typeof window.__UC_readLocked === 'function') {
                        text = window.__UC_readLocked() || '';
                    }
                    if (!text) {
                        const els = document.querySelectorAll(asstSel);
                        if (els.length > prev) {
                            text = (els[els.length - 1].innerText || '');
                        }
                    }
                    return {text: (text || '').trim(), generating: !!stopBtn};
                }""",
                [stop_sel, assistant_sel, prev_count],
            ) or {}
            cur = state.get("text") or ""
            generating = bool(state.get("generating"))
            now = time.monotonic()
            # Stream the incremental growth (prefix-stripped) as it renders.
            if on_delta is not None and cur:
                delta, emitted = _stream_delta(cur, emitted)
                if delta:
                    try:
                        on_delta(delta)
                    except Exception:  # noqa: BLE001 — never let a sink break capture
                        pass
            # Any sign of life resets the idle clock — this is what survives the
            # stop-button flicker that made the old loop bail empty at ~0s.
            if generating or cur:
                last_activity = now
            # While only grok's reasoning banner is showing (no real answer yet),
            # keep waiting — never let the banner settle as the reply (the cause
            # of the "Thinking about your request — Ns" only-banner captures).
            if cur and _is_reasoning_banner(_strip_thought_prefix(cur)):
                last_activity = now
                last_text = ""
                stable = 0
                continue
            if cur:
                if cur == last_text:
                    stable += 1
                else:
                    stable = 0
                    last_text = cur
                # Complete: reply present, generation stopped, text settled.
                if not generating and stable >= 2:
                    break
            # Idle-bail: no text AND no generating signal for the whole window.
            if (now - last_activity) >= early_bail_s:
                logger.warning(
                    "[grok_fast] toolkit idle-bail: no reply text or generating "
                    "signal for %ds — treating as non-response.", early_bail_s,
                )
                bailed = True
                break

        resp = _strip_thought_prefix(last_text)
        if not resp:
            diag = self._capture_empty_diagnostic(page, assistant_sel)
            logger.warning(
                "[grok_fast] toolkit empty response after %ds (bailed=%s). diagnostic=%s",
                int(time.monotonic() - t0), bailed, diag,
            )
        final_url = page.url
        return {
            "response": resp,
            "url": final_url,
            "conversation_id": _conv_id_from_url(final_url),
        }

    def _poll_dom_response(
        self, page, assistant_sel: str, prev_count: int, timeout_s: int,
        on_delta=None,
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
        # Early-bail: if Grok shows NO sign of forming a reply (no generating
        # signal, no new assistant block) within this window, stop waiting —
        # a silent moderation block / non-response otherwise burns the whole
        # timeout_s. The legitimate "thinking before first token" gap is well
        # under a minute, so 60s is safe; override via UC_GROK_EARLY_BAIL_S.
        early_bail_s = int(os.environ.get("UC_GROK_EARLY_BAIL_S", "60"))
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        last_text = ""
        stable = 0
        emitted = ""  # text already streamed via on_delta (prefix-stripped)
        seen_generating = False
        saw_new_block = False
        bailed = False
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
            if state.get("generating"):
                seen_generating = True
            if state.get("ready"):
                saw_new_block = True
            # No response forming at all after the early-bail window → stop.
            if (
                not seen_generating
                and not saw_new_block
                and (time.monotonic() - t0) >= early_bail_s
            ):
                logger.warning(
                    "[grok_fast] early-bail: no generating signal or new "
                    "assistant block after %ds — treating as non-response.",
                    early_bail_s,
                )
                bailed = True
                break
            if not state.get("ready"):
                continue
            cur = state.get("text") or ""
            # Stream the incremental growth (prefix-stripped) as it renders.
            if on_delta is not None and cur:
                delta, emitted = _stream_delta(cur, emitted)
                if delta:
                    try:
                        on_delta(delta)
                    except Exception:  # noqa: BLE001 — never let a sink break capture
                        pass
            # Reasoning-banner-only text isn't a real answer yet — keep waiting.
            if cur and _is_reasoning_banner(_strip_thought_prefix(cur)):
                last_text = ""
                stable = 0
                continue
            if state.get("generating"):
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
        resp = _strip_thought_prefix(last_text)
        if not resp:
            # Empty reply (bail or timeout): capture what Grok actually shows so
            # the failure is explainable (silent refusal vs visible moderation
            # message vs genuine non-response).
            diag = self._capture_empty_diagnostic(page, assistant_sel)
            logger.warning(
                "[grok_fast] empty response after %ds (bailed=%s). diagnostic=%s",
                int(time.monotonic() - t0), bailed, diag,
            )
        return {
            "response": resp,
            "url": final_url,
            "conversation_id": _conv_id_from_url(final_url),
        }

    def _capture_empty_diagnostic(self, page, assistant_sel: str) -> dict:
        """Snapshot the page when a confirmed-submit produced no reply text.

        Distinguishes the empty-render failure modes: a visible refusal /
        moderation message (``refusal_language`` populated), Grok producing
        an assistant block we failed to read (``last_assistant_snippet`` set
        but text empty), or a genuine silent non-response (no blocks, no
        refusal). Logged so kayla/ceraph-style empties become diagnosable.
        """
        try:
            return page.evaluate(
                """(asstSel) => {
                    const els = document.querySelectorAll(asstSel);
                    const last = els.length
                        ? (els[els.length - 1].innerText || '').trim().slice(0, 400)
                        : null;
                    const body = document.body ? (document.body.innerText || '') : '';
                    const re = /(I (can'?t|cannot|won'?t|will not)|not able to|against (our|the|my) (guidelines|policy|principles)|content policy|I'?m sorry|unable to (help|assist|continue)|can'?t (help|assist|continue|generate)|violat|not comfortable|won'?t be able)/i;
                    const m = body.match(re);
                    const refusal = m ? body.slice(Math.max(0, m.index - 60), m.index + 200) : null;
                    return {
                        url: location.href,
                        assistant_blocks: els.length,
                        last_assistant_snippet: last,
                        refusal_language: refusal,
                        composer_present: !!document.querySelector('div.ProseMirror'),
                        body_chars: body.length,
                    };
                }""",
                assistant_sel,
            ) or {}
        except Exception as e:  # noqa: BLE001
            return {"diag_error": str(e)}


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
    on_delta=None,
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
            on_delta=on_delta,
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
