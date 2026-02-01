/**
 * FrameDiscovery - Discovers and classifies iframe elements.
 *
 * Finds all <iframe> elements on the page, classifies them as
 * same-origin or cross-origin, and watches for dynamically added iframes.
 */

export class FrameDiscovery {
  constructor() {
    this.frames = new Map(); // iframe element -> FrameInfo
    this._observer = null;
    this._callbacks = [];
  }

  /**
   * Register a callback for newly discovered frames.
   *
   * @param {function} cb - Called with { iframe, info } for each new frame.
   */
  onFrame(cb) {
    this._callbacks.push(cb);
  }

  /**
   * Scan for all current iframes and start watching for new ones.
   *
   * @returns {Array<{ iframe: HTMLIFrameElement, info: object }>}
   */
  start() {
    // Scan existing iframes
    const existing = this._scanAll();

    // Watch for new iframes
    this._observer = new MutationObserver(mutations => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType !== 1) continue;
          if (node.tagName === 'IFRAME') {
            this._processIframe(node);
          }
          // Also check descendants
          if (node.querySelectorAll) {
            node.querySelectorAll('iframe').forEach(f => this._processIframe(f));
          }
        }
      }
    });

    this._observer.observe(document.body, { childList: true, subtree: true });

    return existing;
  }

  /**
   * Stop watching for new iframes.
   */
  stop() {
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }
  }

  /**
   * Scan all iframes currently in the DOM.
   *
   * @returns {Array<{ iframe: HTMLIFrameElement, info: object }>}
   */
  _scanAll() {
    const results = [];
    document.querySelectorAll('iframe').forEach(iframe => {
      const info = this._processIframe(iframe);
      if (info) results.push({ iframe, info });
    });
    return results;
  }

  /**
   * Process a single iframe element: classify and register it.
   *
   * @param {HTMLIFrameElement} iframe
   * @returns {object|null} The frame info, or null if already known.
   */
  _processIframe(iframe) {
    if (this.frames.has(iframe)) return null;

    const info = this._classify(iframe);
    this.frames.set(iframe, info);

    // Notify callbacks
    this._callbacks.forEach(cb => cb({ iframe, info }));

    return info;
  }

  /**
   * Classify an iframe as same-origin, cross-origin, or inaccessible.
   *
   * @param {HTMLIFrameElement} iframe
   * @returns {object} Frame classification info.
   */
  _classify(iframe) {
    const src = iframe.src || '';
    const info = {
      src,
      origin: null,
      sameOrigin: false,
      accessible: false,
      hasContent: false,
      visible: this._isVisible(iframe),
      size: {
        width: iframe.offsetWidth,
        height: iframe.offsetHeight
      }
    };

    // Determine origin
    try {
      if (src) {
        const url = new URL(src, location.href);
        info.origin = url.origin;
        info.sameOrigin = url.origin === location.origin;
      } else {
        // No src = about:blank or srcdoc, same-origin by default
        info.origin = location.origin;
        info.sameOrigin = true;
      }
    } catch (e) {
      info.origin = 'unknown';
    }

    // Test actual accessibility
    try {
      const doc = iframe.contentDocument;
      if (doc) {
        info.accessible = true;
        info.hasContent = doc.body && doc.body.children.length > 0;
      }
    } catch (e) {
      // Cross-origin — contentDocument throws
      info.accessible = false;
    }

    return info;
  }

  /**
   * Check if an iframe is visible (non-zero dimensions, not hidden).
   *
   * @param {HTMLIFrameElement} iframe
   * @returns {boolean}
   */
  _isVisible(iframe) {
    if (iframe.offsetWidth === 0 && iframe.offsetHeight === 0) return false;
    try {
      const style = getComputedStyle(iframe);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (parseFloat(style.opacity) === 0) return false;
    } catch (e) {}
    return true;
  }

  /**
   * Get all discovered frames.
   *
   * @returns {Array<{ iframe: HTMLIFrameElement, info: object }>}
   */
  getAll() {
    return [...this.frames.entries()].map(([iframe, info]) => ({ iframe, info }));
  }

  /**
   * Get only same-origin accessible frames.
   *
   * @returns {Array<{ iframe: HTMLIFrameElement, info: object }>}
   */
  getAccessible() {
    return this.getAll().filter(f => f.info.accessible);
  }

  /**
   * Get only cross-origin frames.
   *
   * @returns {Array<{ iframe: HTMLIFrameElement, info: object }>}
   */
  getCrossOrigin() {
    return this.getAll().filter(f => !f.info.sameOrigin);
  }

  /**
   * Get only visible frames.
   *
   * @returns {Array<{ iframe: HTMLIFrameElement, info: object }>}
   */
  getVisible() {
    return this.getAll().filter(f => f.info.visible);
  }
}
