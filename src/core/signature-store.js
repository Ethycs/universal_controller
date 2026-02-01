/**
 * SignatureStore - Captures, stores, and retrieves UI pattern signatures
 * for cross-session persistence via GM_setValue/GM_getValue.
 *
 * A signature captures everything needed to re-identify and re-bind
 * a UI pattern on a future visit:
 *  - Structural: LSH fingerprint, element tag/attrs, container attributes
 *  - Behavioral: observed diff changes, send method results
 *  - Phrasal: placeholder patterns, aria-labels, button text
 *  - Site: hostname, pathname pattern
 *  - Framework: React/Vue/Angular/vanilla detection
 */

const STORAGE_KEY = 'uc_signatures';

/**
 * Detect the frontend framework in use on the page.
 *
 * @returns {string} One of 'react', 'vue', 'angular', 'svelte', 'vanilla'.
 */
function detectFramework() {
  try {
    // React
    if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__ ||
        document.querySelector('[data-reactroot]') ||
        document.querySelector('[data-reactid]')) {
      // Check for Next.js
      if (window.__NEXT_DATA__) return 'react-next';
      return 'react';
    }
    // Vue
    if (window.__VUE__ || window.__vue_app__ ||
        document.querySelector('[data-v-]') ||
        document.querySelector('[__vue_app__]')) {
      // Check for Nuxt
      if (window.__NUXT__) return 'vue-nuxt';
      return 'vue';
    }
    // Angular
    if (window.ng || document.querySelector('[ng-version]') ||
        document.querySelector('[_nghost-]')) {
      return 'angular';
    }
    // Svelte
    if (document.querySelector('[class*="svelte-"]')) {
      return 'svelte';
    }
  } catch (e) {}
  return 'vanilla';
}

/**
 * Extract text signals from an element and its nearby context.
 *
 * @param {HTMLElement} el
 * @returns {{ placeholders: string[], ariaLabels: string[], buttonTexts: string[] }}
 */
function extractTextSignals(el) {
  const placeholders = [];
  const ariaLabels = [];
  const buttonTexts = [];

  if (!el) return { placeholders, ariaLabels, buttonTexts };

  // Collect from element and its descendants
  const root = el.closest('[class*="chat"]') || el.parentElement || el;

  root.querySelectorAll('input, textarea').forEach(input => {
    if (input.placeholder) placeholders.push(input.placeholder);
    const al = input.getAttribute('aria-label');
    if (al) ariaLabels.push(al);
  });

  root.querySelectorAll('button, [role="button"]').forEach(btn => {
    const text = btn.textContent?.trim();
    if (text && text.length < 50) buttonTexts.push(text);
    const al = btn.getAttribute('aria-label');
    if (al) ariaLabels.push(al);
  });

  root.querySelectorAll('[contenteditable="true"]').forEach(ce => {
    const al = ce.getAttribute('aria-label');
    if (al) ariaLabels.push(al);
    const ph = ce.getAttribute('data-placeholder') || ce.getAttribute('placeholder');
    if (ph) placeholders.push(ph);
  });

  return { placeholders, ariaLabels, buttonTexts };
}

/**
 * Extract structural attributes from an element for fingerprinting.
 *
 * @param {HTMLElement} el
 * @returns {object}
 */
function extractStructuralAttrs(el) {
  if (!el) return {};
  return {
    tag: el.tagName,
    id: el.id || null,
    role: el.getAttribute('role') || null,
    ariaLive: el.getAttribute('aria-live') || null,
    dataTestId: el.getAttribute('data-testid') || null,
    className: el.className?.toString?.().slice(0, 100) || null,
    childCount: el.children.length
  };
}

export class SignatureStore {
  constructor() {
    this.signatures = this._load();
  }

  /**
   * Load signatures from GM_setValue storage.
   *
   * @returns {object} Map of hostname -> array of signatures.
   */
  _load() {
    try {
      const raw = GM_getValue(STORAGE_KEY, '{}');
      return JSON.parse(raw);
    } catch (e) {
      return {};
    }
  }

  /**
   * Persist signatures to GM_setValue storage.
   */
  _save() {
    try {
      GM_setValue(STORAGE_KEY, JSON.stringify(this.signatures));
    } catch (e) {
      console.warn('[UC] Failed to save signatures:', e);
    }
  }

