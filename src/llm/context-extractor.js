/**
 * LLM Context Extractor - Packages element context for LLM-assisted
 * code generation.
 *
 * Extracts:
 *   - Simplified HTML of the target element and surrounding context
 *   - Attributes, ARIA properties, computed styles
 *   - Detection evidence (structural, phrasal, behavioral scores)
 *   - Framework detection results
 *   - Send method results (which fallback worked)
 *   - Suggested approach for interacting with the element
 */

/**
 * Extract a simplified HTML representation of an element.
 * Strips long attribute values and limits depth/breadth.
 *
 * @param {HTMLElement} el
 * @param {number} [maxDepth=3]
 * @param {number} [maxChildren=5]
 * @returns {string}
 */
function simplifyHTML(el, maxDepth = 3, maxChildren = 5) {
  if (!el || maxDepth < 0) return '';

  const tag = el.tagName.toLowerCase();
  const attrs = [];

  for (const attr of el.attributes) {
    let value = attr.value;
    // Truncate long values
    if (value.length > 60) value = value.slice(0, 60) + '...';
    // Skip style attribute (too verbose)
    if (attr.name === 'style') continue;
    // Skip class if very long (minified)
    if (attr.name === 'class' && value.length > 80) {
      value = value.split(' ').slice(0, 3).join(' ') + '...';
    }
    attrs.push(`${attr.name}="${value}"`);
  }

  const attrStr = attrs.length > 0 ? ' ' + attrs.join(' ') : '';

  // Leaf node
  if (el.children.length === 0) {
    const text = el.textContent?.trim();
    if (text && text.length < 100) {
      return `<${tag}${attrStr}>${text}</${tag}>`;
    }
    if (tag === 'input' || tag === 'br' || tag === 'hr' || tag === 'img') {
      return `<${tag}${attrStr} />`;
    }
    return `<${tag}${attrStr}></${tag}>`;
  }

  // Recurse into children
  const childHTML = [...el.children]
    .slice(0, maxChildren)
    .map(c => '  ' + simplifyHTML(c, maxDepth - 1, maxChildren).split('\n').join('\n  '))
    .join('\n');

  const more = el.children.length > maxChildren
    ? `\n  <!-- +${el.children.length - maxChildren} more children -->`
    : '';

  return `<${tag}${attrStr}>\n${childHTML}${more}\n</${tag}>`;
}

/**
 * Detect the frontend framework on the page.
 *
 * @returns {string}
 */
function detectFramework() {
  try {
    if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || document.querySelector('[data-reactroot]')) {
      return window.__NEXT_DATA__ ? 'React (Next.js)' : 'React';
    }
    if (window.__VUE__ || document.querySelector('[data-v-]')) {
      return window.__NUXT__ ? 'Vue (Nuxt)' : 'Vue';
    }
    if (window.ng || document.querySelector('[ng-version]')) return 'Angular';
    if (document.querySelector('[class*="svelte-"]')) return 'Svelte';
  } catch (e) {}
  return 'Vanilla/Unknown';
}

/**
 * Extract computed style properties relevant to interaction.
 *
 * @param {HTMLElement} el
 * @returns {object}
 */
function getRelevantStyles(el) {
  try {
    const s = getComputedStyle(el);
    return {
      display: s.display,
      position: s.position,
      overflow: s.overflow,
      visibility: s.visibility,
      pointerEvents: s.pointerEvents
    };
  } catch (e) {
    return {};
  }
}

/**
 * Extract full context for an LLM prompt.
 *
 * @param {object} params
 * @param {HTMLElement} params.el - The target element.
 * @param {string} params.patternName - The detected pattern type.
 * @param {object} [params.evidence] - Detection evidence scores.
 * @param {object} [params.components] - Detected sub-components.
 * @param {object} [params.sendResult] - Result from setText/chatSend.
 * @param {string} [params.path] - Element path.
 * @returns {string} Formatted context string for LLM.
 */
