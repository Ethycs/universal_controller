/**
 * tabs.js - Tab switching and event handler wiring for Universal Controller.
 *
 * Provides setupEventHandlers() which attaches all click/keypress listeners
 * to the panel DOM elements, connecting them to the controller and renderers.
 */

import {
  renderDiff, renderResults, renderBoundAPIs, updateStats,
  renderSignatures, renderPassiveResults
} from './renderers.js';
import { executeAPI } from './api-console.js';

/**
 * Switch the active tab in the panel.
 *
 * @param {string} tabName - One of 'scan', 'detect', 'api', 'settings', 'log'.
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
 * @param {HTMLElement} panel - The #uc-panel element.
 * @param {object} controller - The UniversalController instance.
 * @returns {object} An object with state accessors and helpers.
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

  function refreshSignatures() {
    const sigList = panel.querySelector('#uc-sig-list');
    const sigCount = panel.querySelector('#sig-count');
    if (sigList) {
      renderSignatures(controller.signatures, sigList, sigCount);
    }
  }

  function refreshPassive() {
    const passiveList = panel.querySelector('#uc-passive-list');
    if (passiveList) {
      renderPassiveResults(controller.getPassiveResults(), passiveList);
    }
  }

  // --- Tabs ---
  panel.querySelectorAll('.uc-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      currentTab = switchTab(tab.dataset.tab, panel);
      // Refresh tab-specific content
      if (currentTab === 'settings') {
        refreshSignatures();
        refreshPassive();
      }
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

  // --- Capture Send Button ---
  panel.querySelector('#btn-capture-send').addEventListener('click', async () => {
    const apis = controller.listBoundAPIs();
    if (apis.length === 0) {
      controller.log('warn', 'Bind a pattern first, then capture its send button');
      return;
    }
    const pattern = apis[0].pattern;
    controller.log('info', 'Click the SEND button on the page (Esc to cancel)');

    // Minimize panel so user can see the page
    const panelEl = document.getElementById('uc-panel');
    panelEl.style.opacity = '0.3';
    panelEl.style.pointerEvents = 'none';

    // Scope the capture to the detected container's region
    const api = controller.getAPI(pattern);
    const scope = api?.components?.input || api?.el || null;

    const el = await controller.captureClick('Click the send/submit button...', {
      scope,
      patternName: pattern,
      componentKey: 'sendButton'
    });

    panelEl.style.opacity = '';
    panelEl.style.pointerEvents = '';

    if (el) {
      controller.log('success', `Send button captured for ${pattern}`);
      refreshStats();
    }
  });

  // --- Save Signature ---
  panel.querySelector('#btn-save-sig').addEventListener('click', () => {
    const apis = controller.listBoundAPIs();
    if (apis.length === 0) {
      controller.log('warn', 'No bound APIs to save signatures for');
      return;
    }
    let saved = 0;
    for (const { pattern } of apis) {
      const sig = controller.saveSignature(pattern);
      if (sig) saved++;
    }
    controller.log('success', `Saved ${saved} signature(s) for ${location.hostname}`);
    refreshSignatures();
  });

  // --- Copy for LLM ---
  panel.querySelector('#btn-copy-llm').addEventListener('click', () => {
    const apis = controller.listBoundAPIs();
    if (apis.length === 0) {
      controller.log('warn', 'No bound APIs. Bind a pattern first.');
      return;
    }
    // Generate context for the first bound pattern
    const context = controller.getLLMContext(apis[0].pattern);
    if (context) {
      navigator.clipboard.writeText(context).then(() => {
        controller.log('success', `LLM context for ${apis[0].pattern} copied to clipboard`);
      }).catch(() => {
        // Fallback: show in console output
        panel.querySelector('#uc-api-output').textContent = context;
        controller.log('info', 'Clipboard unavailable - context shown in console output');
      });
    }
  });

  // --- Verify ---
  panel.querySelector('#btn-verify').addEventListener('click', async () => {
    const apis = controller.listBoundAPIs();
    if (apis.length === 0) {
      controller.log('warn', 'No bound APIs to verify');
      return;
    }
    const pattern = apis[0].pattern;
    const verifier = controller.createVerifier(pattern);
    if (!verifier) return;

    const api = controller.getAPI(pattern);
    const output = panel.querySelector('#uc-api-output');
    output.textContent = `Verifying ${pattern}...\nPerform an action (e.g., send a message) to test.`;

    // For chat, auto-verify send
    if (pattern === 'chat' && api) {
      const result = await verifier.verify('send', () => api.send('UC verification test'));
      output.textContent = `Verification: ${result.passed ? 'PASSED' : 'FAILED'}\nGuarantee: ${result.guarantee}\n\n` +
        result.results.map(r => `${r.passed ? '\u2705' : '\u274C'} [${r.phase}] ${r.desc}`).join('\n');
    } else {
      output.textContent = `Verifier created for ${pattern}.\nUse console: UniversalController.createVerifier('${pattern}')`;
    }
  });

  // --- Heap Scan ---
  panel.querySelector('#btn-heap-scan').addEventListener('click', () => {
    const apis = controller.listBoundAPIs();
    const targetEl = apis.length > 0 ? controller.getAPI(apis[0].pattern)?.el : null;
    const result = controller.heapScan(targetEl);
    const output = panel.querySelector('#uc-api-output');

    let text = `Framework: ${result.framework.framework} ${result.framework.version || ''}\n`;
    text += `Globals: ${result.globals.length} interesting objects found\n`;
    if (result.globals.length > 0) {
      text += result.globals.map(g => `  ${g.name} (${g.type})`).join('\n') + '\n';
    }
    if (result.elementState) {
      text += `\nElement state (${result.elementState.framework}):\n`;
      text += `  Component: ${result.elementState.type || 'N/A'}\n`;
      if (result.elementState.hooks?.length > 0) {
        text += `  Hooks: ${result.elementState.hooks.length} found\n`;
      }
    }
    output.textContent = text;
    currentTab = switchTab('api', panel);
  });

  // --- Unbind / Refresh ---
  panel.querySelector('#btn-unbind-all').addEventListener('click', () => {
    controller.unbindAll();
    renderBoundAPIs(controller.listBoundAPIs(), panel.querySelector('#bound-apis-list'), controller);
  });

  panel.querySelector('#btn-refresh-bindings').addEventListener('click', () => {
    renderBoundAPIs(controller.listBoundAPIs(), panel.querySelector('#bound-apis-list'), controller);
  });

  // --- Settings: Auto-Bind toggle ---
  const autoBindToggle = panel.querySelector('#uc-toggle-autobind');
  if (autoBindToggle) {
    autoBindToggle.checked = controller.autoBindEnabled;
    autoBindToggle.addEventListener('change', () => {
      controller.setAutoBind(autoBindToggle.checked);
    });
  }

  // --- Settings: Passive Mode toggle ---
  const passiveToggle = panel.querySelector('#uc-toggle-passive');
  if (passiveToggle) {
    passiveToggle.checked = controller.passive.enabled;
    passiveToggle.addEventListener('change', () => {
      controller.togglePassive();
      const passiveList = panel.querySelector('#uc-passive-list');
      if (passiveList) {
        passiveList.dataset.active = String(controller.passive.enabled);
        refreshPassive();
      }
    });

    // Refresh passive results when new patterns are inferred
    controller.passive.onPattern(() => {
      if (currentTab === 'settings') refreshPassive();
    });
  }

  // --- Settings: Signature management ---
  const clearSigsBtn = panel.querySelector('#btn-clear-sigs');
  if (clearSigsBtn) {
    clearSigsBtn.addEventListener('click', () => {
      controller.signatures.clearAll();
      refreshSignatures();
      controller.log('info', 'All signatures cleared');
    });
  }

  const refreshSigsBtn = panel.querySelector('#btn-refresh-sigs');
  if (refreshSigsBtn) {
    refreshSigsBtn.addEventListener('click', refreshSignatures);
  }

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
    refreshStats,
    refreshSignatures,
    refreshPassive
  };
}