  /**
   * Capture a signature for a confirmed-working binding.
   *
   * @param {object} params
   * @param {string} params.patternName - The pattern type (chat, form, etc.)
   * @param {object} params.components - The bound components ({ container, input, ... })
   * @param {object} params.lshSignature - The LSH signature ({ fingerprint, features, minhash })
   * @param {object} [params.sendMethodResult] - Result from setText ({ method, success })
   * @param {object} [params.diffEvidence] - Diff changes observed during detection
   * @param {string} [params.path] - The element path
   * @returns {object} The captured signature.
   */
  capture(params) {
    const {
      patternName,
      components,
      lshSignature,
      sendMethodResult,
      diffEvidence,
      path
    } = params;

    const hostname = location.hostname;
    const pathname = location.pathname;

    const signature = {
      id: `${hostname}_${patternName}_${Date.now()}`,
      patternName,
      site: {
        hostname,
        pathPattern: pathname.replace(/\/[a-f0-9-]{20,}/gi, '/*').replace(/\/\d+/g, '/*'),
        fullPath: pathname
      },
      structural: {
        fingerprint: lshSignature?.fingerprint || null,
        features: lshSignature?.features?.slice(0, 20) || [],
        minhashArray: lshSignature?.minhash ? Array.from(lshSignature.minhash) : null,
        container: extractStructuralAttrs(components?.container),
        input: extractStructuralAttrs(components?.input)
      },
      phrasal: extractTextSignals(components?.container || components?.input),
      behavioral: {
        sendMethod: sendMethodResult?.method || null,
        sendSuccess: sendMethodResult?.success || null,
        diffEvidence: diffEvidence || null
      },
      framework: detectFramework(),
      path: path || null,
      createdAt: Date.now(),
      lastUsed: Date.now(),
      useCount: 1,
      confirmed: true
    };

    // Store under hostname
    if (!this.signatures[hostname]) {
      this.signatures[hostname] = [];
    }

    // Remove old signature for same pattern on same site
    this.signatures[hostname] = this.signatures[hostname].filter(
      s => s.patternName !== patternName
    );

    this.signatures[hostname].push(signature);
    this._save();

    return signature;
  }

  /**
   * Get all signatures for the current hostname.
   *
   * @returns {Array<object>}
   */
  getForCurrentSite() {
    return this.signatures[location.hostname] || [];
  }

  /**
   * Get signatures for a specific hostname.
   *
   * @param {string} hostname
   * @returns {Array<object>}
   */
  getForSite(hostname) {
    return this.signatures[hostname] || [];
  }

  /**
   * Find a signature matching a pattern name for the current site.
   *
   * @param {string} patternName
   * @returns {object|null}
   */
  findForPattern(patternName) {
    const sigs = this.getForCurrentSite();
    return sigs.find(s => s.patternName === patternName) || null;
  }

  /**
   * Find signatures across all sites that match a given LSH fingerprint.
   *
   * @param {string} fingerprint
   * @returns {Array<object>}
   */
  findByFingerprint(fingerprint) {
    const results = [];
    for (const [, sigs] of Object.entries(this.signatures)) {
      for (const sig of sigs) {
        if (sig.structural?.fingerprint === fingerprint) {
          results.push(sig);
        }
      }
    }
    return results;
  }

  /**
   * Mark a signature as recently used (updates lastUsed and useCount).
   *
   * @param {string} signatureId
   */
  markUsed(signatureId) {
    for (const [, sigs] of Object.entries(this.signatures)) {
      const sig = sigs.find(s => s.id === signatureId);
      if (sig) {
        sig.lastUsed = Date.now();
        sig.useCount = (sig.useCount || 0) + 1;
        this._save();
        return;
      }
    }
  }

  /**
   * Delete a signature by id.
   *
   * @param {string} signatureId
   * @returns {boolean}
   */
  delete(signatureId) {
    for (const [hostname, sigs] of Object.entries(this.signatures)) {
      const idx = sigs.findIndex(s => s.id === signatureId);
      if (idx !== -1) {
        sigs.splice(idx, 1);
        if (sigs.length === 0) delete this.signatures[hostname];
        this._save();
        return true;
      }
    }
    return false;
  }

  /**
   * Delete all signatures for a given hostname.
   *
   * @param {string} hostname
   */
  deleteForSite(hostname) {
    delete this.signatures[hostname];
    this._save();
  }

  /**
   * Get all signatures across all sites.
   *
   * @returns {Array<{ hostname: string, signatures: Array<object> }>}
   */
  getAll() {
    return Object.entries(this.signatures).map(([hostname, sigs]) => ({
      hostname,
      signatures: sigs
    }));
  }

  /**
   * Get total count of stored signatures.
   *
   * @returns {number}
   */
  get count() {
    let total = 0;
    for (const sigs of Object.values(this.signatures)) {
      total += sigs.length;
    }
    return total;
  }

  /**
   * Clear all stored signatures.
   */
  clearAll() {
    this.signatures = {};
    this._save();
  }
}
