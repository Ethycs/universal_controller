"""Shim so litellm proxy's config can find ``uc_handler``.

The proxy resolves ``custom_handler`` dotted paths by treating them as
file paths relative to the config file's directory, not as regular
Python imports. The real module is at
``uc_browser/llm_providers/uc.py``; the proxy's loader can't reach that
by package path, so we re-export the singleton handler from this file
(which sits next to ``litellm.config.yaml``).

Referenced from ``litellm.config.yaml`` as
``custom_handler: uc_provider_loader.uc_handler``.
"""

import logging as _logging
import os as _os

# Surface the browser-driver logs (submit-path decisions, anchor capture,
# empty diagnostics) through the litellm proxy's stdout. litellm doesn't
# configure third-party loggers, so without this the grok_fast INFO lines
# that say which path (toolkit vs bespoke) handled a send are invisible.
# Opt out with UC_GROK_QUIET=1.
if _os.environ.get("UC_GROK_QUIET") != "1":
    _uc_log = _logging.getLogger("uc_browser")
    _uc_log.setLevel(_logging.INFO)
    if not _uc_log.handlers:
        _h = _logging.StreamHandler()
        _h.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        _uc_log.addHandler(_h)
    _uc_log.propagate = False

from uc_browser.llm_providers.uc import uc_handler  # noqa: F401,E402
