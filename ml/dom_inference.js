/**
 * Vanilla JS forward pass for the DOM raster MLP classifier.
 *
 * Implements: StandardScaler → Dense(128,relu) → Dense(64,relu) → Dense(8,softmax)
 * using raw weight arrays exported by scripts/train_dom_classifier.py.
 *
 * No TensorFlow.js. ~530K params, <1ms inference on a 4096-element input.
 */

/**
 * StandardScaler: x = (x - mean) / scale
 */
function scaleInput(x, mean, scale) {
    const out = new Float32Array(x.length);
    for (let i = 0; i < x.length; i++)
        out[i] = (x[i] - mean[i]) / (scale[i] || 1);
    return out;
}

/**
 * PCA transform: x_pca = (x - mean) @ components.T
 * @param {Float32Array} x - [n_features]
 * @param {number[]} mean - [n_features]
 * @param {number[][]} components - [n_components][n_features]
 * @returns {Float32Array} - [n_components]
 */
function pcaTransform(x, mean, components) {
    const n = components.length;
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
        let sum = 0;
        const row = components[i];
        for (let j = 0; j < x.length; j++)
            sum += (x[j] - mean[j]) * row[j];
        out[i] = sum;
    }
    return out;
}

/**
 * Dense layer: y = x @ W + b, with optional ReLU or softmax.
 */
function dense(x, kernel, bias, activation) {
    const outSize = bias.length;
    const out = new Float32Array(outSize);
    for (let j = 0; j < outSize; j++) {
        let sum = bias[j];
        for (let i = 0; i < x.length; i++)
            sum += x[i] * kernel[i][j];
        out[j] = (activation === 'relu' && sum < 0) ? 0 : sum;
    }
    if (activation === 'softmax') {
        const max = Math.max(...out);
        let expSum = 0;
        for (let i = 0; i < out.length; i++) { out[i] = Math.exp(out[i] - max); expSum += out[i]; }
        for (let i = 0; i < out.length; i++) out[i] /= expSum;
    }
    return out;
}

/**
 * Run full inference: Scaler → PCA → Dense(relu) → Dense(relu) → Dense(softmax)
 *
 * @param {number[]} grid - flat raster (e.g. 4096 elements)
 * @param {object} weights - from weights.json: {layers: [{type, ...}]}
 * @param {string[]} labels - class names
 * @returns {{label: string, confidence: number, scores: Object}}
 */
function classify(grid, weights, labels) {
    let x = grid;

    for (const layer of weights.layers) {
        if (layer.type === 'Scaler')
            x = scaleInput(x, layer.mean, layer.scale);
        else if (layer.type === 'PCA')
            x = pcaTransform(x, layer.mean, layer.components);
        else if (layer.type === 'Dense')
            x = dense(x, layer.kernel, layer.bias, layer.activation || 'relu');
    }

    let bestIdx = 0;
    for (let i = 1; i < x.length; i++)
        if (x[i] > x[bestIdx]) bestIdx = i;

    const scores = {};
    for (let i = 0; i < labels.length; i++)
        scores[labels[i]] = Math.round(x[i] * 1000) / 1000;

    return {
        label: labels[bestIdx] || 'unknown',
        confidence: Math.round(x[bestIdx] * 1000) / 1000,
        scores,
    };
}

if (typeof module !== 'undefined') module.exports = { classify, scaleInput, pcaTransform, dense };
