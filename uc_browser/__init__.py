"""Universal Controller — Python control plane for the UC Chrome extension.

Public API:
    UCBrowser, BrowserMode — main browser automation class + mode enum

Optional submodules (import directly):
    uc_browser.chrome_cookies   — rookiepy-based Chrome cookie extraction
    uc_browser.dom_classifier   — ML chat-input classification
    uc_browser._paths           — extension/model path resolution
"""

from uc_browser.browser import BrowserMode, UCBrowser

__all__ = ["UCBrowser", "BrowserMode"]
__version__ = "0.1.0"
