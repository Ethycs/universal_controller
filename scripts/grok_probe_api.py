"""Probe Grok's chat send mechanism from two angles.

Angle 2 — fetch/XHR hook injected before page JS runs:
    Logs URL, method, request body, response status, response excerpt
    for every API-shaped call. Filters to JSON / SSE so we don't drown
    in static asset requests.

Angle 3 — React fiber walk from div.ProseMirror upward:
    For each ancestor fiber, dump prop keys that look send-shaped
    (send/submit/chat/handle*) plus a function-signature summary.

Sends one message, then prints a structured report:

    {
      "framework": {...},
      "api_calls": [...],
      "fiber_send_props": [...],
    }

Use the report to decide whether to bind to (a) the API endpoint via
httpx replay, or (b) the React callback via page.evaluate().
"""

from __future__ import annotations

import json
import time

from uc_browser import BrowserMode, UCBrowser


INIT_SCRIPT = r"""
(() => {
  // ──────────────────────────────────────────────────────────
  // ANGLE 2: hook fetch + XMLHttpRequest BEFORE page JS runs
  // ──────────────────────────────────────────────────────────
  if (window.__GROK_PROBE_INSTALLED) return;
  window.__GROK_PROBE_INSTALLED = true;
  window.__GROK_PROBE_CALLS = [];

  function pushCall(entry) {
    try {
      window.__GROK_PROBE_CALLS.push(entry);
      // Cap to avoid runaway.
      if (window.__GROK_PROBE_CALLS.length > 200) {
        window.__GROK_PROBE_CALLS.shift();
      }
    } catch (e) {}
  }

  // Heuristic: skip static asset / analytics noise, keep anything that
  // looks like a backend RPC.
  function looksLikeApi(url) {
    try {
      const u = new URL(url, location.origin);
      if (u.host !== location.host) return false;
      const p = u.pathname.toLowerCase();
      if (p.match(/\.(js|css|png|jpg|jpeg|gif|svg|webp|woff2?|ico|map)(\?|$)/)) return false;
      if (p.startsWith('/_next/static')) return false;
      return true;
    } catch (e) {
      return false;
    }
  }

  // ── fetch hook ────────────────────────────────────────────
  const origFetch = window.fetch;
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' ? input : (input?.url || String(input));
    const method = (init?.method || (input?.method) || 'GET').toUpperCase();
    let bodyStr = null;
    if (init?.body) {
      try {
        bodyStr = typeof init.body === 'string'
          ? init.body
          : (init.body.toString?.() || '[non-string body]');
      } catch (e) { bodyStr = '[body unreadable]'; }
    }
    // Capture init.headers — covers most of what gets sent. Some pages
    // also pass an Request object as `input` with headers attached;
    // catch that case too.
    let reqHeaders = null;
    try {
      const h = {};
      const src = init?.headers || input?.headers;
      if (src) {
        if (typeof src.forEach === 'function') {
          src.forEach((v, k) => { h[k] = String(v).slice(0, 200); });
        } else if (Array.isArray(src)) {
          src.forEach(([k, v]) => { h[k] = String(v).slice(0, 200); });
        } else {
          for (const k of Object.keys(src)) h[k] = String(src[k]).slice(0, 200);
        }
        reqHeaders = h;
      }
    } catch (e) {}
    const t0 = performance.now();
    let r;
    try {
      r = await origFetch.apply(this, arguments);
    } catch (e) {
      if (looksLikeApi(url)) {
        pushCall({source: 'fetch', url, method, request_body: bodyStr,
                  error: String(e), elapsed_ms: Math.round(performance.now()-t0)});
      }
      throw e;
    }
    const elapsed_ms = Math.round(performance.now() - t0);
    if (looksLikeApi(url)) {
      let respText = null;
      let respPreview = null;
      try {
        // Clone so we don't drain the original stream.
        const clone = r.clone();
        const ctype = clone.headers.get('content-type') || '';
        if (ctype.includes('json') || ctype.includes('text') || ctype.includes('event-stream')) {
          respText = await clone.text();
          respPreview = respText.slice(0, 1500);
        }
        pushCall({
          source: 'fetch', url, method,
          request_headers: reqHeaders,
          request_body: bodyStr ? bodyStr.slice(0, 1500) : null,
          status: r.status,
          response_content_type: ctype,
          response_preview: respPreview,
          response_full_length: respText ? respText.length : null,
          elapsed_ms,
        });
      } catch (e) {
        pushCall({source: 'fetch', url, method, status: r.status,
                  request_body: bodyStr, error: 'clone read failed: ' + e,
                  elapsed_ms});
      }
    }
    return r;
  };

  // ── XHR hook ─────────────────────────────────────────────
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__probe_method = method;
    this.__probe_url = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    const url = this.__probe_url;
    const method = (this.__probe_method || 'GET').toUpperCase();
    const t0 = performance.now();
    if (looksLikeApi(url)) {
      this.addEventListener('load', () => {
        let preview = null;
        try { preview = String(this.responseText || '').slice(0, 1500); } catch (e) {}
        pushCall({
          source: 'xhr', url, method,
          request_body: body ? String(body).slice(0, 1500) : null,
          status: this.status,
          response_preview: preview,
          elapsed_ms: Math.round(performance.now() - t0),
        });
      });
    }
    return origSend.apply(this, arguments);
  };

  // ──────────────────────────────────────────────────────────
  // ANGLE 3: helper to walk React fibers
  // ──────────────────────────────────────────────────────────
  window.__GROK_PROBE_walkFiber = function(rootEl, maxDepth) {
    if (!rootEl) return {error: 'no rootEl'};
    maxDepth = maxDepth || 30;
    let fiber = null;
    for (const k of Object.keys(rootEl)) {
      if (k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')) {
        fiber = rootEl[k];
        break;
      }
    }
    if (!fiber) return {error: 'no react fiber on this element — not a React app?'};

    const sendRe = /^(on)?(send|submit|chat|message|input|prompt|handle.*(send|submit|chat))/i;
    const out = [];
    let depth = 0;
    let f = fiber;
    while (f && depth < maxDepth) {
      const props = f.memoizedProps || f.pendingProps;
      const propHits = [];
      if (props && typeof props === 'object') {
        for (const k of Object.keys(props)) {
          if (typeof props[k] === 'function' && sendRe.test(k)) {
            propHits.push({key: k, fn_length: props[k].length,
                           fn_name: props[k].name || '(anon)'});
          }
        }
      }
      // Also peek at any state hooks (memoizedState) for send-named refs.
      const stateHits = [];
      let mem = f.memoizedState;
      let hookIdx = 0;
      while (mem && hookIdx < 30) {
        try {
          const val = mem.memoizedState;
          if (typeof val === 'function' && val.name && sendRe.test(val.name)) {
            stateHits.push({hookIdx, name: val.name, fn_length: val.length});
          }
          if (val && typeof val === 'object' && val.current
              && typeof val.current === 'function'
              && val.current.name && sendRe.test(val.current.name)) {
            stateHits.push({hookIdx, name: val.current.name, fn_length: val.current.length,
                            via: 'ref'});
          }
        } catch (e) {}
        mem = mem.next;
        hookIdx++;
      }
      if (propHits.length || stateHits.length) {
        out.push({
          depth,
          type: (typeof f.type === 'function' ? (f.type.displayName || f.type.name)
                : (typeof f.type === 'string' ? f.type : '?')),
          prop_hits: propHits,
          state_hits: stateHits,
        });
      }
      f = f.return;
      depth++;
    }
    return {hits: out, walked_depth: depth};
  };
})();
"""


