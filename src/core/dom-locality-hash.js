/**
 * DOMLocalityHash - Locality-sensitive hashing for DOM subtrees.
 *
 * Extracts structural features from a DOM element and its descendants,
 * then computes a MinHash signature over shingled feature sequences.
 * Produces a compact fingerprint for comparing structural similarity
 * between DOM subtrees.
 *
 * Supports band-based LSH bucketing for fast candidate retrieval.
 */

export class DOMLocalityHash {
  constructor(options = {}) {
    this.shingleSize = options.shingleSize || 3;
    this.numHashes = options.numHashes || 64;
    this.numBands = options.numBands || 16;
    this.rowsPerBand = this.numHashes / this.numBands;
    this.seeds = Array.from({ length: this.numHashes }, (_, i) => i * 0x9e3779b9);

    // Band-based LSH buckets: Map<bandIndex, Map<bucketHash, Set<fingerprint>>>
    this.buckets = new Map();
    for (let b = 0; b < this.numBands; b++) {
      this.buckets.set(b, new Map());
    }

    // Store all indexed signatures for retrieval
    this.index = new Map(); // fingerprint -> { signature, metadata }
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

  /**
   * Compute a MinHash signature from a set of shingles.
   *
   * @param {Set<string>} shingles
   * @returns {Uint32Array} The MinHash array of length numHashes.
   */
  minhash(shingles) {
    const mh = new Uint32Array(this.numHashes).fill(0xFFFFFFFF);
    for (const shingle of shingles) {
      const h = this.hash32(shingle);
      for (let i = 0; i < this.numHashes; i++) {
        const permuted = (h ^ this.seeds[i]) >>> 0;
        if (permuted < mh[i]) mh[i] = permuted;
      }
    }
    return mh;
  }

  /**
   * Generate a full signature for a DOM element.
   * Uses all 64 hashes for the fingerprint.
   *
   * @param {HTMLElement} el
   * @returns {{ features: string[], minhash: Uint32Array, fingerprint: string }}
   */
  signature(el) {
    const features = this.extractFeatures(el);
    const shingles = new Set();

    for (let i = 0; i <= features.length - this.shingleSize; i++) {
      shingles.add(features.slice(i, i + this.shingleSize).join('|'));
    }

    const mh = this.minhash(shingles);

    // Use ALL 64 hashes for the fingerprint (was previously sliced to 8)
    const fingerprint = Array.from(mh).map(h => h.toString(16).padStart(8, '0')).join('');

    return { features, minhash: mh, fingerprint };
  }

  /**
   * Estimate Jaccard similarity between two MinHash signatures.
   *
   * @param {{ minhash: Uint32Array }} sig1
   * @param {{ minhash: Uint32Array }} sig2
   * @returns {number} Estimated Jaccard similarity in [0, 1].
   */
  similarity(sig1, sig2) {
    if (!sig1?.minhash || !sig2?.minhash) return 0;

    let agree = 0;
    const len = Math.min(sig1.minhash.length, sig2.minhash.length);
    for (let i = 0; i < len; i++) {
      if (sig1.minhash[i] === sig2.minhash[i]) agree++;
    }
    return agree / len;
  }

  /**
   * Compute a band hash for a specific band of the MinHash signature.
   *
   * @param {Uint32Array} mh - The full MinHash array.
   * @param {number} bandIndex
   * @returns {number}
   */
  bandHash(mh, bandIndex) {
    const start = bandIndex * this.rowsPerBand;
    let h = 0x811c9dc5;
    for (let i = start; i < start + this.rowsPerBand; i++) {
      h ^= mh[i];
      h = Math.imul(h, 0x01000193);
    }
    return h >>> 0;
  }

  /**
   * Add a signature to the LSH index for fast candidate retrieval.
   *
   * @param {string} key - A unique identifier (e.g., element path or site+path).
   * @param {{ minhash: Uint32Array, fingerprint: string }} sig - The signature to index.
   * @param {object} [metadata] - Optional metadata to store alongside the signature.
   */
  addToIndex(key, sig, metadata = {}) {
    this.index.set(sig.fingerprint, { signature: sig, key, metadata });

    for (let b = 0; b < this.numBands; b++) {
      const bh = this.bandHash(sig.minhash, b);
      const band = this.buckets.get(b);
      if (!band.has(bh)) band.set(bh, new Set());
      band.get(bh).add(sig.fingerprint);
    }
  }

  /**
   * Remove a signature from the LSH index.
   *
   * @param {string} fingerprint - The fingerprint of the signature to remove.
   */
  removeFromIndex(fingerprint) {
    const entry = this.index.get(fingerprint);
    if (!entry) return;

    for (let b = 0; b < this.numBands; b++) {
      const bh = this.bandHash(entry.signature.minhash, b);
      const band = this.buckets.get(b);
      const bucket = band.get(bh);
      if (bucket) {
        bucket.delete(fingerprint);
        if (bucket.size === 0) band.delete(bh);
      }
    }

    this.index.delete(fingerprint);
  }

  /**
   * Query the LSH index for candidate similar signatures.
   * Returns candidates that share at least one band hash.
   *
   * @param {{ minhash: Uint32Array }} sig - The query signature.
   * @returns {Array<{ key: string, fingerprint: string, similarity: number, metadata: object }>}
   */
  querySimilar(sig) {
    const candidateFingerprints = new Set();

    for (let b = 0; b < this.numBands; b++) {
      const bh = this.bandHash(sig.minhash, b);
      const band = this.buckets.get(b);
      const bucket = band.get(bh);
      if (bucket) {
        for (const fp of bucket) {
          candidateFingerprints.add(fp);
        }
      }
    }

    const results = [];
    for (const fp of candidateFingerprints) {
      const entry = this.index.get(fp);
      if (!entry) continue;

      const sim = this.similarity(sig, entry.signature);
      results.push({
        key: entry.key,
        fingerprint: fp,
        similarity: sim,
        metadata: entry.metadata
      });
    }

    return results.sort((a, b) => b.similarity - a.similarity);
  }

  /**
   * Clear the entire LSH index.
   */
  clearIndex() {
    this.index.clear();
    for (let b = 0; b < this.numBands; b++) {
      this.buckets.get(b).clear();
    }
  }
}
