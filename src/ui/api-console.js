/**
 * api-console.js - API console command parser and executor.
 *
 * Provides a safe, eval-free command parser that supports:
 *   UC.pattern.method(args)
 *   UC.pattern.property
 *   UC.pattern.property.nested
 *   UniversalController.method(args)
 *   UniversalController.property
 *   await UC.chat.send('hello')
 *
 * Walks the object graph directly — no eval().
 */

/**
 * Parse a raw argument string into an array of JS values.
 * Handles strings (single/double/backtick quoted), numbers, booleans,
 * null, undefined, simple JSON-like objects, and arrays.
 *
 * @param {string} argsStr - The raw arguments portion inside parentheses.
 * @returns {Array} Parsed argument values.
 */
export function parseArgs(argsStr) {
  if (!argsStr || argsStr.trim() === '') return [];

  const args = [];
  let current = '';
  let inString = false;
  let stringChar = '';
  let braceDepth = 0;
  let bracketDepth = 0;

  for (let i = 0; i < argsStr.length; i++) {
    const char = argsStr[i];

    if (!inString && (char === '"' || char === "'" || char === '`')) {
      inString = true;
      stringChar = char;
      current += char;
      continue;
    }

    if (inString && char === stringChar && argsStr[i - 1] !== '\\') {
      inString = false;
      current += char;
      continue;
    }

    if (inString) {
      current += char;
      continue;
    }

    if (char === '{') braceDepth++;
    if (char === '}') braceDepth--;
    if (char === '[') bracketDepth++;
    if (char === ']') bracketDepth--;

    if (braceDepth === 0 && bracketDepth === 0 && char === ',') {
      args.push(parseValue(current.trim()));
      current = '';
      continue;
    }

    current += char;
  }

  if (current.trim()) {
    args.push(parseValue(current.trim()));
  }

  return args;
}

/**
 * Parse a single value token into a JS primitive or object.
 *
 * @param {string} str - A trimmed token string.
 * @returns {*} The parsed value.
 */
