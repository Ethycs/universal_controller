// ============================================
// CHAT API ACTIONS
// ============================================

import { setText, submitInput } from './text-input.js';

/**
 * Sends a chat message by setting text in the input and then submitting it.
 *
 * @param {object} components - The detected chat components ({ input, container, sendButton }).
 * @param {string} text - The message text to send.
 * @param {function} [log] - Optional logging function with signature (type, msg).
 * @returns {{ success: boolean, error?: string }}
 */
export function chatSend(components, text, log) {
  const logFn = log || (() => {});
  const { input } = components;
  if (!input) return { success: false, error: 'No input found' };

  setText(input, text);

  setTimeout(() => {
    const method = submitInput(input, logFn);
    logFn('success', `Sent via ${method}: "${text.slice(0, 30)}${text.length > 30 ? '...' : ''}"`);
  }, 50);

  return { success: true };
}

/**
 * Retrieves all visible messages from a chat container by walking leaf text nodes.
 *
 * @param {object} components - The detected chat components ({ container }).
 * @returns {Array<{ text: string, el: HTMLElement }>}
 */
export function chatGetMessages(components) {
  const { container } = components;
  if (!container) return [];

  const messages = [];
  const walk = (el) => {
    const text = el.innerText?.trim();
    if (el.children.length === 0 && text && text.length > 0 && text.length < 1000) {
      messages.push({ text, el });
    } else {
      [...el.children].forEach(walk);
    }
  };
  walk(container);
  return messages;
}

/**
 * Observes a chat container for new messages using a MutationObserver.
 *
 * @param {object} components - The detected chat components ({ container }).
 * @param {function} callback - Called with { text, el } for each new node added.
 * @returns {function|null} A disconnect function to stop observing, or null if no container.
 */
export function chatOnMessage(components, callback) {
  const { container } = components;
  if (!container) return null;

  const observer = new MutationObserver(mutations => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType === 1) callback({ text: node.innerText, el: node });
      }
    }
  });

  observer.observe(container, { childList: true, subtree: true });
  return () => observer.disconnect();
}
