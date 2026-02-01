/**
 * renderers.js - Rendering functions for the Universal Controller UI.
 *
 * Each function takes data and a DOM container (or element references)
 * and updates the innerHTML to reflect the current state.
 */

// ---- Helpers ----

export function getIcon(p) {
  return {
    chat: '\u{1F4AC}',
    form: '\u{1F4DD}',
    login: '\u{1F510}',
    dropdown: '\u{1F4CB}',
    modal: '\u{1FA9F}',
    search: '\u{1F50D}',
    cookie: '\u{1F36A}',
    feed: '\u{1F4F0}'
  }[p] || '\u{2753}';
}

export function getConfClass(c) {
  if (c >= 0.7) return 'uc-conf-high';
  if (c >= 0.5) return 'uc-conf-med';
  return 'uc-conf-low';
}

// ---- Render: Diff ----

/**
 * Render a scan diff into the diff-list container.
 * @param {object|null} diff - The diff result from ValueScanner.nextScan().
 * @param {HTMLElement} container - The #diff-list element.
 */
export function renderDiff(diff, container) {
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
          <span>\u2192</span>
          <span class="uc-diff-after">${String(ch.after).slice(0, 20)}</span>
        </div>
      `).join('')}
    </div>
  `).join('');
}

// ---- Render: Detection Results ----

/**
 * Render detection results into the results container.
 * Attaches bind/highlight event listeners to each result row.
 *
 * @param {Array} results - Array of detection result objects.
 * @param {HTMLElement} container - The #uc-results element.
 * @param {object} controller - The UniversalController instance (for bind/highlight).
 * @param {Function} updateStatsFn - Callback to refresh stats after binding.
 */
export function renderResults(results, container, controller, updateStatsFn) {
  if (results.length === 0) {
    container.innerHTML = `
      <div style="color: #606078; text-align: center; padding: 10px; font-size: 11px;">
        No patterns detected
      </div>
    `;
    return;
  }

  container.innerHTML = results.map((r, i) => `
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

  // Attach event listeners for bind buttons
  container.querySelectorAll('.uc-bind-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.index);
      const r = results[idx];
      if (r) {
        controller.bind(r.patternName, r.path);
        updateStatsFn();
        controller.log('success', `Bound ${r.patternName} API`);
      }
    });
  });

  // Attach event listeners for highlight buttons
  container.querySelectorAll('.uc-highlight-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.index);
      const r = results[idx];
      if (r?.el) {
        controller.highlight(r.el);
        r.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    });
  });
}

// ---- Render: Bound APIs ----

/**
 * Render the list of currently bound APIs.
 *
 * @param {Array} apis - Array from controller.listBoundAPIs().
 * @param {HTMLElement} container - The #bound-apis-list element.
 * @param {object} controller - The UniversalController instance.
 */
export function renderBoundAPIs(apis, container, controller) {
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
      renderBoundAPIs(controller.listBoundAPIs(), container, controller);
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

// ---- Update Stats ----

/**
 * Update the stats display in the Scan tab header.
 *
 * @param {object} stats - The stats object from controller.stats.
 * @param {object} elements - Object with references to stat value DOM elements.
 * @param {HTMLElement} elements.snapshots - #stat-snapshots element.
 * @param {HTMLElement} elements.elementsEl - #stat-elements element.
 * @param {HTMLElement} elements.changed - #stat-changed element.
 * @param {HTMLElement} elements.detected - #stat-detected element.
 * @param {number|null} changedCount - The changed count from lastDiff.summary.changed (or null).
 */
export function updateStats(stats, elements, changedCount) {
  elements.snapshots.textContent = stats.snapshots;
  elements.elementsEl.textContent = stats.elements;
  elements.detected.textContent = stats.detected;

  if (changedCount !== null && changedCount !== undefined) {
    elements.changed.textContent = changedCount;
  }
}
