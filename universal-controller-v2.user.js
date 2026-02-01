// ==UserScript==
// @name         Universal Controller v2
// @namespace    https://github.com/universal-controller
// @version      0.2.0
// @description  Cheat Engine-style value scanning + LSH + phrasal detection for universal UI API binding
// @author       Universal Controller
// @match        *://*/*
// @grant        GM_registerMenuCommand
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_addStyle
// @grant        unsafeWindow
// @run-at       document-idle
// ==/UserScript==

(function() {
  'use strict';

  // ============================================
  // STYLES
  // ============================================

  GM_addStyle(`
    #uc-panel {
      position: fixed;
      bottom: 20px;
      right: 20px;
      width: 420px;
      max-height: 600px;
      background: #0a0a0f;
      border: 1px solid #2a2a3a;
      border-radius: 12px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 13px;
      color: #e8e8f0;
      z-index: 2147483647;
      box-shadow: 0 10px 40px rgba(0,0,0,0.5);
      display: none;
      flex-direction: column;
      overflow: hidden;
    }

    #uc-panel.visible { display: flex; }

    #uc-header {
      padding: 12px 16px;
      background: linear-gradient(135deg, #12121a 0%, #1a1a25 100%);
      border-bottom: 1px solid #2a2a3a;
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: move;
    }

    #uc-title {
      font-weight: 600;
      font-size: 14px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    #uc-title::before {
      content: '';
      width: 8px;
      height: 8px;
      background: #00d4ff;
      border-radius: 50%;
      animation: uc-pulse 2s infinite;
    }

    @keyframes uc-pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }

    #uc-close {
      background: none;
      border: none;
      color: #606078;
      font-size: 18px;
      cursor: pointer;
      padding: 0;
      line-height: 1;
    }

    #uc-close:hover { color: #e8e8f0; }

    #uc-tabs {
      display: flex;
      border-bottom: 1px solid #2a2a3a;
      background: #12121a;
    }

    .uc-tab {
      flex: 1;
      padding: 10px;
      background: none;
      border: none;
      color: #606078;
      font-size: 11px;
      cursor: pointer;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      transition: all 0.15s ease;
      position: relative;
    }

    .uc-tab:hover { color: #9090a8; }

    .uc-tab.active {
      color: #00d4ff;
    }

    .uc-tab.active::after {
      content: '';
      position: absolute;
      bottom: 0;
      left: 0;
      right: 0;
      height: 2px;
      background: #00d4ff;
    }

    .uc-tab-content {
      display: none;
      padding: 12px 16px;
      overflow-y: auto;
      flex: 1;
    }

    .uc-tab-content.active { display: block; }

    #uc-body {
      overflow-y: auto;
      flex: 1;
      display: flex;
      flex-direction: column;
    }

    .uc-section {
      margin-bottom: 16px;
    }

    .uc-section:last-child { margin-bottom: 0; }

    .uc-section-title {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: #606078;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .uc-section-title .uc-badge {
      background: rgba(0, 212, 255, 0.15);
      color: #00d4ff;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 9px;
    }

    .uc-btn-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 6px;
    }

    .uc-btn-grid-3 {
      grid-template-columns: repeat(3, 1fr);
    }

    .uc-btn {
      padding: 8px 12px;
      background: #1a1a25;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
      color: #e8e8f0;
      font-size: 11px;
      cursor: pointer;
      transition: all 0.15s ease;
      text-align: center;
    }

    .uc-btn:hover {
      background: #252530;
      border-color: #3a3a4a;
    }

    .uc-btn.primary {
      background: linear-gradient(135deg, #00d4ff, #a855f7);
      border: none;
      color: white;
      grid-column: span 2;
    }

    .uc-btn.primary:hover { opacity: 0.9; }

    .uc-btn.success {
      background: #10b981;
      border-color: #10b981;
    }

    .uc-btn.warning {
      background: #f59e0b;
      border-color: #f59e0b;
      color: #000;
    }

    .uc-btn.active {
      border-color: #00d4ff;
      background: rgba(0, 212, 255, 0.1);
    }

    .uc-btn.full {
      grid-column: span 2;
    }

    .uc-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .uc-pattern-grid {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 6px;
    }

    .uc-pattern {
      padding: 8px 4px;
      background: #1a1a25;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
      text-align: center;
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .uc-pattern:hover { border-color: #3a3a4a; }

    .uc-pattern.active {
      border-color: #00d4ff;
      background: rgba(0, 212, 255, 0.1);
    }

    .uc-pattern-icon { font-size: 16px; }

    .uc-pattern-name {
      font-size: 9px;
      color: #9090a8;
      margin-top: 2px;
    }

    #uc-log {
      background: #0a0a0f;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
      padding: 8px;
      max-height: 120px;
      overflow-y: auto;
      font-family: 'SF Mono', 'Fira Code', monospace;
      font-size: 10px;
    }

    .uc-log-entry {
      padding: 2px 0;
      border-bottom: 1px solid rgba(255,255,255,0.03);
      display: flex;
      gap: 6px;
      align-items: flex-start;
    }

    .uc-log-time {
      color: #606078;
      flex-shrink: 0;
    }

    .uc-log-type {
      padding: 1px 4px;
      border-radius: 3px;
      font-size: 8px;
      font-weight: 600;
      text-transform: uppercase;
      flex-shrink: 0;
    }

    .uc-log-type.info { background: rgba(0, 212, 255, 0.15); color: #00d4ff; }
    .uc-log-type.success { background: rgba(16, 185, 129, 0.15); color: #10b981; }
    .uc-log-type.warn { background: rgba(245, 158, 11, 0.15); color: #f59e0b; }
    .uc-log-type.error { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
    .uc-log-type.detect { background: rgba(168, 85, 247, 0.15); color: #a855f7; }
    .uc-log-type.scan { background: rgba(236, 72, 153, 0.15); color: #ec4899; }

    .uc-log-msg {
      color: #9090a8;
      word-break: break-all;
      line-height: 1.4;
    }

    .uc-results {
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 200px;
      overflow-y: auto;
    }

    .uc-result {
      background: #1a1a25;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
      padding: 10px;
    }

    .uc-result-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }

    .uc-result-type {
      font-weight: 600;
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .uc-result-confidence {
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 4px;
      font-family: monospace;
    }

    .uc-conf-high { background: rgba(16, 185, 129, 0.15); color: #10b981; }
    .uc-conf-med { background: rgba(245, 158, 11, 0.15); color: #f59e0b; }
    .uc-conf-low { background: rgba(239, 68, 68, 0.15); color: #ef4444; }

    .uc-result-evidence {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 4px;
      margin-bottom: 8px;
    }

    .uc-evidence-item {
      background: #0a0a0f;
      padding: 4px;
      border-radius: 4px;
      text-align: center;
    }

    .uc-evidence-label {
      font-size: 8px;
      color: #606078;
      text-transform: uppercase;
    }

    .uc-evidence-value {
      font-size: 11px;
      font-weight: 600;
      color: #00d4ff;
    }

    .uc-result-path {
      font-family: monospace;
      font-size: 9px;
      color: #606078;
      margin-bottom: 8px;
      word-break: break-all;
      background: #0a0a0f;
      padding: 4px 6px;
      border-radius: 4px;
    }

    .uc-result-actions {
      display: flex;
      gap: 6px;
    }

    .uc-result-actions .uc-btn {
      flex: 1;
      padding: 6px;
      font-size: 10px;
    }

    #uc-api-input {
      width: 100%;
      padding: 8px 10px;
      background: #1a1a25;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
      color: #e8e8f0;
      font-family: 'SF Mono', 'Fira Code', monospace;
      font-size: 11px;
      margin-bottom: 8px;
    }

    #uc-api-input:focus {
      outline: none;
      border-color: #00d4ff;
    }

    #uc-api-output {
      background: #0a0a0f;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
      padding: 8px;
      font-family: monospace;
      font-size: 10px;
      color: #10b981;
      min-height: 60px;
      max-height: 150px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    .uc-highlight {
      outline: 3px solid #00d4ff !important;
      outline-offset: 2px !important;
      animation: uc-highlight-pulse 1s ease-in-out;
    }

    @keyframes uc-highlight-pulse {
      0%, 100% { outline-color: #00d4ff; }
      50% { outline-color: #a855f7; }
    }

    #uc-fab {
      position: fixed;
      bottom: 20px;
      right: 20px;
      width: 50px;
      height: 50px;
      background: linear-gradient(135deg, #00d4ff, #a855f7);
      border: none;
      border-radius: 50%;
      color: white;
      font-size: 20px;
      cursor: pointer;
      z-index: 2147483646;
      box-shadow: 0 4px 15px rgba(0, 212, 255, 0.3);
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.2s ease;
    }

    #uc-fab:hover { transform: scale(1.1); }
    #uc-fab.hidden { display: none; }

    .uc-diff-list {
      max-height: 200px;
      overflow-y: auto;
      background: #0a0a0f;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
    }

    .uc-diff-item {
      padding: 6px 8px;
      border-bottom: 1px solid #1a1a25;
      font-size: 10px;
      font-family: monospace;
    }

    .uc-diff-item:last-child { border-bottom: none; }

    .uc-diff-path {
      color: #a855f7;
      margin-bottom: 2px;
    }

    .uc-diff-change {
      display: flex;
      gap: 8px;
      color: #9090a8;
    }

    .uc-diff-key { color: #00d4ff; }
    .uc-diff-before { color: #ef4444; }
    .uc-diff-after { color: #10b981; }

    .uc-scan-status {
      background: #1a1a25;
      border: 1px solid #2a2a3a;
      border-radius: 6px;
      padding: 10px;
      text-align: center;
      margin-bottom: 12px;
    }

    .uc-scan-status.active {
      border-color: #10b981;
      background: rgba(16, 185, 129, 0.1);
    }

    .uc-scan-count {
      font-size: 24px;
      font-weight: 700;
      color: #00d4ff;
    }

    .uc-scan-label {
      font-size: 10px;
      color: #606078;
      text-transform: uppercase;
    }

    .uc-filter-row {
      display: flex;
      gap: 6px;
      margin-bottom: 8px;
    }

    .uc-filter-select {
      flex: 1;
      padding: 6px 8px;
      background: #1a1a25;
      border: 1px solid #2a2a3a;
      border-radius: 4px;
      color: #e8e8f0;
      font-size: 11px;
    }

    .uc-filter-input {
      flex: 2;
      padding: 6px 8px;
      background: #1a1a25;
      border: 1px solid #2a2a3a;
      border-radius: 4px;
      color: #e8e8f0;
      font-size: 11px;
      font-family: monospace;
    }

    .uc-stats-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 6px;
      margin-bottom: 12px;
    }

    .uc-stat {
      background: #1a1a25;
      border-radius: 6px;
      padding: 8px;
      text-align: center;
    }

    .uc-stat-value {
      font-size: 16px;
      font-weight: 700;
      color: #00d4ff;
    }

    .uc-stat-label {
      font-size: 8px;
      color: #606078;
      text-transform: uppercase;
    }
  `);

  // ============================================
  // VALUE SCANNER (Cheat Engine Style)
  // ============================================

  class ValueScanner {
    constructor() {
      this.snapshots = [];
      this.watchlist = new Map();
    }

    snapshot() {
      const snap = {
        timestamp: Date.now(),
        elements: new Map()
      };

      document.querySelectorAll('*').forEach(el => {
        try {
          const path = this.getPath(el);
          const values = this.extractValues(el);

          if (Object.keys(values).length > 0) {
            snap.elements.set(path, {
              el,
              path,
              values,
              tag: el.tagName,
              id: el.id,
              className: el.className?.toString?.().slice(0, 50)
            });
          }
        } catch (e) {}
      });

      this.snapshots.push(snap);

      if (this.snapshots.length > 10) {
        this.snapshots.shift();
      }

      return snap;
    }

    extractValues(el) {
      const values = {};

      // Text content (leaf nodes only)
      if (el.childNodes.length <= 3) {
        const text = el.innerText?.trim();
        if (text && text.length < 500 && text.length > 0) {
          values.text = text;
          values.textLength = text.length;
        }
      }

      // Input values
      if ('value' in el && el.value !== undefined) {
        values.value = el.value;
        values.valueLength = el.value?.length || 0;
      }

      // Checked/selected state
      if ('checked' in el) values.checked = el.checked;
      if ('selected' in el) values.selected = el.selected;

      // Child count
      values.childCount = el.children.length;

      // Scroll position
      if (el.scrollHeight > el.clientHeight) {
        values.scrollTop = el.scrollTop;
        values.scrollHeight = el.scrollHeight;
        values.isScrollable = true;
      }

      // Visibility
      try {
        const style = getComputedStyle(el);
        values.display = style.display;
        values.visibility = style.visibility;
        values.opacity = parseFloat(style.opacity);
      } catch (e) {}

      // Dimensions
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 || rect.height > 0) {
        values.width = Math.round(rect.width);
        values.height = Math.round(rect.height);
      }

      // ARIA state
      ['aria-expanded', 'aria-hidden', 'aria-selected', 'aria-checked', 'aria-pressed'].forEach(attr => {
        if (el.hasAttribute(attr)) {
          values[attr] = el.getAttribute(attr);
        }
      });

      // Data attributes
      for (const attr of el.attributes) {
        if (attr.name.startsWith('data-') && attr.value.length < 100) {
          values[attr.name] = attr.value;
        }
      }

      return values;
    }

    firstScan() {
      this.snapshots = [];
      return this.snapshot();
    }

    nextScan() {
      if (this.snapshots.length === 0) {
        return this.firstScan();
      }
      const before = this.snapshots[this.snapshots.length - 1];
      const after = this.snapshot();
      return this.diff(before, after);
    }

    diff(before, after) {
      const results = {
        changed: [],
        unchanged: [],
        added: [],
        removed: [],
        increased: [],
        decreased: [],
        summary: {}
      };

      for (const [path, afterData] of after.elements) {
        const beforeData = before.elements.get(path);

        if (!beforeData) {
          results.added.push({ path, el: afterData.el, values: afterData.values });
          continue;
        }

        const changes = this.diffValues(beforeData.values, afterData.values);

        if (changes.length > 0) {
          const changeData = {
            path,
            el: afterData.el,
            changes,
            before: beforeData.values,
            after: afterData.values
          };
          results.changed.push(changeData);

          for (const change of changes) {
            if (typeof change.before === 'number' && typeof change.after === 'number') {
              if (change.after > change.before) {
                results.increased.push({ path, el: afterData.el, ...change });
              } else if (change.after < change.before) {
                results.decreased.push({ path, el: afterData.el, ...change });
              }
            }
          }
        } else {
          results.unchanged.push({ path, el: afterData.el });
        }
      }

      for (const [path, beforeData] of before.elements) {
        if (!after.elements.has(path)) {
          results.removed.push({ path, values: beforeData.values });
        }
      }

      results.summary = {
        changed: results.changed.length,
        added: results.added.length,
        removed: results.removed.length,
        increased: results.increased.length,
        decreased: results.decreased.length
      };

      return results;
    }

    diffValues(before, after) {
      const changes = [];
      const allKeys = new Set([...Object.keys(before), ...Object.keys(after)]);

      for (const key of allKeys) {
        const bVal = before[key];
        const aVal = after[key];

        if (bVal !== aVal) {
          changes.push({
            key,
            before: bVal,
            after: aVal,
            type: this.categorizeChange(key, bVal, aVal)
          });
        }
      }

      return changes;
    }

    categorizeChange(key, before, after) {
      if (key === 'childCount' && after > before) return 'children-added';
      if (key === 'childCount' && after < before) return 'children-removed';
      if (key === 'textLength' && after > before) return 'text-grew';
      if (key === 'textLength' && after < before) return 'text-shrunk';
      if (key === 'value' && after === '') return 'input-cleared';
      if (key === 'value' && before === '') return 'input-filled';
      if (key === 'scrollTop') return 'scrolled';
      if (key === 'display' && after !== 'none' && before === 'none') return 'became-visible';
      if (key === 'display' && after === 'none') return 'became-hidden';
      if (key === 'aria-expanded') return 'aria-toggled';
      if (key === 'checked') return 'check-toggled';
      return 'value-changed';
    }

    // Auto-detect patterns from diff
    detectPattern(diff) {
      const detected = [];

      // Chat detection
      const chat = this.detectChat(diff);
      if (chat) detected.push(chat);

      // Form detection
      const form = this.detectForm(diff);
      if (form) detected.push(form);

      // Dropdown detection
      const dropdown = this.detectDropdown(diff);
      if (dropdown) detected.push(dropdown);

      // Modal detection
      const modal = this.detectModal(diff);
      if (modal) detected.push(modal);

      return detected;
    }

    detectChat(diff) {
      const inputCleared = diff.changed.filter(c =>
        c.changes.some(ch => ch.type === 'input-cleared')
      );

      const childrenAdded = diff.changed.filter(c =>
        c.changes.some(ch => ch.type === 'children-added')
      );

      if (inputCleared.length > 0 && childrenAdded.length > 0) {
        const container = childrenAdded.sort((a, b) => {
          const aChange = a.changes.find(c => c.key === 'childCount');
          const bChange = b.changes.find(c => c.key === 'childCount');
          return ((bChange?.after || 0) - (bChange?.before || 0)) -
                 ((aChange?.after || 0) - (aChange?.before || 0));
        })[0];

        return {
          pattern: 'chat',
          confidence: 0.95,
          proof: 'input-cleared + children-added',
          components: {
            container: container?.el,
            input: inputCleared[0]?.el
          }
        };
      }

      return null;
    }

    detectForm(diff) {
      const inputsCleared = diff.changed.filter(c =>
        c.changes.some(ch => ch.type === 'input-cleared')
      );

      const becameVisible = diff.changed.filter(c =>
        c.changes.some(ch => ch.type === 'became-visible')
      );

      if (inputsCleared.length >= 2) {
        const formEl = inputsCleared[0]?.el?.closest('form');

        return {
          pattern: 'form',
          confidence: 0.85,
          proof: `${inputsCleared.length} inputs cleared`,
          components: {
            form: formEl,
            inputs: inputsCleared.map(i => i.el)
          }
        };
      }

      return null;
    }

    detectDropdown(diff) {
      const ariaToggled = diff.changed.filter(c =>
        c.changes.some(ch => ch.key === 'aria-expanded')
      );

      const becameVisible = diff.changed.filter(c =>
        c.changes.some(ch => ch.type === 'became-visible')
      );

      if (ariaToggled.length > 0) {
        const trigger = ariaToggled[0]?.el;
        const expanded = ariaToggled[0]?.after?.['aria-expanded'] === 'true';

        return {
          pattern: 'dropdown',
          confidence: 0.9,
          state: expanded ? 'opened' : 'closed',
          proof: 'aria-expanded toggled',
          components: {
            trigger,
            menu: becameVisible[0]?.el
          }
        };
      }

      return null;
    }

    detectModal(diff) {
      const becameVisible = diff.changed.filter(c => {
        const wasHidden = c.before.display === 'none';
        const isVisible = c.after.display !== 'none';
        return wasHidden && isVisible;
      });

      const fixedVisible = becameVisible.filter(c => {
        try {
          return getComputedStyle(c.el).position === 'fixed';
        } catch (e) { return false; }
      });

      if (fixedVisible.length > 0) {
        return {
          pattern: 'modal',
          confidence: 0.85,
          state: 'opened',
          proof: 'fixed element became visible',
          components: {
            container: fixedVisible[0]?.el
          }
        };
      }

      return null;
    }

    filterByType(diff, changeType) {
      return diff.changed.filter(c =>
        c.changes.some(ch => ch.type === changeType)
      );
    }

    getPath(el) {
      const parts = [];
      while (el && el !== document.body && el.parentElement) {
        const idx = [...el.parentElement.children].indexOf(el);
        parts.unshift(`${el.tagName}[${idx}]`);
        el = el.parentElement;
      }
      return parts.join('>');
    }

    get snapshotCount() {
      return this.snapshots.length;
    }

    get lastSnapshot() {
      return this.snapshots[this.snapshots.length - 1];
    }

    get elementCount() {
      return this.lastSnapshot?.elements.size || 0;
    }
  }

  // ============================================
  // PHRASAL SCANNER
  // ============================================

  class PhrasalScanner {
    constructor() {
      this.patterns = {
        chat: {
          strong: ['send message', 'send a message', 'type a message', 'write a message', 'reply'],
          medium: ['chat', 'message', 'conversation', 'dm', 'direct message'],
          placeholders: ['type here', 'write something', 'enter message', 'say something'],
          buttons: ['send', 'reply', 'post'],
          negative: ['email', 'subscribe', 'newsletter', 'search']
        },
        form: {
          strong: ['submit', 'sign up', 'register', 'create account', 'subscribe'],
          medium: ['email', 'password', 'username', 'name', 'phone', 'address'],
          labels: ['required', 'optional', 'invalid', 'error'],
          buttons: ['submit', 'send', 'continue', 'next', 'save'],
          negative: ['search', 'filter']
        },
        login: {
          strong: ['sign in', 'log in', 'login', 'forgot password', 'remember me'],
          medium: ['username', 'email', 'password'],
          buttons: ['sign in', 'log in', 'login'],
          negative: ['create account', 'sign up', 'register']
        },
        search: {
          strong: ['search'],
          medium: ['find', 'look up', 'filter'],
          placeholders: ['search', 'search...', 'find'],
          buttons: ['search', 'find', 'go'],
          negative: ['message', 'chat', 'password']
        },
        dropdown: {
          strong: ['select', 'choose', 'pick one'],
          medium: ['option', 'select an option'],
          negative: []
        },
        modal: {
          strong: ['close', 'dismiss'],
          medium: ['cancel', 'confirm', 'ok', 'done'],
          buttons: ['close', 'cancel', 'ok', 'confirm', 'done', '×'],
          negative: []
        },
        cookie: {
          strong: ['accept cookies', 'cookie policy', 'we use cookies', 'cookie consent'],
          medium: ['privacy', 'gdpr', 'consent', 'preferences'],
          buttons: ['accept', 'accept all', 'reject', 'manage'],
          negative: []
        },
        feed: {
          strong: ['load more', 'show more'],
          medium: ['posts', 'feed', 'timeline', 'updates'],
          negative: []
        }
      };
    }

    extractText(el) {
      const texts = {
        innerText: (el.innerText || '').toLowerCase().slice(0, 1000),
        placeholder: (el.placeholder || '').toLowerCase(),
        ariaLabel: (el.getAttribute('aria-label') || '').toLowerCase(),
        buttons: [],
        inputs: [],
        labels: []
      };

      el.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(btn => {
        texts.buttons.push((btn.innerText || btn.value || '').toLowerCase());
      });

      el.querySelectorAll('input, textarea').forEach(input => {
        texts.inputs.push({
          placeholder: (input.placeholder || '').toLowerCase(),
          ariaLabel: (input.getAttribute('aria-label') || '').toLowerCase(),
          name: (input.name || '').toLowerCase(),
          type: input.type || ''
        });

        if (input.id) {
          const label = document.querySelector(`label[for="${input.id}"]`);
          if (label) texts.labels.push(label.innerText.toLowerCase());
        }
      });

      return texts;
    }

    score(el, patternName) {
      const pattern = this.patterns[patternName];
      if (!pattern) return { score: 0, matches: [] };

      const texts = this.extractText(el);
      const allText = [
        texts.innerText,
        texts.placeholder,
        texts.ariaLabel,
        ...texts.buttons,
        ...texts.labels,
        ...texts.inputs.map(i => `${i.placeholder} ${i.ariaLabel} ${i.name}`)
      ].join(' ');

      let score = 0;
      const matches = [];

      // Strong signals
      pattern.strong?.forEach(phrase => {
        if (allText.includes(phrase)) {
          score += 0.35;
          matches.push({ phrase, strength: 'strong' });
        }
      });

      // Medium signals
      pattern.medium?.forEach(phrase => {
        if (allText.includes(phrase)) {
          score += 0.15;
          matches.push({ phrase, strength: 'medium' });
        }
      });

      // Placeholder patterns
      pattern.placeholders?.forEach(phrase => {
        const hasPlaceholder = texts.inputs.some(i =>
          i.placeholder.includes(phrase)
        ) || texts.placeholder.includes(phrase);

        if (hasPlaceholder) {
          score += 0.25;
          matches.push({ phrase, strength: 'placeholder' });
        }
      });

      // Button patterns
      pattern.buttons?.forEach(phrase => {
        if (texts.buttons.some(b => b.includes(phrase))) {
          score += 0.2;
          matches.push({ phrase, strength: 'button' });
        }
      });

      // Negative signals
      pattern.negative?.forEach(phrase => {
        if (allText.includes(phrase)) {
          score -= 0.25;
          matches.push({ phrase, strength: 'negative' });
        }
      });

      score = Math.max(0, Math.min(1, score));

      return { score, matches };
    }
  }

  // ============================================
  // DOM LOCALITY-SENSITIVE HASHING
  // ============================================

  class DOMLocalityHash {
    constructor() {
      this.shingleSize = 3;
      this.numHashes = 64;
      this.seeds = Array.from({ length: this.numHashes }, (_, i) => i * 0x9e3779b9);
    }

    extractFeatures(el) {
      const features = [];

      const walk = (node, depth = 0) => {
        if (node.nodeType !== 1 || depth > 6) return;

        features.push(`tag:${node.tagName}`);
        features.push(`depth:${Math.min(depth, 10)}`);
        features.push(`children:${this.bucketCount(node.children.length)}`);

        try {
          const style = getComputedStyle(node);
          if (node.scrollHeight > node.clientHeight &&
              ['auto', 'scroll'].includes(style.overflowY)) {
            features.push('scrollable');
          }
          if (style.position === 'fixed') features.push('fixed');
        } catch (e) {}

        if (node.querySelector('input,textarea')) features.push('has-input');
        if (node.querySelector('button')) features.push('has-button');
        if (node.getAttribute('role')) features.push(`role:${node.getAttribute('role')}`);
        if (node.getAttribute('aria-live')) features.push('aria-live');
        if (node.getAttribute('aria-haspopup')) features.push('aria-haspopup');

        const childTags = [...node.children].slice(0, 5).map(c => c.tagName).join(',');
        if (childTags) features.push(`shape:${childTags}`);

        const childTagCounts = {};
        [...node.children].forEach(c => {
          childTagCounts[c.tagName] = (childTagCounts[c.tagName] || 0) + 1;
        });
        const maxRepeat = Math.max(...Object.values(childTagCounts), 0);
        if (maxRepeat > 2) features.push(`repeat:${this.bucketCount(maxRepeat)}`);

        [...node.children].forEach(c => walk(c, depth + 1));
      };

      walk(el);
      return features;
    }

    bucketCount(n) {
      if (n === 0) return '0';
      if (n === 1) return '1';
      if (n <= 3) return '2-3';
      if (n <= 10) return '4-10';
      return '10+';
    }

    hash32(str) {
      let h = 0x811c9dc5;
      for (let i = 0; i < str.length; i++) {
        h ^= str.charCodeAt(i);
        h = Math.imul(h, 0x01000193);
      }
      return h >>> 0;
    }

    signature(el) {
      const features = this.extractFeatures(el);
      const shingles = new Set();
      
      for (let i = 0; i <= features.length - this.shingleSize; i++) {
        shingles.add(features.slice(i, i + this.shingleSize).join('|'));
      }

      const minhash = new Uint32Array(this.numHashes).fill(0xFFFFFFFF);
      for (const shingle of shingles) {
        const h = this.hash32(shingle);
        for (let i = 0; i < this.numHashes; i++) {
          const permuted = (h ^ this.seeds[i]) >>> 0;
          if (permuted < minhash[i]) minhash[i] = permuted;
        }
      }

      return {
        features,
        fingerprint: Array.from(minhash.slice(0, 8)).map(h => h.toString(16).padStart(8, '0')).join('')
      };
    }
  }

  // ============================================
  // UNIVERSAL CONTROLLER
  // ============================================

  class UniversalController {
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

      const selectors = {
        chat: ['[role="log"]', '[aria-live]', '[class*="message"]', '[class*="chat"]', '[class*="conversation"]'],
        form: ['form', '[role="form"]', '[class*="form"]'],
        dropdown: ['[aria-haspopup]', '[aria-expanded]', '[class*="dropdown"]', '[class*="select"]'],
        modal: ['[role="dialog"]', '[aria-modal]', '[class*="modal"]', '[class*="dialog"]', '[class*="popup"]'],
        login: ['[class*="login"]', '[class*="signin"]', 'form'],
        search: ['[class*="search"]', '[role="search"]', 'input[type="search"]'],
        cookie: ['[class*="cookie"]', '[class*="consent"]', '[class*="gdpr"]'],
        feed: ['[class*="feed"]', '[class*="timeline"]', '[class*="posts"]']
      };

      const patternSelectors = selectors[patternName] || [];

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
      if (['chat', 'feed'].includes(patternName)) {
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

      const rules = {
        chat: { scrollable: 3, 'has-input-nearby': 3, 'repeated-children': 2, 'aria-live': 2 },
        form: { 'form-tag': 4, 'has-input': 3, 'has-button': 2 },
        dropdown: { 'aria-haspopup': 3, 'aria-expanded': 2 },
        modal: { 'fixed-position': 3, 'role-dialog': 3, 'has-close': 1 },
        login: { 'has-password': 4, 'form-tag': 2, 'has-button': 1 },
        search: { 'has-input': 3, 'search-type': 3 },
        cookie: { 'fixed-position': 2, 'has-button': 2 },
        feed: { scrollable: 3, 'repeated-children': 4 }
      };

      const patternRules = rules[patternName] || {};

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

      const api = {
        pattern: patternName,
        path,
        el,
        components,
        send: (text) => this.chatSend(components, text),
        getMessages: () => this.chatGetMessages(components),
        onMessage: (cb) => this.chatOnMessage(components, cb),
        fill: (data) => this.formFill(components, data),
        submit: () => this.formSubmit(components),
        getValues: () => this.formGetValues(components),
        toggle: () => this.dropdownToggle(components),
        select: (value) => this.dropdownSelect(components, value),
        close: () => this.modalClose(components),
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
    // ACTIONS
    // ============================================

    // ============================================
    // GENERIC ACTIONS (framework-agnostic)
    // ============================================

    setText(input, text) {
      if (!input) return false;
      
      input.focus();
      
      // Clear existing content
      if (input.select) {
        input.select();
      } else if (input.contentEditable === 'true') {
        // contenteditable
        const range = document.createRange();
        range.selectNodeContents(input);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
      
      // Method 1: Paste simulation (most universal)
      try {
        const dt = new DataTransfer();
        dt.setData('text/plain', text);
        const pasteEvent = new ClipboardEvent('paste', {
          bubbles: true,
          cancelable: true,
          clipboardData: dt
        });
        input.dispatchEvent(pasteEvent);
      } catch (e) {
        // DataTransfer not supported in some contexts
      }
      
      // Verify or fallback to execCommand
      const currentValue = input.value ?? input.textContent;
      if (currentValue !== text) {
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, text);
      }
      
      // Final fallback: direct set + events
      const finalValue = input.value ?? input.textContent;
      if (finalValue !== text) {
        if ('value' in input) {
          const setter = Object.getOwnPropertyDescriptor(
            input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
            'value'
          )?.set;
          setter?.call(input, text) || (input.value = text);
        } else {
          input.textContent = text;
        }
        input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
      }
      
      return true;
    }

    findSubmitButton(input) {
      const searchRoots = [
        input?.closest('form'),
        input?.closest('[class*="chat"]'),
        input?.closest('[class*="composer"]'),
        input?.closest('[class*="input"]'),
        input?.closest('[data-testid]'),
        input?.parentElement?.parentElement?.parentElement,
        document.body
      ].filter(Boolean);

      const selectors = [
        'button[type="submit"]',
        'button[aria-label*="send" i]',
        'button[aria-label*="Submit" i]',
        'button[data-testid*="send" i]',
        'button:not([type="button"]):not([aria-label*="attach" i]):not([aria-label*="upload" i])',
        '[role="button"][aria-label*="send" i]'
      ];

      for (const root of searchRoots) {
        for (const selector of selectors) {
          try {
            const btn = root.querySelector(selector);
            if (btn && !btn.disabled && btn.offsetParent !== null) {
              return btn;
            }
          } catch (e) {}
        }
      }
      
      return null;
    }

    submitInput(input) {
      const form = input?.closest('form');
      const btn = this.findSubmitButton(input);
      
      if (btn && !btn.disabled) {
        this.log('info', `Clicking button: ${btn.textContent?.slice(0, 20) || btn.ariaLabel || 'submit'}`);
        btn.click();
        return 'button';
      }
      
      if (form?.requestSubmit) {
        this.log('info', 'Using form.requestSubmit()');
        form.requestSubmit();
        return 'form';
      }
      
      if (form) {
        this.log('info', 'Using form.submit()');
        form.submit();
        return 'form';
      }
      
      this.log('info', 'Sending Enter key');
      input?.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter',
        code: 'Enter',
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true
      }));
      return 'enter';
    }

    chatSend(components, text) {
      const { input } = components;
      if (!input) return { success: false, error: 'No input found' };

      this.setText(input, text);
      
      setTimeout(() => {
        const method = this.submitInput(input);
        this.log('success', `Sent via ${method}: "${text.slice(0, 30)}${text.length > 30 ? '...' : ''}"`);
      }, 50);

      return { success: true };
    }

    chatGetMessages(components) {
      const { container } = components;
      if (!container) return [];

      const messages = [];
      const walk = (el) => {
        const text = el.innerText?.trim();
        if (el.children.length === 0 && text && text.length > 0 && text.length < 1000) {
          messages.push({ text, el });
        } else {
          [...el.children].forEach(walk);
        }
      };
      walk(container);
      return messages;
    }

    chatOnMessage(components, callback) {
      const { container } = components;
      if (!container) return null;

      const observer = new MutationObserver(mutations => {
        for (const m of mutations) {
          for (const node of m.addedNodes) {
            if (node.nodeType === 1) callback({ text: node.innerText, el: node });
          }
        }
      });

      observer.observe(container, { childList: true, subtree: true });
      return () => observer.disconnect();
    }

    formFill(components, data) {
      const { fields } = components;
      if (!fields) return { success: false };

      const filled = [];

      for (const field of fields) {
        const key = field.name || field.id || field.placeholder?.toLowerCase() ||
                    field.getAttribute('aria-label')?.toLowerCase();

        const match = Object.entries(data).find(([k]) =>
          key?.toLowerCase().includes(k.toLowerCase())
        );

        if (match) {
          this.setText(field, match[1]);
          filled.push({ key, value: match[1] });
        }
      }

      this.log('success', `Filled ${filled.length} fields`);
      return { success: true, filled };
    }

    formSubmit(components) {
      const { submitButton, container, fields } = components;
      const input = fields?.[0] || container?.querySelector('input');
      
      if (submitButton) {
        submitButton.click();
      } else {
        this.submitInput(input);
      }
      
      this.log('success', 'Form submitted');
      return { success: true };
    }

    formGetValues(components) {
      const { fields } = components;
      const values = {};
      fields?.forEach((f, i) => {
        values[f.name || f.id || `field-${i}`] = f.value;
      });
      return values;
    }

    dropdownToggle(components) {
      components.trigger?.click();
      return { success: true };
    }

    dropdownSelect(components, value) {
      const { trigger, menu } = components;
      trigger?.click();
      setTimeout(() => {
        const options = menu?.querySelectorAll('[role="option"], li, [class*="option"]') || [];
        [...options].find(o => o.innerText?.includes(value))?.click();
      }, 100);
      return { success: true };
    }

    modalClose(components) {
      const { closeButton, container } = components;
      if (closeButton) closeButton.click();
      else container?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27, bubbles: true }));
      return { success: true };
    }

    // ============================================
    // UTILITIES
    // ============================================

    highlight(el, duration = 2000) {
      el?.classList.add('uc-highlight');
      setTimeout(() => el?.classList.remove('uc-highlight'), duration);
    }

    getPath(el) {
      const parts = [];
      while (el && el !== document.body && el.parentElement) {
        const idx = [...el.parentElement.children].indexOf(el);
        parts.unshift(`${el.tagName}[${idx}]`);
        el = el.parentElement;
      }
      return parts.join('>');
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

  // ============================================
  // UI
  // ============================================

  const controller = new UniversalController();
  let panelVisible = false;
  let selectedPattern = 'chat';
  let detectionResults = [];
  let currentTab = 'scan';

  function createUI() {
    const fab = document.createElement('button');
    fab.id = 'uc-fab';
    fab.innerHTML = '⚡';
    fab.title = 'Universal Controller v2';
    fab.addEventListener('click', togglePanel);
    document.body.appendChild(fab);

    const panel = document.createElement('div');
    panel.id = 'uc-panel';
    panel.innerHTML = `
      <div id="uc-header">
        <div id="uc-title">Universal Controller v2</div>
        <button id="uc-close">&times;</button>
      </div>
      <div id="uc-tabs">
        <button class="uc-tab active" data-tab="scan">Scan</button>
        <button class="uc-tab" data-tab="detect">Detect</button>
        <button class="uc-tab" data-tab="api">API</button>
        <button class="uc-tab" data-tab="log">Log</button>
      </div>
      <div id="uc-body">
        <!-- SCAN TAB -->
        <div class="uc-tab-content active" data-tab="scan">
          <div class="uc-stats-grid">
            <div class="uc-stat">
              <div class="uc-stat-value" id="stat-snapshots">0</div>
              <div class="uc-stat-label">Snapshots</div>
            </div>
            <div class="uc-stat">
              <div class="uc-stat-value" id="stat-elements">0</div>
              <div class="uc-stat-label">Elements</div>
            </div>
            <div class="uc-stat">
              <div class="uc-stat-value" id="stat-changed">-</div>
              <div class="uc-stat-label">Changed</div>
            </div>
            <div class="uc-stat">
              <div class="uc-stat-value" id="stat-detected">0</div>
              <div class="uc-stat-label">Detected</div>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Cheat Engine Mode <span class="uc-badge">Value Scan</span></div>
            <div class="uc-btn-grid">
              <button class="uc-btn primary" id="btn-first-scan">📸 First Scan (Baseline)</button>
              <button class="uc-btn warning" id="btn-next-scan">🔄 Next Scan (Diff)</button>
              <button class="uc-btn success" id="btn-auto-detect">🎯 Auto-Detect Pattern</button>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Last Diff</div>
            <div class="uc-diff-list" id="diff-list">
              <div class="uc-diff-item" style="color: #606078; text-align: center;">
                Run First Scan, perform an action, then Next Scan
              </div>
            </div>
          </div>
        </div>

        <!-- DETECT TAB -->
        <div class="uc-tab-content" data-tab="detect">
          <div class="uc-section">
            <div class="uc-section-title">Pattern</div>
            <div class="uc-pattern-grid">
              <div class="uc-pattern active" data-pattern="chat">
                <div class="uc-pattern-icon">💬</div>
                <div class="uc-pattern-name">Chat</div>
              </div>
              <div class="uc-pattern" data-pattern="form">
                <div class="uc-pattern-icon">📝</div>
                <div class="uc-pattern-name">Form</div>
              </div>
              <div class="uc-pattern" data-pattern="login">
                <div class="uc-pattern-icon">🔐</div>
                <div class="uc-pattern-name">Login</div>
              </div>
              <div class="uc-pattern" data-pattern="dropdown">
                <div class="uc-pattern-icon">📋</div>
                <div class="uc-pattern-name">Drop</div>
              </div>
              <div class="uc-pattern" data-pattern="modal">
                <div class="uc-pattern-icon">🪟</div>
                <div class="uc-pattern-name">Modal</div>
              </div>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Three-Signal Detection</div>
            <div class="uc-btn-grid">
              <button class="uc-btn primary" id="btn-detect">🔍 Detect Selected Pattern</button>
              <button class="uc-btn" id="btn-detect-all">Scan All</button>
              <button class="uc-btn" id="btn-signatures">LSH Sigs</button>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Results</div>
            <div class="uc-results" id="uc-results">
              <div style="color: #606078; text-align: center; padding: 10px; font-size: 11px;">
                No patterns detected yet
              </div>
            </div>
          </div>
        </div>

        <!-- API TAB -->
        <div class="uc-tab-content" data-tab="api">
          <div class="uc-section">
            <div class="uc-section-title">Quick Actions</div>
            <div class="uc-btn-grid">
              <button class="uc-btn" id="btn-chat-send">💬 Send Chat</button>
              <button class="uc-btn" id="btn-form-fill">📝 Fill Form</button>
              <button class="uc-btn" id="btn-dropdown-toggle">📋 Toggle Drop</button>
              <button class="uc-btn" id="btn-modal-close">🪟 Close Modal</button>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Bound APIs</div>
            <div id="bound-apis-list" class="uc-results" style="max-height: 150px;">
              <div style="color: #606078; text-align: center; padding: 10px; font-size: 11px;">
                None yet. Detect and bind patterns first.
              </div>
            </div>
            <div class="uc-btn-grid" style="margin-top: 8px;">
              <button class="uc-btn" id="btn-unbind-all">🗑️ Unbind All</button>
              <button class="uc-btn" id="btn-refresh-bindings">🔄 Refresh List</button>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Console</div>
            <input type="text" id="uc-api-input" placeholder="UC.chat.send('Hello')">
            <div id="uc-api-output">// Output appears here
// Commands:
//   UC.chat.send('text')
//   UC.chat.unbind()
//   UC.form.fill({email: '...'})
//   UniversalController.unbindAll()
//   UniversalController.listBoundAPIs()</div>
          </div>
        </div>

        <!-- LOG TAB -->
        <div class="uc-tab-content" data-tab="log">
          <div id="uc-log"></div>
        </div>
      </div>
    `;
    document.body.appendChild(panel);

    // Event listeners
    document.getElementById('uc-close').addEventListener('click', togglePanel);

    // Tabs
    document.querySelectorAll('.uc-tab').forEach(tab => {
      tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    // Pattern selection
    document.querySelectorAll('.uc-pattern').forEach(el => {
      el.addEventListener('click', () => {
        document.querySelectorAll('.uc-pattern').forEach(p => p.classList.remove('active'));
        el.classList.add('active');
        selectedPattern = el.dataset.pattern;
      });
    });

    // Scan buttons
    document.getElementById('btn-first-scan').addEventListener('click', () => {
      controller.firstScan();
      updateStats();
      renderDiff(null);
    });

    document.getElementById('btn-next-scan').addEventListener('click', () => {
      const diff = controller.nextScan();
      updateStats();
      renderDiff(diff);
    });

    document.getElementById('btn-auto-detect').addEventListener('click', () => {
      const detected = controller.autoDetect();
      updateStats();
      if (detected.length > 0) {
        detectionResults = detected.map(d => ({
          path: d.components.container ? controller.getPath(d.components.container) : '',
          el: d.components.container,
          patternName: d.pattern,
          confidence: d.confidence,
          evidence: { behavioral: 1, proof: d.proof },
          components: d.components
        }));
        switchTab('detect');
        renderResults();
      }
    });

    // Detect buttons
    document.getElementById('btn-detect').addEventListener('click', () => {
      detectionResults = controller.detect(selectedPattern, 'BEHAVIORAL');
      renderResults();
      updateStats();
    });

    document.getElementById('btn-detect-all').addEventListener('click', () => {
      const patterns = ['chat', 'form', 'login', 'dropdown', 'modal', 'search', 'cookie', 'feed'];
      detectionResults = patterns.flatMap(p => controller.detect(p, 'BEHAVIORAL'));
      renderResults();
      updateStats();
    });

    document.getElementById('btn-signatures').addEventListener('click', () => {
      const sigs = controller.getAllSignatures();
      document.getElementById('uc-api-output').textContent = sigs.map(s =>
        `${s.fingerprint} ${s.tag} ${s.features.slice(0, 3).join(', ')}`
      ).join('\n');
      switchTab('api');
    });

    // API buttons
    document.getElementById('btn-chat-send').addEventListener('click', () => {
      const api = controller.getAPI('chat');
      if (api) {
        const text = prompt('Message:', 'Hello from UC!');
        if (text) api.send(text);
      } else {
        controller.log('warn', 'Bind chat API first');
      }
    });

    document.getElementById('btn-form-fill').addEventListener('click', () => {
      const api = controller.getAPI('form') || controller.getAPI('login');
      if (api) {
        api.fill({ email: 'test@example.com', name: 'Test User', password: 'test123' });
      } else {
        controller.log('warn', 'Bind form API first');
      }
    });

    document.getElementById('btn-dropdown-toggle').addEventListener('click', () => {
      const api = controller.getAPI('dropdown');
      if (api) api.toggle();
      else controller.log('warn', 'Bind dropdown API first');
    });

    document.getElementById('btn-modal-close').addEventListener('click', () => {
      const api = controller.getAPI('modal');
      if (api) api.close();
      else controller.log('warn', 'Bind modal API first');
    });

    document.getElementById('btn-unbind-all').addEventListener('click', () => {
      controller.unbindAll();
      renderBoundAPIs();
    });

    document.getElementById('btn-refresh-bindings').addEventListener('click', () => {
      renderBoundAPIs();
    });

    // API console
    document.getElementById('uc-api-input').addEventListener('keypress', (e) => {
      if (e.key === 'Enter') executeAPI();
    });

    // Log handler
    controller.onLog((type, msg) => {
      const log = document.getElementById('uc-log');
      if (!log) return;

      const time = new Date().toLocaleTimeString('en-US', { hour12: false });
      const entry = document.createElement('div');
      entry.className = 'uc-log-entry';
      entry.innerHTML = `
        <span class="uc-log-time">${time.slice(0, 5)}</span>
        <span class="uc-log-type ${type}">${type}</span>
        <span class="uc-log-msg">${msg}</span>
      `;
      log.appendChild(entry);
      log.scrollTop = log.scrollHeight;

      while (log.children.length > 100) log.removeChild(log.firstChild);
    });

    makeDraggable(panel, document.getElementById('uc-header'));
  }

  function togglePanel() {
    panelVisible = !panelVisible;
    document.getElementById('uc-panel').classList.toggle('visible', panelVisible);
    document.getElementById('uc-fab').classList.toggle('hidden', panelVisible);
    if (panelVisible) updateStats();
  }

  function switchTab(tabName) {
    currentTab = tabName;
    document.querySelectorAll('.uc-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
    document.querySelectorAll('.uc-tab-content').forEach(c => c.classList.toggle('active', c.dataset.tab === tabName));
  }

  function updateStats() {
    const stats = controller.stats;
    document.getElementById('stat-snapshots').textContent = stats.snapshots;
    document.getElementById('stat-elements').textContent = stats.elements;
    document.getElementById('stat-detected').textContent = stats.detected;

    if (controller.lastDiff) {
      document.getElementById('stat-changed').textContent = controller.lastDiff.summary.changed;
    }

    // Update bound APIs list
    renderBoundAPIs();
  }

  function renderBoundAPIs() {
    const container = document.getElementById('bound-apis-list');
    if (!container) return;

    const apis = controller.listBoundAPIs();

    if (apis.length === 0) {
      container.innerHTML = `
        <div style="color: #606078; text-align: center; padding: 10px; font-size: 11px;">
          None yet. Detect and bind patterns first.
        </div>
      `;
      return;
    }

    container.innerHTML = apis.map(api => `
      <div class="uc-result" style="padding: 8px;">
        <div class="uc-result-header" style="margin-bottom: 4px;">
          <span class="uc-result-type">${getIcon(api.pattern)} UC.${api.pattern}</span>
          <span class="uc-result-confidence uc-conf-high">bound</span>
        </div>
        <div class="uc-result-path" style="margin-bottom: 6px;">${api.path?.slice(-50) || 'N/A'}</div>
        <div class="uc-result-actions">
          <button class="uc-btn uc-unbind-btn" data-pattern="${api.pattern}">Unbind</button>
          <button class="uc-btn uc-highlight-bound-btn" data-pattern="${api.pattern}">Show</button>
        </div>
      </div>
    `).join('');

    // Attach unbind handlers
    container.querySelectorAll('.uc-unbind-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const pattern = btn.dataset.pattern;
        controller.unbind(pattern);
        renderBoundAPIs();
      });
    });

    // Attach highlight handlers
    container.querySelectorAll('.uc-highlight-bound-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const pattern = btn.dataset.pattern;
        const api = controller.getAPI(pattern);
        if (api?.el) {
          controller.highlight(api.el);
          api.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
    });
  }

  function renderDiff(diff) {
    const container = document.getElementById('diff-list');

    if (!diff || diff.changed.length === 0) {
      container.innerHTML = `
        <div class="uc-diff-item" style="color: #606078; text-align: center;">
          ${diff ? 'No changes detected' : 'Run First Scan, perform an action, then Next Scan'}
        </div>
      `;
      return;
    }

    container.innerHTML = diff.changed.slice(0, 20).map(c => `
      <div class="uc-diff-item">
        <div class="uc-diff-path">${c.path.slice(-50)}</div>
        ${c.changes.slice(0, 3).map(ch => `
          <div class="uc-diff-change">
            <span class="uc-diff-key">${ch.key}</span>
            <span class="uc-diff-before">${String(ch.before).slice(0, 20)}</span>
            <span>→</span>
            <span class="uc-diff-after">${String(ch.after).slice(0, 20)}</span>
          </div>
        `).join('')}
      </div>
    `).join('');
  }

  function renderResults() {
    const container = document.getElementById('uc-results');

    if (detectionResults.length === 0) {
      container.innerHTML = `
        <div style="color: #606078; text-align: center; padding: 10px; font-size: 11px;">
          No patterns detected
        </div>
      `;
      return;
    }

    container.innerHTML = detectionResults.map((r, i) => `
      <div class="uc-result">
        <div class="uc-result-header">
          <span class="uc-result-type">${getIcon(r.patternName)} ${r.patternName.toUpperCase()}</span>
          <span class="uc-result-confidence ${getConfClass(r.confidence)}">${(r.confidence * 100).toFixed(0)}%</span>
        </div>
        ${r.evidence ? `
          <div class="uc-result-evidence">
            <div class="uc-evidence-item">
              <div class="uc-evidence-label">Struct</div>
              <div class="uc-evidence-value">${((r.evidence.structural || 0) * 100).toFixed(0)}%</div>
            </div>
            <div class="uc-evidence-item">
              <div class="uc-evidence-label">Phrase</div>
              <div class="uc-evidence-value">${((r.evidence.phrasal || 0) * 100).toFixed(0)}%</div>
            </div>
            <div class="uc-evidence-item">
              <div class="uc-evidence-label">ARIA</div>
              <div class="uc-evidence-value">${((r.evidence.semantic || 0) * 100).toFixed(0)}%</div>
            </div>
            <div class="uc-evidence-item">
              <div class="uc-evidence-label">Behav</div>
              <div class="uc-evidence-value">${((r.evidence.behavioral || 0) * 100).toFixed(0)}%</div>
            </div>
          </div>
        ` : ''}
        <div class="uc-result-path">${r.path?.slice(-60) || 'N/A'}</div>
        <div class="uc-result-actions">
          <button class="uc-btn success uc-bind-btn" data-index="${i}">Bind</button>
          <button class="uc-btn uc-highlight-btn" data-index="${i}">Show</button>
        </div>
      </div>
    `).join('');

    // Attach event listeners for bind/highlight buttons
    container.querySelectorAll('.uc-bind-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.index);
        const r = detectionResults[idx];
        if (r) {
          controller.bind(r.patternName, r.path);
          updateStats();
          controller.log('success', `Bound ${r.patternName} API`);
        }
      });
    });

    container.querySelectorAll('.uc-highlight-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.index);
        const r = detectionResults[idx];
        if (r?.el) {
          controller.highlight(r.el);
          r.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
    });
  }

  function getIcon(p) {
    return { chat: '💬', form: '📝', login: '🔐', dropdown: '📋', modal: '🪟', search: '🔍', cookie: '🍪', feed: '📰' }[p] || '❓';
  }

  function getConfClass(c) {
    if (c >= 0.7) return 'uc-conf-high';
    if (c >= 0.5) return 'uc-conf-med';
    return 'uc-conf-low';
  }

  function executeAPI() {
    const input = document.getElementById('uc-api-input');
    const output = document.getElementById('uc-api-output');
    const code = input.value.trim();

    if (!code) return;

    try {
      // Parse simple commands without eval
      // Supports: UC.pattern.method(args) or UniversalController.method(args)
      
      let result;
      
      // UC.pattern.method('arg') or UC.pattern.method()
      const ucMatch = code.match(/^UC\.(\w+)\.(\w+)\((.*)\)$/);
      if (ucMatch) {
        const [, pattern, method, argsStr] = ucMatch;
        const api = controller.getAPI(pattern);
        if (!api) {
          output.textContent = `Error: UC.${pattern} not bound`;
          return;
        }
        if (typeof api[method] !== 'function') {
          output.textContent = `Error: UC.${pattern}.${method} is not a function`;
          return;
        }
        const args = parseArgs(argsStr);
        result = api[method](...args);
        output.textContent = JSON.stringify(result, null, 2) || 'undefined';
        controller.log('success', `Executed: UC.${pattern}.${method}()`);
        return;
      }

      // UC.pattern.property
      const ucPropMatch = code.match(/^UC\.(\w+)\.(\w+)$/);
      if (ucPropMatch) {
        const [, pattern, prop] = ucPropMatch;
        const api = controller.getAPI(pattern);
        if (!api) {
          output.textContent = `Error: UC.${pattern} not bound`;
          return;
        }
        result = api[prop];
        if (result instanceof Element) {
          output.textContent = `[Element: ${result.tagName}#${result.id || ''}.${result.className?.toString?.().split(' ')[0] || ''}]`;
        } else {
          output.textContent = JSON.stringify(result, null, 2) || 'undefined';
        }
        return;
      }

      // UniversalController.method()
      const ctrlMatch = code.match(/^UniversalController\.(\w+)\((.*)\)$/);
      if (ctrlMatch) {
        const [, method, argsStr] = ctrlMatch;
        if (typeof controller[method] !== 'function') {
          output.textContent = `Error: UniversalController.${method} is not a function`;
          return;
        }
        const args = parseArgs(argsStr);
        result = controller[method](...args);
        output.textContent = JSON.stringify(result, null, 2) || 'undefined';
        controller.log('success', `Executed: UniversalController.${method}()`);
        return;
      }

      // UniversalController.property
      const ctrlPropMatch = code.match(/^UniversalController\.(\w+)$/);
      if (ctrlPropMatch) {
        const [, prop] = ctrlPropMatch;
        result = controller[prop];
        output.textContent = JSON.stringify(result, null, 2) || 'undefined';
        return;
      }

      output.textContent = `Error: Could not parse command. Try:\n  UC.chat.send('hello')\n  UC.chat.getMessages()\n  UC.chat.components\n  UniversalController.listBoundAPIs()`;

    } catch (e) {
      output.textContent = `Error: ${e.message}`;
      controller.log('error', e.message);
    }
  }

  function parseArgs(argsStr) {
    if (!argsStr || argsStr.trim() === '') return [];
    
    const args = [];
    let current = '';
    let inString = false;
    let stringChar = '';
    let braceDepth = 0;
    
    for (let i = 0; i < argsStr.length; i++) {
      const char = argsStr[i];
      
      if (!inString && (char === '"' || char === "'")) {
        inString = true;
        stringChar = char;
      } else if (inString && char === stringChar && argsStr[i-1] !== '\\') {
        inString = false;
      } else if (!inString && char === '{') {
        braceDepth++;
      } else if (!inString && char === '}') {
        braceDepth--;
      } else if (!inString && braceDepth === 0 && char === ',') {
        args.push(parseValue(current.trim()));
        current = '';
        continue;
      }
      
      current += char;
    }
    
    if (current.trim()) {
      args.push(parseValue(current.trim()));
    }
    
    return args;
  }

  function parseValue(str) {
    // String
    if ((str.startsWith("'") && str.endsWith("'")) || 
        (str.startsWith('"') && str.endsWith('"'))) {
      return str.slice(1, -1);
    }
    // Number
    if (!isNaN(str) && str !== '') {
      return Number(str);
    }
    // Boolean
    if (str === 'true') return true;
    if (str === 'false') return false;
    // Null/undefined
    if (str === 'null') return null;
    if (str === 'undefined') return undefined;
    // Object (simple JSON)
    if (str.startsWith('{') && str.endsWith('}')) {
      try {
        // Convert single quotes to double for JSON.parse
        const jsonStr = str.replace(/'/g, '"').replace(/(\w+):/g, '"$1":');
        return JSON.parse(jsonStr);
      } catch (e) {
        return str;
      }
    }
    return str;
  }

  function makeDraggable(el, handle) {
    let offsetX, offsetY, isDragging = false;

    handle.addEventListener('mousedown', (e) => {
      isDragging = true;
      offsetX = e.clientX - el.offsetLeft;
      offsetY = e.clientY - el.offsetTop;
    });

    document.addEventListener('mousemove', (e) => {
      if (!isDragging) return;
      el.style.left = (e.clientX - offsetX) + 'px';
      el.style.top = (e.clientY - offsetY) + 'px';
      el.style.right = 'auto';
      el.style.bottom = 'auto';
    });

    document.addEventListener('mouseup', () => isDragging = false);
  }

  // Global handlers
  unsafeWindow.UC_bind = (i) => {
    const r = detectionResults[i];
    if (r) {
      controller.bind(r.patternName, r.path);
      updateStats();
    }
  };

  unsafeWindow.UC_highlight = (i) => {
    const r = detectionResults[i];
    if (r?.el) {
      controller.highlight(r.el);
      r.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  };

  // Expose globally
  unsafeWindow.UniversalController = controller;
  unsafeWindow.UC = {};

  // Menu commands
  GM_registerMenuCommand('Toggle Panel', togglePanel);
  GM_registerMenuCommand('First Scan', () => controller.firstScan());
  GM_registerMenuCommand('Next Scan + Auto-Detect', () => {
    controller.nextScan();
    controller.autoDetect();
  });

  // Initialize
  createUI();
  controller.log('info', 'Universal Controller v2 loaded');
  controller.log('info', `Page: ${location.hostname}`);

})();
