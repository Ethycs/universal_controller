/**
 * extension-entry.js — Chrome MV3 entry point for Universal Controller.
 *
 * Replaces UC's index.js (which uses Tampermonkey APIs + injects a UI panel).
 * This version:
 *  - Creates a UniversalController instance
 *  - Exposes the full API on window.__UC and window.__UC_* functions
 *  - Exposes window.UniversalController and window.UC for direct access
 *  - No UI panel, no GM_addStyle, no GM_registerMenuCommand
 *  - Starts passive detection and frame scanning automatically
 *
 * Built by Rollup from UC submodule source + this entry point.
 */

import { UniversalController } from '../../src/detection/universal-controller.js';
import { FrameRPCChild, isInIframe } from '../../src/iframe/frame-rpc.js';
import { chatSend, chatGetMessages, chatOnMessage } from '../../src/actions/chat-api.js';
import { formFill, formSubmit, formGetValues } from '../../src/actions/form-api.js';
import { dropdownToggle, dropdownSelect } from '../../src/actions/dropdown-api.js';
import { modalClose } from '../../src/actions/modal-api.js';
import { setText } from '../../src/actions/text-input.js';
import { extractLLMContext, generateCopyContext } from '../../src/llm/context-extractor.js';
import { fullHeapScan, scanFramework } from '../../src/llm/heap-scanner.js';
import { PatternVerifier } from '../../src/llm/state-machine.js';

// ── Self-detection nonce ───────────────────────────────────────────────
const UC_NONCE = crypto.getRandomValues(new Uint32Array(1))[0].toString(36);
const UC_ATTR = 'data-uc-nonce';

// ── Controller instance ────────────────────────────────────────────────
const controller = new UniversalController({ nonce: UC_NONCE, nonceAttr: UC_ATTR });
const verifier = new PatternVerifier();
const logFn = (type, msg) => controller.log(type, msg);

// ── State object ───────────────────────────────────────────────────────
window.__UC = {
  version: '1.0.0',
  ready: false,
  mode: null,
  timestamp: 0,
  url: location.href,
  scan: null,
  diff: null,
  patterns: {},
};

// ── Scan-diff workflow ─────────────────────────────────────────────────

window.__UC_firstScan = function () {
  const snap = controller.firstScan();
  window.__UC.scan = { elements: snap.elements.size, timestamp: snap.timestamp };
  window.__UC.diff = null;
  window.__UC.mode = 'scan';
  window.__UC.timestamp = Date.now();
  return window.__UC.scan;
};

window.__UC_nextScan = function () {
  const diff = controller.nextScan();
  // If firstScan was never called, nextScan returns a snapshot (no .summary)
  if (!diff || !diff.summary) {
    console.warn('[UC] nextScan returned a snapshot, not a diff. Call __UC_firstScan() first.');
    window.__UC.mode = 'scan';
    window.__UC.timestamp = Date.now();
    return { changed: 0, added: 0, removed: 0, increased: 0, decreased: 0 };
  }
  window.__UC.diff = diff.summary;
  window.__UC.mode = 'diffed';
  window.__UC.timestamp = Date.now();
  return diff.summary;
};

window.__UC_autoDetect = function () {
  const detected = controller.autoDetect();
  for (const d of detected) {
    const pats = window.__UC.patterns[d.pattern] || [];
    pats.push({
      selector: _safeSelector(d.components?.container || d.components?.input),
      confidence: d.confidence,
      proof: d.proof,
      source: 'scan-diff',
    });
    window.__UC.patterns[d.pattern] = pats;
  }
  window.__UC.mode = 'detected';
  window.__UC.timestamp = Date.now();
  return detected.map(d => ({
    pattern: d.pattern,
    confidence: d.confidence,
    proof: d.proof,
    selector: _safeSelector(d.components?.container || d.components?.input),
  }));
};

// ── Three-signal static detection ──────────────────────────────────────

window.__UC_detect = function (patternName, guarantee) {
  const results = controller.detect(patternName, guarantee || 'BEHAVIORAL');
  window.__UC.patterns[patternName] = results.map(_serializeResult);
  window.__UC.mode = 'detected';
  window.__UC.timestamp = Date.now();
  return window.__UC.patterns[patternName];
};

window.__UC_detectAll = function (guarantee) {
  const patterns = ['chat', 'form', 'dropdown', 'modal', 'login', 'search', 'cookie', 'feed'];
  const all = {};
  for (const name of patterns) {
    const results = controller.detect(name, guarantee || 'BEHAVIORAL');
    all[name] = results.map(_serializeResult);
  }
  window.__UC.patterns = all;
  window.__UC.mode = 'detected';
  window.__UC.timestamp = Date.now();
  window.__UC.ready = true;
  return all;
};

// ── Binding ────────────────────────────────────────────────────────────

window.__UC_bind = function (patternName) {
  // Find the best detected result for this pattern
  const results = controller.detect(patternName, 'STRUCTURAL');
  if (results.length === 0) return null;
  const best = results[0];
  const api = controller.bind(patternName, best.path);
  return api ? { pattern: patternName, path: best.path } : null;
};

window.__UC_unbind = function (patternName) {
  return controller.unbind(patternName);
};

/**
 * Bind a pattern using an ML-discovered selector instead of UC detection.
 *
 * Bypasses the three-signal detector — used when ML finds a chat input
 * that UC's heuristics missed. Creates the same binding that enables
 * __UC_chatSend(), __UC_chatGetMessages(), etc.
 *
 * @param {string} patternName - "chat", "form", "search", etc.
 * @param {string} selector - CSS selector for the container element
 * @returns {{pattern, selector}} or null
 */
window.__UC_bindBySelector = function (patternName, selector) {
  const el = document.querySelector(selector);
  if (!el) return null;

  // Build a minimal detection result for the controller to bind
  const path = [];
  let cur = el;
  while (cur && cur !== document.body) {
    const parent = cur.parentElement;
    if (!parent) break;
    const idx = Array.from(parent.children).indexOf(cur);
    path.unshift(idx);
    cur = parent;
  }
  const pathStr = 'body>' + path.join('>');

  // Attempt to bind — controller.bind creates the action API
  const api = controller.bind(patternName, pathStr);
  if (api) return { pattern: patternName, selector };

  // Fallback: if controller.bind needs a detection result first,
  // manually register the element as a detected pattern
  try {
    controller._bindings = controller._bindings || {};
    controller._bindings[patternName] = {
      el,
      path: pathStr,
      components: { container: el, input: el.querySelector('textarea, [contenteditable="true"], input') },
    };
    return { pattern: patternName, selector };
  } catch (e) {
    return null;
  }
};

window.__UC_listBound = function () {
  return controller.listBoundAPIs();
};

// ── Action APIs (work on bound patterns) ───────────────────────────────

