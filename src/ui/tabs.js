/**
 * tabs.js - Tab switching and event handler wiring for Universal Controller.
 *
 * Provides setupEventHandlers() which attaches all click/keypress listeners
 * to the panel DOM elements, connecting them to the controller and renderers.
 */

import { renderDiff, renderResults, renderBoundAPIs, updateStats } from './renderers.js';
import { executeAPI } from './api-console.js';

/**
 * Switch the active tab in the panel.
 *
 * @param {string} tabName - One of 'scan', 'detect', 'api', 'log'.
 * @param {HTMLElement} panel - The #uc-panel element.
 * @returns {string} The new active tab name.
 */
export function switchTab(tabName, panel) {
  panel.querySelectorAll('.uc-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === tabName)
  );
  panel.querySelectorAll('.uc-tab-content').forEach(c =>
    c.classList.toggle('active', c.dataset.tab === tabName)
  );
  return tabName;
}

/**
 * Attach all event handlers for the panel.
 *
 * This wires up:
 *  - Tab switching
 *  - Pattern selector
 *  - Scan buttons (first scan, next scan, auto-detect)
 *  - Detect buttons (detect, detect all, signatures)
 *  - API quick-action buttons (chat send, form fill, dropdown toggle, modal close)
 *  - Unbind/refresh bindings buttons
 *  - API console input
 *  - Log callback
 *
 * @param {HTMLElement} panel - The #uc-panel element.
 * @param {object} controller - The UniversalController instance.
 * @returns {object} An object with state accessors and helpers:
 *   { getSelectedPattern, getDetectionResults, getCurrentTab, refreshStats }
 */
export function setupEventHandlers(panel, controller) {
  let selectedPattern = 'chat';
  let detectionResults = [];
  let currentTab = 'scan';

  // --- Helper: collect stat elements ---
  const statElements = {
    snapshots: panel.querySelector('#stat-snapshots'),
    elementsEl: panel.querySelector('#stat-elements'),
    changed: panel.querySelector('#stat-changed'),
    detected: panel.querySelector('#stat-detected')
  };

  function refreshStats() {
    const stats = controller.stats;
    const changedCount = controller.lastDiff ? controller.lastDiff.summary.changed : null;
    updateStats(stats, statElements, changedCount);

    // Also refresh bound APIs list
    const apisContainer = panel.querySelector('#bound-apis-list');
    if (apisContainer) {
      renderBoundAPIs(controller.listBoundAPIs(), apisContainer, controller);
    }
  }

  // --- Tabs ---
  panel.querySelectorAll('.uc-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      currentTab = switchTab(tab.dataset.tab, panel);
    });
  });

  // --- Pattern selection ---
  panel.querySelectorAll('.uc-pattern').forEach(el => {
    el.addEventListener('click', () => {
      panel.querySelectorAll('.uc-pattern').forEach(p => p.classList.remove('active'));
      el.classList.add('active');
      selectedPattern = el.dataset.pattern;
    });
  });

  // --- Scan buttons ---
  panel.querySelector('#btn-first-scan').addEventListener('click', () => {
    controller.firstScan();
    refreshStats();
    renderDiff(null, panel.querySelector('#diff-list'));
  });

  panel.querySelector('#btn-next-scan').addEventListener('click', () => {
    const diff = controller.nextScan();
    refreshStats();
    renderDiff(diff, panel.querySelector('#diff-list'));
  });

  panel.querySelector('#btn-auto-detect').addEventListener('click', () => {
    const detected = controller.autoDetect();
    refreshStats();
    if (detected.length > 0) {
      detectionResults = detected.map(d => ({
        path: d.components.container ? controller.getPath(d.components.container) : '',
        el: d.components.container,
        patternName: d.pattern,
        confidence: d.confidence,
        evidence: { behavioral: 1, proof: d.proof },
        components: d.components
      }));
      currentTab = switchTab('detect', panel);
      renderResults(detectionResults, panel.querySelector('#uc-results'), controller, refreshStats);
    }
  });

  // --- Detect buttons ---
  panel.querySelector('#btn-detect').addEventListener('click', () => {
    detectionResults = controller.detect(selectedPattern, 'BEHAVIORAL');
    renderResults(detectionResults, panel.querySelector('#uc-results'), controller, refreshStats);
    refreshStats();
  });

  panel.querySelector('#btn-detect-all').addEventListener('click', () => {
    const patterns = ['chat', 'form', 'login', 'dropdown', 'modal', 'search', 'cookie', 'feed'];
    detectionResults = patterns.flatMap(p => controller.detect(p, 'BEHAVIORAL'));
    renderResults(detectionResults, panel.querySelector('#uc-results'), controller, refreshStats);
    refreshStats();
  });

  panel.querySelector('#btn-signatures').addEventListener('click', () => {
    const sigs = controller.getAllSignatures();
    panel.querySelector('#uc-api-output').textContent = sigs.map(s =>
      `${s.fingerprint} ${s.tag} ${s.features.slice(0, 3).join(', ')}`
    ).join('\n');
    currentTab = switchTab('api', panel);
  });

  // --- API quick-action buttons ---
  panel.querySelector('#btn-chat-send').addEventListener('click', () => {
    const api = controller.getAPI('chat');
    if (api) {
      const text = prompt('Message:', 'Hello from UC!');
      if (text) api.send(text);
    } else {
      controller.log('warn', 'Bind chat API first');
    }
  });

  panel.querySelector('#btn-form-fill').addEventListener('click', () => {
    const api = controller.getAPI('form') || controller.getAPI('login');
    if (api) {
      api.fill({ email: 'test@example.com', name: 'Test User', password: 'test123' });
    } else {
      controller.log('warn', 'Bind form API first');
    }
  });

  panel.querySelector('#btn-dropdown-toggle').addEventListener('click', () => {
    const api = controller.getAPI('dropdown');
    if (api) api.toggle();
    else controller.log('warn', 'Bind dropdown API first');
  });

  panel.querySelector('#btn-modal-close').addEventListener('click', () => {
    const api = controller.getAPI('modal');
    if (api) api.close();
    else controller.log('warn', 'Bind modal API first');
  });

  // --- Unbind / Refresh ---
  panel.querySelector('#btn-unbind-all').addEventListener('click', () => {
    controller.unbindAll();
    renderBoundAPIs(controller.listBoundAPIs(), panel.querySelector('#bound-apis-list'), controller);
  });

  panel.querySelector('#btn-refresh-bindings').addEventListener('click', () => {
    renderBoundAPIs(controller.listBoundAPIs(), panel.querySelector('#bound-apis-list'), controller);
  });

  // --- API console ---
  panel.querySelector('#uc-api-input').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
      const input = panel.querySelector('#uc-api-input');
      const output = panel.querySelector('#uc-api-output');
      const result = executeAPI(input.value, controller);
      if (result && typeof result.then === 'function') {
        output.textContent = 'Awaiting...';
        result.then(r => { output.textContent = r.output; });
      } else if (result) {
        output.textContent = result.output;
      }
    }
  });

  // --- Log handler ---
  controller.onLog((type, msg) => {
    const log = panel.querySelector('#uc-log');
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

  // Return state accessors so index.js can read/modify shared state
  return {
    getSelectedPattern() { return selectedPattern; },
    getDetectionResults() { return detectionResults; },
    getCurrentTab() { return currentTab; },
    refreshStats
  };
}
