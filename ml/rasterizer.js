/**
 * DOM bounding-box rasterizer.
 *
 * Converts a subtree of the DOM into a small spatial feature map by
 * projecting element bounding rects onto a grid. Each cell encodes what
 * kinds of elements occupy that region.
 *
 * Output: Float32Array of shape [gridSize, gridSize, channels]
 *   Channel 0: interactive elements (input, textarea, button, [contenteditable])
 *   Channel 1: text density (normalized character count)
 *   Channel 2: iframe / embedded content
 *   Channel 3: z-index / overlay layer (fixed/sticky positioning)
 *
 * Usage from Playwright:
 *   const raster = page.evaluate(rasterizeJS, { selector: '#my-widget', gridSize: 32 });
 */
(args) => {
    const { selector, gridSize = 32, viewport = false } = args || {};
    const channels = 4;
    const grid = new Array(gridSize * gridSize * channels).fill(0);

    // Determine the bounding region to rasterize
    let root, bounds;
    if (viewport || !selector) {
        root = document.body;
        bounds = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    } else {
        root = document.querySelector(selector);
        if (!root) return null;
        const r = root.getBoundingClientRect();
        bounds = { left: r.left, top: r.top, width: r.width, height: r.height };
    }

    if (bounds.width === 0 || bounds.height === 0) return null;

    const scaleX = gridSize / bounds.width;
    const scaleY = gridSize / bounds.height;

    // Interactive element tags/attributes
    const INTERACTIVE_TAGS = new Set([
        'INPUT', 'TEXTAREA', 'BUTTON', 'SELECT', 'A',
    ]);

    function fillRect(gx1, gy1, gx2, gy2, channel, value) {
        const x1 = Math.max(0, Math.min(gridSize - 1, gx1));
        const y1 = Math.max(0, Math.min(gridSize - 1, gy1));
        const x2 = Math.max(0, Math.min(gridSize - 1, gx2));
        const y2 = Math.max(0, Math.min(gridSize - 1, gy2));
        for (let y = y1; y <= y2; y++) {
            for (let x = x1; x <= x2; x++) {
                const idx = (y * gridSize + x) * channels + channel;
                grid[idx] = Math.max(grid[idx], value);
            }
        }
    }

    // Walk all descendants
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
        const rect = node.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
            const gx1 = Math.floor((rect.left - bounds.left) * scaleX);
            const gy1 = Math.floor((rect.top - bounds.top) * scaleY);
            const gx2 = Math.floor((rect.right - bounds.left) * scaleX);
            const gy2 = Math.floor((rect.bottom - bounds.top) * scaleY);

            // Channel 0: Interactive elements
            const isInteractive = INTERACTIVE_TAGS.has(node.tagName)
                || node.contentEditable === 'true'
                || node.getAttribute('role') === 'textbox'
                || node.getAttribute('role') === 'button'
                || node.getAttribute('role') === 'combobox';
            if (isInteractive) {
                fillRect(gx1, gy1, gx2, gy2, 0, 1.0);
            }

            // Channel 1: Text density
            const textLen = (node.innerText || '').length;
            if (textLen > 0) {
                const density = Math.min(textLen / 500, 1.0);
                fillRect(gx1, gy1, gx2, gy2, 1, density);
            }

            // Channel 2: Iframes / embedded content
            if (node.tagName === 'IFRAME' || node.tagName === 'EMBED' || node.tagName === 'OBJECT') {
                fillRect(gx1, gy1, gx2, gy2, 2, 1.0);
            }

            // Channel 3: Overlay / fixed positioning
            const style = getComputedStyle(node);
            if (style.position === 'fixed' || style.position === 'sticky') {
                fillRect(gx1, gy1, gx2, gy2, 3, 1.0);
            } else {
                const z = parseInt(style.zIndex);
                if (z > 100) {
                    fillRect(gx1, gy1, gx2, gy2, 3, Math.min(z / 10000, 1.0));
                }
            }
        }
        node = walker.nextNode();
    }

    // Also extract accessibility features as a separate metadata object
    const a11y = {
        roles: [],
        ariaLabels: [],
        hasLiveRegion: false,
    };
    root.querySelectorAll('[role]').forEach(el => {
        const role = el.getAttribute('role');
        if (role && !a11y.roles.includes(role)) a11y.roles.push(role);
    });
    root.querySelectorAll('[aria-label]').forEach(el => {
        const label = el.getAttribute('aria-label').toLowerCase();
        if (!a11y.ariaLabels.includes(label)) a11y.ariaLabels.push(label);
    });
    if (root.querySelector('[aria-live]')) {
        a11y.hasLiveRegion = true;
    }

    return { grid, gridSize, channels, a11y };
}