window.__UC_chatSend = function (text) {
  const api = controller.getAPI('chat');
  if (!api) { _autoBind('chat'); }
  const api2 = controller.getAPI('chat');
  if (!api2) return false;
  return api2.send(text);
};

window.__UC_chatGetMessages = function () {
  const api = controller.getAPI('chat');
  if (!api) return [];
  return api.getMessages();
};

window.__UC_chatOnMessage = function (callback) {
  const api = controller.getAPI('chat');
  if (!api) return null;
  return api.onMessage(callback);
};

window.__UC_formFill = function (data) {
  const api = controller.getAPI('form');
  if (!api) { _autoBind('form'); }
  const api2 = controller.getAPI('form');
  if (!api2) return false;
  return api2.fill(data);
};

window.__UC_formSubmit = function () {
  const api = controller.getAPI('form');
  if (!api) return false;
  return api.submit();
};

window.__UC_formGetValues = function () {
  const api = controller.getAPI('form');
  if (!api) return {};
  return api.getValues();
};

window.__UC_dropdownToggle = function () {
  const api = controller.getAPI('dropdown');
  if (!api) { _autoBind('dropdown'); }
  const api2 = controller.getAPI('dropdown');
  if (!api2) return false;
  api2.toggle();
  return true;
};

window.__UC_dropdownSelect = function (value) {
  const api = controller.getAPI('dropdown');
  if (!api) { _autoBind('dropdown'); }
  const api2 = controller.getAPI('dropdown');
  if (!api2) return false;
  api2.select(value);
  return true;
};

window.__UC_modalClose = function () {
  const api = controller.getAPI('modal');
  if (!api) { _autoBind('modal'); }
  const api2 = controller.getAPI('modal');
  if (!api2) return false;
  api2.close();
  return true;
};

// ── Convenience helpers (kept from previous version) ───────────────────

window.__UC_dismiss = function () {
  const cookies = window.__UC.patterns.cookie || [];
  for (const cc of cookies) {
    if (cc.accept_selector) {
      const btn = document.querySelector(cc.accept_selector);
      if (btn) { btn.click(); return true; }
    }
  }
  // Fallback: try detecting and binding
  const results = controller.detect('cookie', 'STRUCTURAL');
  if (results.length > 0 && results[0].components) {
    const comp = results[0].components;
    const acceptBtn = comp.container?.querySelector(
      'button[class*="accept" i], button[class*="agree" i]'
    ) || Array.from(comp.container?.querySelectorAll('button, a') || []).find(b => {
      const t = (b.textContent || '').toLowerCase().trim();
      return t.includes('accept') || t.includes('agree') || t.includes('got it') || t.includes('allow') || t === 'ok';
    });
    if (acceptBtn) { acceptBtn.click(); return true; }
  }
  return false;
};

window.__UC_fillSearch = function (query) {
  // Try bound API first
  const api = controller.getAPI('search');
  if (api && api.components?.input) {
    setText(api.components.input, query);
    return true;
  }
  // Fallback: detect and use
  const results = controller.detect('search', 'STRUCTURAL');
  if (results.length === 0) return false;
  const input = results[0].components?.input;
  if (!input) return false;
  setText(input, query);
  return true;
};

window.__UC_getVisibleText = function () {
  const feeds = window.__UC.patterns.feed || [];
  if (feeds.length > 0 && feeds[0].selector) {
    const el = document.querySelector(feeds[0].selector);
    if (el) return el.innerText;
  }
  return document.body.innerText;
};

// ── Generic input/button discovery (pattern-agnostic) ──────────────────

/**
 * Find ALL interactive inputs on the page, scored by chat-likelihood.
 * Works on ChatGPT, Claude, Slack, Discord, etc. without needing
 * pattern-specific selectors.
 */
window.__UC_findInputs = function () {
  const candidates = [
    ...document.querySelectorAll(
      '[contenteditable="true"], textarea, '
      + 'input[type="text"], input[type="search"], input:not([type])'
    ),
  ];

  return candidates
    .map(el => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);

      // Skip invisible / inert
      if (rect.width === 0 || rect.height === 0) return null;
      if (style.display === 'none' || style.visibility === 'hidden') return null;
      if (el.closest('[inert]') || el.closest('[aria-hidden="true"]')) return null;

      let score = 0;

      // Tag type scoring
      const isCE = el.contentEditable === 'true';
      if (isCE) score += 3;
      else if (el.tagName === 'TEXTAREA') score += 2;
      else score += 1;

      // Size scoring — larger inputs are more likely primary
      score += Math.min(rect.width / 400, 1.5);
      score += Math.min(rect.height / 100, 1);

      // Position — lower on page = more likely chat input (vs header search)
      const yRatio = rect.top / window.innerHeight;
      if (yRatio > 0.6) score += 2;
      else if (yRatio > 0.3) score += 1;

      // Placeholder / aria-label text signals
      const hint = (
        (el.getAttribute('placeholder') || '') + ' ' +
        (el.getAttribute('aria-label') || '') + ' ' +
        (el.getAttribute('data-testid') || '') + ' ' +
        (el.id || '')
      ).toLowerCase();

      const chatSignals = ['message', 'ask', 'type', 'prompt', 'chat', 'send', 'compose', 'reply'];
      const searchSignals = ['search', 'find', 'query', 'filter', 'keyword'];

      for (const s of chatSignals) { if (hint.includes(s)) { score += 2; break; } }
      for (const s of searchSignals) { if (hint.includes(s)) { score -= 1; break; } }

      return {
        selector: _safeSelector(el),
        tag: el.tagName,
        contentEditable: isCE,
        placeholder: el.getAttribute('placeholder') || '',
        ariaLabel: el.getAttribute('aria-label') || '',
        score: Math.round(score * 100) / 100,
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.score - a.score);
};

/**
 * Find submit/send buttons near a given input element.
 * Walks up the DOM through form, fieldset, composer containers.
 */
