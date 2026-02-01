// ============================================
// DROPDOWN API ACTIONS
// ============================================

/**
 * Toggles a dropdown by clicking its trigger element.
 *
 * @param {object} components - The detected dropdown components ({ trigger, menu }).
 * @returns {{ success: boolean }}
 */
export function dropdownToggle(components) {
  components.trigger?.click();
  return { success: true };
}

/**
 * Selects an option from a dropdown by clicking the trigger, then finding and
 * clicking a matching option in the menu after a short delay.
 *
 * @param {object} components - The detected dropdown components ({ trigger, menu }).
 * @param {string} value - The text to match against option labels.
 * @returns {{ success: boolean }}
 */
export function dropdownSelect(components, value) {
  const { trigger, menu } = components;
  trigger?.click();
  setTimeout(() => {
    const options = menu?.querySelectorAll('[role="option"], li, [class*="option"]') || [];
    [...options].find(o => o.innerText?.includes(value))?.click();
  }, 100);
  return { success: true };
}
