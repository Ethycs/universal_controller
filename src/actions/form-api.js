// ============================================
// FORM API ACTIONS
// ============================================

import { setText, submitInput } from './text-input.js';

/**
 * Match a form field against a key using priority-based matching.
 * Priority: name (exact) > id (exact) > type > placeholder (substring) > aria-label > label[for]
 *
 * @param {HTMLElement} field - The form field element.
 * @param {string} key - The key to match against.
 * @returns {number} Match score (0 = no match, higher = better match).
 */
function matchField(field, key) {
  const k = key.toLowerCase();

  // Priority 1: exact name match
  if (field.name && field.name.toLowerCase() === k) return 6;

  // Priority 2: exact id match
  if (field.id && field.id.toLowerCase() === k) return 5;

  // Priority 3: type match (for things like "password", "email")
  if (field.type && field.type.toLowerCase() === k) return 4;

  // Priority 4: placeholder substring match
  const placeholder = field.placeholder?.toLowerCase();
  if (placeholder && placeholder.includes(k)) return 3;

  // Priority 5: aria-label substring match
  const ariaLabel = field.getAttribute('aria-label')?.toLowerCase();
  if (ariaLabel && ariaLabel.includes(k)) return 2;

  // Priority 6: associated label text
  const labelFor = field.id ? document.querySelector(`label[for="${field.id}"]`) : null;
  if (labelFor && labelFor.textContent?.toLowerCase().includes(k)) return 1;

  // Also check partial name/id matches
  if (field.name && field.name.toLowerCase().includes(k)) return 0.5;
  if (field.id && field.id.toLowerCase().includes(k)) return 0.5;

  return 0;
}

/**
 * Set a value on a form field, handling different field types appropriately.
 *
 * @param {HTMLElement} field - The form field element.
 * @param {*} value - The value to set.
 */
function setFieldValue(field, value) {
  const tag = field.tagName;
  const type = field.type?.toLowerCase();

  // Checkbox
  if (type === 'checkbox') {
    const shouldCheck = value === true || value === 'true' || value === 1 || value === 'on';
    if (field.checked !== shouldCheck) {
      field.checked = shouldCheck;
      field.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return;
  }

  // Radio
  if (type === 'radio') {
    if (field.value === String(value)) {
      field.checked = true;
      field.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return;
  }

  // Select dropdown
  if (tag === 'SELECT') {
    const valueStr = String(value).toLowerCase();
    const option = [...field.options].find(o =>
      o.value.toLowerCase() === valueStr ||
      o.textContent?.toLowerCase().includes(valueStr)
    );
    if (option) {
      field.value = option.value;
      field.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return;
  }

  // Text inputs, textareas, and everything else
  setText(field, String(value));
}

/**
 * Get the current value of a form field, handling different types.
 *
 * @param {HTMLElement} field - The form field element.
 * @returns {*} The field's current value.
 */
function getFieldValue(field) {
  const type = field.type?.toLowerCase();

  if (type === 'checkbox') return field.checked;
  if (type === 'radio') return field.checked ? field.value : undefined;

  if (field.tagName === 'SELECT') {
    const selected = field.options[field.selectedIndex];
    return selected ? { value: selected.value, text: selected.textContent?.trim() } : null;
  }

  return field.value;
}

/**
 * Fills form fields by matching data keys to fields using priority-based matching.
 *
 * @param {object} components - The detected form components ({ fields, submitButton, container }).
 * @param {object} data - Key-value pairs where keys are matched against field identifiers.
 * @param {function} [log] - Optional logging function with signature (type, msg).
 * @returns {{ success: boolean, filled: Array<{ key: string, field: string, value: * }> }}
 */
export function formFill(components, data, log) {
  const logFn = log || (() => {});
  const { fields } = components;
  if (!fields || fields.length === 0) return { success: false, filled: [] };

  const filled = [];
  const usedFields = new Set();

  for (const [dataKey, dataValue] of Object.entries(data)) {
    // Score all fields against this key
    let bestField = null;
    let bestScore = 0;

    for (const field of fields) {
      if (usedFields.has(field)) continue;
      const score = matchField(field, dataKey);
      if (score > bestScore) {
        bestScore = score;
        bestField = field;
      }
    }

    if (bestField && bestScore > 0) {
      setFieldValue(bestField, dataValue);
      usedFields.add(bestField);
      filled.push({
        key: dataKey,
        field: bestField.name || bestField.id || bestField.type,
        value: dataValue
      });
    }
  }

  logFn('success', `Filled ${filled.length}/${Object.keys(data).length} fields`);
  return { success: filled.length > 0, filled };
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
 * Retrieves current values from all form fields with proper type handling.
 *
 * @param {object} components - The detected form components ({ fields }).
 * @returns {object} Key-value pairs of field identifiers to their current values.
 */
export function formGetValues(components) {
  const { fields } = components;
  const values = {};

  fields?.forEach((f, i) => {
    const key = f.name || f.id || `field-${i}`;
    const value = getFieldValue(f);
    if (value !== undefined) {
      values[key] = value;
    }
  });

  return values;
}