window.__UC_findButtons = function (inputSelector) {
  const input = inputSelector ? document.querySelector(inputSelector) : null;

  // Build search roots: walk up from input through likely containers
  const roots = [];
  if (input) {
    let cur = input;
    for (let i = 0; i < 6 && cur && cur !== document.body; i++) {
      roots.push(cur);
      cur = cur.parentElement;
    }
  }
  // Also search form, fieldset, and common composer containers
  if (input) {
    const extra = [
      input.closest('form'),
      input.closest('fieldset'),
      input.closest('[class*="composer" i]'),
      input.closest('[class*="chat" i]'),
      input.closest('[class*="prompt" i]'),
      input.closest('[data-testid]'),
    ].filter(Boolean);
    for (const e of extra) {
      if (!roots.includes(e)) roots.push(e);
    }
  }
  if (roots.length === 0) roots.push(document.body);

  const seen = new Set();
  const results = [];

  const skipPatterns = /toggle|menu|attach|upload|expand|close|collapse|emoji|format|more/i;
  const sendPatterns = /send|submit|post|enter|go/i;
  const searchPatterns = /search|find/i;

  for (const root of roots) {
    for (const btn of root.querySelectorAll('button, [role="button"], input[type="submit"]')) {
      const key = _safeSelector(btn);
      if (seen.has(key)) continue;
      seen.add(key);

      const rect = btn.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;

      const label = (
        (btn.innerText || '') + ' ' +
        (btn.getAttribute('aria-label') || '') + ' ' +
        (btn.getAttribute('data-testid') || '') + ' ' +
        (btn.title || '')
      ).toLowerCase().trim();

      let score = 0;

      // Label scoring
      if (sendPatterns.test(label)) score += 4;
      else if (searchPatterns.test(label)) score += 2;
      if (skipPatterns.test(label)) score -= 3;
      if (btn.getAttribute('aria-haspopup')) score -= 2;

      // Type scoring
      if (btn.type === 'submit') score += 2;

      // Proximity to input (lower = closer = better)
      if (input) {
        const inputRect = input.getBoundingClientRect();
        const dist = Math.abs(rect.x - inputRect.x) + Math.abs(rect.y - inputRect.y);
        score += Math.max(0, 3 - dist / 200);
      }

      if (score > 0) {
        results.push({
          selector: key,
          label: label.slice(0, 60),
          type: btn.type || '',
          score: Math.round(score * 100) / 100,
        });
      }
    }
  }

  return results.sort((a, b) => b.score - a.score);
};

/**
 * Framework-aware text input using UC's setText().
 * Handles contenteditable (Slate, ProseMirror, TipTap), React synthetic
 * events, execCommand, paste simulation, and native inputs.
 */
window.__UC_setText = function (selector, text) {
  const el = document.querySelector(selector);
  if (!el) return { success: false, error: 'Element not found' };
  try {
    el.focus();
    const result = setText(el, text);
    return { success: true, method: result?.method || 'unknown' };
  } catch (e) {
    return { success: false, error: e.message };
  }
};

/**
 * Click a button by selector, with optional pre-focus.
 */
window.__UC_clickButton = function (selector) {
  const el = document.querySelector(selector);
  if (!el) return false;
  el.click();
  return true;
};

/**
 * After a scan-diff, find where new content appeared.
 * Returns elements that had children-added or text-grew changes,
 * sorted by text length (largest = most likely response container).
 */
window.__UC_findNewContent = function () {
  if (!controller.lastDiff) return [];
  const diff = controller.lastDiff;

  const results = [];

  // Skip non-content elements
  function _isContent(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    // Skip head, script, style, nav, header elements
    if (['head', 'script', 'style', 'noscript', 'link', 'meta'].includes(tag)) return false;
    // Skip nav/sidebar containers
    if (el.getAttribute('role') === 'navigation') return false;
    if (el.tagName === 'NAV') return false;
    // Skip if it's inside <head>
    if (el.closest('head')) return false;
    // Skip invisible
    try {
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
    } catch (e) {}
    return true;
  }

  // Newly added elements with text content
  for (const item of diff.added || []) {
    if (item.el && _isContent(item.el)) {
      const text = (item.el.innerText || '').trim();
      if (text.length > 20) {
        results.push({
          selector: _safeSelector(item.el),
          change: 'added',
          textLength: text.length,
          text: text.slice(0, 500),
        });
      }
    }
  }

  // Elements where children were added (response container)
  for (const item of diff.increased || []) {
    if (item.key === 'childCount' && item.el && _isContent(item.el)) {
      const text = (item.el.innerText || '').trim();
      if (text.length > 10) {
        results.push({
          selector: _safeSelector(item.el),
          change: 'children-added',
          childDelta: (item.after || 0) - (item.before || 0),
          textLength: text.length,
          text: text.slice(0, 500),
        });
      }
    }
  }

  // Elements where text grew
  for (const item of (diff.changed || [])) {
    for (const ch of (item.changes || [])) {
      if (ch.type === 'text-grew' && item.el && _isContent(item.el)) {
        const text = (item.el.innerText || '').trim();
        if (text.length > 20) {
          results.push({
            selector: _safeSelector(item.el),
            change: 'text-grew',
            textLength: text.length,
            text: text.slice(0, 500),
          });
        }
      }
    }
  }

  // Deduplicate and sort by text length
  const seen = new Set();
  return results
    .filter(r => { if (seen.has(r.selector)) return false; seen.add(r.selector); return true; })
    .sort((a, b) => b.textLength - a.textLength);
};

// ── Trigram-based response extraction ──────────────────────────────────

/**
 * Compute character trigram set from text.
 * Returns a Set of 3-character substrings.
 */
function _trigrams(text) {
  const s = new Set();
  const t = text.toLowerCase();
  for (let i = 0; i <= t.length - 3; i++) {
    s.add(t.slice(i, i + 3));
  }
  return s;
}

/** Stored baseline trigram set, captured before an action.
 *  Exposed on window so page.evaluate() in Python can check it. */
window._baselineTrigrams = null;

/**
 * Capture a trigram fingerprint of all visible text on the page.
 * Call this BEFORE sending a message.
 */
window.__UC_captureBaseline = function () {
  const text = document.body.innerText || '';
  window._baselineTrigrams = _trigrams(text);
  return { trigrams: window._baselineTrigrams.size, textLength: text.length };
};

/**
 * Extract the response text by finding DOM elements whose trigrams
 * are mostly NEW (not present in the baseline).
 *
 * For each visible text-bearing element, computes:
 *   new_ratio = |element_trigrams - baseline_trigrams| / |element_trigrams|
 *
 * Elements with high new_ratio contain new text (the response).
 * Elements with low new_ratio contain pre-existing text (nav, sidebar).
 *
 * Returns array of {selector, text, newRatio, trigramCount} sorted by
 * newRatio * textLength descending (most new content first).
 */
