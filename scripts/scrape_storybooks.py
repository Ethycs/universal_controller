"""Scrape design library Storybook instances to build training data.

For each component story:
  1. Navigate to the story iframe
  2. Rasterize the rendered component (32x32x4 bounding-box grid)
  3. Extract accessibility features
  4. Label by Storybook category (Input, Search, Chat, Form, Modal, etc.)

Output: data/training/storybook_samples.json
  [{grid: [...], a11y: {...}, label: "search", source: "mui", story: "..."}]

Usage:
  pixi run python scripts/scrape_storybooks.py
  pixi run python scripts/scrape_storybooks.py --headless
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("scrape_storybooks")

# ── Storybook target registry ────────────────────────────────────────

# Each entry: {url, name, stories: [{id, label}]}
# Story IDs follow Storybook's URL convention: /iframe.html?id=STORY_ID
# Labels are our classification targets.

STORYBOOK_TARGETS = [
    {
        "name": "mui",
        "url": "https://mui.com/material-ui/react-text-field/",
        "iframe_base": "https://mui.com/material-ui/",
        "stories": [
            # Inputs
            {"path": "react-text-field/#basic-textfield", "label": "form_field"},
            {"path": "react-text-field/#select", "label": "form_field"},
            {"path": "react-autocomplete/", "label": "search"},
            # Buttons (negatives)
            {"path": "react-button/", "label": "button"},
            {"path": "react-button-group/", "label": "button"},
            # Navigation (negatives)
            {"path": "react-tabs/", "label": "navigation"},
            {"path": "react-breadcrumbs/", "label": "navigation"},
            # Modals
            {"path": "react-dialog/", "label": "modal"},
            {"path": "react-modal/", "label": "modal"},
        ],
    },
    {
        "name": "antd",
        "url": "https://ant.design/components/input",
        "stories": [
            {"path": "/components/input", "label": "form_field"},
            {"path": "/components/input-number", "label": "form_field"},
            {"path": "/components/auto-complete", "label": "search"},
            {"path": "/components/mentions", "label": "chat_input"},
            {"path": "/components/select", "label": "form_field"},
            # Negatives
            {"path": "/components/button", "label": "button"},
            {"path": "/components/table", "label": "data_display"},
            {"path": "/components/card", "label": "data_display"},
            {"path": "/components/modal", "label": "modal"},
        ],
    },
    {
        "name": "chakra",
        "url": "https://v2.chakra-ui.com/docs/components/input",
        "stories": [
            {"path": "/docs/components/input", "label": "form_field"},
            {"path": "/docs/components/textarea", "label": "form_field"},
            {"path": "/docs/components/number-input", "label": "form_field"},
            {"path": "/docs/components/pin-input", "label": "form_field"},
            # Negatives
            {"path": "/docs/components/button", "label": "button"},
            {"path": "/docs/components/tabs", "label": "navigation"},
            {"path": "/docs/components/modal", "label": "modal"},
        ],
    },
    {
        "name": "bootstrap",
        "url": "https://getbootstrap.com/docs/5.3/forms/overview/",
        "stories": [
            # Inputs
            {"path": "/docs/5.3/forms/form-control/", "label": "form_field"},
            {"path": "/docs/5.3/forms/select/", "label": "form_field"},
            {"path": "/docs/5.3/forms/input-group/", "label": "form_field"},
            {"path": "/docs/5.3/forms/floating-labels/", "label": "form_field"},
            # Search (navbar search)
            {"path": "/docs/5.3/components/navbar/", "label": "search"},
            # Modals
            {"path": "/docs/5.3/components/modal/", "label": "modal"},
            {"path": "/docs/5.3/components/offcanvas/", "label": "modal"},
            # Navigation
            {"path": "/docs/5.3/components/navs-tabs/", "label": "navigation"},
            {"path": "/docs/5.3/components/breadcrumb/", "label": "navigation"},
            {"path": "/docs/5.3/components/pagination/", "label": "navigation"},
            # Buttons (negatives)
            {"path": "/docs/5.3/components/buttons/", "label": "button"},
            {"path": "/docs/5.3/components/button-group/", "label": "button"},
            # Data display (negatives)
            {"path": "/docs/5.3/content/tables/", "label": "data_display"},
            {"path": "/docs/5.3/components/card/", "label": "data_display"},
            {"path": "/docs/5.3/components/list-group/", "label": "data_display"},
            {"path": "/docs/5.3/components/accordion/", "label": "data_display"},
        ],
    },
    {
        "name": "mantine",
        "url": "https://mantine.dev/core/text-input/",
        "stories": [
            # Inputs
            {"path": "/core/text-input/", "label": "form_field"},
            {"path": "/core/textarea/", "label": "form_field"},
            {"path": "/core/number-input/", "label": "form_field"},
            {"path": "/core/password-input/", "label": "form_field"},
            {"path": "/core/select/", "label": "form_field"},
            {"path": "/core/autocomplete/", "label": "search"},
            {"path": "/core/combobox/", "label": "search"},
            # Modals
            {"path": "/core/modal/", "label": "modal"},
            {"path": "/core/drawer/", "label": "modal"},
            # Navigation
            {"path": "/core/tabs/", "label": "navigation"},
            {"path": "/core/breadcrumbs/", "label": "navigation"},
            {"path": "/core/stepper/", "label": "navigation"},
            {"path": "/core/pagination/", "label": "navigation"},
            # Buttons (negatives)
            {"path": "/core/button/", "label": "button"},
            {"path": "/core/action-icon/", "label": "button"},
            # Data display (negatives)
            {"path": "/core/table/", "label": "data_display"},
            {"path": "/core/card/", "label": "data_display"},
            {"path": "/core/accordion/", "label": "data_display"},
        ],
    },
    {
        "name": "radix",
        "url": "https://www.radix-ui.com/themes/docs/components/text-field",
        "stories": [
            # Inputs
            {"path": "/themes/docs/components/text-field", "label": "form_field"},
            {"path": "/themes/docs/components/text-area", "label": "form_field"},
            {"path": "/themes/docs/components/select", "label": "form_field"},
            {"path": "/themes/docs/components/checkbox", "label": "form_field"},
            # Modals
            {"path": "/themes/docs/components/dialog", "label": "modal"},
            {"path": "/themes/docs/components/alert-dialog", "label": "modal"},
            # Navigation
            {"path": "/themes/docs/components/tabs", "label": "navigation"},
            # Buttons (negatives)
            {"path": "/themes/docs/components/button", "label": "button"},
            {"path": "/themes/docs/components/icon-button", "label": "button"},
            # Data display (negatives)
            {"path": "/themes/docs/components/table", "label": "data_display"},
            {"path": "/themes/docs/components/card", "label": "data_display"},
        ],
    },
    {
        "name": "headlessui",
        "url": "https://headlessui.com",
        "stories": [
            # Inputs / search
            {"path": "/react/combobox", "label": "search"},
            {"path": "/react/listbox", "label": "form_field"},
            # Modals
            {"path": "/react/dialog", "label": "modal"},
            # Navigation
            {"path": "/react/tabs", "label": "navigation"},
            {"path": "/react/disclosure", "label": "navigation"},
            # Buttons / menus
            {"path": "/react/menu", "label": "button"},
            {"path": "/react/popover", "label": "modal"},
        ],
    },
]

# ── Chat sites — real chat interfaces for chat_input training data ────

CHAT_TARGETS = [
    # AI chat interfaces (full-page chat with input at bottom)
    {"url": "https://chat.openai.com", "name": "chatgpt", "label": "chat_input"},
    {"url": "https://claude.ai", "name": "claude", "label": "chat_input"},
    {"url": "https://gemini.google.com", "name": "gemini", "label": "chat_input"},
    {"url": "https://copilot.microsoft.com", "name": "copilot", "label": "chat_input"},
    {"url": "https://www.perplexity.ai", "name": "perplexity", "label": "chat_input"},
    {"url": "https://poe.com", "name": "poe", "label": "chat_input"},
    {"url": "https://www.deepseek.com", "name": "deepseek", "label": "chat_input"},
    # Chat widget platforms (have demo widgets on landing page)
    {"url": "https://www.tidio.com", "name": "tidio", "label": "chat_input"},
    {"url": "https://landbot.io", "name": "landbot", "label": "chat_input"},
    {"url": "https://www.ada.cx", "name": "ada", "label": "chat_input"},
    {"url": "https://www.chatbase.co", "name": "chatbase", "label": "chat_input"},
    {"url": "https://botpress.com", "name": "botpress", "label": "chat_input"},
    {"url": "https://flowxo.com", "name": "flowxo", "label": "chat_input"},
    {"url": "https://www.snatchbot.me", "name": "snatchbot", "label": "chat_input"},
    {"url": "https://octaneai.com", "name": "octane", "label": "chat_input"},
]


# ── Rasterizer ───────────────────────────────────────────────────────

_RASTERIZER_JS = (
    Path(__file__).resolve().parent.parent / "ext" / "rasterizer.js"
).read_text(encoding="utf-8")

# Element-handle versions: called as el.evaluate(js, arg)
# These take the element as `this` (implicit first arg in Playwright el.evaluate)

_RASTERIZER_JS_EL = """(gridSize) => {
    const root = this;
    const channels = 4;
    const grid = new Array(gridSize * gridSize * channels).fill(0);
    const r = root.getBoundingClientRect();
    const bounds = { left: r.left, top: r.top, width: r.width, height: r.height };
    if (bounds.width === 0 || bounds.height === 0) return null;
    const scaleX = gridSize / bounds.width, scaleY = gridSize / bounds.height;
    const INTERACTIVE = new Set(['INPUT','TEXTAREA','BUTTON','SELECT','A']);
    function fill(gx1,gy1,gx2,gy2,ch,val) {
        for (let y=Math.max(0,gy1);y<=Math.min(gridSize-1,gy2);y++)
          for (let x=Math.max(0,gx1);x<=Math.min(gridSize-1,gx2);x++)
            grid[(y*gridSize+x)*channels+ch] = Math.max(grid[(y*gridSize+x)*channels+ch], val);
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
        const rect = node.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
            const gx1=Math.floor((rect.left-bounds.left)*scaleX);
            const gy1=Math.floor((rect.top-bounds.top)*scaleY);
            const gx2=Math.floor((rect.right-bounds.left)*scaleX);
            const gy2=Math.floor((rect.bottom-bounds.top)*scaleY);
            if (INTERACTIVE.has(node.tagName)||node.contentEditable==='true')
                fill(gx1,gy1,gx2,gy2,0,1.0);
            const tl=(node.innerText||'').length;
            if (tl>0) fill(gx1,gy1,gx2,gy2,1,Math.min(tl/500,1.0));
            if (node.tagName==='IFRAME'||node.tagName==='EMBED')
                fill(gx1,gy1,gx2,gy2,2,1.0);
            const style=getComputedStyle(node);
            if (style.position==='fixed'||style.position==='sticky')
                fill(gx1,gy1,gx2,gy2,3,1.0);
        }
        node = walker.nextNode();
    }
    const a11y = { roles: [], ariaLabels: [], hasLiveRegion: false };
    root.querySelectorAll('[role]').forEach(el => {
        const rv=el.getAttribute('role'); if(rv&&!a11y.roles.includes(rv)) a11y.roles.push(rv);
    });
    root.querySelectorAll('[aria-label]').forEach(el => {
        const lv=el.getAttribute('aria-label').toLowerCase(); if(!a11y.ariaLabels.includes(lv)) a11y.ariaLabels.push(lv);
    });
    a11y.hasLiveRegion = !!root.querySelector('[aria-live]');
    return { grid, gridSize, channels, a11y };
}"""

_CODE_FEATURES_JS_EL = """() => {
    const root = this;
    const all = root.querySelectorAll('*');
    const totalEls = all.length || 1;
    const tags = {};
    let interactive = 0;
    const iTags = new Set(['INPUT','TEXTAREA','BUTTON','SELECT','A']);
    for (const el of all) {
        tags[el.tagName] = (tags[el.tagName]||0) + 1;
        if (iTags.has(el.tagName) || el.contentEditable === 'true') interactive++;
    }
    const cls = Array.from(all).map(e=>((e.className||'')+' '+(e.id||'')).toLowerCase()).join(' ');
    const text = (root.innerText||'').toLowerCase();
    const rect = root.getBoundingClientRect();
    return {
        n_input: tags['INPUT']||0, n_textarea: tags['TEXTAREA']||0,
        n_button: tags['BUTTON']||0, n_select: tags['SELECT']||0,
        n_a: tags['A']||0, n_iframe: root.querySelectorAll('iframe').length,
        interactive_ratio: interactive/totalEls,
        depth: (function d(e,n){let m=n;for(const c of e.children)m=Math.max(m,d(c,n+1));return m})(root,0),
        child_count: root.children.length,
        has_role: root.querySelectorAll('[role]').length,
        has_aria_label: root.querySelectorAll('[aria-label]').length,
        has_placeholder: root.querySelectorAll('[placeholder]').length,
        has_contenteditable: root.querySelectorAll('[contenteditable="true"]').length,
        role_textbox: root.querySelectorAll('[role="textbox"]').length,
        role_dialog: root.querySelectorAll('[role="dialog"]').length,
        role_search: root.querySelectorAll('[role="search"],[role="searchbox"]').length,
        role_navigation: root.querySelectorAll('[role="navigation"]').length,
        role_form: root.querySelectorAll('[role="form"]').length,
        kw_chat: /chat|message|compose|messenger/i.test(cls)?1:0,
        kw_search: /search|find|query|autocomplete|combobox/i.test(cls)?1:0,
        kw_login: /login|signin|sign-in|auth|password/i.test(cls)?1:0,
        kw_modal: /modal|dialog|overlay|popup|drawer/i.test(cls)?1:0,
        kw_nav: /nav|menu|sidebar|breadcrumb|tabs|pagination/i.test(cls)?1:0,
        kw_form: /form|field|input|label|control/i.test(cls)?1:0,
        kw_feed: /feed|list|card|grid|item|article/i.test(cls)?1:0,
        word_count: text.split(/\\s+/).filter(w=>w.length>0).length,
        has_send: /\\bsend\\b|\\bsubmit\\b|\\bpost\\b/i.test(text)?1:0,
        has_search_text: /\\bsearch\\b|\\bfind\\b/i.test(text)?1:0,
        has_login_text: /\\blogin\\b|\\bsign in\\b|\\bpassword\\b/i.test(text)?1:0,
        has_live_region: root.querySelector('[aria-live]')?1:0,
        is_fixed: getComputedStyle(root).position==='fixed'?1:0,
        is_bottom_right: (rect.bottom>window.innerHeight*0.7&&rect.right>window.innerWidth*0.7)?1:0,
        rel_width: Math.round(rect.width/window.innerWidth*100)/100,
        rel_height: Math.round(rect.height/window.innerHeight*100)/100,
    };
}"""


def rasterize_element(page, selector=None, viewport=False, grid_size=32):
    """Run the bounding-box rasterizer on a page element."""
    result = page.evaluate(
        _RASTERIZER_JS,
        {"selector": selector, "gridSize": grid_size, "viewport": viewport},
    )
    return result


_CODE_FEATURES_JS = r"""(selector) => {
    const root = selector ? document.querySelector(selector) : document.body;
    if (!root) return null;
    const all = root.querySelectorAll('*');
    const totalEls = all.length || 1;
    const tags = {};
    let interactive = 0;
    const iTags = new Set(['INPUT','TEXTAREA','BUTTON','SELECT','A']);
    for (const el of all) {
        tags[el.tagName] = (tags[el.tagName]||0) + 1;
        if (iTags.has(el.tagName) || el.contentEditable === 'true') interactive++;
    }
    const cls = Array.from(all).map(e => ((e.className||'')+' '+(e.id||'')).toLowerCase()).join(' ');
    const text = (root.innerText||'').toLowerCase();
    const rect = root.getBoundingClientRect();
    return {
        n_input: tags['INPUT']||0, n_textarea: tags['TEXTAREA']||0,
        n_button: tags['BUTTON']||0, n_select: tags['SELECT']||0,
        n_a: tags['A']||0, n_iframe: root.querySelectorAll('iframe').length,
        interactive_ratio: interactive/totalEls,
        depth: (function d(e,n){let m=n;for(const c of e.children)m=Math.max(m,d(c,n+1));return m})(root,0),
        child_count: root.children.length,
        has_role: root.querySelectorAll('[role]').length,
        has_aria_label: root.querySelectorAll('[aria-label]').length,
        has_placeholder: root.querySelectorAll('[placeholder]').length,
        has_contenteditable: root.querySelectorAll('[contenteditable="true"]').length,
        role_textbox: root.querySelectorAll('[role="textbox"]').length,
        role_dialog: root.querySelectorAll('[role="dialog"]').length,
        role_search: root.querySelectorAll('[role="search"],[role="searchbox"]').length,
        role_navigation: root.querySelectorAll('[role="navigation"]').length,
        role_form: root.querySelectorAll('[role="form"]').length,
        kw_chat: /chat|message|compose|messenger/i.test(cls)?1:0,
        kw_search: /search|find|query|autocomplete|combobox/i.test(cls)?1:0,
        kw_login: /login|signin|sign-in|auth|password/i.test(cls)?1:0,
        kw_modal: /modal|dialog|overlay|popup|drawer/i.test(cls)?1:0,
        kw_nav: /nav|menu|sidebar|breadcrumb|tabs|pagination/i.test(cls)?1:0,
        kw_form: /form|field|input|label|control/i.test(cls)?1:0,
        kw_feed: /feed|list|card|grid|item|article/i.test(cls)?1:0,
        word_count: text.split(/\s+/).filter(w=>w.length>0).length,
        has_send: /\bsend\b|\bsubmit\b|\bpost\b/i.test(text)?1:0,
        has_search_text: /\bsearch\b|\bfind\b/i.test(text)?1:0,
        has_login_text: /\blogin\b|\bsign in\b|\bpassword\b/i.test(text)?1:0,
        has_live_region: root.querySelector('[aria-live]')?1:0,
        is_fixed: getComputedStyle(root).position==='fixed'?1:0,
        is_bottom_right: (rect.bottom>window.innerHeight*0.7&&rect.right>window.innerWidth*0.7)?1:0,
        rel_width: Math.round(rect.width/window.innerWidth*100)/100,
        rel_height: Math.round(rect.height/window.innerHeight*100)/100,
    };
}"""


def _extract_code_features(page, selector):
    """Extract DOM code features from an element for stage 2 training."""
    try:
        return page.evaluate(_CODE_FEATURES_JS, selector)
    except Exception:
        return None


def rasterize_all_demos(page, grid_size=32):
    """Find demo/example containers on a docs page and rasterize each."""
    # Common patterns for demo containers in design library docs
    demo_selectors = [
        ".MuiPaper-root .MuiBox-root",  # MUI
        ".ant-space-vertical > .ant-space-item",  # Ant Design
        ".chakra-stack > div",  # Chakra
        ".bd-example",  # Bootstrap
        ".m_396ce5cb",  # Mantine demo blocks
        '[class*="PlaygroundPreview"]',  # Mantine playground
        ".rt-Box",  # Radix Themes
        '[class*="demo"]',  # Generic
        '[class*="example"]',
        '[class*="preview"]',
        '[class*="sandbox"]',
        "iframe[title]",  # Storybook iframes
    ]

    samples = []
    for sel in demo_selectors:
        elements = page.query_selector_all(sel)
        for el in elements[:10]:  # Cap per selector
            try:
                box = el.bounding_box()
                if not box or box["width"] < 50 or box["height"] < 20:
                    continue

                # Tag the element so we can find it via querySelector
                page.evaluate(
                    "(el) => el.setAttribute('data-scrape-id', 'target')", el,
                )
                result = page.evaluate(
                    _RASTERIZER_JS,
                    {"selector": "[data-scrape-id=target]", "gridSize": grid_size, "viewport": False},
                )
                if not result or not result.get("grid"):
                    page.evaluate("(el) => el.removeAttribute('data-scrape-id')", el)
                    continue

                # Extract code features
                try:
                    code_feats = page.evaluate(
                        _CODE_FEATURES_JS, "[data-scrape-id=target]",
                    )
                    if code_feats:
                        result["code_features"] = code_feats
                except Exception as e:
                    logger.debug("Code features failed: %s", e)

                page.evaluate("(el) => el.removeAttribute('data-scrape-id')", el)

                samples.append(result)
            except Exception:
                continue

    # Fallback: rasterize full viewport
    if not samples:
        result = rasterize_element(page, viewport=True, grid_size=grid_size)
        if result and result.get("grid"):
            samples.append(result)

    return samples


# ── Scraper ──────────────────────────────────────────────────────────


def scrape_target(page, target: dict, grid_size: int = 32) -> list[dict]:
    """Scrape all stories from a single Storybook target."""
    samples = []
    base_url = target["url"].rstrip("/")
    name = target["name"]

    for story in target.get("stories", []):
        label = story["label"]
        path = story["path"]

        # Build URL
        if path.startswith("http"):
            url = path
        elif path.startswith("/"):
            # Absolute path on same domain
            from urllib.parse import urlparse

            parsed = urlparse(base_url)
            url = f"{parsed.scheme}://{parsed.netloc}{path}"
        else:
            url = f"{base_url}/{path}"

        logger.info("  %s / %s → %s", name, label, url)

        try:
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # Let demos render
        except Exception as e:
            logger.warning("  Failed to load %s: %s", url, e)
            continue

        # Rasterize demo containers
        rasters = rasterize_all_demos(page, grid_size)
        logger.info("    %d raster(s) extracted", len(rasters))

        for raster in rasters:
            sample = {
                "grid": raster["grid"],
                "a11y": raster.get("a11y", {}),
                "label": label,
                "source": name,
                "url": url,
            }
            if raster.get("code_features"):
                sample["code_features"] = raster["code_features"]
            samples.append(sample)

    return samples


def scrape_chat_sites(page, targets: list[dict], grid_size: int = 32) -> list[dict]:
    """Scrape real chat interfaces for chat_input training data.

    Strategy: load each chat site, find interactive containers
    (inputs, textareas, contenteditable), rasterize them + their
    parent containers, and extract code features.
    """
    samples = []

    # Selectors that commonly contain chat input areas
    chat_selectors = [
        # Direct input elements in chat context
        'textarea',
        '[contenteditable="true"]',
        '[role="textbox"]',
        # Common chat container patterns
        '[class*="chat" i]',
        '[class*="composer" i]',
        '[class*="message" i]',
        '[class*="prompt" i]',
        '[class*="input" i]',
        '[id*="chat" i]',
        '[id*="prompt" i]',
        # Framework-specific
        '[data-testid*="input"]',
        '[data-testid*="prompt"]',
        '[data-testid*="chat"]',
        # Fixed-position widgets (customer support bots)
        '[class*="widget" i]',
        '[class*="launcher" i]',
        '[id*="widget" i]',
    ]

    for target in targets:
        url = target["url"]
        name = target["name"]
        label = target["label"]

        logger.info("  %s → %s", name, url)

        try:
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)  # Let chat widgets load
        except Exception as e:
            logger.warning("    Failed to load: %s", e)
            continue

        # 1. Rasterize full viewport (the whole page IS a chat interface)
        try:
            page.evaluate(
                "(el) => el.setAttribute('data-scrape-id', 'target')",
                page.query_selector("body"),
            )
            vp_result = page.evaluate(
                _RASTERIZER_JS,
                {"selector": None, "gridSize": grid_size, "viewport": True},
            )
            if vp_result and vp_result.get("grid"):
                code_feats = page.evaluate(_CODE_FEATURES_JS, "body")
                sample = {
                    "grid": vp_result["grid"],
                    "a11y": vp_result.get("a11y", {}),
                    "label": label,
                    "source": name,
                    "url": url,
                }
                if code_feats:
                    sample["code_features"] = code_feats
                samples.append(sample)
        except Exception:
            pass

        # 2. Find and rasterize specific chat-like containers
        found = 0
        for sel in chat_selectors:
            try:
                elements = page.query_selector_all(sel)
            except Exception:
                continue

            for el in elements[:5]:
                try:
                    box = el.bounding_box()
                    if not box or box["width"] < 30 or box["height"] < 15:
                        continue

                    page.evaluate(
                        "(el) => el.setAttribute('data-scrape-id', 'target')", el,
                    )
                    result = page.evaluate(
                        _RASTERIZER_JS,
                        {"selector": "[data-scrape-id=target]", "gridSize": grid_size, "viewport": False},
                    )
                    if not result or not result.get("grid"):
                        page.evaluate("(el) => el.removeAttribute('data-scrape-id')", el)
                        continue

                    code_feats = page.evaluate(
                        _CODE_FEATURES_JS, "[data-scrape-id=target]",
                    )

                    sample = {
                        "grid": result["grid"],
                        "a11y": result.get("a11y", {}),
                        "label": label,
                        "source": name,
                        "url": url,
                    }
                    if code_feats:
                        sample["code_features"] = code_feats
                    samples.append(sample)
                    found += 1

                    page.evaluate("(el) => el.removeAttribute('data-scrape-id')", el)
                except Exception:
                    continue

        logger.info("    %d sample(s)", found + 1)  # +1 for viewport

    return samples


def main():
    parser = argparse.ArgumentParser(description="Scrape Storybooks for training data")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument("--grid-size", type=int, default=32, help="Raster grid size")
    parser.add_argument("--chat-only", action="store_true", help="Only scrape chat sites")
    parser.add_argument("--skip-chat", action="store_true", help="Skip chat sites")
    parser.add_argument(
        "--output",
        default="data/training/storybook_samples.json",
        help="Output file path",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing samples if any (append mode)
    existing = []
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            logger.info("Loaded %d existing samples", len(existing))
        except Exception:
            pass

    from playwright.sync_api import sync_playwright

    all_samples = list(existing)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        if not args.chat_only:
            for target in STORYBOOK_TARGETS:
                logger.info("Scraping %s (%d stories)...", target["name"], len(target.get("stories", [])))
                samples = scrape_target(page, target, grid_size=args.grid_size)
                all_samples.extend(samples)
                logger.info("  → %d samples total\n", len(all_samples))

        if not args.skip_chat:
            logger.info("Scraping %d chat sites...", len(CHAT_TARGETS))
            chat_samples = scrape_chat_sites(page, CHAT_TARGETS, grid_size=args.grid_size)
            all_samples.extend(chat_samples)
            logger.info("  → %d samples total (incl. %d chat)\n", len(all_samples), len(chat_samples))

        context.close()
        browser.close()

    # Save
    output_path.write_text(
        json.dumps(all_samples, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Saved %d samples → %s", len(all_samples), output_path)

    # Print label distribution
    from collections import Counter

    dist = Counter(s["label"] for s in all_samples)
    for label, count in dist.most_common():
        logger.info("  %s: %d", label, count)


if __name__ == "__main__":
    main()
