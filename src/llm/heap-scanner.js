/**
 * HeapScanner - Scans JavaScript globals and framework internals
 * for patterns that correlate with DOM elements.
 *
 * - Detects framework (React, Vue, Angular, Svelte)
 * - Finds React fiber nodes and their state
 * - Finds Vue component instances
 * - Scans window.* for known object shapes (stores, routers, etc.)
 */

/**
 * Scan for framework-specific internal structures.
 *
 * @returns {object} Framework detection results.
 */
export function scanFramework() {
  const result = {
    framework: 'unknown',
    version: null,
    details: {}
  };

  // React
  try {
    if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__) {
      result.framework = 'react';
      const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
      if (hook.renderers) {
        for (const [, renderer] of hook.renderers) {
          result.version = renderer.version || null;
        }
      }
      result.details.hasDevtools = true;
      result.details.isNext = !!window.__NEXT_DATA__;
      result.details.nextBuildId = window.__NEXT_DATA__?.buildId || null;
    }
  } catch (e) {}

  // Vue
  try {
    if (window.__VUE__) {
      result.framework = 'vue';
      result.version = window.__VUE__?.version || null;
      result.details.isNuxt = !!window.__NUXT__;
    } else if (window.__vue_app__) {
      result.framework = 'vue';
      result.version = window.__vue_app__?.version || '3.x';
    }
  } catch (e) {}

  // Angular
  try {
    if (window.ng) {
      result.framework = 'angular';
      const versionEl = document.querySelector('[ng-version]');
      result.version = versionEl?.getAttribute('ng-version') || null;
    }
  } catch (e) {}

  // Svelte
  try {
    const svelteEl = document.querySelector('[class*="svelte-"]');
    if (svelteEl) {
      result.framework = 'svelte';
    }
  } catch (e) {}

  return result;
}

/**
 * Get the React fiber node for a DOM element (React 16+).
 *
 * @param {HTMLElement} el
 * @returns {object|null} The fiber node, or null if not found/not React.
 */
export function getReactFiber(el) {
  if (!el) return null;

  // React stores fiber refs on DOM nodes with keys like __reactFiber$ or __reactInternalInstance$
  for (const key of Object.keys(el)) {
    if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) {
      return el[key];
    }
  }

  return null;
}

/**
 * Extract React component state and props from a fiber node.
 *
 * @param {object} fiber - A React fiber node.
 * @returns {{ type: string, props: object, state: *, hooks: Array }|null}
 */
export function extractReactState(fiber) {
  if (!fiber) return null;

  const result = {
    type: null,
    props: {},
    state: null,
    hooks: []
  };

  // Walk up to find the nearest function/class component
  let current = fiber;
  while (current) {
    if (typeof current.type === 'function') {
      result.type = current.type.displayName || current.type.name || 'Anonymous';

      // Props (sanitized)
      if (current.memoizedProps) {
        for (const [k, v] of Object.entries(current.memoizedProps)) {
          if (typeof v === 'function') {
            result.props[k] = '[Function]';
          } else if (v instanceof Element) {
            result.props[k] = `[Element: ${v.tagName}]`;
          } else {
            try {
              JSON.stringify(v);
              result.props[k] = v;
            } catch (e) {
              result.props[k] = '[Circular]';
            }
          }
        }
      }

      // Class component state
      if (current.memoizedState && typeof current.memoizedState === 'object' && !current.memoizedState.memoizedState) {
        result.state = current.memoizedState;
      }

      // Hooks (function components)
      if (current.memoizedState && current.memoizedState.memoizedState !== undefined) {
        let hookState = current.memoizedState;
        let hookIndex = 0;
        while (hookState && hookIndex < 20) {
          try {
            const value = hookState.memoizedState;
            if (value !== undefined && typeof value !== 'function') {
              result.hooks.push({ index: hookIndex, value });
            }
          } catch (e) {}
          hookState = hookState.next;
          hookIndex++;
        }
      }

      break;
    }
    current = current.return;
  }

  return result.type ? result : null;
}

