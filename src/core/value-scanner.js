/**
 * ValueScanner - Cheat Engine style DOM value scanner.
 *
 * Takes snapshots of all DOM element values and diffs successive snapshots
 * to detect changes, additions, and removals. Also includes heuristic
 * pattern detectors for chat, form, dropdown, and modal UI patterns.
 */

import { getElementPath, resolveElement } from './element-path.js';

export class ValueScanner {
  constructor() {
    this.snapshots = [];
    this.watchlist = new Map();
  }

  snapshot() {
    const snap = {
      timestamp: Date.now(),
      elements: new Map()
    };

    document.querySelectorAll('*').forEach(el => {
      try {
        const path = getElementPath(el);
        const values = this.extractValues(el);

        if (Object.keys(values).length > 0) {
          snap.elements.set(path, {
            el,
            path,
            values,
            tag: el.tagName,
            id: el.id,
            className: el.className?.toString?.().slice(0, 50)
          });
        }
      } catch (e) {}
    });

    this.snapshots.push(snap);

    if (this.snapshots.length > 10) {
      this.snapshots.shift();
    }

    return snap;
  }

  extractValues(el) {
    const values = {};

    // Text content (leaf nodes only)
    if (el.childNodes.length <= 3) {
      const text = el.innerText?.trim();
      if (text && text.length < 500 && text.length > 0) {
        values.text = text;
        values.textLength = text.length;
      }
    }

    // Input values
    if ('value' in el && el.value !== undefined) {
      values.value = el.value;
      values.valueLength = el.value?.length || 0;
    }

    // Checked/selected state
    if ('checked' in el) values.checked = el.checked;
    if ('selected' in el) values.selected = el.selected;

    // Child count
    values.childCount = el.children.length;

    // Scroll position
    if (el.scrollHeight > el.clientHeight) {
      values.scrollTop = el.scrollTop;
      values.scrollHeight = el.scrollHeight;
      values.isScrollable = true;
    }

    // Visibility
    try {
      const style = getComputedStyle(el);
      values.display = style.display;
      values.visibility = style.visibility;
      values.opacity = parseFloat(style.opacity);
    } catch (e) {}

    // Dimensions
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 || rect.height > 0) {
      values.width = Math.round(rect.width);
      values.height = Math.round(rect.height);
    }

    // ARIA state
    ['aria-expanded', 'aria-hidden', 'aria-selected', 'aria-checked', 'aria-pressed'].forEach(attr => {
      if (el.hasAttribute(attr)) {
        values[attr] = el.getAttribute(attr);
      }
    });

    // Data attributes
    for (const attr of el.attributes) {
      if (attr.name.startsWith('data-') && attr.value.length < 100) {
        values[attr.name] = attr.value;
      }
    }

