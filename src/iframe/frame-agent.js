/**
 * FrameAgent - Manages scanning agents inside same-origin iframes.
 *
 * For each accessible iframe, creates a lightweight scanner that runs
 * inside the iframe's document context and reports results back to
 * the parent controller.
 */

import { ValueScanner } from '../core/value-scanner.js';
import { PhrasalScanner } from '../core/phrasal-scanner.js';
import { DOMLocalityHash } from '../core/dom-locality-hash.js';
import { getElementPath } from '../core/element-path.js';

/**
 * A lightweight agent that runs inside an iframe's document.
 * Has its own scanner instances but reports to the parent controller.
 */
class IframeAgent {
  constructor(iframe, parentLog) {
    this.iframe = iframe;
    this.doc = iframe.contentDocument;
    this.win = iframe.contentWindow;
    this.log = parentLog;

    this.scanner = new ValueScanner();
    this.phrasal = new PhrasalScanner();
    this.lsh = new DOMLocalityHash();

    this.detected = new Map();
    this.frameId = `frame-${iframe.src || 'blank'}-${Date.now()}`;
  }

  /**
   * Run pattern detection inside this iframe.
   *
   * @param {string} patternName
   * @param {object} patterns - The PATTERNS config.
   * @returns {Array<object>} Detection results with frame context.
   */
  detect(patternName, patterns) {
    if (!this.doc || !this.doc.body) return [];

    const results = [];
    const patternConfig = patterns[patternName];
    if (!patternConfig) return [];

    const selectors = patternConfig.selectors || [];
    const checked = new Set();

    for (const selector of selectors) {
      try {
        this.doc.querySelectorAll(selector).forEach(el => {
          const path = getElementPath(el);
          if (checked.has(path)) return;
          checked.add(path);

          const score = this._scoreStructural(el, patternConfig);
          const phrasal = this.phrasal.score(el, patternName);
          const confidence = score * 0.4 + phrasal.score * 0.35 + this._checkSemantic(el, patternName) * 0.25;

          if (confidence > 0.3) {
            results.push({
              path: `${this.frameId}>${path}`,
              el,
              patternName,
              confidence,
              frameId: this.frameId,
              iframe: this.iframe,
              isIframe: true,
              evidence: {
                structural: score,
                phrasal: phrasal.score,
                semantic: this._checkSemantic(el, patternName)
              }
            });
          }
        });
      } catch (e) {}
    }

    return results.sort((a, b) => b.confidence - a.confidence);
  }

  _scoreStructural(el, config) {
    const rules = config.rules || {};
    let score = 0, total = 0;

    for (const [rule, weight] of Object.entries(rules)) {
      total += weight;
      if (this._checkRule(el, rule)) score += weight;
    }

    return total > 0 ? score / total : 0;
  }

  _checkRule(el, rule) {
    try {
      const style = this.win.getComputedStyle(el);
      const checks = {
        'scrollable': () => el.scrollHeight > el.clientHeight && ['auto', 'scroll'].includes(style.overflowY),
        'has-input': () => !!el.querySelector('input,textarea'),
        'has-input-nearby': () => !!el.querySelector('input,textarea') || !!el.parentElement?.querySelector('input,textarea'),
        'has-button': () => !!el.querySelector('button,input[type="submit"]'),
        'has-password': () => !!el.querySelector('input[type="password"]'),
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

  _checkSemantic(el, patternName) {
    const checks = {
      chat: () => el.getAttribute('role') === 'log' || el.hasAttribute('aria-live'),
      form: () => el.tagName === 'FORM' || el.getAttribute('role') === 'form',
      dropdown: () => !!el.querySelector('[aria-haspopup]'),
      modal: () => el.getAttribute('role') === 'dialog',
      login: () => el.tagName === 'FORM' && !!el.querySelector('input[type="password"]'),
      search: () => el.getAttribute('role') === 'search',
      feed: () => el.getAttribute('role') === 'feed'
    };
    return checks[patternName]?.() ? 1 : 0;
  }

  /**
   * Take a value scan snapshot inside this iframe.
   *
   * @returns {object} The snapshot.
   */
  snapshot() {
    return this.scanner.snapshot();
  }

  /**
   * Get an LSH signature for an element inside this iframe.
   *
   * @param {HTMLElement} el
   * @returns {object}
   */
  signature(el) {
    return this.lsh.signature(el);
  }
}

/**
 * Manages all iframe agents.
 */
export class FrameAgentManager {
  constructor(logFn) {
    this.agents = new Map(); // iframe element -> IframeAgent
    this.log = logFn || (() => {});
  }

  /**
   * Create an agent for a same-origin iframe.
   *
   * @param {HTMLIFrameElement} iframe
   * @returns {IframeAgent|null}
   */
  createAgent(iframe) {
    if (this.agents.has(iframe)) return this.agents.get(iframe);

    try {
      // Verify access
      const doc = iframe.contentDocument;
      if (!doc) return null;

      const agent = new IframeAgent(iframe, this.log);
      this.agents.set(iframe, agent);
      this.log('info', `Agent created for iframe: ${iframe.src || 'about:blank'}`);
      return agent;
    } catch (e) {
      this.log('warn', `Cannot create agent for iframe: ${e.message}`);
      return null;
    }
  }

  /**
   * Remove an agent for an iframe.
   *
   * @param {HTMLIFrameElement} iframe
   */
  removeAgent(iframe) {
    this.agents.delete(iframe);
  }

  /**
   * Run detection across all iframe agents.
   *
   * @param {string} patternName
   * @param {object} patterns - The PATTERNS config.
   * @returns {Array<object>} Combined results from all frames.
   */
  detectAll(patternName, patterns) {
    const results = [];
    for (const [, agent] of this.agents) {
      try {
        results.push(...agent.detect(patternName, patterns));
      } catch (e) {
        this.log('warn', `Detection failed in iframe: ${e.message}`);
      }
    }
    return results;
  }

  /**
   * Get all active agents.
   *
   * @returns {Array<{ iframe: HTMLIFrameElement, agent: IframeAgent }>}
   */
  getAll() {
    return [...this.agents.entries()].map(([iframe, agent]) => ({ iframe, agent }));
  }

  /**
   * Get the count of active agents.
   *
   * @returns {number}
   */
  get count() {
    return this.agents.size;
  }
}