export function parseValue(str) {
  // String (single, double, or backtick quoted)
  if ((str.startsWith("'") && str.endsWith("'")) ||
      (str.startsWith('"') && str.endsWith('"')) ||
      (str.startsWith('`') && str.endsWith('`'))) {
    return str.slice(1, -1);
  }
  // Number
  if (!isNaN(str) && str !== '') return Number(str);
  // Boolean
  if (str === 'true') return true;
  if (str === 'false') return false;
  // Null/undefined
  if (str === 'null') return null;
  if (str === 'undefined') return undefined;
  // Array
  if (str.startsWith('[') && str.endsWith(']')) {
    try {
      return JSON.parse(str.replace(/'/g, '"'));
    } catch (e) {
      return str;
    }
  }
  // Object (simple JSON)
  if (str.startsWith('{') && str.endsWith('}')) {
    try {
      // Convert single quotes to double, unquoted keys to quoted
      const jsonStr = str.replace(/'/g, '"').replace(/(\w+)\s*:/g, '"$1":');
      return JSON.parse(jsonStr);
    } catch (e) {
      return str;
    }
  }
  return str;
}

/**
 * Parse a command string into its component parts.
 *
 * Supports:
 *   root.chain.of.properties
 *   root.chain.method(args)
 *   await root.chain.method(args)
 *
 * @param {string} command
 * @returns {{ isAwait: boolean, root: string, chain: string[], methodCall: { name: string, args: string } | null }}
 */
function parseCommand(command) {
  let code = command.trim();

  // Handle await prefix
  const isAwait = code.startsWith('await ');
  if (isAwait) code = code.slice(6).trim();

  // Find the last method call: method(args) at the end
  // Use a careful approach to find matching parens
  const lastOpenParen = findLastMethodCall(code);
  if (lastOpenParen !== -1) {
    const prefix = code.slice(0, lastOpenParen);
    const argsStr = code.slice(lastOpenParen + 1, -1); // strip trailing )

    const lastDot = prefix.lastIndexOf('.');
    if (lastDot !== -1) {
      const objPath = prefix.slice(0, lastDot);
      const method = prefix.slice(lastDot + 1);
      const parts = objPath.split('.');
      return { isAwait, root: parts[0], chain: parts.slice(1), methodCall: { name: method, args: argsStr } };
    }
  }

  // Property access: UC.chat.components.input
  const parts = code.split('.');
  return { isAwait, root: parts[0], chain: parts.slice(1), methodCall: null };
}

/**
 * Find the index of the opening paren for the last method call in a command string.
 * Ensures the closing paren is at the end of the string.
 *
 * @param {string} code
 * @returns {number} Index of opening paren, or -1 if no method call found.
 */
function findLastMethodCall(code) {
  if (!code.endsWith(')')) return -1;

  let depth = 0;
  for (let i = code.length - 1; i >= 0; i--) {
    if (code[i] === ')') depth++;
    if (code[i] === '(') {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/**
 * Walk an object graph following a chain of property names.
 *
 * @param {object} obj - Starting object.
 * @param {string[]} chain - Property names to traverse.
 * @param {object} controller - The controller (for UC.pattern resolution).
 * @returns {{ value: *, error?: string }}
 */
function walkChain(obj, chain, controller) {
  let current = obj;

  for (let i = 0; i < chain.length; i++) {
    const prop = chain[i];

    // Special handling for UC proxy: first property is the pattern name
    if (current?._isUCProxy && i === 0) {
      const api = controller.getAPI(prop);
      if (!api) return { value: undefined, error: `UC.${prop} not bound` };
      current = api;
      continue;
    }

    if (current === null || current === undefined) {
      return { value: undefined, error: `Cannot read property '${prop}' of ${current}` };
    }

    current = current[prop];
  }

  return { value: current };
}

/**
 * Format a result value for display in the console.
 *
 * @param {*} value
 * @returns {string}
 */
function formatResult(value) {
  if (value === undefined) return 'undefined';
  if (value === null) return 'null';

  if (value instanceof Element) {
    const id = value.id ? `#${value.id}` : '';
    const cls = value.className?.toString?.().split(' ')[0];
    return `[Element: ${value.tagName}${id}${cls ? '.' + cls : ''}]`;
  }

  if (value instanceof NodeList || value instanceof HTMLCollection) {
    return `[NodeList: ${value.length} elements]`;
  }

  if (typeof value === 'function') {
    return `[Function: ${value.name || 'anonymous'}]`;
  }

  try {
    const json = JSON.stringify(value, (key, val) => {
      if (val instanceof Element) {
        return `[Element: ${val.tagName}#${val.id || ''}]`;
      }
      if (typeof val === 'function') {
        return `[Function: ${val.name || 'anonymous'}]`;
      }
      return val;
    }, 2);
    return json || 'undefined';
  } catch (e) {
    return String(value);
  }
}

/**
 * Execute a console command against the controller.
 * Supports property chains, method calls, and `await` for Promises.
 *
 * @param {string} command - The command string to execute.
 * @param {object} controller - The UniversalController instance.
 * @returns {Promise<{ output: string }>|{ output: string }|undefined}
 */
export function executeAPI(command, controller) {
  const code = command.trim();
  if (!code) return undefined;

  try {
    const parsed = parseCommand(code);

    // Resolve root object
    let rootObj;
    if (parsed.root === 'UniversalController') {
      rootObj = controller;
    } else if (parsed.root === 'UC') {
      rootObj = { _isUCProxy: true, controller };
    } else {
      return {
        output: `Error: Unknown root '${parsed.root}'. Use UC.* or UniversalController.*\n` +
                `Examples:\n  UC.chat.send('hello')\n  UC.chat.getMessages()\n  UC.chat.components\n` +
                `  UniversalController.detect('chat')\n  UniversalController.stats\n` +
                `  UniversalController.listBoundAPIs()`
      };
    }

    if (parsed.methodCall) {
      // Method call: walk to the parent, then call the method
      const parent = walkChain(rootObj, parsed.chain, controller);
      if (parent.error) return { output: `Error: ${parent.error}` };

      const target = parent.value;

      // For UC proxy at root level with no chain, the method is on the pattern API
      if (target?._isUCProxy) {
        return { output: `Error: UC.${parsed.methodCall.name} - specify a pattern first (e.g., UC.chat.${parsed.methodCall.name}())` };
      }

      if (target === null || target === undefined) {
        return { output: `Error: Cannot call '${parsed.methodCall.name}' on ${target}` };
      }

      const fn = target[parsed.methodCall.name];
      if (typeof fn !== 'function') {
        return { output: `Error: '${parsed.methodCall.name}' is not a function` };
      }

      const args = parseArgs(parsed.methodCall.args);
      const result = fn.apply(target, args);

      controller.log('success', `Executed: ${code}`);

      // Handle Promise results with await
      if (parsed.isAwait && result && typeof result.then === 'function') {
        return result.then(
          resolved => ({ output: formatResult(resolved) }),
          err => ({ output: `Error: ${err.message}` })
        );
      }

      return { output: formatResult(result) };
    }

    // Property access
    const result = walkChain(rootObj, parsed.chain, controller);
    if (result.error) return { output: `Error: ${result.error}` };

    // Handle UC proxy with no chain (just "UC")
    if (result.value?._isUCProxy) {
      const apis = controller.listBoundAPIs();
      if (apis.length === 0) {
        return { output: 'UC: No APIs bound. Run detect() and bind() first.' };
      }
      return { output: `UC: Bound APIs: ${apis.map(a => a.pattern).join(', ')}` };
    }

    return { output: formatResult(result.value) };

  } catch (e) {
    controller.log('error', e.message);
    return { output: `Error: ${e.message}` };
  }
}
