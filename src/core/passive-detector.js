/**
 * PassiveDetector - Observes user interactions and DOM mutations
 * to infer UI patterns without requiring explicit scan cycles.
 *
 * Correlates:
 *   - click → element appears = dropdown/modal
 *   - input → children change = chat
 *   - scroll → children added = infinite scroll/feed
 *   - input cleared + children added = chat message sent
 *   - multiple inputs cleared = form submitted
 */

export class PassiveDetector {
  constructor(options = {}) {
    this.enabled = false;
    this.logCallbacks = [];
    this.patternCallbacks = [];

    this.correlationWindow = options.correlationWindow || 1000; // ms
    this.minConfidence = options.minConfidence || 0.6;

    // Event queues for correlation
    this._actionQueue = [];   // user actions (clicks, inputs, scrolls)
    this._mutationQueue = []; // DOM mutations

    // Bound handlers (for cleanup)
    this._onClick = this._handleClick.bind(this);
    this._onInput = this._handleInput.bind(this);
    this._onScroll = this._handleScroll.bind(this);
    this._onKeydown = this._handleKeydown.bind(this);

    this._observer = null;
    this._correlationTimer = null;

    // Detected patterns (deduplicated)
    this.inferred = new Map(); // key -> { pattern, el, confidence, evidence }
  }

  onLog(cb) { this.logCallbacks.push(cb); }
  onPattern(cb) { this.patternCallbacks.push(cb); }

  log(type, msg) {
    this.logCallbacks.forEach(cb => cb(type, msg));
  }

