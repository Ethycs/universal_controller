"""Probe: find Grok's React send handler.

Walks up the React fiber tree from the chat input's PARENT (because
TipTap mounts the input itself, so the fiber lives on a parent), then
walks down to nearby siblings. Logs every fiber whose memoizedProps or
memoizedState contains anything send-shaped.

The output is the menu we pick from to bind to a specific callback.
"""

import json
import time

from uc_browser import BrowserMode, UCBrowser


PROBE_JS = r"""
(() => {
  // ── Get the React fiber on a DOM node ────────────────────
  function getFiber(el) {
    if (!el) return null;
    for (const k of Object.keys(el)) {
      if (k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')) {
        return el[k];
      }
    }
    return null;
  }

  // ── Walk up looking for a non-null fiber ─────────────────
  function nearestFiberUp(el, maxUp) {
    maxUp = maxUp || 20;
    let cur = el;
    for (let i = 0; i < maxUp && cur; i++) {
      const f = getFiber(cur);
      if (f) return {node: cur, fiber: f, hops: i};
      cur = cur.parentElement;
    }
    return null;
  }

  // ── Describe a function ──────────────────────────────────
  function describeFn(fn) {
    if (typeof fn !== 'function') return null;
    return {
      name: fn.name || '(anon)',
      length: fn.length,
      src_head: (fn.toString().slice(0, 180) || '').replace(/\s+/g, ' '),
    };
  }

  // ── Send-shaped key matcher ──────────────────────────────
  const SEND_RE = /^(on)?(send|submit|chat|message|prompt|run|fire|dispatch|handle.*(send|submit|chat|message))/i;

  // ── Inspect props + state on one fiber ───────────────────
  function inspectFiber(f) {
    const hits = {props: [], state_hooks: []};
    try {
      const props = f.memoizedProps || f.pendingProps;
      if (props && typeof props === 'object') {
        for (const k of Object.keys(props)) {
          const v = props[k];
          if (typeof v === 'function' && SEND_RE.test(k)) {
            hits.props.push({key: k, fn: describeFn(v)});
          }
        }
      }
    } catch (e) {}
    // React hooks: memoizedState is a linked list of hook records.
    try {
      let mem = f.memoizedState;
      let idx = 0;
      while (mem && idx < 40) {
        const val = mem.memoizedState;
        if (typeof val === 'function' && val.name && SEND_RE.test(val.name)) {
          hits.state_hooks.push({hookIdx: idx, fn: describeFn(val)});
        }
        // useRef: { current: <thing> }
        if (val && typeof val === 'object' && val.current
            && typeof val.current === 'function'
            && val.current.name && SEND_RE.test(val.current.name)) {
          hits.state_hooks.push({hookIdx: idx, via: 'ref', fn: describeFn(val.current)});
        }
        // useState setter: [value, setValue]. Setter usually has no
        // meaningful name. Skip unless its useState-paired value is
        // a function with a send-shaped name.
        mem = mem.next;
        idx++;
      }
    } catch (e) {}
    return hits;
  }

  // ── Climb the tree from the input, logging hits at each level ─
  const input = document.querySelector('div.ProseMirror');
  if (!input) return {error: 'no div.ProseMirror found'};

  // Find the closest React-mounted ancestor.
  const seed = nearestFiberUp(input, 20);
  if (!seed) return {error: 'no React fiber found going up from div.ProseMirror'};

  const trail = [];
  let f = seed.fiber;
  let depth = 0;
  while (f && depth < 40) {
    const hits = inspectFiber(f);
    if (hits.props.length || hits.state_hooks.length) {
      trail.push({
        depth,
        type: (typeof f.type === 'function'
                ? (f.type.displayName || f.type.name || '(anon-fn)')
                : (typeof f.type === 'string' ? f.type : String(f.type))),
        props: hits.props,
        state_hooks: hits.state_hooks,
      });
    }
    f = f.return;
    depth++;
  }
  return {
    seed: {hops_from_input: seed.hops, tag: seed.node.tagName.toLowerCase()},
    walked_depth: depth,
    hits: trail,
  };
})();
"""


def main():
    uc = UCBrowser(mode=BrowserMode.CHROMIUM_EXT, timeout_ms=30000)
    uc.start()
    try:
        page = uc.open("https://grok.com/", wait_ms=5000)
        uc.dismiss_cookies(page); uc.close_modal(page)
        page.wait_for_selector("div.ProseMirror", timeout=15000)
        # Give React a beat to fully hydrate.
        page.wait_for_timeout(1500)

        result = page.evaluate(PROBE_JS)
        print(json.dumps(result, indent=2))
    finally:
        page.close()
        uc.close()


if __name__ == "__main__":
    main()
