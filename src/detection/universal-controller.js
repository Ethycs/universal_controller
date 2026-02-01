// ============================================
// UNIVERSAL CONTROLLER
// ============================================

import { ValueScanner } from '../core/value-scanner.js';
import { PhrasalScanner } from '../core/phrasal-scanner.js';
import { DOMLocalityHash } from '../core/dom-locality-hash.js';
import { getElementPath, resolveElement } from '../core/element-path.js';

import { setText, submitInput } from '../actions/text-input.js';
import { chatSend, chatGetMessages, chatOnMessage } from '../actions/chat-api.js';
import { formFill, formSubmit, formGetValues } from '../actions/form-api.js';
import { dropdownToggle, dropdownSelect } from '../actions/dropdown-api.js';
import { modalClose } from '../actions/modal-api.js';

import { PATTERNS } from './patterns.js';

export class UniversalController {
  constructor() {
    this.scanner = new ValueScanner();
    this.phrasal = new PhrasalScanner();
    this.lsh = new DOMLocalityHash();
    this.detected = new Map();
    this.boundAPIs = new Map();
    this.logCallbacks = [];
    this.lastDiff = null;
  }

  onLog(cb) { this.logCallbacks.push(cb); }

  log(type, msg) {
    this.logCallbacks.forEach(cb => cb(type, msg));
    console.log(`[UC] [${type.toUpperCase()}] ${msg}`);
  }

  // ============================================
  // SCANNING (Cheat Engine Style)
  // ============================================

  firstScan() {
    const snap = this.scanner.firstScan();
    this.log('scan', `Baseline: ${snap.elements.size} elements captured`);
    return snap;
  }

  nextScan() {
    const diff = this.scanner.nextScan();
    this.lastDiff = diff;
    this.log('scan', `Diff: ${diff.summary.changed} changed, ${diff.summary.added} added, ${diff.summary.removed} removed`);
    return diff;
  }

  autoDetect() {
    if (!this.lastDiff) {
      this.log('warn', 'No diff available. Run firstScan, perform action, then nextScan first.');
      return [];
    }

    const detected = this.scanner.detectPattern(this.lastDiff);

    detected.forEach(d => {
      this.log('detect', `Found ${d.pattern} (${(d.confidence * 100).toFixed(0)}%) - ${d.proof}`);

      if (d.components.container) {
        const path = this.scanner.getPath(d.components.container);
        this.detected.set(path, {
          patternName: d.pattern,
          components: d.components,
          el: d.components.container
        });
      }
    });

    return detected;
  }

  // ============================================
  // THREE-SIGNAL DETECTION
  // ============================================

  detect(patternName, guarantee = 'BEHAVIORAL') {
    this.log('detect', `Scanning for ${patternName}...`);

    const candidates = this.scanStructural(patternName);
    this.log('info', `Found ${candidates.length} structural candidates`);

    const results = [];
    const thresholds = { STRUCTURAL: 0.2, SEMANTIC: 0.35, BEHAVIORAL: 0.5, VERIFIED: 0.7 };

    for (const candidate of candidates) {
      const evidence = {
        structural: candidate.score,
        phrasal: 0,
        semantic: 0,
        behavioral: 0
      };

      // Structural (25%)
      let confidence = candidate.score * 0.25;

      // Phrasal (30%)
      const phrasal = this.phrasal.score(candidate.el, patternName);
      evidence.phrasal = phrasal.score;
      evidence.phrasalMatches = phrasal.matches;
      confidence += phrasal.score * 0.30;

      // Semantic ARIA (15%)
      evidence.semantic = this.checkSemantic(candidate.el, patternName);
      confidence += evidence.semantic * 0.15;

      // Behavioral (30%)
      const components = this.findComponents(candidate.el, patternName);
      evidence.behavioral = this.checkBehavioral(candidate.el, patternName, components);
      confidence += evidence.behavioral * 0.30;

      const signature = this.lsh.signature(candidate.el);

      if (confidence >= thresholds[guarantee]) {
        const path = this.getPath(candidate.el);
        this.detected.set(path, { components, patternName, el: candidate.el });

        results.push({
          path,
          el: candidate.el,
          patternName,
          confidence,
          guarantee,
          evidence,
          components,
          signature: {
            fingerprint: signature.fingerprint,
            features: signature.features.slice(0, 10)
          }
        });
      }
    }

    results.sort((a, b) => b.confidence - a.confidence);

    if (results.length > 0) {
      this.log('success', `Detected ${results.length} ${patternName} pattern(s)`);
    } else {
      this.log('warn', `No ${patternName} found at ${guarantee} level`);
    }

    return results;
  }