window.__UC_extractResponse = function (minNewRatio) {
  if (!window._baselineTrigrams) return [];
  minNewRatio = minNewRatio || 0.5;

  const candidates = [];

  // Scan ALL elements in the body for text content.
  // Use querySelectorAll instead of TreeWalker to reach deeply nested content.
  const allEls = document.body.querySelectorAll('*');
  for (const el of allEls) {
    const tag = el.tagName.toLowerCase();
    // Skip non-content elements
    if (['script', 'style', 'noscript', 'link', 'meta', 'nav', 'head'].includes(tag)) continue;
    if (el.closest('script, style, noscript, nav, head')) continue;
    if (el.getAttribute('role') === 'navigation') continue;
    try {
      const s = getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') continue;
    } catch (e) { continue; }

    const text = (el.innerText || '').trim();
    if (text.length < 20 || text.length > 5000) continue;
    // Prefer leaf-ish elements (fewer than 10 direct children with text)
    if (el.children.length > 30) continue;

    const elTrigrams = _trigrams(text);
    if (elTrigrams.size === 0) continue;

    // Count how many trigrams are NEW (not in baseline)
    let newCount = 0;
    for (const tri of elTrigrams) {
      if (!window._baselineTrigrams.has(tri)) newCount++;
    }
    const newRatio = newCount / elTrigrams.size;

    if (newRatio >= minNewRatio) {
      candidates.push({
        selector: _safeSelector(el),
        text: text,
        newRatio: Math.round(newRatio * 1000) / 1000,
        trigramCount: elTrigrams.size,
        newTrigrams: newCount,
        textLength: text.length,
        // Score: prefer high newRatio AND substantial text
        _score: newRatio * Math.log(text.length + 1),
      });
    }
  }

  // Sort by score descending, deduplicate
  candidates.sort((a, b) => b._score - a._score);
  const seen = new Set();
  return candidates
    .filter(c => {
      if (seen.has(c.selector)) return false;
      // Also skip if text is a substring of an already-seen element
      for (const prev of seen) {
        if (c.text.length < 50) break;
      }
      seen.add(c.selector);
      return true;
    })
    .slice(0, 10)
    .map(c => ({ selector: c.selector, text: c.text, newRatio: c.newRatio, trigramCount: c.trigramCount, textLength: c.textLength }));
};

// ── LLM / Advanced ────────────────────────────────────────────────────

window.__UC_getLLMContext = function (patternName) {
  const results = controller.detect(patternName || 'search', 'STRUCTURAL');
  if (results.length === 0) return null;
  return extractLLMContext({
    el: results[0].el,
    patternName: patternName || results[0].patternName,
    confidence: results[0].confidence,
    evidence: results[0].evidence,
    components: results[0].components,
  });
};

window.__UC_heapScan = function (patternName) {
  let targetEl = null;
  if (patternName) {
    const results = controller.detect(patternName, 'STRUCTURAL');
    if (results.length > 0) targetEl = results[0].el;
  }
  return fullHeapScan(targetEl);
};

window.__UC_scanFramework = function () {
  return scanFramework();
};

window.__UC_verify = function (actionName) {
  // Returns the verifier spec — actual verification requires async action execution
  return verifier.getTraces();
};

// ── Passive detection ──────────────────────────────────────────────────

window.__UC_startPassive = function () {
  controller.startPassive();
  return true;
};

window.__UC_stopPassive = function () {
  controller.stopPassive();
  return true;
};

window.__UC_getPassiveResults = function () {
  return controller.getPassiveResults();
};

// ── Signatures ─────────────────────────────────────────────────────────

window.__UC_saveSignature = function (patternName) {
  return controller.saveSignature(patternName);
};

window.__UC_loadSignatures = function () {
  const sigs = controller.signatures.getForCurrentSite();
  return sigs.map(s => ({ id: s.id, pattern: s.patternName, hostname: s.site?.hostname }));
};

window.__UC_autoBindSignatures = function () {
  return controller.autoBind();
};

window.__UC_getAllSignatures = function () {
  return controller.signatures.getAll();
};

// ── Frame scanning ─────────────────────────────────────────────────────

window.__UC_startFrameScanning = async function () {
  return controller.startFrameScanning();
};

window.__UC_stopFrameScanning = function () {
  controller.stopFrameScanning();
};

// ── Generic MutationObserver for response streaming (no binding needed) ─

/**
 * Watch a container for new child nodes / text changes in real-time.
 * Returns a handle to stop watching. Messages collected in window.__UC_observed.
 *
 * Unlike chatOnMessage, this works WITHOUT binding — just point it at
 * any DOM element and it watches for mutations.
 */
window.__UC_observed = [];
let _observerDisconnect = null;

window.__UC_watchContainer = function (selector) {
  // Disconnect any previous observer
  if (_observerDisconnect) { _observerDisconnect(); _observerDisconnect = null; }
  window.__UC_observed = [];

  const el = document.querySelector(selector);
  if (!el) return false;

  const seen = new Set();
  // Seed with existing text to avoid double-reporting
  for (const child of el.querySelectorAll('*')) {
    const t = (child.innerText || '').trim();
    if (t.length > 5) seen.add(t.slice(0, 100));
  }

  const observer = new MutationObserver((mutations) => {
    for (const mut of mutations) {
      // New nodes added
      for (const node of mut.addedNodes) {
        if (node.nodeType !== 1) continue; // Skip text nodes
        const text = (node.innerText || '').trim();
        if (text.length < 5) continue;
        const key = text.slice(0, 100);
        if (seen.has(key)) continue;
        seen.add(key);
        window.__UC_observed.push({
          type: 'added',
          text: text.slice(0, 2000),
          tag: node.tagName,
          timestamp: Date.now(),
        });
      }
      // Character data changes (streaming tokens)
      if (mut.type === 'characterData') {
        const text = (mut.target.textContent || '').trim();
        if (text.length > 5) {
          // Update last observed entry if same parent
          const last = window.__UC_observed[window.__UC_observed.length - 1];
          if (last && last.type === 'streamed') {
            last.text = text.slice(0, 2000);
            last.timestamp = Date.now();
          } else {
            window.__UC_observed.push({
              type: 'streamed',
              text: text.slice(0, 2000),
              tag: mut.target.parentElement?.tagName || '?',
              timestamp: Date.now(),
            });
          }
        }
      }
    }
  });

  observer.observe(el, { childList: true, subtree: true, characterData: true });
  _observerDisconnect = () => observer.disconnect();
  return true;
};

window.__UC_stopWatching = function () {
  if (_observerDisconnect) { _observerDisconnect(); _observerDisconnect = null; }
  return window.__UC_observed;
};

window.__UC_getObserved = function () {
  return window.__UC_observed;
};

// ── Helpers for the chat() pipeline (replacing inline JS in Python) ───

/**
 * Find chat-input candidate containers and tag each with data-ml-id.
 * Returns array of selectors (one per candidate) for ML classification.
 * Caller must call __UC_clearChatCandidates() to remove the tags.
 */
window.__UC_findChatCandidates = function () {
  const seen = new Set();
  const sels = [];
  const inputs = document.querySelectorAll(
    'textarea, [contenteditable="true"], [role="textbox"], '
    + 'input[type="text"], input:not([type]), '
    + '[class*="chat" i], [class*="message" i], [class*="prompt" i], '
    + '[class*="composer" i], [class*="widget" i]'
  );
  for (const el of inputs) {
    let target = el;
    for (let i = 0; i < 3; i++) {
      if (target.parentElement
          && target.parentElement !== document.body
          && target.parentElement.children.length < 20) {
        target = target.parentElement;
      }
    }
    if (seen.has(target)) continue;
    seen.add(target);
    const id = 'mlchat-' + sels.length;
    target.setAttribute('data-ml-id', id);
    sels.push('[data-ml-id="' + id + '"]');
    if (sels.length >= 20) break;
  }
  return sels;
};

