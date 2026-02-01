/**
 * Behavioral Verification - State machine for verifying UI pattern behavior.
 *
 * Defines temporal invariants for each pattern type and instruments
 * elements to record interaction traces. Allows guarantee escalation
 * from STRUCTURAL → SEMANTIC → BEHAVIORAL → VERIFIED.
 *
 * Example: ChatBoxSpec expects that after sending a message:
 *   1. Input is cleared (within 200ms)
 *   2. Children are added to the container (within 2000ms)
 *   3. Container scrolls down (within 2500ms)
 */

/**
 * Pattern specifications with temporal invariants.
 */
const SPECS = {
  chat: {
    name: 'ChatBox',
    actions: {
      send: {
        preconditions: [
          { check: 'input-has-value', desc: 'Input must contain text' }
        ],
        postconditions: [
          { check: 'input-cleared', timeout: 500, desc: 'Input should be cleared' },
          { check: 'children-added', timeout: 3000, desc: 'New message should appear in container' },
          { check: 'container-scrolled', timeout: 3500, desc: 'Container should scroll to show new message' }
        ]
      }
    }
  },
  form: {
    name: 'Form',
    actions: {
      submit: {
        preconditions: [
          { check: 'has-filled-fields', desc: 'At least one field should have a value' }
        ],
        postconditions: [
          { check: 'inputs-cleared-or-hidden', timeout: 2000, desc: 'Form should clear or navigate away' }
        ]
      }
    }
  },
  dropdown: {
    name: 'Dropdown',
    actions: {
      toggle: {
        preconditions: [],
        postconditions: [
          { check: 'aria-expanded-toggled', timeout: 500, desc: 'aria-expanded should toggle' },
          { check: 'menu-visibility-changed', timeout: 500, desc: 'Menu should appear or disappear' }
        ]
      },
      select: {
        preconditions: [
          { check: 'menu-visible', desc: 'Menu should be open' }
        ],
        postconditions: [
          { check: 'menu-closed', timeout: 500, desc: 'Menu should close after selection' },
          { check: 'trigger-text-changed', timeout: 500, desc: 'Trigger text should update' }
        ]
      }
    }
  },
  modal: {
    name: 'Modal',
    actions: {
      close: {
        preconditions: [
          { check: 'modal-visible', desc: 'Modal should be visible' }
        ],
        postconditions: [
          { check: 'modal-hidden', timeout: 1000, desc: 'Modal should disappear' }
        ]
      }
    }
  }
};

/**
 * Verifier that instruments a bound API and records interaction traces.
 */
export class PatternVerifier {
  constructor(patternName, components, logFn) {
    this.patternName = patternName;
    this.components = components;
    this.spec = SPECS[patternName];
    this.log = logFn || (() => {});
    this.traces = [];
    this.currentGuarantee = 'STRUCTURAL';
  }

  /**
   * Verify an action by checking preconditions, executing,
   * then monitoring postconditions.
   *
   * @param {string} actionName - The action to verify (e.g., 'send', 'submit').
   * @param {function} actionFn - The function that performs the action.
   * @returns {Promise<{ passed: boolean, results: Array<object>, guarantee: string }>}
   */
  async verify(actionName, actionFn) {
    if (!this.spec) {
      return { passed: false, results: [{ check: 'spec', passed: false, desc: 'No spec for pattern' }], guarantee: this.currentGuarantee };
    }

    const action = this.spec.actions[actionName];
    if (!action) {
      return { passed: false, results: [{ check: 'action', passed: false, desc: `No spec for action: ${actionName}` }], guarantee: this.currentGuarantee };
    }

    const results = [];
    const trace = {
      action: actionName,
      timestamp: Date.now(),
      preconditions: [],
      postconditions: [],
      passed: false
    };

    // Check preconditions
    for (const pre of action.preconditions) {
      const passed = this._checkCondition(pre.check);
      trace.preconditions.push({ ...pre, passed });
      results.push({ check: pre.check, passed, desc: pre.desc, phase: 'pre' });
      if (!passed) {
        this.log('warn', `[Verify] Precondition failed: ${pre.desc}`);
      }
    }

    // Capture initial state
    const initialState = this._captureState();

    // Execute the action
    try {
      const result = actionFn();
      if (result && typeof result.then === 'function') {
        await result;
      }
    } catch (e) {
      results.push({ check: 'execution', passed: false, desc: `Action threw: ${e.message}`, phase: 'exec' });
      trace.passed = false;
      this.traces.push(trace);
      return { passed: false, results, guarantee: this.currentGuarantee };
    }

    // Check postconditions with timeouts
    for (const post of action.postconditions) {
      const passed = await this._waitForCondition(post.check, post.timeout, initialState);
      trace.postconditions.push({ ...post, passed });
      results.push({ check: post.check, passed, desc: post.desc, phase: 'post' });
      if (!passed) {
        this.log('warn', `[Verify] Postcondition failed: ${post.desc}`);
      }
    }

    // Determine if all passed
    const allPassed = results.every(r => r.passed);
    trace.passed = allPassed;
    this.traces.push(trace);

    // Escalate guarantee if all passed
    if (allPassed) {
      this.currentGuarantee = 'VERIFIED';
      this.log('success', `[Verify] ${this.spec.name}.${actionName} VERIFIED`);
    }

    return { passed: allPassed, results, guarantee: this.currentGuarantee };
  }

