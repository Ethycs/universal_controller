// ============================================
// CHAT API ACTIONS
// ============================================

import { setText, submitInput } from './text-input.js';

// Track sent messages for own-message filtering
const sentMessages = [];
const MAX_SENT_HISTORY = 50;

/**
 * Sends a chat message by setting text in the input, waiting for the value
 * to propagate (via MutationObserver on the input), then submitting.
 *
 * Returns a Promise that resolves when the message has been submitted.
 *
 * @param {object} components - The detected chat components ({ input, container, sendButton }).
 * @param {string} text - The message text to send.
 * @param {function} [log] - Optional logging function with signature (type, msg).
 * @param {object} [options] - { timeout: number } Timeout in ms before fallback submit (default 500).
 * @returns {Promise<{ success: boolean, method?: string, error?: string }>}
 */
export function chatSend(components, text, log, options = {}) {
  const logFn = log || (() => {});
  const { input } = components;
  const timeout = options.timeout || 500;

  if (!input) return Promise.resolve({ success: false, error: 'No input found' });

  const result = setText(input, text);

  // Track sent message for own-message filtering
  sentMessages.push({ text: text.trim(), timestamp: Date.now() });
  if (sentMessages.length > MAX_SENT_HISTORY) sentMessages.shift();

  return new Promise((resolve) => {
    // Wait for the input value to propagate, then submit
    const checkAndSubmit = () => {
      const method = submitInput(input, logFn);
      const preview = `${text.slice(0, 30)}${text.length > 30 ? '...' : ''}`;
      logFn('success', `Sent via ${method} (setText: ${result.method}): "${preview}"`);
      resolve({ success: true, method, setMethod: result.method });
    };

    // Observe the input for value change confirmation
    const currentValue = input.value ?? input.textContent;
    if (currentValue === text || currentValue?.trim() === text.trim()) {
      // Value already set, submit immediately
      checkAndSubmit();
      return;
    }

    // Use MutationObserver + input event to wait for value propagation
    let settled = false;

    const settle = () => {
      if (settled) return;
      settled = true;
      observer.disconnect();
      clearTimeout(timer);
      checkAndSubmit();
    };

    const observer = new MutationObserver(() => {
      const val = input.value ?? input.textContent;
      if (val === text || val?.trim() === text.trim()) {
        settle();
      }
    });

    observer.observe(input, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true
    });

    // Also listen for input event (covers most frameworks)
    const onInput = () => {
      const val = input.value ?? input.textContent;
      if (val === text || val?.trim() === text.trim()) {
        input.removeEventListener('input', onInput);
        settle();
      }
    };
    input.addEventListener('input', onInput);

    // Fallback timeout — submit even if value didn't propagate yet
    const timer = setTimeout(() => {
      input.removeEventListener('input', onInput);
      settle();
    }, timeout);
  });
}

/**
 * Checks if a message text was recently sent by us.
 *
 * @param {string} text - The message text to check.
 * @param {number} [windowMs=5000] - How far back to check (ms).
 * @returns {boolean}
 */
function isOwnMessage(text, windowMs = 5000) {
  const trimmed = text.trim();
  const now = Date.now();
  return sentMessages.some(m =>
    m.text === trimmed && (now - m.timestamp) < windowMs
  );
}

/**
 * Simple hash for deduplication of message text.
 * @param {string} str
 * @returns {number}
 */
function hashText(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) - h + str.charCodeAt(i)) | 0;
  }
  return h;
}

/**
 * Retrieves all visible messages from a chat container by walking leaf text nodes.
 *
 * @param {object} components - The detected chat components ({ container }).
 * @returns {Array<{ text: string, el: HTMLElement, isOwn: boolean }>}
 */
export function chatGetMessages(components) {
  const { container } = components;
  if (!container) return [];

  const messages = [];
  const seen = new Set();

  const walk = (el) => {
    const text = el.innerText?.trim();
    if (el.children.length === 0 && text && text.length > 0 && text.length < 1000) {
      const hash = hashText(text);
      if (!seen.has(hash)) {
        seen.add(hash);
        messages.push({
          text,
          el,
          isOwn: isOwnMessage(text),
          timestamp: Date.now()
        });
      }
    } else {
      [...el.children].forEach(walk);
    }
  };
  walk(container);
  return messages;
}

/**
 * Observes a chat container for new messages using a MutationObserver.
 * Includes deduplication and own-message filtering.
 *
 * @param {object} components - The detected chat components ({ container }).
 * @param {function} callback - Called with { text, el, isOwn, timestamp } for each new message.
 * @param {object} [options] - { includeOwn: boolean } Whether to include own messages (default true).
 * @returns {function|null} A disconnect function to stop observing, or null if no container.
 */
export function chatOnMessage(components, callback, options = {}) {
  const { container } = components;
  if (!container) return null;

  const includeOwn = options.includeOwn !== false;
  const seenHashes = new Set();

  // Seed with existing messages to avoid re-firing
  const existingTexts = [];
  const walkExisting = (el) => {
    const text = el.innerText?.trim();
    if (el.children.length === 0 && text && text.length > 0 && text.length < 1000) {
      existingTexts.push(text);
    } else {
      [...el.children].forEach(walkExisting);
    }
  };
  walkExisting(container);
  existingTexts.forEach(t => seenHashes.add(hashText(t)));

  const observer = new MutationObserver(mutations => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;

        const text = node.innerText?.trim();
        if (!text || text.length === 0 || text.length >= 1000) continue;

        const hash = hashText(text);
        if (seenHashes.has(hash)) continue;
        seenHashes.add(hash);

        const isOwn = isOwnMessage(text);
        if (!includeOwn && isOwn) continue;

        callback({ text, el: node, isOwn, timestamp: Date.now() });
      }
    }
  });

  observer.observe(container, { childList: true, subtree: true });
  return () => observer.disconnect();
}
