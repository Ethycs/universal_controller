"""Path resolution for UC artifacts (extension, models, JS helpers).

Default paths are relative to the submodule layout where this package
lives at `ext/universal_controller/uc_browser/`. Each path can be overridden
via an environment variable so the package works in non-standard layouts
(installed wheel, Docker container, etc.).

Environment overrides:
    UC_EXTENSION_DIR   - Chrome extension directory (default: ../extension/)
    UC_MODELS_DIR      - Trained ML models (default: ../models/dom_classifier/)
    UC_RASTERIZER_JS   - rasterizer.js path (default: ../ml/rasterizer.js)
"""

import os
from pathlib import Path

# uc_browser/ → universal_controller/  (submodule root)
_PKG_DIR = Path(__file__).resolve().parent
_SUBMODULE_ROOT = _PKG_DIR.parent


def ext_dir() -> Path:
    """Chrome MV3 extension directory (contains manifest.json + dist/)."""
    override = os.environ.get("UC_EXTENSION_DIR")
    if override:
        return Path(override).resolve()
    return _SUBMODULE_ROOT / "extension"


def models_dir() -> Path:
    """Trained ML model directory (dom_classifier .pkl + labels.json)."""
    override = os.environ.get("UC_MODELS_DIR")
    if override:
        return Path(override).resolve()
    return _SUBMODULE_ROOT / "models" / "dom_classifier"


def rasterizer_js() -> Path:
    """Path to ml/rasterizer.js (loaded as text by dom_classifier)."""
    override = os.environ.get("UC_RASTERIZER_JS")
    if override:
        return Path(override).resolve()
    return _SUBMODULE_ROOT / "ml" / "rasterizer.js"