/** Clean up data-ml-id tags after ML classification. */
window.__UC_clearChatCandidates = function () {
  document.querySelectorAll('[data-ml-id]').forEach(
    (el) => el.removeAttribute('data-ml-id'),
  );
};

/**
 * Given a chat container selector (e.g. from ML), find the inner input
 * element and return a stable selector for it.
 */
window.__UC_resolveInnerInput = function (containerSel) {
  const el = document.querySelector(containerSel);
  if (!el) return null;
  const input = el.querySelector(
    'textarea, [contenteditable="true"], [role="textbox"], input',
  );
  if (!input) return null;
  if (input.id) return '#' + CSS.escape(input.id);
  const aria = input.getAttribute('aria-label');
  if (aria) {
    return input.tagName.toLowerCase()
      + '[aria-label="' + aria.replace(/"/g, '\\"') + '"]';
  }
  return containerSel + ' textarea, ' + containerSel + ' [contenteditable="true"]';
};

/**
 * Walk up from the given input to find the best response container,
 * then start the MutationObserver on it. Returns true if a watcher
 * was started.
 */
window.__UC_setupResponseWatcher = function (inputSel) {
  const input = document.querySelector(inputSel);
  if (!input) return false;
  let cur = input;
  for (let i = 0; i < 15 && cur; i++) {
    cur = cur.parentElement;
    if (!cur) break;
    const tag = cur.tagName.toLowerCase();
    const role = cur.getAttribute('role') || '';
    if (tag === 'main' || role === 'main' || role === 'presentation' || role === 'region') {
      const sel = cur.id
        ? '#' + CSS.escape(cur.id)
        : tag + (role ? '[role="' + role + '"]' : '');
      return window.__UC_watchContainer(sel);
    }
    try {
      const style = getComputedStyle(cur);
      if (cur.scrollHeight > cur.clientHeight + 100
          && (style.overflowY === 'auto' || style.overflowY === 'scroll')) {
        const sel = cur.id ? '#' + CSS.escape(cur.id) : 'body';
        return window.__UC_watchContainer(sel);
      }
    } catch (e) {}
  }
  return window.__UC_watchContainer('body');
};

/**
 * Check if the input at `selector` is empty (post-send verification).
 */
window.__UC_isInputCleared = function (selector) {
  const el = document.querySelector(selector);
  if (!el) return true;
  const val = el.value || el.textContent || '';
  return val.trim().length === 0;
};

/**
 * Extract the response text from a container using trigram set difference.
 * Walks descendants of the container, scores each by trigram newness vs.
 * the captured baseline (plus the sent message), returns the best match.
 *
 * IMPORTANT: clones the baseline set before adding sent_message trigrams,
 * so the baseline is preserved across calls (fixes mutation bug).
 */
window.__UC_extractFromContainer = function (containerSel, sentMessage) {
  const el = document.querySelector(containerSel);
  if (!el) return '';
  if (!window._baselineTrigrams) return el.innerText.trim();

  const _tri = (text) => {
    const s = new Set();
    const lc = text.toLowerCase();
    for (let i = 0; i <= lc.length - 3; i++) s.add(lc.slice(i, i + 3));
    return s;
  };

  // Clone the baseline (DON'T mutate the shared one across calls)
  const bl = new Set(window._baselineTrigrams);
  if (sentMessage) {
    for (const t of _tri(sentMessage)) bl.add(t);
  }

  let bestText = '';
  let bestRatio = 0;
  for (const c of el.querySelectorAll('*')) {
    const ct = (c.innerText || '').trim();
    if (ct.length < 2 || ct.length > 5000) continue;
    if (c.children.length > 15) continue;
    if (sentMessage && ct.toLowerCase().includes(sentMessage.toLowerCase().slice(0, 40))) {
      continue;
    }
    const tris = _tri(ct);
    if (!tris.size) continue;
    let n = 0;
    for (const t of tris) {
      if (!bl.has(t)) n++;
    }
    const r = n / tris.size;
    if (r > bestRatio) {
      bestRatio = r;
      bestText = ct;
    }
  }
  return (bestText && bestRatio > 0.3) ? bestText : el.innerText.trim();
};

// ── Anchor-based response locking ─────────────────────────────────────

/**
 * Find DOM elements whose innerText contains the given message.
 * Drills DOWN to the smallest containing element (skips wrappers that
 * include extra siblings' text).
 *
 * Returns up to maxResults candidate anchors, each as
 * {selector, text, depth}. Multiple matches happen when the message
 * echoes in: the chat input, sidebar history, and the main view bubble.
 */
window.__UC_findAnchorCandidates = function (sentMessage, maxResults) {
  if (!sentMessage || sentMessage.length < 3) return [];
  maxResults = maxResults || 5;
  const needle = sentMessage.trim();
  const needleLower = needle.toLowerCase();

  // Candidate elements: any visible element whose direct/descendant text contains the needle
  const all = document.body.querySelectorAll('*');
  const matches = [];

  for (const el of all) {
    // Skip head/script/style/nav
    const tag = el.tagName.toLowerCase();
    if (['script', 'style', 'noscript', 'link', 'meta', 'head'].includes(tag)) continue;
    if (el.closest('script, style, noscript, head')) continue;

    const text = (el.innerText || '').trim();
    if (!text || text.length > 20000) continue;
    if (!text.toLowerCase().includes(needleLower)) continue;

    // Drill DOWN: prefer a descendant whose own text is closer to the needle
    let best = el;
    let bestExcess = text.length - needle.length;
    for (const child of el.querySelectorAll('*')) {
      const ct = (child.innerText || '').trim();
      if (!ct.toLowerCase().includes(needleLower)) continue;
      const excess = ct.length - needle.length;
      if (excess >= 0 && excess < bestExcess) {
        bestExcess = excess;
        best = child;
      }
    }

    matches.push({ el: best, excess: bestExcess });
  }

  // Deduplicate by element and sort by tightest containment (least excess text)
  const seen = new Set();
  const unique = [];
  for (const m of matches) {
    if (seen.has(m.el)) continue;
    seen.add(m.el);
    unique.push(m);
  }
  unique.sort((a, b) => a.excess - b.excess);

  return unique.slice(0, maxResults).map((m) => ({
    selector: _safeSelector(m.el),
    text: (m.el.innerText || '').trim().slice(0, 500),
    excess: m.excess,
  }));
};

/**
 * Given an anchor selector, find the response candidate that follows it
 * in DOM order. Tries:
 *   1. anchor.nextElementSibling
 *   2. anchor.parentElement.nextElementSibling (some sites wrap each
 *      message in its own bubble container)
 *   3. Any element added under the same conversation parent that comes
 *      after the anchor in DOM order.
 *
 * Returns {selector, text} or null.
 */
window.__UC_findResponseAfterAnchor = function (anchorSel) {
  const anchor = document.querySelector(anchorSel);
  if (!anchor) return null;

  function _candidate(el) {
    if (!el) return null;
    const text = (el.innerText || '').trim();
    if (!text || text.length < 1) return null;
    return { selector: _safeSelector(el), text: text.slice(0, 500) };
  }

  // Try 1: direct next sibling
  let cand = _candidate(anchor.nextElementSibling);
  if (cand) return cand;

  // Try 2: parent's next sibling (bubble-container case)
  if (anchor.parentElement) {
    cand = _candidate(anchor.parentElement.nextElementSibling);
    if (cand) return cand;
  }

  // Try 3: walk up looking for a parent whose next sibling has text
  let cur = anchor.parentElement;
  for (let i = 0; i < 5 && cur; i++) {
    cand = _candidate(cur.nextElementSibling);
    if (cand) return cand;
    cur = cur.parentElement;
  }

  return null;
};

/**
 * Tag an element with data-uc-response="1" so it can be re-located
 * cheaply via the stable selector [data-uc-response="1"].
 * Also re-points the active MutationObserver to watch this element.
 */
window.__UC_lockResponse = function (selector) {
  // Clear any previous lock
  document.querySelectorAll('[data-uc-response]').forEach(
    (el) => el.removeAttribute('data-uc-response'),
  );
  const el = document.querySelector(selector);
  if (!el) return null;
  el.setAttribute('data-uc-response', '1');
  // Re-point the watcher to this element so streaming text comes through
  try { window.__UC_watchContainer('[data-uc-response="1"]'); } catch (e) {}
  return '[data-uc-response="1"]';
};

/**
 * Read the locked element's innerText. Returns '' if no lock.
 */
window.__UC_readLocked = function () {
  const el = document.querySelector('[data-uc-response="1"]');
  return el ? (el.innerText || '').trim() : '';
};

// ── DOMLocalityHash (structural fingerprinting) ───────────────────────

/**
 * Compute a structural fingerprint (MinHash LSH) of a DOM element.
 * Used to identify similar DOM structures across pages/visits.
 */
window.__UC_computeSignature = function (selector) {
  const el = selector === 'body' ? document.body : document.querySelector(selector);
  if (!el) return null;
  const sig = controller.lsh.signature(el);
  return {
    fingerprint: sig.fingerprint,
    features: sig.features.slice(0, 20),
    minhash: Array.from(sig.minhash),
  };
};

/**
 * Compare two structural signatures (Jaccard similarity via MinHash).
 * Returns 0.0 (completely different) to 1.0 (structurally identical).
 */
window.__UC_compareSig = function (sig1, sig2) {
  if (!sig1?.minhash || !sig2?.minhash) return 0;
  return controller.lsh.similarity(
    { minhash: new Uint32Array(sig1.minhash) },
    { minhash: new Uint32Array(sig2.minhash) },
  );
};

/**
 * Index a signature for fast cross-session similarity queries.
 */
window.__UC_indexSignature = function (key, sig) {
  if (!sig?.minhash) return false;
  controller.lsh.addToIndex(key, { minhash: new Uint32Array(sig.minhash), fingerprint: sig.fingerprint, features: sig.features }, { key });
  return true;
};

/**
 * Query the LSH index for structurally similar elements.
 */
window.__UC_querySimilar = function (sig) {
  if (!sig?.minhash) return [];
  return controller.lsh.querySimilar({ minhash: new Uint32Array(sig.minhash), fingerprint: sig.fingerprint });
};

// ── Direct controller access ───────────────────────────────────────────

window.UniversalController = controller;
window.UC = {};

// ── Serialization helpers ──────────────────────────────────────────────

function _safeSelector(el) {
  if (!el) return '';
  try {
    if (el.id) return '#' + CSS.escape(el.id);
    const aria = el.getAttribute('aria-label');
    if (aria) return el.tagName.toLowerCase() + '[aria-label="' + aria.replace(/"/g, '\\"') + '"]';
    const name = el.getAttribute('name');
    if (name) return el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\"') + '"]';
    if (el.tagName === 'INPUT') {
      const ph = el.getAttribute('placeholder');
      if (ph) return 'input[placeholder="' + ph.replace(/"/g, '\\"') + '"]';
    }
    // Path-based fallback
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && parts.length < 5) {
      let seg = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      const parent = cur.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) seg += ':nth-child(' + (Array.from(parent.children).indexOf(cur) + 1) + ')';
      }
      parts.unshift(seg);
      cur = parent;
    }
    return parts.join(' > ');
  } catch (e) {
    return '';
  }
}

