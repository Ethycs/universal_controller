/**
 * styles.js - All CSS styles for the Universal Controller UI.
 *
 * Extracted from the GM_addStyle() call in the original monolith.
 * The caller is responsible for injecting these via GM_addStyle(STYLES).
 */

export const STYLES = `
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
`;
