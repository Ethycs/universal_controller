// ============================================
// MODAL API ACTIONS
// ============================================

/**
 * Closes a modal by clicking its close button, or dispatching an Escape key event
 * on the container as a fallback.
 *
 * @param {object} components - The detected modal components ({ closeButton, container }).
 * @returns {{ success: boolean }}
 */
export function modalClose(components) {
  const { closeButton, container } = components;
  if (closeButton) closeButton.click();
  else container?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27, bubbles: true }));
  return { success: true };
}
