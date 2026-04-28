/**
 * Minimal MV3 service worker.
 *
 * Currently a placeholder — the storage adapter uses localStorage directly
 * from the MAIN world. If we need chrome.storage.local (cross-origin persistence),
 * this worker would relay get/set messages from the content script.
 */

chrome.runtime.onInstalled.addListener(() => {
  console.log('[UC] Universal Controller extension installed.');
});
