/**
 * FrameRPC - Cross-origin iframe communication via postMessage.
 *
 * Protocol:
 *   Parent → Child: { type: 'UC_CALL', id, method, args }
 *   Child → Parent: { type: 'UC_RESPONSE', id, result, error }
 *   Child → Parent: { type: 'UC_EVENT', event, data }
 *
 * When the userscript runs inside an iframe, it auto-detects whether
 * it's the parent or child and behaves accordingly.
 */

const UC_CALL = 'UC_CALL';
const UC_RESPONSE = 'UC_RESPONSE';
const UC_EVENT = 'UC_EVENT';
const UC_PING = 'UC_PING';
const UC_PONG = 'UC_PONG';

let messageIdCounter = 0;

/**
 * Parent-side RPC: sends calls to child iframes and receives responses.
 */
export class FrameRPCParent {
  constructor(logFn) {
    this.log = logFn || (() => {});
    this.pendingCalls = new Map(); // id -> { resolve, reject, timeout }
    this.eventCallbacks = []; // { event, callback }
    this.knownFrames = new Set(); // contentWindow references that responded to ping

    this._onMessage = this._handleMessage.bind(this);
    window.addEventListener('message', this._onMessage);
  }

  /**
   * Send a method call to a child iframe and await the response.
   *
   * @param {Window} targetWindow - The iframe's contentWindow.
   * @param {string} method - The method to invoke.
   * @param {Array} [args=[]] - Arguments to pass.
   * @param {number} [timeout=5000] - Timeout in ms.
   * @returns {Promise<*>} The result from the child.
   */
  call(targetWindow, method, args = [], timeout = 5000) {
    const id = `rpc_${++messageIdCounter}_${Date.now()}`;

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingCalls.delete(id);
        reject(new Error(`RPC timeout: ${method}`));
      }, timeout);

      this.pendingCalls.set(id, { resolve, reject, timeout: timer });

      try {
        targetWindow.postMessage({ type: UC_CALL, id, method, args }, '*');
      } catch (e) {
        clearTimeout(timer);
        this.pendingCalls.delete(id);
        reject(e);
      }
    });
  }

  /**
   * Ping a child iframe to check if UC is running there.
   *
   * @param {Window} targetWindow
   * @param {number} [timeout=2000]
   * @returns {Promise<boolean>}
   */
  ping(targetWindow, timeout = 2000) {
    const id = `ping_${++messageIdCounter}`;

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pendingCalls.delete(id);
        resolve(false);
      }, timeout);

      this.pendingCalls.set(id, {
        resolve: () => {
          this.knownFrames.add(targetWindow);
          resolve(true);
        },
        reject: () => resolve(false),
        timeout: timer
      });

      try {
        targetWindow.postMessage({ type: UC_PING, id }, '*');
      } catch (e) {
        clearTimeout(timer);
        this.pendingCalls.delete(id);
        resolve(false);
      }
    });
  }

  /**
   * Register a callback for events from child frames.
   *
   * @param {string} event - Event name to listen for.
   * @param {function} callback - Called with (data, sourceWindow).
   */
  onEvent(event, callback) {
    this.eventCallbacks.push({ event, callback });
  }

  _handleMessage(e) {
    const msg = e.data;
    if (!msg || typeof msg !== 'object') return;

    // Response to a call
    if (msg.type === UC_RESPONSE && this.pendingCalls.has(msg.id)) {
      const pending = this.pendingCalls.get(msg.id);
      this.pendingCalls.delete(msg.id);
      clearTimeout(pending.timeout);

      if (msg.error) {
        pending.reject(new Error(msg.error));
      } else {
        pending.resolve(msg.result);
      }
      return;
    }

    // Pong response
    if (msg.type === UC_PONG && this.pendingCalls.has(msg.id)) {
      const pending = this.pendingCalls.get(msg.id);
      this.pendingCalls.delete(msg.id);
      clearTimeout(pending.timeout);
      pending.resolve(true);
      return;
    }

    // Event from child
    if (msg.type === UC_EVENT) {
      for (const { event, callback } of this.eventCallbacks) {
        if (event === msg.event || event === '*') {
          callback(msg.data, e.source);
        }
      }
    }
  }

  /**
   * Clean up event listener.
   */
  destroy() {
    window.removeEventListener('message', this._onMessage);
    for (const [, pending] of this.pendingCalls) {
      clearTimeout(pending.timeout);
    }
    this.pendingCalls.clear();
  }
}

/**
 * Child-side RPC: receives calls from parent and sends responses/events.
 */
export class FrameRPCChild {
  constructor(controller, logFn) {
    this.controller = controller;
    this.log = logFn || (() => {});

    this._onMessage = this._handleMessage.bind(this);
    window.addEventListener('message', this._onMessage);
  }

  _handleMessage(e) {
    const msg = e.data;
    if (!msg || typeof msg !== 'object') return;

    // Ping from parent
    if (msg.type === UC_PING) {
      e.source.postMessage({ type: UC_PONG, id: msg.id }, '*');
      return;
    }

    // Method call from parent
    if (msg.type === UC_CALL) {
      this._handleCall(msg, e.source);
    }
  }

  async _handleCall(msg, source) {
    const { id, method, args } = msg;

    try {
      // Whitelist of allowed methods
      const allowed = [
        'detect', 'firstScan', 'nextScan', 'autoDetect',
        'bind', 'unbind', 'getAPI', 'listBoundAPIs',
        'getAllSignatures', 'getPassiveResults'
      ];

      if (!allowed.includes(method)) {
        source.postMessage({
          type: UC_RESPONSE, id,
          error: `Method not allowed: ${method}`
        }, '*');
        return;
      }

      if (typeof this.controller[method] !== 'function') {
        source.postMessage({
          type: UC_RESPONSE, id,
          error: `Unknown method: ${method}`
        }, '*');
        return;
      }

      let result = this.controller[method](...(args || []));

      // Handle promises
      if (result && typeof result.then === 'function') {
        result = await result;
      }

      // Serialize result (strip DOM elements)
      const serialized = this._serialize(result);

      source.postMessage({
        type: UC_RESPONSE, id,
        result: serialized
      }, '*');

    } catch (e) {
      source.postMessage({
        type: UC_RESPONSE, id,
        error: e.message
      }, '*');
    }
  }

  /**
   * Emit an event to the parent window.
   *
   * @param {string} event - Event name.
   * @param {*} data - Serializable data.
   */
  emit(event, data) {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({
        type: UC_EVENT,
        event,
        data: this._serialize(data)
      }, '*');
    }
  }

  /**
   * Serialize a value, stripping DOM elements and functions.
   *
   * @param {*} value
   * @returns {*}
   */
  _serialize(value) {
    if (value === null || value === undefined) return value;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;

    if (value instanceof Element) {
      return { _type: 'Element', tag: value.tagName, id: value.id };
    }

    if (Array.isArray(value)) {
      return value.map(v => this._serialize(v));
    }

    if (typeof value === 'object') {
      const obj = {};
      for (const [k, v] of Object.entries(value)) {
        if (typeof v !== 'function' && !(v instanceof Element)) {
          obj[k] = this._serialize(v);
        }
      }
      return obj;
    }

    return undefined;
  }

  /**
   * Clean up event listener.
   */
  destroy() {
    window.removeEventListener('message', this._onMessage);
  }
}

/**
 * Detect whether the current context is inside an iframe.
 *
 * @returns {boolean}
 */
export function isInIframe() {
  try {
    return window.self !== window.top;
  } catch (e) {
    return true; // Cross-origin restriction means we're in an iframe
  }
}