export function extractLLMContext(params) {
  const { el, patternName, evidence, components, sendResult, path } = params;

  const sections = [];

  // Header
  sections.push(`# Universal Controller - Element Context for LLM`);
  sections.push(`Pattern: ${patternName}`);
  sections.push(`Site: ${location.hostname}${location.pathname}`);
  sections.push(`Framework: ${detectFramework()}`);
  sections.push(`Path: ${path || 'N/A'}`);
  sections.push('');

  // Element HTML
  sections.push('## Target Element HTML');
  sections.push('```html');
  if (el) {
    sections.push(simplifyHTML(el, 3, 5));
  } else {
    sections.push('<!-- Element not available -->');
  }
  sections.push('```');
  sections.push('');

  // Parent context (one level up)
  if (el?.parentElement) {
    sections.push('## Parent Context');
    sections.push('```html');
    sections.push(simplifyHTML(el.parentElement, 2, 3));
    sections.push('```');
    sections.push('');
  }

  // Key attributes
  if (el) {
    sections.push('## Key Attributes');
    const attrs = {
      id: el.id || null,
      class: el.className?.toString?.().slice(0, 100) || null,
      role: el.getAttribute('role'),
      'aria-label': el.getAttribute('aria-label'),
      'aria-live': el.getAttribute('aria-live'),
      'aria-expanded': el.getAttribute('aria-expanded'),
      'data-testid': el.getAttribute('data-testid'),
      contenteditable: el.getAttribute('contenteditable'),
      placeholder: el.getAttribute('placeholder')
    };
    for (const [k, v] of Object.entries(attrs)) {
      if (v) sections.push(`- ${k}: ${v}`);
    }

    const styles = getRelevantStyles(el);
    sections.push(`- computed: display=${styles.display}, position=${styles.position}, overflow=${styles.overflow}`);
    sections.push('');
  }

  // Components
  if (components) {
    sections.push('## Detected Components');
    for (const [name, comp] of Object.entries(components)) {
      if (comp instanceof Element) {
        sections.push(`- ${name}: <${comp.tagName.toLowerCase()}> id="${comp.id || ''}" class="${comp.className?.toString?.().slice(0, 50) || ''}"`);
      } else if (Array.isArray(comp)) {
        sections.push(`- ${name}: [${comp.length} elements]`);
      } else if (comp) {
        sections.push(`- ${name}: ${JSON.stringify(comp).slice(0, 80)}`);
      }
    }
    sections.push('');
  }

  // Detection evidence
  if (evidence) {
    sections.push('## Detection Evidence');
    sections.push(`- Structural: ${((evidence.structural || 0) * 100).toFixed(0)}%`);
    sections.push(`- Phrasal: ${((evidence.phrasal || 0) * 100).toFixed(0)}%`);
    sections.push(`- Semantic (ARIA): ${((evidence.semantic || 0) * 100).toFixed(0)}%`);
    sections.push(`- Behavioral: ${((evidence.behavioral || 0) * 100).toFixed(0)}%`);
    if (evidence.phrasalMatches) {
      sections.push(`- Phrasal matches: ${evidence.phrasalMatches.join(', ')}`);
    }
    sections.push('');
  }

  // Send method results
  if (sendResult) {
    sections.push('## Input Method Results');
    sections.push(`- Method used: ${sendResult.method}`);
    sections.push(`- Success: ${sendResult.success}`);
    sections.push('');
  }

  // Instruction for LLM
  sections.push('## Task');
  sections.push(`Write a JavaScript function that interacts with this ${patternName} element.`);
  sections.push('The function should work in a Tampermonkey userscript context.');
  sections.push(`Framework: ${detectFramework()} - use appropriate input simulation methods.`);
  sections.push('');

  return sections.join('\n');
}

/**
 * Generate a compact context string for clipboard copy.
 *
 * @param {object} detectionResult - A result from controller.detect().
 * @param {object} controller - The UniversalController instance.
 * @returns {string}
 */
export function generateCopyContext(detectionResult, controller) {
  const api = controller.getAPI(detectionResult.patternName);
  return extractLLMContext({
    el: detectionResult.el,
    patternName: detectionResult.patternName,
    evidence: detectionResult.evidence,
    components: detectionResult.components || api?.components,
    path: detectionResult.path
  });
}
