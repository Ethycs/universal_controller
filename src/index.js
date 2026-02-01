/**
 * index.js - Entry point for Universal Controller v2.
 *
 * This module is the Rollup entry point. It:
 *  1. Injects CSS styles via GM_addStyle
 *  2. Creates the FAB button and panel DOM elements
 *  3. Instantiates the UniversalController
 *  4. Wires up all event handlers
 *  5. Makes the panel draggable
 *  6. Exposes globals (unsafeWindow.UniversalController, unsafeWindow.UC)
 *  7. Registers Tampermonkey menu commands
 *
 * GM_addStyle, GM_registerMenuCommand, GM_getValue, GM_setValue,
 * and unsafeWindow are Tampermonkey runtime globals -- Rollup will
 * leave them as-is since they are not imported.
 */

import { STYLES } from './styles.js';
import { UniversalController } from './detection/universal-controller.js';
import { createPanelHTML } from './ui/panel.js';
import { setupEventHandlers, switchTab } from './ui/tabs.js';

// ============================================
// INJECT STYLES
// ============================================

GM_addStyle(STYLES);

// ============================================
// PANEL DRAG HELPER
// ============================================

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

// ============================================
// STATE
// ============================================

const controller = new UniversalController();
let panelVisible = false;
let uiState = null; // set after event handlers are wired

// ============================================
// TOGGLE PANEL
// ============================================

function togglePanel() {
  panelVisible = !panelVisible;
  document.getElementById('uc-panel').classList.toggle('visible', panelVisible);
  document.getElementById('uc-fab').classList.toggle('hidden', panelVisible);
  if (panelVisible && uiState) uiState.refreshStats();
}

// ============================================
// CREATE UI
// ============================================

function createUI() {
  // --- FAB button ---
  const fab = document.createElement('button');
  fab.id = 'uc-fab';
  fab.innerHTML = '\u26A1';
  fab.title = 'Universal Controller v2';
  fab.addEventListener('click', togglePanel);
  document.body.appendChild(fab);

  // --- Panel ---
  const panel = document.createElement('div');
  panel.id = 'uc-panel';
  panel.innerHTML = createPanelHTML();
  document.body.appendChild(panel);

  // --- Close button ---
  panel.querySelector('#uc-close').addEventListener('click', togglePanel);

  // --- Wire up all event handlers ---
  uiState = setupEventHandlers(panel, controller);

  // --- Make panel draggable via header ---
  makeDraggable(panel, panel.querySelector('#uc-header'));
}

// ============================================
// GLOBAL HANDLERS (for legacy console access)
// ============================================

unsafeWindow.UC_bind = (i) => {
  const results = uiState?.getDetectionResults();
  const r = results?.[i];
  if (r) {
    controller.bind(r.patternName, r.path);
    uiState.refreshStats();
  }
};

unsafeWindow.UC_highlight = (i) => {
  const results = uiState?.getDetectionResults();
  const r = results?.[i];
  if (r?.el) {
    controller.highlight(r.el);
    r.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
};

// ============================================
// EXPOSE GLOBALS
// ============================================

unsafeWindow.UniversalController = controller;
unsafeWindow.UC = {};

// ============================================
// TAMPERMONKEY MENU COMMANDS
// ============================================

GM_registerMenuCommand('Toggle Panel', togglePanel);
GM_registerMenuCommand('First Scan', () => controller.firstScan());
GM_registerMenuCommand('Next Scan + Auto-Detect', () => {
  controller.nextScan();
  controller.autoDetect();
});

// ============================================
// INITIALIZE
// ============================================

createUI();
controller.log('info', 'Universal Controller v2 loaded');
controller.log('info', `Page: ${location.hostname}`);