/**
 * Get Vue component instance for a DOM element.
 *
 * @param {HTMLElement} el
 * @returns {object|null}
 */
export function getVueInstance(el) {
  if (!el) return null;

  // Vue 3
  if (el.__vue_app__ || el._vnode) {
    return el.__vueParentComponent || el._vnode?.component || null;
  }

  // Vue 2
  if (el.__vue__) {
    return {
      name: el.__vue__.$options?.name || 'Anonymous',
      data: el.__vue__.$data,
      props: el.__vue__.$props,
      computed: Object.keys(el.__vue__.$options?.computed || {})
    };
  }

  // Walk up to find Vue component
  let current = el;
  while (current && current !== document.body) {
    for (const key of Object.keys(current)) {
      if (key.startsWith('__vue')) {
        return current[key];
      }
    }
    current = current.parentElement;
  }

  return null;
}

/**
 * Scan window.* for known object shapes (stores, routers, API clients).
 *
 * @returns {Array<{ name: string, type: string, shape: object }>}
 */
export function scanGlobals() {
  const results = [];
  const checked = new Set();

  // Known global patterns
  const patterns = [
    { match: (k, v) => k.includes('store') || k.includes('Store'), type: 'store' },
    { match: (k, v) => k.includes('router') || k.includes('Router'), type: 'router' },
    { match: (k, v) => k.includes('api') || k.includes('Api') || k.includes('API'), type: 'api' },
    { match: (k, v) => k.includes('socket') || k.includes('Socket'), type: 'websocket' },
    { match: (k, v) => v && typeof v.dispatch === 'function' && typeof v.getState === 'function', type: 'redux-store' },
    { match: (k, v) => v && typeof v.subscribe === 'function' && typeof v.pipe === 'function', type: 'observable' }
  ];

  try {
    for (const key of Object.keys(window)) {
      if (checked.has(key) || key.startsWith('__') || key.startsWith('webkit')) continue;
      checked.add(key);

      try {
        const value = window[key];
        if (value === null || value === undefined) continue;
        if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') continue;

        for (const pattern of patterns) {
          if (pattern.match(key, value)) {
            results.push({
              name: key,
              type: pattern.type,
              shape: describeShape(value)
            });
            break;
          }
        }
      } catch (e) {}
    }
  } catch (e) {}

  return results.slice(0, 20);
}

/**
 * Describe the shape of an object (top-level keys and their types).
 *
 * @param {object} obj
 * @returns {object}
 */
function describeShape(obj) {
  if (!obj || typeof obj !== 'object') return {};

  const shape = {};
  try {
    for (const key of Object.keys(obj).slice(0, 15)) {
      const val = obj[key];
      if (typeof val === 'function') {
        shape[key] = `function(${val.length} args)`;
      } else if (Array.isArray(val)) {
        shape[key] = `array[${val.length}]`;
      } else if (val && typeof val === 'object') {
        shape[key] = `object{${Object.keys(val).slice(0, 3).join(',')}}`;
      } else {
        shape[key] = typeof val;
      }
    }
  } catch (e) {}
  return shape;
}

/**
 * Full heap scan: framework + React/Vue internals + globals.
 *
 * @param {HTMLElement} [targetEl] - Optional element to get framework internals for.
 * @returns {object}
 */
export function fullHeapScan(targetEl) {
  const result = {
    framework: scanFramework(),
    globals: scanGlobals(),
    elementState: null
  };

  if (targetEl) {
    // Try React
    const fiber = getReactFiber(targetEl);
    if (fiber) {
      result.elementState = {
        framework: 'react',
        ...extractReactState(fiber)
      };
    }

    // Try Vue
    if (!result.elementState) {
      const vue = getVueInstance(targetEl);
      if (vue) {
        result.elementState = {
          framework: 'vue',
          instance: vue
        };
      }
    }
  }

  return result;
}
