// ============================================
// DETECTION PATTERN CONFIGURATION
// ============================================

/**
 * Structural selectors, scoring rules, and semantic checks used by
 * UniversalController.scanStructural(), scoreStructural(), and checkSemantic().
 *
 * Each pattern key maps to:
 *   - selectors: CSS selectors used to find candidate elements in the DOM.
 *   - rules: weighted structural rules for scoring candidates.
 *            Each rule name maps to a numeric weight.
 *   - scanRepeatedChildren: if true, scanStructural will also scan all elements
 *            for repeated child structures (used by chat, feed).
 */
export const PATTERNS = {
  chat: {
    selectors: [
      '[role="log"]',
      '[aria-live]',
      '[class*="message"]',
      '[class*="chat"]',
      '[class*="conversation"]'
    ],
    rules: {
      scrollable: 3,
      'has-input-nearby': 3,
      'repeated-children': 2,
      'aria-live': 2
    },
    scanRepeatedChildren: true
  },

  form: {
    selectors: [
      'form',
      '[role="form"]',
      '[class*="form"]'
    ],
    rules: {
      'form-tag': 4,
      'has-input': 3,
      'has-button': 2
    },
    scanRepeatedChildren: false
  },

  dropdown: {
    selectors: [
      '[aria-haspopup]',
      '[aria-expanded]',
      '[class*="dropdown"]',
      '[class*="select"]'
    ],
    rules: {
      'aria-haspopup': 3,
      'aria-expanded': 2
    },
    scanRepeatedChildren: false
  },

  modal: {
    selectors: [
      '[role="dialog"]',
      '[aria-modal]',
      '[class*="modal"]',
      '[class*="dialog"]',
      '[class*="popup"]'
    ],
    rules: {
      'fixed-position': 3,
      'role-dialog': 3,
      'has-close': 1
    },
    scanRepeatedChildren: false
  },

  login: {
    selectors: [
      '[class*="login"]',
      '[class*="signin"]',
      'form'
    ],
    rules: {
      'has-password': 4,
      'form-tag': 2,
      'has-button': 1
    },
    scanRepeatedChildren: false
  },

  search: {
    selectors: [
      '[class*="search"]',
      '[role="search"]',
      'input[type="search"]'
    ],
    rules: {
      'has-input': 3,
      'search-type': 3
    },
    scanRepeatedChildren: false
  },

  cookie: {
    selectors: [
      '[class*="cookie"]',
      '[class*="consent"]',
      '[class*="gdpr"]'
    ],
    rules: {
      'fixed-position': 2,
      'has-button': 2
    },
    scanRepeatedChildren: false
  },

  feed: {
    selectors: [
      '[class*="feed"]',
      '[class*="timeline"]',
      '[class*="posts"]'
    ],
    rules: {
      scrollable: 3,
      'repeated-children': 4
    },
    scanRepeatedChildren: true
  }
};
