// ============================================
// UNIVERSAL CONTROLLER
// ============================================

import { ValueScanner } from '../core/value-scanner.js';
import { PhrasalScanner } from '../core/phrasal-scanner.js';
import { DOMLocalityHash } from '../core/dom-locality-hash.js';
import { getElementPath, resolveElement } from '../core/element-path.js';
import { SignatureStore } from '../core/signature-store.js';
import { PassiveDetector } from '../core/passive-detector.js';
import { FrameDiscovery } from '../iframe/frame-discovery.js';
import { FrameAgentManager } from '../iframe/frame-agent.js';
import { FrameRPCParent, isInIframe } from '../iframe/frame-rpc.js';
import { extractLLMContext, generateCopyContext } from '../llm/context-extractor.js';
import { PatternVerifier } from '../llm/state-machine.js';
import { fullHeapScan } from '../llm/heap-scanner.js';

import { setText } from '../actions/text-input.js';
import { chatSend, chatGetMessages, chatOnMessage } from '../actions/chat-api.js';
import { formFill, formSubmit, formGetValues } from '../actions/form-api.js';
import { dropdownToggle, dropdownSelect } from '../actions/dropdown-api.js';
import { modalClose } from '../actions/modal-api.js';

import { PATTERNS } from './patterns.js';

