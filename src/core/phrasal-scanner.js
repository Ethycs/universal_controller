/**
 * PhrasalScanner - Heuristic text-based UI pattern detector.
 *
 * Scores DOM elements against predefined phrasal patterns to identify
 * common UI components (chat, form, login, search, dropdown, modal,
 * cookie consent, feed) based on text content, placeholders, ARIA
 * labels, and button text.
 */

export class PhrasalScanner {
  constructor() {
    this.patterns = {
      chat: {
        strong: ['send message', 'send a message', 'type a message', 'write a message', 'reply'],
        medium: ['chat', 'message', 'conversation', 'dm', 'direct message'],
        placeholders: ['type here', 'write something', 'enter message', 'say something'],
        buttons: ['send', 'reply', 'post'],
        negative: ['email', 'subscribe', 'newsletter', 'search']
      },
      form: {
        strong: ['submit', 'sign up', 'register', 'create account', 'subscribe'],
        medium: ['email', 'password', 'username', 'name', 'phone', 'address'],
        labels: ['required', 'optional', 'invalid', 'error'],
        buttons: ['submit', 'send', 'continue', 'next', 'save'],
        negative: ['search', 'filter']
      },
      login: {
        strong: ['sign in', 'log in', 'login', 'forgot password', 'remember me'],
        medium: ['username', 'email', 'password'],
        buttons: ['sign in', 'log in', 'login'],
        negative: ['create account', 'sign up', 'register']
      },
      search: {
        strong: ['search'],
        medium: ['find', 'look up', 'filter'],
        placeholders: ['search', 'search...', 'find'],
        buttons: ['search', 'find', 'go'],
        negative: ['message', 'chat', 'password']
      },
      dropdown: {
        strong: ['select', 'choose', 'pick one'],
        medium: ['option', 'select an option'],
        negative: []
      },
      modal: {
        strong: ['close', 'dismiss'],
        medium: ['cancel', 'confirm', 'ok', 'done'],
        buttons: ['close', 'cancel', 'ok', 'confirm', 'done', '\u00d7'],
        negative: []
      },
      cookie: {
        strong: ['accept cookies', 'cookie policy', 'we use cookies', 'cookie consent'],
        medium: ['privacy', 'gdpr', 'consent', 'preferences'],
        buttons: ['accept', 'accept all', 'reject', 'manage'],
        negative: []
      },
      feed: {
        strong: ['load more', 'show more'],
        medium: ['posts', 'feed', 'timeline', 'updates'],
        negative: []
      }
    };
  }

  extractText(el) {
    const texts = {
      innerText: (el.innerText || '').toLowerCase().slice(0, 1000),
      placeholder: (el.placeholder || '').toLowerCase(),
      ariaLabel: (el.getAttribute('aria-label') || '').toLowerCase(),
      buttons: [],
      inputs: [],
      labels: []
    };

    el.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(btn => {
      texts.buttons.push((btn.innerText || btn.value || '').toLowerCase());
    });

    el.querySelectorAll('input, textarea').forEach(input => {
      texts.inputs.push({
        placeholder: (input.placeholder || '').toLowerCase(),
        ariaLabel: (input.getAttribute('aria-label') || '').toLowerCase(),
        name: (input.name || '').toLowerCase(),
        type: input.type || ''
      });

      if (input.id) {
        const label = document.querySelector(`label[for="${input.id}"]`);
        if (label) texts.labels.push(label.innerText.toLowerCase());
      }
    });

    return texts;
  }

  score(el, patternName) {
    const pattern = this.patterns[patternName];
    if (!pattern) return { score: 0, matches: [] };

    const texts = this.extractText(el);
    const allText = [
      texts.innerText,
      texts.placeholder,
      texts.ariaLabel,
      ...texts.buttons,
      ...texts.labels,
      ...texts.inputs.map(i => `${i.placeholder} ${i.ariaLabel} ${i.name}`)
    ].join(' ');

    let score = 0;
    const matches = [];

    // Strong signals
    pattern.strong?.forEach(phrase => {
      if (allText.includes(phrase)) {
        score += 0.35;
        matches.push({ phrase, strength: 'strong' });
      }
    });

    // Medium signals
    pattern.medium?.forEach(phrase => {
      if (allText.includes(phrase)) {
        score += 0.15;
        matches.push({ phrase, strength: 'medium' });
      }
    });

    // Placeholder patterns
    pattern.placeholders?.forEach(phrase => {
      const hasPlaceholder = texts.inputs.some(i =>
        i.placeholder.includes(phrase)
      ) || texts.placeholder.includes(phrase);

      if (hasPlaceholder) {
        score += 0.25;
        matches.push({ phrase, strength: 'placeholder' });
      }
    });

    // Button patterns
    pattern.buttons?.forEach(phrase => {
      if (texts.buttons.some(b => b.includes(phrase))) {
        score += 0.2;
        matches.push({ phrase, strength: 'button' });
      }
    });

    // Negative signals
    pattern.negative?.forEach(phrase => {
      if (allText.includes(phrase)) {
        score -= 0.25;
        matches.push({ phrase, strength: 'negative' });
      }
    });

    score = Math.max(0, Math.min(1, score));

    return { score, matches };
  }
}
