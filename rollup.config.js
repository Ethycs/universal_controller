import resolve from '@rollup/plugin-node-resolve';
import { readFileSync } from 'fs';

const pkg = JSON.parse(readFileSync('./package.json', 'utf8'));

const ghPagesBase = 'https://ethycs.github.io/universal_controller';

const userscriptHeader = `// ==UserScript==
// @name         Universal Controller
// @namespace    https://github.com/Ethycs/universal_controller
// @version      ${pkg.version}
// @description  ${pkg.description}
// @author       Universal Controller
// @match        *://*/*
// @grant        GM_registerMenuCommand
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_addStyle
// @grant        unsafeWindow
// @run-at       document-idle
// @updateURL    ${ghPagesBase}/universal-controller.user.js
// @downloadURL  ${ghPagesBase}/universal-controller.user.js
// @homepageURL  ${ghPagesBase}
// ==/UserScript==
`;

export default {
  input: 'src/index.js',
  output: {
    file: 'dist/universal-controller.user.js',
    format: 'iife',
    banner: userscriptHeader,
    sourcemap: false
  },
  plugins: [
    resolve()
  ]
};