function _serializeResult(r) {
  const out = {
    selector: _safeSelector(r.el),
    confidence: Math.round(r.confidence * 100) / 100,
    path: r.path,
    guarantee: r.guarantee,
    evidence: r.evidence ? {
      structural: Math.round((r.evidence.structural || 0) * 100) / 100,
      phrasal: Math.round((r.evidence.phrasal || 0) * 100) / 100,
      phrasalMatches: r.evidence.phrasalMatches,
      semantic: r.evidence.semantic || 0,
      behavioral: Math.round((r.evidence.behavioral || 0) * 100) / 100,
    } : {},
  };
  // Add pattern-specific component data
  if (r.components) {
    if (r.components.input) {
      out.input_selector = _safeSelector(r.components.input);
      out.placeholder = r.components.input.getAttribute?.('placeholder') || '';
    }
    if (r.components.container) {
      out.container_selector = _safeSelector(r.components.container);
    }
    if (r.components.fields) {
      out.fields = r.components.fields.map?.(f => ({
        name: f.name || f.getAttribute?.('name') || '',
        type: f.type || f.getAttribute?.('type') || '',
        selector: _safeSelector(f),
      })) || [];
    }
    if (r.components.items) {
      out.item_count = r.components.items.length;
      out.scrollable = r.components.container ?
        r.components.container.scrollHeight > r.components.container.clientHeight : false;
    }
    if (r.components.closeButton) {
      out.dismissible = true;
      out.dismiss_selector = _safeSelector(r.components.closeButton);
    }
    if (r.components.acceptButton) {
      out.accept_selector = _safeSelector(r.components.acceptButton);
    }
    if (r.components.submitButton) {
      out.submit_selector = _safeSelector(r.components.submitButton);
    }
    if (r.patternName === 'feed' && r.components.container) {
      const cont = r.components.container;
      out.item_count = cont.children.length;
      out.scrollable = cont.scrollHeight > cont.clientHeight;
      // Build item selector
      if (cont.children.length >= 3) {
        const tags = {};
        for (const c of cont.children) {
          const k = c.tagName.toLowerCase();
          tags[k] = (tags[k] || 0) + 1;
        }
        const best = Object.entries(tags).sort((a, b) => b[1] - a[1])[0];
        if (best) out.item_selector = _safeSelector(cont) + ' > ' + best[0];
      }
    }
  }
  return out;
}