  /**
   * Check a condition synchronously.
   *
   * @param {string} check
   * @returns {boolean}
   */
  _checkCondition(check) {
    const { input, container, trigger, fields } = this.components || {};

    const checks = {
      'input-has-value': () => !!(input?.value || input?.textContent?.trim()),
      'has-filled-fields': () => fields?.some(f => f.value?.trim()),
      'menu-visible': () => {
        const menu = this.components.menu;
        if (!menu) return false;
        try { return getComputedStyle(menu).display !== 'none'; } catch (e) { return false; }
      },
      'modal-visible': () => {
        if (!container) return false;
        try { return getComputedStyle(container).display !== 'none'; } catch (e) { return false; }
      }
    };

    return checks[check]?.() || false;
  }

  /**
   * Wait for a postcondition to become true within a timeout.
   *
   * @param {string} check
   * @param {number} timeout
   * @param {object} initialState
   * @returns {Promise<boolean>}
   */
  _waitForCondition(check, timeout, initialState) {
    return new Promise(resolve => {
      const start = Date.now();

      const poll = () => {
        if (this._checkPostCondition(check, initialState)) {
          resolve(true);
          return;
        }
        if (Date.now() - start > timeout) {
          resolve(false);
          return;
        }
        requestAnimationFrame(poll);
      };

      poll();
    });
  }

  /**
   * Check a postcondition against initial state.
   *
   * @param {string} check
   * @param {object} initialState
   * @returns {boolean}
   */
  _checkPostCondition(check, initialState) {
    const { input, container, trigger, fields } = this.components || {};

    const checks = {
      'input-cleared': () => {
        const current = input?.value || input?.textContent?.trim();
        return !current || current === '';
      },
      'children-added': () => {
        return container && container.children.length > initialState.containerChildCount;
      },
      'container-scrolled': () => {
        return container && container.scrollTop > initialState.containerScrollTop;
      },
      'inputs-cleared-or-hidden': () => {
        if (!fields) return false;
        const allCleared = fields.every(f => !f.value?.trim());
        const hidden = (() => {
          try { return container && getComputedStyle(container).display === 'none'; } catch (e) { return false; }
        })();
        return allCleared || hidden;
      },
      'aria-expanded-toggled': () => {
        const current = (trigger || this.components.trigger)?.getAttribute('aria-expanded');
        return current !== initialState.ariaExpanded;
      },
      'menu-visibility-changed': () => {
        const menu = this.components.menu;
        if (!menu) return false;
        try {
          const visible = getComputedStyle(menu).display !== 'none';
          return visible !== initialState.menuVisible;
        } catch (e) { return false; }
      },
      'menu-closed': () => {
        const menu = this.components.menu;
        if (!menu) return true;
        try { return getComputedStyle(menu).display === 'none'; } catch (e) { return true; }
      },
      'trigger-text-changed': () => {
        const current = (trigger || this.components.trigger)?.textContent?.trim();
        return current !== initialState.triggerText;
      },
      'modal-hidden': () => {
        if (!container) return true;
        try { return getComputedStyle(container).display === 'none'; } catch (e) { return true; }
      }
    };

    return checks[check]?.() || false;
  }

  /**
   * Capture current state for comparison in postcondition checks.
   *
   * @returns {object}
   */
  _captureState() {
    const { input, container, trigger, fields } = this.components || {};
    return {
      inputValue: input?.value || input?.textContent?.trim() || '',
      containerChildCount: container?.children.length || 0,
      containerScrollTop: container?.scrollTop || 0,
      ariaExpanded: trigger?.getAttribute('aria-expanded'),
      menuVisible: (() => {
        const menu = this.components.menu;
        if (!menu) return false;
        try { return getComputedStyle(menu).display !== 'none'; } catch (e) { return false; }
      })(),
      triggerText: trigger?.textContent?.trim() || '',
      fieldValues: fields?.map(f => f.value) || []
    };
  }

  /**
   * Get all recorded traces.
   *
   * @returns {Array<object>}
   */
  getTraces() {
    return this.traces;
  }

  /**
   * Get the current guarantee level.
   *
   * @returns {string}
   */
  getGuarantee() {
    return this.currentGuarantee;
  }
}
