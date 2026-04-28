import resolve from '@rollup/plugin-node-resolve';
import alias from '@rollup/plugin-alias';
import { fileURLToPath } from 'url';
import { dirname, resolve as pathResolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Rollup plugin that injects GM_getValue/GM_setValue shims at the top of the
 * bundle so SignatureStore (which references them as globals) uses our
 * localStorage-based adapter instead of Tampermonkey APIs.
 */
function gmShimPlugin() {
  return {
    name: 'gm-shim',
    intro() {
      return `
// GM_getValue/GM_setValue shims (replaces Tampermonkey storage with localStorage)
function GM_getValue(key, defaultValue) {
  try { const v = localStorage.getItem(key); return v !== null ? v : defaultValue; }
  catch (e) { return defaultValue; }
}
function GM_setValue(key, value) {
  try { localStorage.setItem(key, value); }
  catch (e) { console.warn('[UC] Storage write failed:', e); }
}
// unsafeWindow shim (in extension context, window IS the page context via world: "MAIN")
var unsafeWindow = window;
`;
    }
  };
}

export default {
  input: 'src/extension-entry.js',
  output: {
    file: 'dist/uc-extension.js',
    format: 'iife',
    sourcemap: false,
  },
  plugins: [
    gmShimPlugin(),
    resolve(),
  ],
};
