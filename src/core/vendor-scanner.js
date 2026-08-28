// ============================================
// VENDOR SCANNER - identify the chat ENGINE by its loader fingerprint
// ============================================

/**
 * Identifies which chat-widget engine (Intercom, Zendesk, Crisp, ...) is
 * present on the page. A vendor signature is NOT site-specific — one
 * entry covers every site running that engine — so this is engine-level
 * knowledge, and it lives here in the scanner tier alongside the passive
 * detector.
 *
 * Three complementary in-page signals, ordered by recall:
 *   1. Resource timing  — performance.getEntriesByType('resource') lists
 *      EVERY network resource the page fetched, including lazily-injected
 *      widget loaders that a one-shot <script> scan misses. This is the
 *      in-page equivalent of network sniffing, and it is why the scanner
 *      beats a static DOM read for real customer sites (widgets lazy-load).
 *   2. Window globals   — window.Intercom, window.$crisp, ... appear once
 *      the loader runs, even if the request already flushed from the DOM.
 *   3. Script srcs + inline HTML — the static fallback.
 *
 * FINGERPRINTS are the chat-RUNTIME CDN host, never a shared analytics tag
 * (e.g. HubSpot chat = js.usemessages.com, NOT js.hs-scripts.com which is
 * tracking present on countless non-chat sites — that distinction kills
 * the false positives seen when fingerprinting the analytics CDN).
 */

export const VENDOR_SIGNATURES = [
  { name: 'intercom', fps: ['widget.intercom.io', 'js.intercomcdn.com', 'intercomcdn'], globals: ['Intercom'] },
  { name: 'zendesk', fps: ['static.zdassets.com', 'ekr/snippet.js', 'zopim'], globals: ['zE', '$zopim'] },
  { name: 'crisp', fps: ['client.crisp.chat'], globals: ['$crisp'] },
  { name: 'ada', fps: ['static.ada.support'], globals: ['adaEmbed'] },
  { name: 'gorgias', fps: ['config.gorgias.chat', 'gorgias.chat/'], globals: ['GorgiasChat'] },
  { name: 'kustomer', fps: ['cdn.kustomerapp.com'], globals: ['Kustomer'] },
  { name: 'freshchat', fps: ['wchat.freshchat.com'], globals: ['fcWidget'] },
  { name: 'liveperson', fps: ['lptag.liveperson.net', 'lpsnmedia.net'], globals: ['lpTag'] },
  { name: 'landbot', fps: ['cdn.landbot.io', 'landbot.online'], globals: ['Landbot'] },
  { name: 'botpress', fps: ['cdn.botpress.cloud', 'botpress.com/webchat'], globals: ['botpress', 'botpressWebChat'] },
  { name: 'olark', fps: ['static.olark.com'], globals: ['olark'] },
  { name: 'drift', fps: ['js.driftt.com', 'js.driff.com', 'drift.com/include'], globals: ['drift', 'driftt'] },
  { name: 'tawk', fps: ['embed.tawk.to'], globals: ['Tawk_API'] },
  { name: 'tidio', fps: ['code.tidio.co'], globals: ['tidioChatApi'] },
  { name: 'zoho-salesiq', fps: ['salesiq.zoho', 'salesiq.zohopublic'], globals: ['$zoho'] },
  { name: 'livechat', fps: ['cdn.livechatinc.com', 'cdn.livechat-static.com'], globals: ['LiveChatWidget', 'LC_API'] },
  // HubSpot chat runtime only — NOT js.hs-scripts.com (tracking).
  { name: 'hubspot', fps: ['js.usemessages.com', 'js.hs-banner.com/conversations'], globals: ['HubSpotConversations'] },
];

export class VendorScanner {
  constructor() {
    // Vendors ever seen this page-load (resource entries can be evicted
    // from the buffer, so we accumulate rather than re-query only).
    this._seen = new Set();
    this._perfObserver = null;
  }

  /**
   * Begin continuously observing resource loads. Optional — scan() works
   * without it via the resource buffer — but this guarantees we catch
   * loads even if the buffer overflows and evicts entries.
   */
  start() {
    if (this._perfObserver || typeof PerformanceObserver === 'undefined') return;
    try {
      this._perfObserver = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) this._matchUrl(entry.name);
      });
      this._perfObserver.observe({ type: 'resource', buffered: true });
    } catch (e) { /* resource timing unsupported */ }
  }

  stop() {
    if (this._perfObserver) { this._perfObserver.disconnect(); this._perfObserver = null; }
  }

  _matchUrl(url) {
    const u = (url || '').toLowerCase();
    for (const sig of VENDOR_SIGNATURES) {
      if (sig.fps.some(f => u.includes(f))) this._seen.add(sig.name);
    }
  }

  /**
   * Return the list of vendor engine names present on this page, using
   * all three signals. Deduplicated; accumulates across calls.
   *
   * @returns {string[]}
   */
  scan() {
    // 1. Resource timing (network layer — catches lazy loads).
    try {
      if (performance && performance.getEntriesByType) {
        for (const e of performance.getEntriesByType('resource')) this._matchUrl(e.name);
      }
    } catch (e) { /* ignore */ }

    // 2. Window globals.
    for (const sig of VENDOR_SIGNATURES) {
      for (const g of sig.globals) {
        try { if (typeof window[g] !== 'undefined') { this._seen.add(sig.name); break; } } catch (e) { /* cross-origin */ }
      }
    }

    // 3. Static script srcs + a bounded inline-HTML sweep.
    try {
      const srcs = [...document.querySelectorAll('script[src]')].map(s => (s.src || '').toLowerCase());
      for (const s of srcs) this._matchUrl(s);
    } catch (e) { /* ignore */ }

    return [...this._seen];
  }
}