    return values;
  }

  firstScan() {
    this.snapshots = [];
    return this.snapshot();
  }

  nextScan() {
    if (this.snapshots.length === 0) {
      return this.firstScan();
    }
    const before = this.snapshots[this.snapshots.length - 1];
    const after = this.snapshot();
    return this.diff(before, after);
  }

  diff(before, after) {
    const results = {
      changed: [],
      unchanged: [],
      added: [],
      removed: [],
      increased: [],
      decreased: [],
      summary: {}
    };

    for (const [path, afterData] of after.elements) {
      const beforeData = before.elements.get(path);

      if (!beforeData) {
        results.added.push({ path, el: afterData.el, values: afterData.values });
        continue;
      }

      const changes = this.diffValues(beforeData.values, afterData.values);

      if (changes.length > 0) {
        const changeData = {
          path,
          el: afterData.el,
          changes,
          before: beforeData.values,
          after: afterData.values
        };
        results.changed.push(changeData);

        for (const change of changes) {
          if (typeof change.before === 'number' && typeof change.after === 'number') {
            if (change.after > change.before) {
              results.increased.push({ path, el: afterData.el, ...change });
            } else if (change.after < change.before) {
              results.decreased.push({ path, el: afterData.el, ...change });
            }
          }
        }
      } else {
        results.unchanged.push({ path, el: afterData.el });
      }
    }

    for (const [path, beforeData] of before.elements) {
      if (!after.elements.has(path)) {
        results.removed.push({ path, values: beforeData.values });
      }
    }

    results.summary = {
      changed: results.changed.length,
      added: results.added.length,
      removed: results.removed.length,
      increased: results.increased.length,
      decreased: results.decreased.length
    };

    return results;
  }

  diffValues(before, after) {
    const changes = [];
    const allKeys = new Set([...Object.keys(before), ...Object.keys(after)]);

    for (const key of allKeys) {
      const bVal = before[key];
      const aVal = after[key];

      if (bVal !== aVal) {
        changes.push({
          key,
          before: bVal,
          after: aVal,
          type: this.categorizeChange(key, bVal, aVal)
        });
      }
    }

    return changes;
  }

  categorizeChange(key, before, after) {
    if (key === 'childCount' && after > before) return 'children-added';
    if (key === 'childCount' && after < before) return 'children-removed';
    if (key === 'textLength' && after > before) return 'text-grew';
    if (key === 'textLength' && after < before) return 'text-shrunk';
    if (key === 'value' && after === '') return 'input-cleared';
    if (key === 'value' && before === '') return 'input-filled';
    if (key === 'scrollTop') return 'scrolled';
    if (key === 'display' && after !== 'none' && before === 'none') return 'became-visible';
    if (key === 'display' && after === 'none') return 'became-hidden';
    if (key === 'aria-expanded') return 'aria-toggled';
    if (key === 'checked') return 'check-toggled';
    return 'value-changed';
  }

  // Auto-detect patterns from diff
  detectPattern(diff) {
    const detected = [];

    // Chat detection
    const chat = this.detectChat(diff);
    if (chat) detected.push(chat);

    // Form detection
    const form = this.detectForm(diff);
    if (form) detected.push(form);

    // Dropdown detection
    const dropdown = this.detectDropdown(diff);
    if (dropdown) detected.push(dropdown);

    // Modal detection
    const modal = this.detectModal(diff);
    if (modal) detected.push(modal);

    return detected;
  }

  detectChat(diff) {
    const inputCleared = diff.changed.filter(c =>
      c.changes.some(ch => ch.type === 'input-cleared')
    );

    const childrenAdded = diff.changed.filter(c =>
      c.changes.some(ch => ch.type === 'children-added')
    );

    if (inputCleared.length > 0 && childrenAdded.length > 0) {
      const container = childrenAdded.sort((a, b) => {
        const aChange = a.changes.find(c => c.key === 'childCount');
        const bChange = b.changes.find(c => c.key === 'childCount');
        return ((bChange?.after || 0) - (bChange?.before || 0)) -
               ((aChange?.after || 0) - (aChange?.before || 0));
      })[0];

      return {
        pattern: 'chat',
        confidence: 0.95,
        proof: 'input-cleared + children-added',
        components: {
          container: container?.el,
          input: inputCleared[0]?.el
        }
      };
    }

    return null;
  }

  detectForm(diff) {
    const inputsCleared = diff.changed.filter(c =>
      c.changes.some(ch => ch.type === 'input-cleared')
    );

    const becameVisible = diff.changed.filter(c =>
      c.changes.some(ch => ch.type === 'became-visible')
    );

    if (inputsCleared.length >= 2) {
      const formEl = inputsCleared[0]?.el?.closest('form');

      return {
        pattern: 'form',
        confidence: 0.85,
        proof: `${inputsCleared.length} inputs cleared`,
        components: {
          form: formEl,
          inputs: inputsCleared.map(i => i.el)
        }
      };
    }

    return null;
  }

  detectDropdown(diff) {
    const ariaToggled = diff.changed.filter(c =>
      c.changes.some(ch => ch.key === 'aria-expanded')
    );

    const becameVisible = diff.changed.filter(c =>
      c.changes.some(ch => ch.type === 'became-visible')
    );

    if (ariaToggled.length > 0) {
      const trigger = ariaToggled[0]?.el;
      const expanded = ariaToggled[0]?.after?.['aria-expanded'] === 'true';

      return {
        pattern: 'dropdown',
        confidence: 0.9,
        state: expanded ? 'opened' : 'closed',
        proof: 'aria-expanded toggled',
        components: {
          trigger,
          menu: becameVisible[0]?.el
        }
      };
    }

    return null;
  }

  detectModal(diff) {
    const becameVisible = diff.changed.filter(c => {
      const wasHidden = c.before.display === 'none';
      const isVisible = c.after.display !== 'none';
      return wasHidden && isVisible;
    });

    const fixedVisible = becameVisible.filter(c => {
      try {
        return getComputedStyle(c.el).position === 'fixed';
      } catch (e) { return false; }
    });

    if (fixedVisible.length > 0) {
      return {
        pattern: 'modal',
        confidence: 0.85,
        state: 'opened',
        proof: 'fixed element became visible',
        components: {
          container: fixedVisible[0]?.el
        }
      };
    }

    return null;
  }

  filterByType(diff, changeType) {
    return diff.changed.filter(c =>
      c.changes.some(ch => ch.type === changeType)
    );
  }

  getPath(el) {
    return getElementPath(el);
  }

  resolveElement(path) {
    return resolveElement(path);
  }

  get snapshotCount() {
    return this.snapshots.length;
  }

  get lastSnapshot() {
    return this.snapshots[this.snapshots.length - 1];
  }

  get elementCount() {
    return this.lastSnapshot?.elements.size || 0;
  }
}