  scanStructural(patternName) {
    const candidates = [];
    const checked = new Set();

    const patternConfig = PATTERNS[patternName];
    const patternSelectors = patternConfig?.selectors || [];

    patternSelectors.forEach(selector => {
      try {
        document.querySelectorAll(selector).forEach(el => {
          const path = this.getPath(el);
          if (checked.has(path)) return;
          checked.add(path);

          const score = this.scoreStructural(el, patternName);
          if (score > 0.2) {
            candidates.push({ el, score, path });
          }
        });
      } catch (e) {}
    });

    // Also scan for repeated children (chat, feed)
    if (patternConfig?.scanRepeatedChildren) {
      document.querySelectorAll('*').forEach(el => {
        if (this.hasRepeatedChildren(el)) {
          const path = this.getPath(el);
          if (checked.has(path)) return;
          checked.add(path);

          const score = this.scoreStructural(el, patternName);
          if (score > 0.2) {
            candidates.push({ el, score, path });
          }
        }
      });
    }

    return candidates.sort((a, b) => b.score - a.score).slice(0, 10);
  }

  hasRepeatedChildren(el) {
    if (el.children.length < 2) return false;
    const tags = {};
    [...el.children].forEach(c => {
      const key = c.className ? c.className.toString().split(' ')[0] : c.tagName;
      tags[key] = (tags[key] || 0) + 1;
    });
    return Math.max(...Object.values(tags)) >= 2;
  }

  scoreStructural(el, patternName) {
    let score = 0;
    let total = 0;

    const patternRules = PATTERNS[patternName]?.rules || {};

    for (const [rule, weight] of Object.entries(patternRules)) {
      total += weight;
      if (this.checkRule(el, rule)) score += weight;
    }

    return total > 0 ? score / total : 0;
  }

  checkRule(el, rule) {
    try {
      const style = getComputedStyle(el);
      const checks = {
        'scrollable': () => el.scrollHeight > el.clientHeight && ['auto', 'scroll'].includes(style.overflowY),
        'has-input': () => !!el.querySelector('input,textarea'),
        'has-input-nearby': () => !!el.querySelector('input,textarea') || !!el.parentElement?.querySelector('input,textarea'),
        'has-button': () => !!el.querySelector('button,input[type="submit"]'),
        'has-password': () => !!el.querySelector('input[type="password"]'),
        'repeated-children': () => this.hasRepeatedChildren(el),
        'aria-live': () => el.hasAttribute('aria-live'),
        'aria-haspopup': () => el.hasAttribute('aria-haspopup') || !!el.querySelector('[aria-haspopup]'),
        'aria-expanded': () => el.hasAttribute('aria-expanded') || !!el.querySelector('[aria-expanded]'),
        'form-tag': () => el.tagName === 'FORM',
        'role-dialog': () => el.getAttribute('role') === 'dialog',
        'fixed-position': () => style.position === 'fixed',
        'has-close': () => !!el.querySelector('[class*="close"], button'),
        'search-type': () => !!el.querySelector('input[type="search"]')
      };
      return checks[rule]?.() || false;
    } catch (e) {
      return false;
    }
  }

  checkSemantic(el, patternName) {
    const semantics = {
      chat: () => el.getAttribute('role') === 'log' || el.hasAttribute('aria-live'),
      form: () => el.tagName === 'FORM' || el.getAttribute('role') === 'form',
      dropdown: () => !!el.querySelector('[aria-haspopup]') || el.hasAttribute('aria-haspopup'),
      modal: () => el.getAttribute('role') === 'dialog' || el.getAttribute('aria-modal') === 'true',
      login: () => el.tagName === 'FORM' && !!el.querySelector('input[type="password"]'),
      search: () => el.getAttribute('role') === 'search' || !!el.querySelector('input[type="search"]'),
      cookie: () => false,
      feed: () => el.getAttribute('role') === 'feed'
    };
    return semantics[patternName]?.() ? 1 : 0;
  }

  findComponents(el, patternName) {
    const finders = {
      chat: () => {
        const container = el;
        const root = el.closest('[class*="chat"]') || el.parentElement || document.body;
        let input = root.querySelector('input,textarea,[contenteditable="true"]');
        if (!input) input = document.querySelector('[class*="chat"] input, [class*="chat"] textarea');
        let sendButton = root.querySelector('button');
        return { container, input, sendButton };
      },
      form: () => ({
        container: el.tagName === 'FORM' ? el : el.querySelector('form') || el,
        fields: [...el.querySelectorAll('input,textarea,select')],
        submitButton: el.querySelector('button[type="submit"], button, input[type="submit"]')
      }),
      dropdown: () => ({
        trigger: el.querySelector('[aria-haspopup], [aria-expanded]') || el.querySelector('button') || el,
        menu: el.querySelector('[role="listbox"], [role="menu"], ul, [class*="menu"]')
      }),
      modal: () => ({
        container: el,
        closeButton: el.querySelector('[class*="close"], button:first-of-type')
      }),
      login: () => ({
        container: el,
        fields: [...el.querySelectorAll('input')],
        submitButton: el.querySelector('button, input[type="submit"]')
      }),
      search: () => ({
        input: el.querySelector('input[type="search"], input'),
        submitButton: el.querySelector('button')
      }),
      feed: () => ({
        container: el,
        items: [...el.children]
      })
    };
    return finders[patternName]?.() || {};
  }

