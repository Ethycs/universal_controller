/**
 * panel.js - Generates the main panel HTML for Universal Controller.
 *
 * Returns the full inner-HTML string that is injected into the #uc-panel div.
 * Includes all four tabs: Scan, Detect, API, and Log.
 */

export function createPanelHTML() {
  return `
      <div id="uc-header">
        <div id="uc-title">Universal Controller v2</div>
        <button id="uc-close">&times;</button>
      </div>
      <div id="uc-tabs">
        <button class="uc-tab active" data-tab="scan">Scan</button>
        <button class="uc-tab" data-tab="detect">Detect</button>
        <button class="uc-tab" data-tab="api">API</button>
        <button class="uc-tab" data-tab="settings">\u{2699}\u{FE0F}</button>
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
              <button class="uc-btn primary" id="btn-first-scan">\u{1F4F8} First Scan (Baseline)</button>
              <button class="uc-btn warning" id="btn-next-scan">\u{1F504} Next Scan (Diff)</button>
              <button class="uc-btn success" id="btn-auto-detect">\u{1F3AF} Auto-Detect Pattern</button>
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
                <div class="uc-pattern-icon">\u{1F4AC}</div>
                <div class="uc-pattern-name">Chat</div>
              </div>
              <div class="uc-pattern" data-pattern="form">
                <div class="uc-pattern-icon">\u{1F4DD}</div>
                <div class="uc-pattern-name">Form</div>
              </div>
              <div class="uc-pattern" data-pattern="login">
                <div class="uc-pattern-icon">\u{1F510}</div>
                <div class="uc-pattern-name">Login</div>
              </div>
              <div class="uc-pattern" data-pattern="dropdown">
                <div class="uc-pattern-icon">\u{1F4CB}</div>
                <div class="uc-pattern-name">Drop</div>
              </div>
              <div class="uc-pattern" data-pattern="modal">
                <div class="uc-pattern-icon">\u{1FA9F}</div>
                <div class="uc-pattern-name">Modal</div>
              </div>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Three-Signal Detection</div>
            <div class="uc-btn-grid">
              <button class="uc-btn primary" id="btn-detect">\u{1F50D} Detect Selected Pattern</button>
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
              <button class="uc-btn" id="btn-chat-send">\u{1F4AC} Send Chat</button>
              <button class="uc-btn" id="btn-form-fill">\u{1F4DD} Fill Form</button>
              <button class="uc-btn" id="btn-dropdown-toggle">\u{1F4CB} Toggle Drop</button>
              <button class="uc-btn" id="btn-modal-close">\u{1FA9F} Close Modal</button>
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
              <button class="uc-btn warning" id="btn-capture-send">\u{1F3AF} Capture Send Btn</button>
              <button class="uc-btn success" id="btn-save-sig">\u{1F4BE} Save Sig</button>
              <button class="uc-btn primary" id="btn-copy-llm">\u{1F4CB} Copy for LLM</button>
              <button class="uc-btn" id="btn-verify">\u{2705} Verify</button>
              <button class="uc-btn" id="btn-heap-scan">\u{1F9E0} Heap Scan</button>
              <button class="uc-btn" id="btn-unbind-all">\u{1F5D1}\u{FE0F} Unbind All</button>
              <button class="uc-btn" id="btn-refresh-bindings">\u{1F504} Refresh</button>
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

        <!-- SETTINGS TAB -->
        <div class="uc-tab-content" data-tab="settings">
          <div class="uc-section">
            <div class="uc-section-title">Features</div>
            <div class="uc-toggle-row">
              <label class="uc-toggle-label">
                <input type="checkbox" id="uc-toggle-autobind" checked>
                <span>Auto-Bind on page load</span>
              </label>
            </div>
            <div class="uc-toggle-row">
              <label class="uc-toggle-label">
                <input type="checkbox" id="uc-toggle-passive">
                <span>Passive Observation</span>
              </label>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Saved Signatures <span class="uc-badge" id="sig-count">0</span></div>
            <div id="uc-sig-list" class="uc-results" style="max-height: 200px;">
              <div style="color: #606078; text-align: center; padding: 10px; font-size: 11px;">
                No saved signatures
              </div>
            </div>
            <div class="uc-btn-grid" style="margin-top: 8px;">
              <button class="uc-btn" id="btn-clear-sigs">\u{1F5D1}\u{FE0F} Clear All Sigs</button>
              <button class="uc-btn" id="btn-refresh-sigs">\u{1F504} Refresh</button>
            </div>
          </div>

          <div class="uc-section">
            <div class="uc-section-title">Passive Detections</div>
            <div id="uc-passive-list" class="uc-results" style="max-height: 150px;">
              <div style="color: #606078; text-align: center; padding: 10px; font-size: 11px;">
                Enable passive mode to detect patterns automatically
              </div>
            </div>
          </div>
        </div>

        <!-- LOG TAB -->
        <div class="uc-tab-content" data-tab="log">
          <div id="uc-log"></div>
        </div>
      </div>
  `;
}
