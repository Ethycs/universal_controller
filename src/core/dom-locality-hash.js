/**
 * DOMLocalityHash - Locality-sensitive hashing for DOM subtrees.
 *
 * Extracts structural features from a DOM element and its descendants,
 * then computes a MinHash signature over shingled feature sequences.
 * Produces a compact fingerprint that can be used to compare structural
 * similarity between DOM subtrees.
 */

export class DOMLocalityHash {
  constructor() {
    this.shingleSize = 3;
    this.numHashes = 64;
    this.seeds = Array.from({ length: this.numHashes }, (_, i) => i * 0x9e3779b9);
  }

  extractFeatures(el) {
    const features = [];

    const walk = (node, depth = 0) => {
      if (node.nodeType !== 1 || depth > 6) return;

      features.push(`tag:${node.tagName}`);
      features.push(`depth:${Math.min(depth, 10)}`);
      features.push(`children:${this.bucketCount(node.children.length)}`);

      try {
        const style = getComputedStyle(node);
        if (node.scrollHeight > node.clientHeight &&
            ['auto', 'scroll'].includes(style.overflowY)) {
          features.push('scrollable');
        }
        if (style.position === 'fixed') features.push('fixed');
      } catch (e) {}

      if (node.querySelector('input,textarea')) features.push('has-input');
      if (node.querySelector('button')) features.push('has-button');
      if (node.getAttribute('role')) features.push(`role:${node.getAttribute('role')}`);
      if (node.getAttribute('aria-live')) features.push('aria-live');
      if (node.getAttribute('aria-haspopup')) features.push('aria-haspopup');

      const childTags = [...node.children].slice(0, 5).map(c => c.tagName).join(',');
      if (childTags) features.push(`shape:${childTags}`);

      const childTagCounts = {};
      [...node.children].forEach(c => {
        childTagCounts[c.tagName] = (childTagCounts[c.tagName] || 0) + 1;
      });
      const maxRepeat = Math.max(...Object.values(childTagCounts), 0);
      if (maxRepeat > 2) features.push(`repeat:${this.bucketCount(maxRepeat)}`);

      [...node.children].forEach(c => walk(c, depth + 1));
    };

    walk(el);
    return features;
  }

  bucketCount(n) {
    if (n === 0) return '0';
    if (n === 1) return '1';
    if (n <= 3) return '2-3';
    if (n <= 10) return '4-10';
    return '10+';
  }

  hash32(str) {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return h >>> 0;
  }

  signature(el) {
    const features = this.extractFeatures(el);
    const shingles = new Set();

    for (let i = 0; i <= features.length - this.shingleSize; i++) {
      shingles.add(features.slice(i, i + this.shingleSize).join('|'));
    }

    const minhash = new Uint32Array(this.numHashes).fill(0xFFFFFFFF);
    for (const shingle of shingles) {
      const h = this.hash32(shingle);
      for (let i = 0; i < this.numHashes; i++) {
        const permuted = (h ^ this.seeds[i]) >>> 0;
        if (permuted < minhash[i]) minhash[i] = permuted;
      }
    }

    return {
      features,
      fingerprint: Array.from(minhash.slice(0, 8)).map(h => h.toString(16).padStart(8, '0')).join('')
    };
  }
}