export class UniversalController {
  constructor(options = {}) {
    this.nonceAttr = options.nonceAttr || 'data-uc-nonce';
    this.nonce = options.nonce || null;
    this.scanner = new ValueScanner({ nonceAttr: this.nonceAttr });
    this.phrasal = new PhrasalScanner();
    this.lsh = new DOMLocalityHash();
    this.signatures = new SignatureStore();
    this.passive = new PassiveDetector({ nonceAttr: this.nonceAttr });
    this.frameDiscovery = new FrameDiscovery();
    this.frameAgents = new FrameAgentManager((type, msg) => this.log(type, msg));
    this.frameRPC = isInIframe() ? null : new FrameRPCParent((type, msg) => this.log(type, msg));
    this.isChild = isInIframe();
    this.detected = new Map();
    this.boundAPIs = new Map();
    this.logCallbacks = [];
    this.lastDiff = null;
    this.autoBindEnabled = true;

    // Wire passive detector logging through our log system
    this.passive.onLog((type, msg) => this.log(type, msg));

    // When passive detection infers a pattern, register it in detected map
    this.passive.onPattern((inferred) => {
      if (inferred.container) {
        const path = this.getPath(inferred.container);
        const components = this.findComponents(inferred.container, inferred.pattern);
        // Merge any extra components from passive detection
        if (inferred.input) components.input = inferred.input;
        if (inferred.trigger) components.trigger = inferred.trigger;
        this.detected.set(path, {
          patternName: inferred.pattern,
          components,
          el: inferred.container,
          source: 'passive'
        });
      }
    });
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

  _isOwnElement(el) {
    return el.closest(`[${this.nonceAttr}]`) !== null;
  }

  scanStructural(patternName) {
    const candidates = [];
    const checked = new Set();

    const patternConfig = PATTERNS[patternName];
    const patternSelectors = patternConfig?.selectors || [];

    patternSelectors.forEach(selector => {
      try {
        document.querySelectorAll(selector).forEach(el => {
          if (this._isOwnElement(el)) return;
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
        if (this._isOwnElement(el)) return;
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

  /**
   * Probe candidate input elements by writing a test string and verifying it appears.
   * Returns the first candidate where setText succeeds, then cleans up.
   * This is the Cheat Engine paradigm: try → verify → iterate.
   *
   * @param {Array<HTMLElement>} candidates
   * @returns {{ input: HTMLElement, method: string }|null}
   */
  _probeInput(candidates) {
    const probeText = `\u200B`; // zero-width space (invisible, minimal side-effects)

    for (const c of candidates) {
      // Skip inert / aria-hidden / our own elements
      if (c.closest('[inert]') || c.closest('[aria-hidden="true"]') || this._isOwnElement(c)) continue;

      // Snapshot the current value
      const isContentEditable = c.contentEditable === 'true' && !('value' in c);
      const before = isContentEditable ? c.textContent : c.value;

      // Try setting probe text
      const result = setText(c, probeText);

      // Check: did the text actually land?
      const after = isContentEditable ? c.textContent : c.value;
      const probeWorked = after?.includes(probeText);

      // Clean up: restore original value
      if (isContentEditable) {
        // Clear via selectAll + delete for contenteditable
        try {
          c.focus();
          document.execCommand('selectAll', false, null);
          document.execCommand('delete', false, null);
        } catch (e) {
          c.textContent = '';
        }
        c.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContent' }));
      } else {
        setText(c, before || '');
      }

      if (probeWorked) {
        this.log('info', `Probed input: ${c.tagName}${c.getAttribute('data-testid') ? '[' + c.getAttribute('data-testid') + ']' : ''} (${result.method})`);
        return { input: c, method: result.method };
      }
    }

    return null;
  }

  /**
   * Find the send/submit button near a chat input, filtering out menu/toggle/upload buttons.
   *
   * @param {HTMLElement} input
   * @param {HTMLElement} fallbackRoot
   * @returns {HTMLElement|null}
   */
  _findChatSendButton(input, fallbackRoot) {
    const roots = [
      input?.closest('fieldset'),
      input?.closest('[data-testid*="chat"]'),
      input?.closest('[class*="chat"]'),
      input?.closest('[class*="composer"]'),
      input?.parentElement?.parentElement?.parentElement,
      fallbackRoot,
      document.body
    ].filter(Boolean);

    const selectors = [
      'button[aria-label*="send" i]',
      'button[aria-label*="submit" i]',
      'button[data-testid*="send" i]',
      'button[type="submit"]'
    ];

    for (const root of roots) {
      for (const sel of selectors) {
        const btn = root.querySelector(sel);
        if (btn) return btn;
      }
    }

    // Fallback: last non-menu button (send buttons typically appear at the end)
    for (const root of roots) {
      const btns = [...root.querySelectorAll('button')].filter(b =>
        !b.getAttribute('aria-label')?.match(/toggle|menu|attach|upload|expand|close/i) &&
        !b.getAttribute('aria-haspopup')
      );
      if (btns.length > 0) return btns[btns.length - 1];
    }

    return null;
  }

  findComponents(el, patternName) {
    const finders = {
      chat: () => {
        const container = el;
        const root = el.closest('[class*="chat"]') || el.closest('[data-testid*="chat"]') || el.parentElement || document.body;

        // Gather all candidate inputs from the vicinity and document-wide
        const candidates = [
          ...root.querySelectorAll('[contenteditable="true"][role="textbox"], [contenteditable="true"], textarea, input:not([type="hidden"]):not([type="file"])'),
          ...document.querySelectorAll('[data-testid="chat-input"], [data-testid*="prompt"] [contenteditable="true"], [data-testid*="prompt"] textarea')
        ];

        // Probe: iterate candidates, try setText, verify text appears, clean up
        const probed = this._probeInput(candidates);
        let input = probed?.input || null;

        // If probe failed, fall back to first interactive candidate
        if (!input) {
          for (const c of candidates) {
            if (!c.closest('[inert]') && !c.closest('[aria-hidden="true"]') && !this._isOwnElement(c)) {
              input = c;
              break;
            }
          }
        }

        const sendButton = this._findChatSendButton(input, root);
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
  // SIGNATURE PERSISTENCE
  // ============================================

  /**
   * Save a signature for a confirmed-working binding.
   * Call this after the user confirms the binding works correctly.
   *
   * @param {string} patternName - The pattern to save a signature for.
   * @param {object} [sendMethodResult] - Result from the last setText call.
   * @returns {object|null} The captured signature, or null if not bound.
   */
  saveSignature(patternName, sendMethodResult) {
    const api = this.boundAPIs.get(patternName);
    if (!api) {
      this.log('error', `Cannot save signature: ${patternName} not bound`);
      return null;
    }

    const lshSig = this.lsh.signature(api.el);

    // Also add to the LSH index for cross-site similarity matching
    this.lsh.addToIndex(
      `${location.hostname}:${patternName}`,
      lshSig,
      { patternName, hostname: location.hostname }
    );

    const sig = this.signatures.capture({
      patternName,
      components: api.components,
      lshSignature: lshSig,
      sendMethodResult,
      diffEvidence: this.lastDiff?.summary || null,
      path: api.path
    });

    this.log('success', `Signature saved for ${patternName} on ${location.hostname}`);
    return sig;
  }

  /**
   * Attempt to auto-bind patterns using saved signatures for the current site.
   * Runs detection for each saved pattern and binds the best match.
   *
   * @returns {Array<string>} List of pattern names that were auto-bound.
   */
  autoBind() {
    if (!this.autoBindEnabled) {
      this.log('info', 'Auto-bind is disabled');
      return [];
    }

    const savedSigs = this.signatures.getForCurrentSite();
    if (savedSigs.length === 0) {
      this.log('info', `No saved signatures for ${location.hostname}`);
      return [];
    }

    this.log('info', `Found ${savedSigs.length} saved signature(s) for ${location.hostname}`);
    const bound = [];

    for (const sig of savedSigs) {
      try {
        // Try to detect the pattern using normal detection
        const results = this.detect(sig.patternName, 'STRUCTURAL');

        if (results.length > 0) {
          // If we have the LSH fingerprint, try to match by similarity
          let bestMatch = results[0];

          if (sig.structural?.minhashArray) {
            const savedMinhash = new Uint32Array(sig.structural.minhashArray);
            let bestSimilarity = 0;

            for (const result of results) {
              const candidateSig = this.lsh.signature(result.el);
              const sim = this.lsh.similarity(
                { minhash: savedMinhash },
                candidateSig
              );
              if (sim > bestSimilarity) {
                bestSimilarity = sim;
                bestMatch = result;
              }
            }
          }

          // Bind the best match
          const api = this.bind(sig.patternName, bestMatch.path);
          if (api) {
            this.signatures.markUsed(sig.id);
            bound.push(sig.patternName);
            this.log('success', `Auto-bound ${sig.patternName} (saved signature)`);
          }
        }
      } catch (e) {
        this.log('warn', `Auto-bind failed for ${sig.patternName}: ${e.message}`);
      }
    }

    return bound;
  }

  /**
   * Toggle auto-bind feature.
   *
   * @param {boolean} enabled
   */
  setAutoBind(enabled) {
    this.autoBindEnabled = enabled;
    this.log('info', `Auto-bind ${enabled ? 'enabled' : 'disabled'}`);
  }

  // ============================================
  // PASSIVE OBSERVATION
  // ============================================

  /**
   * Start passive observation mode.
   * Captures user interactions and correlates with DOM mutations
   * to infer UI patterns without manual scanning.
   */
  startPassive() {
    this.passive.start();
  }

  /**
   * Stop passive observation mode.
   */
  stopPassive() {
    this.passive.stop();
  }

  /**
   * Toggle passive observation mode.
   *
   * @returns {boolean} New enabled state.
   */
  togglePassive() {
    if (this.passive.enabled) {
      this.stopPassive();
    } else {
      this.startPassive();
    }
    return this.passive.enabled;
  }

  /**
   * Get passively inferred patterns.
   *
   * @returns {Array<object>}
   */
  getPassiveResults() {
    return this.passive.getInferred();
  }

  // ============================================
  // CROSS-IFRAME SUPPORT
  // ============================================

  /**
   * Start frame discovery and create agents for same-origin iframes.
   * Also pings cross-origin iframes for UC instances via RPC.
   *
   * @returns {Promise<{ sameOrigin: number, crossOrigin: number, rpcActive: number }>}
   */
  async startFrameScanning() {
    if (this.isChild) {
      this.log('info', 'Running as child frame, skipping frame scanning');
      return { sameOrigin: 0, crossOrigin: 0, rpcActive: 0 };
    }

    // Discover all iframes
    const frames = this.frameDiscovery.start();
    this.log('info', `Discovered ${frames.length} iframe(s)`);

    // Create agents for accessible (same-origin) frames
    let sameOrigin = 0;
    for (const { iframe, info } of frames) {
      if (info.accessible && info.hasContent) {
        const agent = this.frameAgents.createAgent(iframe);
        if (agent) sameOrigin++;
      }
    }

    // Watch for new iframes and auto-create agents
    this.frameDiscovery.onFrame(({ iframe, info }) => {
      this.log('info', `New iframe detected: ${info.src || 'about:blank'} (${info.sameOrigin ? 'same-origin' : 'cross-origin'})`);
      if (info.accessible && info.hasContent) {
        this.frameAgents.createAgent(iframe);
      }
    });

    // Ping cross-origin frames for UC instances
    let rpcActive = 0;
    if (this.frameRPC) {
      const crossOriginFrames = this.frameDiscovery.getCrossOrigin();
      for (const { iframe } of crossOriginFrames) {
        try {
          const hasUC = await this.frameRPC.ping(iframe.contentWindow);
          if (hasUC) {
            rpcActive++;
            this.log('success', `UC found in cross-origin iframe: ${iframe.src}`);
          }
        } catch (e) {}
      }
    }

    const crossOrigin = this.frameDiscovery.getCrossOrigin().length;
    this.log('info', `Frames: ${sameOrigin} same-origin agents, ${crossOrigin} cross-origin, ${rpcActive} with UC`);

    return { sameOrigin, crossOrigin, rpcActive };
  }

  /**
   * Stop frame scanning and clean up.
   */
  stopFrameScanning() {
    this.frameDiscovery.stop();
  }

  /**
   * Run detection across all frames (main page + iframes).
   *
   * @param {string} patternName
   * @param {string} [guarantee='BEHAVIORAL']
   * @returns {Promise<Array<object>>} Combined results from all frames.
   */
  async detectAcrossFrames(patternName, guarantee = 'BEHAVIORAL') {
    // Detect in main page
    const mainResults = this.detect(patternName, guarantee);

    // Detect in same-origin iframe agents
    const iframeResults = this.frameAgents.detectAll(patternName, PATTERNS);
    for (const result of iframeResults) {
      this.detected.set(result.path, {
        patternName: result.patternName,
        components: {},
        el: result.el,
        iframe: result.iframe,
        source: 'iframe-agent'
      });
    }

    // Detect in cross-origin frames via RPC
    const rpcResults = [];
    if (this.frameRPC) {
      for (const win of this.frameRPC.knownFrames) {
        try {
          const results = await this.frameRPC.call(win, 'detect', [patternName, guarantee]);
          if (Array.isArray(results)) {
            for (const r of results) {
              rpcResults.push({
                ...r,
                isIframe: true,
                source: 'rpc'
              });
            }
          }
        } catch (e) {
          this.log('warn', `RPC detect failed: ${e.message}`);
        }
      }
    }

    const allResults = [...mainResults, ...iframeResults, ...rpcResults];
    allResults.sort((a, b) => b.confidence - a.confidence);

    if (iframeResults.length > 0 || rpcResults.length > 0) {
      this.log('info', `Cross-frame results: ${mainResults.length} main + ${iframeResults.length} iframe-agent + ${rpcResults.length} RPC`);
    }

    return allResults;
  }

  /**
   * Get info about discovered frames.
   *
   * @returns {object}
   */
  getFrameInfo() {
    return {
      total: this.frameDiscovery.getAll().length,
      accessible: this.frameDiscovery.getAccessible().length,
      crossOrigin: this.frameDiscovery.getCrossOrigin().length,
      agents: this.frameAgents.count,
      rpcFrames: this.frameRPC?.knownFrames.size || 0,
      isChild: this.isChild
    };
  }

  // ============================================
  // LLM INTEGRATION
  // ============================================

  /**
   * Generate LLM context for a bound pattern.
   * Packages element HTML, attributes, evidence, and framework info
   * into a formatted prompt string suitable for Claude/ChatGPT.
   *
   * @param {string} patternName - The pattern to generate context for.
   * @returns {string|null} The formatted context, or null if not bound.
   */
  getLLMContext(patternName) {
    const api = this.boundAPIs.get(patternName);
    if (!api) {
      this.log('error', `Cannot generate LLM context: ${patternName} not bound`);
      return null;
    }

    return extractLLMContext({
      el: api.el,
      patternName,
      evidence: api.evidence,
      components: api.components,
      path: api.path
    });
  }

  /**
   * Generate LLM context from a detection result (not yet bound).
   *
   * @param {object} detectionResult - A result from detect().
   * @returns {string}
   */
  getLLMContextForResult(detectionResult) {
    return generateCopyContext(detectionResult, this);
  }

  /**
   * Create a verifier for a bound pattern to test behavioral guarantees.
   *
   * @param {string} patternName
   * @returns {PatternVerifier|null}
   */
  createVerifier(patternName) {
    const api = this.boundAPIs.get(patternName);
    if (!api) {
      this.log('error', `Cannot create verifier: ${patternName} not bound`);
      return null;
    }

    const verifier = new PatternVerifier(
      patternName,
      api.components,
      (type, msg) => this.log(type, msg)
    );

    this.log('info', `Verifier created for ${patternName}`);
    return verifier;
  }

  /**
   * Run a full heap scan for framework detection and state extraction.
   *
   * @param {HTMLElement} [targetEl] - Optional element to extract framework state for.
   * @returns {object}
   */
  heapScan(targetEl) {
    const result = fullHeapScan(targetEl);
    this.log('info', `Heap scan: ${result.framework.framework} ${result.framework.version || ''}, ${result.globals.length} globals found`);
    return result;
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
          if (this._isOwnElement(el)) return;
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
      bound: this.boundAPIs.size,
      signatures: this.signatures.count,
      frames: this.frameAgents.count
    };
  }
}