function _autoBind(patternName) {
  const results = controller.detect(patternName, 'STRUCTURAL');
  if (results.length > 0) {
    controller.bind(patternName, results[0].path);
  }
}

// ── DOM Rasterizer ────────────────────────────────────────────────────
// Renders DOM bounding-box geometry into a small spatial feature grid
// for ML-based UI pattern classification.

window.__UC_rasterize = function (selector, gridSize = 32) {
  const channels = 4;
  const grid = new Array(gridSize * gridSize * channels).fill(0);

  let root, bounds;
  if (!selector) {
    root = document.body;
    bounds = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
  } else {
    root = document.querySelector(selector);
    if (!root) return null;
    const r = root.getBoundingClientRect();
    bounds = { left: r.left, top: r.top, width: r.width, height: r.height };
  }
  if (bounds.width === 0 || bounds.height === 0) return null;

  const scaleX = gridSize / bounds.width;
  const scaleY = gridSize / bounds.height;
  const INTERACTIVE = new Set(['INPUT', 'TEXTAREA', 'BUTTON', 'SELECT', 'A']);

  function fill(gx1, gy1, gx2, gy2, ch, val) {
    const x1 = Math.max(0, Math.min(gridSize - 1, gx1));
    const y1 = Math.max(0, Math.min(gridSize - 1, gy1));
    const x2 = Math.max(0, Math.min(gridSize - 1, gx2));
    const y2 = Math.max(0, Math.min(gridSize - 1, gy2));
    for (let y = y1; y <= y2; y++)
      for (let x = x1; x <= x2; x++)
        grid[(y * gridSize + x) * channels + ch] = Math.max(grid[(y * gridSize + x) * channels + ch], val);
  }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let node = walker.currentNode;
  while (node) {
    const rect = node.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      const gx1 = Math.floor((rect.left - bounds.left) * scaleX);
      const gy1 = Math.floor((rect.top - bounds.top) * scaleY);
      const gx2 = Math.floor((rect.right - bounds.left) * scaleX);
      const gy2 = Math.floor((rect.bottom - bounds.top) * scaleY);

      // Ch0: interactive elements
      if (INTERACTIVE.has(node.tagName) || node.contentEditable === 'true'
          || node.getAttribute('role') === 'textbox' || node.getAttribute('role') === 'button')
        fill(gx1, gy1, gx2, gy2, 0, 1.0);

      // Ch1: text density
      const textLen = (node.innerText || '').length;
      if (textLen > 0) fill(gx1, gy1, gx2, gy2, 1, Math.min(textLen / 500, 1.0));

      // Ch2: iframes
      if (node.tagName === 'IFRAME' || node.tagName === 'EMBED')
        fill(gx1, gy1, gx2, gy2, 2, 1.0);

      // Ch3: overlay / fixed
      const style = getComputedStyle(node);
      if (style.position === 'fixed' || style.position === 'sticky')
        fill(gx1, gy1, gx2, gy2, 3, 1.0);
      else {
        const z = parseInt(style.zIndex);
        if (z > 100) fill(gx1, gy1, gx2, gy2, 3, Math.min(z / 10000, 1.0));
      }
    }
    node = walker.nextNode();
  }

  // Accessibility metadata
  const a11y = { roles: [], ariaLabels: [], hasLiveRegion: false };
  root.querySelectorAll('[role]').forEach(el => {
    const r = el.getAttribute('role');
    if (r && !a11y.roles.includes(r)) a11y.roles.push(r);
  });
  root.querySelectorAll('[aria-label]').forEach(el => {
    const l = el.getAttribute('aria-label').toLowerCase();
    if (!a11y.ariaLabels.includes(l)) a11y.ariaLabels.push(l);
  });
  a11y.hasLiveRegion = !!root.querySelector('[aria-live]');

  return { grid, gridSize, channels, a11y };
};

// ── Stage 2: DOM code feature extractor ───────────────────────────────
// Extracts ~30 structural/semantic features from a DOM subtree for
// classification by a sklearn RandomForest (runs in Python).

