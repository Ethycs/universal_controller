// ============================================
// FORM API ACTIONS
// ============================================

import { setText, submitInput } from './text-input.js';

/**
 * Fills form fields by matching data keys to field names, ids, placeholders, or aria-labels.
 *
 * @param {object} components - The detected form components ({ fields, submitButton, container }).
 * @param {object} data - Key-value pairs where keys are matched against field identifiers.
 * @param {function} [log] - Optional logging function with signature (type, msg).
 * @returns {{ success: boolean, filled?: Array<{ key: string, value: string }> }}
 */
export function formFill(components, data, log) {
  const logFn = log || (() => {});
  const { fields } = components;
  if (!fields) return { success: false };

  const filled = [];

  for (const field of fields) {
    const key = field.name || field.id || field.placeholder?.toLowerCase() ||
                field.getAttribute('aria-label')?.toLowerCase();

    const match = Object.entries(data).find(([k]) =>
      key?.toLowerCase().includes(k.toLowerCase())
    );

    if (match) {
      setText(field, match[1]);
      filled.push({ key, value: match[1] });
    }
  }

  logFn('success', `Filled ${filled.length} fields`);
  return { success: true, filled };
}

/**
 * Submits a form by clicking the submit button or falling back to submitInput.
 *
 * @param {object} components - The detected form components ({ submitButton, container, fields }).
 * @param {function} [log] - Optional logging function with signature (type, msg).
 * @returns {{ success: boolean }}
 */
export function formSubmit(components, log) {
  const logFn = log || (() => {});
  const { submitButton, container, fields } = components;
  const input = fields?.[0] || container?.querySelector('input');

  if (submitButton) {
    submitButton.click();
  } else {
    submitInput(input, logFn);
  }

  logFn('success', 'Form submitted');
  return { success: true };
}

/**
 * Retrieves current values from all form fields.
 *
 * @param {object} components - The detected form components ({ fields }).
 * @returns {object} Key-value pairs of field names/ids to their current values.
 */
export function formGetValues(components) {
  const { fields } = components;
  const values = {};
  fields?.forEach((f, i) => {
    values[f.name || f.id || `field-${i}`] = f.value;
  });
  return values;
}
