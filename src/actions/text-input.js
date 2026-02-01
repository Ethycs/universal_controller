// ============================================
// TEXT INPUT ACTIONS (framework-agnostic)
// ============================================

/**
 * Sets text on an input element using multiple strategies for maximum compatibility.
 * Tries paste simulation first, then execCommand, then direct value setting with events.
 *
 * @param {HTMLElement} input - The input, textarea, or contenteditable element.
 * @param {string} text - The text to set.
 * @returns {boolean} True if the input was non-null and the operation was attempted.
 */
export function setText(input, text) {
  if (!input) return false;

  input.focus();

  // Clear existing content
  if (input.select) {
    input.select();
  } else if (input.contentEditable === 'true') {
    // contenteditable
    const range = document.createRange();
    range.selectNodeContents(input);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // Method 1: Paste simulation (most universal)
  try {
    const dt = new DataTransfer();
    dt.setData('text/plain', text);
    const pasteEvent = new ClipboardEvent('paste', {
      bubbles: true,
      cancelable: true,
      clipboardData: dt
    });
    input.dispatchEvent(pasteEvent);
  } catch (e) {
    // DataTransfer not supported in some contexts
  }

  // Verify or fallback to execCommand
  const currentValue = input.value ?? input.textContent;
  if (currentValue !== text) {
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, text);
  }

  // Final fallback: direct set + events
  const finalValue = input.value ?? input.textContent;
  if (finalValue !== text) {
    if ('value' in input) {
      const setter = Object.getOwnPropertyDescriptor(
        input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
        'value'
      )?.set;
      setter?.call(input, text) || (input.value = text);
    } else {
      input.textContent = text;
    }
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
  }

  return true;
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