  /**
   * Start passive observation.
   */
  start() {
    if (this.enabled) return;
    this.enabled = true;

    // Passive event listeners (capture phase, non-blocking)
    document.addEventListener('click', this._onClick, { capture: true, passive: true });
    document.addEventListener('input', this._onInput, { capture: true, passive: true });
    document.addEventListener('scroll', this._onScroll, { capture: true, passive: true });
    document.addEventListener('keydown', this._onKeydown, { capture: true, passive: true });

    // MutationObserver on body
    this._observer = new MutationObserver(this._handleMutations.bind(this));
    this._observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['aria-expanded', 'aria-hidden', 'style', 'class', 'hidden']
    });

    // Periodic correlation
    this._correlationTimer = setInterval(() => this._correlate(), 500);

    this.log('info', 'Passive observation started');
  }

  /**
   * Stop passive observation.
   */
  stop() {
    if (!this.enabled) return;
    this.enabled = false;

    document.removeEventListener('click', this._onClick, { capture: true });
    document.removeEventListener('input', this._onInput, { capture: true });
    document.removeEventListener('scroll', this._onScroll, { capture: true });
    document.removeEventListener('keydown', this._onKeydown, { capture: true });

    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }

    if (this._correlationTimer) {
      clearInterval(this._correlationTimer);
      this._correlationTimer = null;
    }

    this.log('info', 'Passive observation stopped');
  }

  // ============================================
  // EVENT HANDLERS
  // ============================================

  _handleClick(e) {
    this._actionQueue.push({
      type: 'click',
      target: e.target,
      timestamp: Date.now(),
      meta: {
        tag: e.target.tagName,
        hasAriaExpanded: e.target.hasAttribute('aria-expanded'),
        ariaHaspopup: e.target.hasAttribute('aria-haspopup'),
        isButton: e.target.tagName === 'BUTTON' || e.target.getAttribute('role') === 'button'
      }
    });
    this._trimQueue(this._actionQueue);
  }

  _handleInput(e) {
    this._actionQueue.push({
      type: 'input',
      target: e.target,
      timestamp: Date.now(),
      meta: {
        tag: e.target.tagName,
        value: e.target.value?.slice(0, 50),
        isEmpty: !e.target.value
      }
    });
    this._trimQueue(this._actionQueue);
  }

  _handleScroll(e) {
    const el = e.target === document ? document.scrollingElement : e.target;
    if (!el || el === document) return;

    this._actionQueue.push({
      type: 'scroll',
      target: el,
      timestamp: Date.now(),
      meta: {
        scrollTop: el.scrollTop,
        scrollHeight: el.scrollHeight,
        atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 50
      }
    });
    this._trimQueue(this._actionQueue);
  }

  _handleKeydown(e) {
    if (e.key !== 'Enter') return;

    this._actionQueue.push({
      type: 'enter',
      target: e.target,
      timestamp: Date.now(),
      meta: {
        tag: e.target.tagName,
        isInput: ['INPUT', 'TEXTAREA'].includes(e.target.tagName),
        value: e.target.value?.slice(0, 50)
      }
    });
    this._trimQueue(this._actionQueue);
  }

  _handleMutations(mutations) {
    const now = Date.now();

    for (const m of mutations) {
      if (m.type === 'childList' && m.addedNodes.length > 0) {
        this._mutationQueue.push({
          type: 'children-added',
          target: m.target,
          timestamp: now,
          count: m.addedNodes.length,
          nodes: [...m.addedNodes].filter(n => n.nodeType === 1)
        });
      }

      if (m.type === 'childList' && m.removedNodes.length > 0) {
        this._mutationQueue.push({
          type: 'children-removed',
          target: m.target,
          timestamp: now,
          count: m.removedNodes.length
        });
      }

      if (m.type === 'attributes') {
        const attr = m.attributeName;
        const val = m.target.getAttribute(attr);
        this._mutationQueue.push({
          type: 'attr-changed',
          target: m.target,
          timestamp: now,
          attr,
          value: val
        });
      }
    }

    this._trimQueue(this._mutationQueue);
  }

  // ============================================
  // CORRELATION ENGINE
  // ============================================

  _correlate() {
    const now = Date.now();
    const window = this.correlationWindow;

    // Clean old entries
    this._actionQueue = this._actionQueue.filter(a => now - a.timestamp < window * 2);
    this._mutationQueue = this._mutationQueue.filter(m => now - m.timestamp < window * 2);

    // For each recent action, look for correlated mutations
    for (const action of this._actionQueue) {
      if (now - action.timestamp > window) continue;
      if (action._correlated) continue;

      const mutations = this._mutationQueue.filter(m =>
        m.timestamp >= action.timestamp &&
        m.timestamp <= action.timestamp + window
      );

      if (mutations.length === 0) continue;

      const inferred = this._inferPattern(action, mutations);
      if (inferred && inferred.confidence >= this.minConfidence) {
        action._correlated = true;
        this._reportPattern(inferred);
      }
    }
  }

  _inferPattern(action, mutations) {
    // Click + aria-expanded toggled = dropdown
    if (action.type === 'click' && action.meta.hasAriaExpanded) {
      const ariaChange = mutations.find(m =>
        m.type === 'attr-changed' && m.attr === 'aria-expanded'
      );
      if (ariaChange) {
        return {
          pattern: 'dropdown',
          confidence: 0.9,
          evidence: 'click + aria-expanded toggled',
          container: action.target.closest('[role="combobox"], [role="listbox"]') || action.target.parentElement,
          trigger: action.target
        };
      }
    }

    // Click + fixed/dialog element appears = modal
    if (action.type === 'click') {
      const appeared = mutations.filter(m => m.type === 'children-added');
      for (const m of appeared) {
        for (const node of m.nodes) {
          try {
            const style = getComputedStyle(node);
            const isDialog = node.getAttribute('role') === 'dialog' ||
                             node.getAttribute('aria-modal') === 'true';
            const isFixed = style.position === 'fixed' || style.position === 'absolute';
            if (isDialog || (isFixed && node.offsetWidth > 200 && node.offsetHeight > 100)) {
              return {
                pattern: 'modal',
                confidence: isDialog ? 0.95 : 0.75,
                evidence: isDialog ? 'dialog role appeared' : 'fixed element appeared after click',
                container: node
              };
            }
          } catch (e) {}
        }
      }
    }

    // Enter key on input + input cleared + children added = chat
    if (action.type === 'enter' && action.meta.isInput) {
      const childrenAdded = mutations.filter(m => m.type === 'children-added');
      if (childrenAdded.length > 0) {
        // Find the container where children were added
        const container = childrenAdded.sort((a, b) => b.count - a.count)[0]?.target;
        return {
          pattern: 'chat',
          confidence: 0.85,
          evidence: 'enter + children added (message sent)',
          container,
          input: action.target
        };
      }
    }

    // Scroll near bottom + children added = feed/infinite scroll
    if (action.type === 'scroll' && action.meta.atBottom) {
      const childrenAdded = mutations.filter(m =>
        m.type === 'children-added' &&
        (m.target === action.target || action.target.contains(m.target))
      );
      if (childrenAdded.length > 0) {
        return {
          pattern: 'feed',
          confidence: 0.7,
          evidence: 'scroll to bottom + children added',
          container: action.target
        };
      }
    }

    return null;
  }

  _reportPattern(inferred) {
    const key = `${inferred.pattern}:${inferred.container?.tagName || ''}:${inferred.container?.id || ''}`;

    // Deduplicate
    const existing = this.inferred.get(key);
    if (existing && existing.confidence >= inferred.confidence) return;

    this.inferred.set(key, inferred);

    this.log('detect', `[Passive] Inferred ${inferred.pattern} (${(inferred.confidence * 100).toFixed(0)}%) - ${inferred.evidence}`);

    // Notify callbacks
    this.patternCallbacks.forEach(cb => cb(inferred));
  }

  _trimQueue(queue, maxSize = 100) {
    while (queue.length > maxSize) queue.shift();
  }

  /**
   * Get all passively inferred patterns.
   *
   * @returns {Array<object>}
   */
  getInferred() {
    return [...this.inferred.values()];
  }

  /**
   * Clear inferred patterns.
   */
  clearInferred() {
    this.inferred.clear();
  }
}