window.__UC_extractCodeFeatures = function (selector) {
  const root = selector ? document.querySelector(selector) : document.body;
  if (!root) return null;

  const all = root.querySelectorAll('*');
  const totalEls = all.length || 1;

  // ── Tag counts ──
  const tagCounts = {};
  const interactiveTags = ['INPUT', 'TEXTAREA', 'BUTTON', 'SELECT', 'A'];
  let interactiveCount = 0;
  for (const el of all) {
    tagCounts[el.tagName] = (tagCounts[el.tagName] || 0) + 1;
    if (interactiveTags.includes(el.tagName) || el.contentEditable === 'true')
      interactiveCount++;
  }

  // ── Attribute signals ──
  let hasRole = 0, hasAriaLabel = 0, hasPlaceholder = 0;
  let hasContentEditable = 0, roleTextbox = 0, roleDialog = 0;
  let roleSearch = 0, roleNavigation = 0, roleForm = 0;
  for (const el of all) {
    if (el.getAttribute('role')) hasRole++;
    if (el.getAttribute('aria-label')) hasAriaLabel++;
    if (el.getAttribute('placeholder')) hasPlaceholder++;
    if (el.contentEditable === 'true') hasContentEditable++;
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role === 'textbox') roleTextbox++;
    if (role === 'dialog') roleDialog++;
    if (role === 'search' || role === 'searchbox') roleSearch++;
    if (role === 'navigation') roleNavigation++;
    if (role === 'form') roleForm++;
  }

  // ── Class/ID keyword signals ──
  const classText = Array.from(all).map(el =>
    ((el.className || '') + ' ' + (el.id || '')).toLowerCase()
  ).join(' ');

  const kwChat = /chat|message|compose|messenger|conversation/i.test(classText) ? 1 : 0;
  const kwSearch = /search|find|query|filter|autocomplete|combobox/i.test(classText) ? 1 : 0;
  const kwLogin = /login|signin|sign-in|auth|password|credential/i.test(classText) ? 1 : 0;
  const kwModal = /modal|dialog|overlay|popup|drawer|offcanvas/i.test(classText) ? 1 : 0;
  const kwNav = /nav|menu|sidebar|breadcrumb|tabs?|pagination/i.test(classText) ? 1 : 0;
  const kwForm = /form|field|input|label|control/i.test(classText) ? 1 : 0;
  const kwFeed = /feed|list|card|grid|item|article|post/i.test(classText) ? 1 : 0;

  // ── Structural ratios ──
  const depth = (function maxDepth(el, d) {
    let max = d;
    for (const c of el.children) max = Math.max(max, maxDepth(c, d + 1));
    return max;
  })(root, 0);

  const childCount = root.children.length;

  // ── Text content signals ──
  const text = (root.innerText || '').toLowerCase();
  const wordCount = text.split(/\s+/).filter(w => w.length > 0).length;
  const hasSend = /\bsend\b|\bsubmit\b|\bpost\b/i.test(text) ? 1 : 0;
  const hasSearch = /\bsearch\b|\bfind\b/i.test(text) ? 1 : 0;
  const hasLogin = /\blogin\b|\bsign in\b|\bpassword\b/i.test(text) ? 1 : 0;
  const hasLiveRegion = root.querySelector('[aria-live]') ? 1 : 0;

  // ── Iframe presence ──
  const iframeCount = root.querySelectorAll('iframe').length;

  // ── Position / visibility ──
  const rect = root.getBoundingClientRect();
  const isFixed = getComputedStyle(root).position === 'fixed' ? 1 : 0;
  const isBottomRight = (rect.bottom > window.innerHeight * 0.7 && rect.right > window.innerWidth * 0.7) ? 1 : 0;
  const relWidth = rect.width / window.innerWidth;
  const relHeight = rect.height / window.innerHeight;

  return {
    // Tag composition (6)
    n_input: tagCounts['INPUT'] || 0,
    n_textarea: tagCounts['TEXTAREA'] || 0,
    n_button: tagCounts['BUTTON'] || 0,
    n_select: tagCounts['SELECT'] || 0,
    n_a: tagCounts['A'] || 0,
    n_iframe: iframeCount,
    // Ratios (3)
    interactive_ratio: interactiveCount / totalEls,
    depth: depth,
    child_count: childCount,
    // ARIA / attributes (9)
    has_role: hasRole,
    has_aria_label: hasAriaLabel,
    has_placeholder: hasPlaceholder,
    has_contenteditable: hasContentEditable,
    role_textbox: roleTextbox,
    role_dialog: roleDialog,
    role_search: roleSearch,
    role_navigation: roleNavigation,
    role_form: roleForm,
    // Class/ID keywords (7)
    kw_chat: kwChat,
    kw_search: kwSearch,
    kw_login: kwLogin,
    kw_modal: kwModal,
    kw_nav: kwNav,
    kw_form: kwForm,
    kw_feed: kwFeed,
    // Text signals (5)
    word_count: wordCount,
    has_send: hasSend,
    has_search_text: hasSearch,
    has_login_text: hasLogin,
    has_live_region: hasLiveRegion,
    // Position (4)
    is_fixed: isFixed,
    is_bottom_right: isBottomRight,
    rel_width: Math.round(relWidth * 100) / 100,
    rel_height: Math.round(relHeight * 100) / 100,
  };
};

// ── Vanilla JS MLP classifier (no TFJS) ──────────────────────────────
// Forward pass for sklearn MLPClassifier trained by train_dom_classifier.py.
// Weights injected by Python via __UC_loadWeights() before __UC_classify().
// Architecture: StandardScaler → Dense(128,relu) → Dense(64,relu) → Dense(8,softmax)

let _classifierWeights = null;
let _classifierLabels = null;

window.__UC_loadWeights = function (weightsJson, labels) {
  _classifierWeights = weightsJson;
  _classifierLabels = labels;
  return true;
};

window.__UC_classify = function (selector, gridSize = 32) {
  if (!_classifierWeights || !_classifierLabels) return null;

  const raster = window.__UC_rasterize(selector, gridSize);
  if (!raster || !raster.grid) return null;

  const layers = _classifierWeights.layers;
  let x = raster.grid; // flat array, e.g. 4096 elements

  for (const layer of layers) {
    if (layer.type === 'Scaler') {
      const out = new Float32Array(x.length);
      for (let i = 0; i < x.length; i++)
        out[i] = (x[i] - layer.mean[i]) / (layer.scale[i] || 1);
      x = out;
    } else if (layer.type === 'PCA') {
      const comps = layer.components; // [n_components][n_features]
      const n = comps.length;
      const out = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        let s = 0;
        const row = comps[i];
        for (let j = 0; j < x.length; j++) s += (x[j] - layer.mean[j]) * row[j];
        out[i] = s;
      }
      x = out;
    } else if (layer.type === 'Dense') {
      const K = layer.kernel, B = layer.bias;
      const out = new Float32Array(B.length);
      for (let j = 0; j < B.length; j++) {
        let s = B[j];
        for (let i = 0; i < x.length; i++) s += x[i] * K[i][j];
        out[j] = (layer.activation === 'relu' && s < 0) ? 0 : s;
      }
      if (layer.activation === 'softmax') {
        const mx = Math.max(...out);
        let expSum = 0;
        for (let i = 0; i < out.length; i++) { out[i] = Math.exp(out[i] - mx); expSum += out[i]; }
        for (let i = 0; i < out.length; i++) out[i] /= expSum;
      }
      x = out;
    }
  }

  // x is now softmax probabilities
  let bestIdx = 0;
  for (let i = 1; i < x.length; i++) if (x[i] > x[bestIdx]) bestIdx = i;

  const scores = {};
  for (let i = 0; i < _classifierLabels.length; i++)
    scores[_classifierLabels[i]] = Math.round(x[i] * 1000) / 1000;

  return {
    label: _classifierLabels[bestIdx] || 'unknown',
    confidence: Math.round(x[bestIdx] * 1000) / 1000,
    scores,
    a11y: raster.a11y,
  };
};

// ── Initialize ─────────────────────────────────────────────────────────

if (isInIframe()) {
  const childRPC = new FrameRPCChild(controller);
  console.log('[UC] Extension loaded (child frame)');
} else {
  console.log('[UC] Extension loaded. Call __UC_detectAll() or __UC_firstScan() to begin.');

  // Auto-bind from saved signatures after page settles
  setTimeout(() => {
    const bound = controller.autoBind();
    if (bound.length > 0) {
      console.log('[UC] Auto-bound from saved signatures:', bound.join(', '));
      // Expose bound APIs on window.UC
      for (const name of bound) {
        const api = controller.getAPI(name);
        if (api) window.UC[name] = api;
      }
    }
    // Start frame scanning
    controller.startFrameScanning().then(info => {
      if (info.sameOrigin > 0 || info.rpcActive > 0) {
        console.log(`[UC] Frames: ${info.sameOrigin} agents, ${info.rpcActive} RPC`);
      }
    }).catch(() => {});
  }, 2000);
}

window.__UC.ready = true;
window.__UC.timestamp = Date.now();