  checkBehavioral(el, patternName, components) {
    const checks = {
      chat: () => {
        const { input, container } = components;
        const hasInput = input && ['INPUT', 'TEXTAREA'].includes(input.tagName);
        const hasContainer = container && container.children.length > 0;
        return (hasInput && hasContainer) ? 1 : (hasContainer ? 0.5 : 0);
      },
      form: () => {
        const { fields } = components;
        return fields?.length > 0 ? 1 : 0;
      },
      dropdown: () => {
        const { trigger } = components;
        return trigger?.hasAttribute('aria-expanded') ? 1 : (trigger ? 0.5 : 0);
      },
      modal: () => {
        const { container } = components;
        try {
          return container && getComputedStyle(container).display !== 'none' ? 1 : 0;
        } catch (e) { return 0; }
      },
      login: () => {
        const { fields } = components;
        const hasPassword = fields?.some(f => f.type === 'password');
        return hasPassword ? 1 : 0;
      },
      search: () => {
        const { input } = components;
        return input ? 1 : 0;
      },
      feed: () => {
        const { items } = components;
        return items?.length > 2 ? 1 : 0;
      }
    };
    return checks[patternName]?.() || 0;
  }

  // ============================================
  // API BINDING
  // ============================================

  bind(patternName, path) {
    const detected = this.detected.get(path);
    if (!detected) {
      this.log('error', `No detected pattern at ${path}`);
      return null;
    }

    const { components, el } = detected;

    // Unbind existing API for this pattern if exists
    this.unbind(patternName);

    // Create a log helper that delegates to this.log bound to this instance
    const logFn = (type, msg) => this.log(type, msg);

    const api = {
      pattern: patternName,
      path,
      el,
      components,
      send: (text) => chatSend(components, text, logFn),
      getMessages: () => chatGetMessages(components),
      onMessage: (cb) => chatOnMessage(components, cb),
      fill: (data) => formFill(components, data, logFn),
      submit: () => formSubmit(components, logFn),
      getValues: () => formGetValues(components),
      toggle: () => dropdownToggle(components),
      select: (value) => dropdownSelect(components, value),
      close: () => modalClose(components),
      // Meta
      unbind: () => this.unbind(patternName),
      rebind: (newPath) => this.bind(patternName, newPath)
    };

    this.boundAPIs.set(patternName, api);
    this.log('success', `API bound for ${patternName}`);

    if (!unsafeWindow.UC) unsafeWindow.UC = {};
    unsafeWindow.UC[patternName] = api;

    return api;
  }

  unbind(patternName) {
    if (this.boundAPIs.has(patternName)) {
      this.boundAPIs.delete(patternName);
      if (unsafeWindow.UC) {
        delete unsafeWindow.UC[patternName];
      }
      this.log('info', `Unbound ${patternName} API`);
      return true;
    }
    return false;
  }

  unbindAll() {
    const patterns = [...this.boundAPIs.keys()];
    patterns.forEach(p => this.unbind(p));
    this.log('info', `Unbound all APIs (${patterns.length})`);
  }

  rebind(patternName, newPath) {
    return this.bind(patternName, newPath);
  }

  getAPI(patternName) {
    return this.boundAPIs.get(patternName) || null;
  }

  listBoundAPIs() {
    return [...this.boundAPIs.entries()].map(([name, api]) => ({
      pattern: name,
      path: api.path,
      el: api.el
    }));
  }

  // ============================================
  // UTILITIES
  // ============================================

  highlight(el, duration = 2000) {
    el?.classList.add('uc-highlight');
    setTimeout(() => el?.classList.remove('uc-highlight'), duration);
  }

  getPath(el) {
    return getElementPath(el);
  }

  resolveElement(path) {
    return resolveElement(path);
  }

  getAllSignatures() {
    const sigs = [];
    const seen = new Set();

    ['[role]', '[aria-live]', 'form', 'input', 'button', '[class*="chat"]', '[class*="modal"]'].forEach(sel => {
      try {
        document.querySelectorAll(sel).forEach(el => {
          const path = this.getPath(el);
          if (seen.has(path)) return;
          seen.add(path);

          const sig = this.lsh.signature(el);
          sigs.push({
            path,
            tag: el.tagName,
            fingerprint: sig.fingerprint.slice(0, 16),
            features: sig.features.slice(0, 6)
          });
        });
      } catch (e) {}
    });

    return sigs.slice(0, 50);
  }

  get stats() {
    return {
      snapshots: this.scanner.snapshotCount,
      elements: this.scanner.elementCount,
      detected: this.detected.size,
      bound: this.boundAPIs.size
    };
  }
}
