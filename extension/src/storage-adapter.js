/**
 * Storage adapter — replaces GM_getValue/GM_setValue with chrome.storage.local.
 *
 * Since the content script runs in world: "MAIN" (page context), it cannot
 * directly access chrome.storage. We use a thin localStorage fallback that
 * works in MAIN world. For cross-session persistence beyond the page,
 * the background service worker can be extended to sync to chrome.storage.
 */

const STORAGE_KEY = 'uc_signatures';

/**
 * Drop-in replacement for GM_getValue.
 * @param {string} key
 * @param {string} defaultValue
 * @returns {string}
 */
export function GM_getValue(key, defaultValue) {
  try {
    const val = localStorage.getItem(key);
    return val !== null ? val : defaultValue;
  } catch (e) {
    return defaultValue;
  }
}

/**
 * Drop-in replacement for GM_setValue.
 * @param {string} key
 * @param {string} value
 */
export function GM_setValue(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    console.warn('[UC] Storage write failed:', e);
  }
}
