/**
 * api-console.js - API console command parser and executor.
 *
 * Provides a safe, eval-free command parser that supports:
 *   UC.pattern.method(args)
 *   UC.pattern.property
 *   UniversalController.method(args)
 *   UniversalController.property
 */

/**
 * Parse a raw argument string into an array of JS values.
 * Handles strings (single/double quoted), numbers, booleans,
 * null, undefined, and simple JSON-like objects.
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

  for (let i = 0; i < argsStr.length; i++) {
    const char = argsStr[i];

    if (!inString && (char === '"' || char === "'")) {
      inString = true;
      stringChar = char;
    } else if (inString && char === stringChar && argsStr[i - 1] !== '\\') {
      inString = false;
    } else if (!inString && char === '{') {
      braceDepth++;
    } else if (!inString && char === '}') {
      braceDepth--;
    } else if (!inString && braceDepth === 0 && char === ',') {
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
  // String
  if ((str.startsWith("'") && str.endsWith("'")) ||
      (str.startsWith('"') && str.endsWith('"'))) {
    return str.slice(1, -1);
  }
  // Number
  if (!isNaN(str) && str !== '') {
    return Number(str);
  }
  // Boolean
  if (str === 'true') return true;
  if (str === 'false') return false;
  // Null/undefined
  if (str === 'null') return null;
  if (str === 'undefined') return undefined;
  // Object (simple JSON)
  if (str.startsWith('{') && str.endsWith('}')) {
    try {
      // Convert single quotes to double for JSON.parse
      const jsonStr = str.replace(/'/g, '"').replace(/(\w+):/g, '"$1":');
      return JSON.parse(jsonStr);
    } catch (e) {
      return str;
    }
  }
  return str;
}

/**
 * Execute a console command against the controller.
 *
 * Reads from #uc-api-input and writes results to #uc-api-output.
 *
 * @param {string} command - The command string to execute.
 * @param {object} controller - The UniversalController instance.
 * @returns {{ output: string }|undefined} The output text, or undefined on empty input.
 */
export function executeAPI(command, controller) {
  const code = command.trim();
  if (!code) return undefined;

  try {
    let result;

    // UC.pattern.method('arg') or UC.pattern.method()
    const ucMatch = code.match(/^UC\.(\w+)\.(\w+)\((.*)\)$/);
    if (ucMatch) {
      const [, pattern, method, argsStr] = ucMatch;
      const api = controller.getAPI(pattern);
      if (!api) {
        return { output: `Error: UC.${pattern} not bound` };
      }
      if (typeof api[method] !== 'function') {
        return { output: `Error: UC.${pattern}.${method} is not a function` };
      }
      const args = parseArgs(argsStr);
      result = api[method](...args);
      controller.log('success', `Executed: UC.${pattern}.${method}()`);
      return { output: JSON.stringify(result, null, 2) || 'undefined' };
    }

    // UC.pattern.property
    const ucPropMatch = code.match(/^UC\.(\w+)\.(\w+)$/);
    if (ucPropMatch) {
      const [, pattern, prop] = ucPropMatch;
      const api = controller.getAPI(pattern);
      if (!api) {
        return { output: `Error: UC.${pattern} not bound` };
      }
      result = api[prop];
      if (result instanceof Element) {
        return {
          output: `[Element: ${result.tagName}#${result.id || ''}.${result.className?.toString?.().split(' ')[0] || ''}]`
        };
      }
      return { output: JSON.stringify(result, null, 2) || 'undefined' };
    }

    // UniversalController.method()
    const ctrlMatch = code.match(/^UniversalController\.(\w+)\((.*)\)$/);
    if (ctrlMatch) {
      const [, method, argsStr] = ctrlMatch;
      if (typeof controller[method] !== 'function') {
        return { output: `Error: UniversalController.${method} is not a function` };
      }
      const args = parseArgs(argsStr);
      result = controller[method](...args);
      controller.log('success', `Executed: UniversalController.${method}()`);
      return { output: JSON.stringify(result, null, 2) || 'undefined' };
    }

    // UniversalController.property
    const ctrlPropMatch = code.match(/^UniversalController\.(\w+)$/);
    if (ctrlPropMatch) {
      const [, prop] = ctrlPropMatch;
      result = controller[prop];
      return { output: JSON.stringify(result, null, 2) || 'undefined' };
    }

    return {
      output: `Error: Could not parse command. Try:\n  UC.chat.send('hello')\n  UC.chat.getMessages()\n  UC.chat.components\n  UniversalController.listBoundAPIs()`
    };

  } catch (e) {
    controller.log('error', e.message);
    return { output: `Error: ${e.message}` };
  }
}
