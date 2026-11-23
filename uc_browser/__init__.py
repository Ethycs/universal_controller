"""Universal Controller — Python control plane for the UC Chrome extension.

Public API (loaded on first access, so importing a light submodule such as
``uc_browser.service.client`` never pulls in Playwright):
    UCBrowser, BrowserMode — main browser automation class + mode enum
    ServiceClient          — plain-HTTP client for the UC service

Optional submodules (import directly):
    uc_browser.chrome_cookies   — rookiepy-based Chrome cookie extraction
    uc_browser.dom_classifier   — ML chat-input classification
    uc_browser._paths           — extension/model path resolution
"""

import importlib

_LAZY = {
    "UCBrowser": "uc_browser.browser",
    "BrowserMode": "uc_browser.browser",
    "ServiceClient": "uc_browser.service.client",
}

__all__ = list(_LAZY)
__version__ = "0.1.0"


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted([*globals(), *_LAZY])
