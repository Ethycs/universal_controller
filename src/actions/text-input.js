// ============================================
// TEXT INPUT ACTIONS (framework-agnostic)
// ============================================

/**
 * Sets text on an input/textarea element using multiple strategies.
 * Order: native setter + InputEvent (React/Vue) → execCommand → paste simulation.
 * Each attempt is verified before trying the next fallback.
 *
 * @param {HTMLElement} input - The input, textarea, or contenteditable element.
 * @param {string} text - The text to set.
 * @returns {{ success: boolean, method: string }}
 */
export function setText(input, text) {
  if (!input) return { success: false, method: 'none' };

  // Route contenteditable elements to dedicated handler
  if (input.contentEditable === 'true' && !('value' in input)) {
    return setContentEditable(input, text);
  }

  input.focus();

  // Method 1: Native setter + InputEvent (works on React/Vue/Angular)
  try {
    const proto = input.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) {
      setter.call(input, text);
    } else {
      input.value = text;
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));

    if (input.value === text) {
      return { success: true, method: 'nativeSetter' };
    }
  } catch (e) {}

  // Method 2: execCommand('insertText')
  try {
    input.focus();
    input.select();
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, text);

    if (input.value === text) {
      return { success: true, method: 'execCommand' };
    }
  } catch (e) {}

  // Method 3: Paste simulation (may be restricted by CSP)
  try {
    input.focus();
    if (input.select) input.select();

    const dt = new DataTransfer();
    dt.setData('text/plain', text);
    const pasteEvent = new ClipboardEvent('paste', {
      bubbles: true,
      cancelable: true,
      clipboardData: dt
    });
    input.dispatchEvent(pasteEvent);

    if (input.value === text) {
      return { success: true, method: 'paste' };
    }
  } catch (e) {}

  // Final fallback: brute-force value assignment
  input.value = text;
  input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
  return { success: input.value === text, method: 'directAssign' };
}

/**
 * Sets text on a contenteditable element using dedicated strategies.
 *
 * @param {HTMLElement} el - The contenteditable element.
 * @param {string} text - The text to set.
 * @returns {{ success: boolean, method: string }}
 */
function setContentEditable(el, text) {
  el.focus();

  // Select all existing content
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);

  // Method 1: execCommand (preserves undo history, fires input events)
  try {
    document.execCommand('insertText', false, text);
    if (el.textContent.trim() === text.trim()) {
      return { success: true, method: 'execCommand' };
    }
  } catch (e) {}

  // Method 2: Direct textContent assignment
  el.textContent = text;
  el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
  return { success: el.textContent.trim() === text.trim(), method: 'directTextContent' };
}

/**
 * Searches for a submit button near the given input element.
 * Checks several ancestor containers and tries multiple button selectors.
 *
 * @param {HTMLElement} input - The input element to search near.
 * @returns {HTMLElement|null} The submit button element, or null if not found.
 */
export function findSubmitButton(input) {
  const searchRoots = [
    input?.closest('form'),
    input?.closest('[class*="chat"]'),
    input?.closest('[class*="composer"]'),
    input?.closest('[class*="input"]'),
    input?.closest('[data-testid]'),
    input?.parentElement?.parentElement?.parentElement,
    document.body
  ].filter(Boolean);

  const selectors = [
    'button[type="submit"]',
    'button[aria-label*="send" i]',
    'button[aria-label*="Submit" i]',
    'button[data-testid*="send" i]',
    'button:not([type="button"]):not([aria-label*="attach" i]):not([aria-label*="upload" i])',
    '[role="button"][aria-label*="send" i]'
  ];

  for (const root of searchRoots) {
    for (const selector of selectors) {
      try {
        const btn = root.querySelector(selector);
        if (btn && !btn.disabled && btn.offsetParent !== null) {
          return btn;
        }
      } catch (e) {}
    }
  }

  return null;
}

/**
 * Submits the input by trying several strategies in order:
 * 1. Click a nearby submit button
 * 2. form.requestSubmit()
 * 3. form.submit()
 * 4. Dispatch an Enter keydown event
 *
 * @param {HTMLElement} input - The input element to submit.
 * @param {function} [log] - Optional logging function with signature (type, msg).
 * @returns {string} The method used: 'button', 'form', or 'enter'.
 */
export function submitInput(input, log) {
  const logFn = log || (() => {});
  const form = input?.closest('form');
  const btn = findSubmitButton(input);

  if (btn && !btn.disabled) {
    logFn('info', `Clicking button: ${btn.textContent?.slice(0, 20) || btn.ariaLabel || 'submit'}`);
    btn.click();
    return 'button';
  }

  if (form?.requestSubmit) {
    logFn('info', 'Using form.requestSubmit()');
    form.requestSubmit();
    return 'form';
  }

  if (form) {
    logFn('info', 'Using form.submit()');
    form.submit();
    return 'form';
  }

  logFn('info', 'Sending Enter key');
  input?.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter',
    code: 'Enter',
    keyCode: 13,
    which: 13,
    bubbles: true,
    cancelable: true
  }));
  return 'enter';
}