def main():
    uc = UCBrowser(mode=BrowserMode.CHROMIUM_EXT, timeout_ms=30000)
    uc.start()
    try:
        # Inject the hook BEFORE any page JS runs. Playwright supports this
        # via add_init_script on the context — every page gets it.
        ctx = uc._context
        ctx.add_init_script(script=INIT_SCRIPT)
        print("[probe] injected pre-document-start hook into context")

        page = uc.open("https://grok.com/", wait_ms=5000)
        uc.dismiss_cookies(page); uc.close_modal(page)
        page.wait_for_selector("div.ProseMirror", timeout=15000)
        print("[probe] grok loaded; sending one test message")

        # Send a message via the same recipe GrokClient.send uses.
        page.evaluate("(a) => window.__UC_setText(a[0], a[1])",
                      ["div.ProseMirror", "Echo back exactly: probe-1"])
        page.focus("div.ProseMirror")
        page.keyboard.press("Enter")

        # Wait for the response to fully arrive so we capture both the
        # stream-start request and any follow-ups.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            done = page.evaluate(
                "() => !document.querySelector('button[aria-label*=\"stop\" i]')"
                "       && document.querySelectorAll('div[data-testid=\"assistant-message\"]').length > 0"
            )
            if done:
                break

        # Give XHR onload handlers a tick to fire.
        page.wait_for_timeout(500)

        # ── Angle 2: pull captured API calls ───────────────────────
        calls = page.evaluate("() => window.__GROK_PROBE_CALLS")
        print()
        print(f"=== ANGLE 2: {len(calls)} API call(s) captured ===")
        for i, c in enumerate(calls):
            print(f"\n--- call #{i+1} ---")
            print(f"  {c.get('method')} {c.get('url')}")
            print(f"  source={c.get('source')}  status={c.get('status')}  "
                  f"elapsed={c.get('elapsed_ms')}ms")
            ctype = c.get("response_content_type")
            if ctype:
                print(f"  content-type: {ctype}")
            hdrs = c.get("request_headers")
            if hdrs:
                print(f"  request headers ({len(hdrs)}):")
                for k, v in hdrs.items():
                    print(f"    {k}: {v}")
            req = c.get("request_body")
            if req:
                print(f"  request body ({len(req)} chars):")
                print(f"    {req[:600]}")
            resp = c.get("response_preview")
            if resp:
                full_len = c.get("response_full_length")
                print(f"  response preview ({len(resp)} chars"
                      + (f", full={full_len}" if full_len else "")
                      + "):")
                print(f"    {resp[:600]}")

        # ── Angle 3: walk React fibers from div.ProseMirror ────────
        print()
        print("=== ANGLE 3: React fiber walk from div.ProseMirror ===")
        fiber_report = page.evaluate(
            "() => window.__GROK_PROBE_walkFiber(document.querySelector('div.ProseMirror'))"
        )
        print(json.dumps(fiber_report, indent=2))

    finally:
        try:
            page.close()
        except Exception:
            pass
        uc.close()


if __name__ == "__main__":
    main()
