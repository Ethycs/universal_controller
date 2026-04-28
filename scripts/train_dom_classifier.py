"""Train a UI pattern classifier on spatial features from DOM rasters.

Input:  data/training/storybook_samples.json (relative to UC root)
Output: models/dom_classifier/raster_classifier.pkl (relative to UC root)

Instead of flattening the 32x32x4 grid into 4096 raw pixels, we extract
~25 spatial features that encode layout patterns:
  - Where are interactive elements? (top=search, bottom=chat)
  - Where is text dense? (top=messages, scattered=feed)
  - Is there a fixed overlay? (modal/widget)
  - How symmetric/spread is the layout?

Same RandomForest as the code classifier — fast, small, interpretable.

Usage:
  pixi run python scripts/train_dom_classifier.py
"""

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logger = logging.getLogger("train_dom_classifier")

LABELS = [
    "search", "chat_input", "form_field", "modal",
    "login_form", "button", "navigation", "data_display",
]
LABEL_TO_IDX = {label: idx for idx, label in enumerate(LABELS)}


# ── Spatial feature extraction ───────────────────────────────────────

SPATIAL_FEATURE_NAMES = [
    # Channel 0: interactive elements (input, button, a, textarea)
    "interactive_density",           # total interactive pixels / grid area
    "interactive_top_quarter",       # density in top 25% rows (search bars)
    "interactive_bottom_quarter",    # density in bottom 25% rows (chat inputs)
    "interactive_left_half",         # density in left half
    "interactive_right_half",        # density in right half
    "interactive_center_of_mass_y",  # vertical CoM (0=top, 1=bottom)
    "interactive_center_of_mass_x",  # horizontal CoM (0=left, 1=right)
    "interactive_spread_y",          # std dev of vertical positions
    "interactive_spread_x",          # std dev of horizontal positions
    # Channel 1: text density
    "text_density",                  # total text signal / grid area
    "text_top_75pct",                # text in top 75% (chat: messages above input)
    "text_bottom_25pct",             # text in bottom 25%
    "text_vertical_gradient",        # top_half - bottom_half (positive = top-heavy)
    # Channel 2: iframe presence
    "iframe_density",                # any iframes? (widget pattern)
    "iframe_bottom_right",           # iframe in bottom-right quadrant (chat widget)
    # Channel 3: overlay / fixed positioning
    "overlay_density",               # total fixed/high-z elements
    "overlay_covers_center",         # fixed elements covering center (modal)
    "overlay_bottom_right_only",     # fixed only in bottom-right (widget)
    # Cross-channel
    "interactive_over_text_ratio",   # interactive / (text + 0.01)
    "vertical_structure",            # are elements arranged vertically? (column layout)
    "horizontal_structure",          # are elements arranged horizontally? (nav bar)
    "quadrant_diversity",            # how many quadrants have content
    "compactness",                   # how concentrated is content (vs spread)
    "aspect_signal",                 # width-dominant vs height-dominant content
]


def extract_spatial_features(grid_flat: list, grid_size: int = 32, channels: int = 4) -> dict:
    """Extract spatial layout features from a 32x32x4 raster grid.

    Returns dict of ~25 features that capture WHERE things are,
    not just WHAT is present.
    """
    grid = np.array(grid_flat, dtype=np.float32).reshape(grid_size, grid_size, channels)
    area = grid_size * grid_size

    # Separate channels
    ch_interactive = grid[:, :, 0]  # interactive elements
    ch_text = grid[:, :, 1]         # text density
    ch_iframe = grid[:, :, 2] if channels > 2 else np.zeros((grid_size, grid_size))
    ch_overlay = grid[:, :, 3] if channels > 3 else np.zeros((grid_size, grid_size))

    q1 = grid_size // 4       # top quarter boundary
    q3 = 3 * grid_size // 4   # bottom quarter boundary
    mid = grid_size // 2

    # ── Channel 0: interactive ──
    i_total = ch_interactive.sum()
    feats = {
        "interactive_density": i_total / area,
        "interactive_top_quarter": ch_interactive[:q1, :].sum() / max(i_total, 0.01),
        "interactive_bottom_quarter": ch_interactive[q3:, :].sum() / max(i_total, 0.01),
        "interactive_left_half": ch_interactive[:, :mid].sum() / max(i_total, 0.01),
        "interactive_right_half": ch_interactive[:, mid:].sum() / max(i_total, 0.01),
    }

    # Center of mass for interactive elements
    ys, xs = np.where(ch_interactive > 0)
    if len(ys) > 0:
        feats["interactive_center_of_mass_y"] = ys.mean() / grid_size
        feats["interactive_center_of_mass_x"] = xs.mean() / grid_size
        feats["interactive_spread_y"] = ys.std() / grid_size
        feats["interactive_spread_x"] = xs.std() / grid_size
    else:
        feats["interactive_center_of_mass_y"] = 0.5
        feats["interactive_center_of_mass_x"] = 0.5
        feats["interactive_spread_y"] = 0
        feats["interactive_spread_x"] = 0

    # ── Channel 1: text ──
    t_total = ch_text.sum()
    feats["text_density"] = t_total / area
    feats["text_top_75pct"] = ch_text[:q3, :].sum() / max(t_total, 0.01)
    feats["text_bottom_25pct"] = ch_text[q3:, :].sum() / max(t_total, 0.01)
    top_half = ch_text[:mid, :].sum()
    bottom_half = ch_text[mid:, :].sum()
    feats["text_vertical_gradient"] = (top_half - bottom_half) / max(t_total, 0.01)

    # ── Channel 2: iframe ──
    feats["iframe_density"] = ch_iframe.sum() / area
    feats["iframe_bottom_right"] = ch_iframe[mid:, mid:].sum() / max(ch_iframe.sum(), 0.01)

    # ── Channel 3: overlay ──
    o_total = ch_overlay.sum()
    feats["overlay_density"] = o_total / area
    center_region = ch_overlay[q1:q3, q1:q3]
    feats["overlay_covers_center"] = center_region.sum() / max(o_total, 0.01)
    br_region = ch_overlay[mid:, mid:]
    total_minus_br = o_total - br_region.sum()
    feats["overlay_bottom_right_only"] = (
        1.0 if br_region.sum() > 0 and total_minus_br < 0.1 else 0.0
    )

    # ── Cross-channel ──
    feats["interactive_over_text_ratio"] = i_total / max(t_total, 0.01)

    # Vertical vs horizontal structure
    col_sums = (ch_interactive + ch_text).sum(axis=0)  # sum per column
    row_sums = (ch_interactive + ch_text).sum(axis=1)  # sum per row
    feats["vertical_structure"] = row_sums.std() / max(row_sums.mean(), 0.01)
    feats["horizontal_structure"] = col_sums.std() / max(col_sums.mean(), 0.01)

    # Quadrant diversity: how many of 4 quadrants have content
    content = ch_interactive + ch_text
    quadrants = [
        content[:mid, :mid].sum(),
        content[:mid, mid:].sum(),
        content[mid:, :mid].sum(),
        content[mid:, mid:].sum(),
    ]
    feats["quadrant_diversity"] = sum(1 for q in quadrants if q > 0.5) / 4.0

    # Compactness: how concentrated is content
    all_ys, all_xs = np.where(content > 0)
    if len(all_ys) > 1:
        feats["compactness"] = 1.0 - (all_ys.std() * all_xs.std()) / (grid_size * grid_size / 4)
    else:
        feats["compactness"] = 1.0

    # Aspect signal: wider content vs taller content
    if len(all_ys) > 0:
        y_range = all_ys.max() - all_ys.min() + 1
        x_range = all_xs.max() - all_xs.min() + 1
        feats["aspect_signal"] = x_range / max(y_range, 1)
    else:
        feats["aspect_signal"] = 1.0

    return feats


# ── Data loading ─────────────────────────────────────────────────────


def load_samples(path: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Load samples, extract spatial features, return (X, y, raw_labels)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    X_list = []
    y_list = []
    raw_labels = []

    for sample in data:
        label = sample.get("label", "")
        if label not in LABEL_TO_IDX:
            continue

        grid = sample["grid"]
        grid_size = sample.get("gridSize", 32)
        channels = sample.get("channels", 4)

        feats = extract_spatial_features(grid, grid_size, channels)
        row = [feats.get(f, 0) for f in SPATIAL_FEATURE_NAMES]
        X_list.append(row)
        y_list.append(LABEL_TO_IDX[label])
        raw_labels.append(label)

    return np.array(X_list, dtype=np.float32), np.array(y_list), raw_labels


# ── Main ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Train DOM raster spatial classifier")
    parser.add_argument(
        "--input",
        default="data/training/storybook_samples.json",
        help="Training data path",
    )
    parser.add_argument(
        "--output",
        default="models/dom_classifier/raster_classifier.pkl",
        help="Output pickle path",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    logger.info("Loading samples from %s", args.input)
    X, y, raw_labels = load_samples(args.input)
    logger.info("Loaded %d samples, %d spatial features", len(X), X.shape[1])

    if len(X) < 10:
        logger.error("Too few samples (%d). Run scraper first.", len(X))
        return

    dist = Counter(raw_labels)
    for label, count in dist.most_common():
        logger.info("  %s: %d", label, count)

    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42,
        stratify=y if len(set(y)) > 1 else None,
    )
    logger.info("Train: %d, Val: %d", len(X_train), len(X_val))

    clf = RandomForestClassifier(
        n_estimators=100, random_state=42, class_weight="balanced",
    )
    clf.fit(X_train, y_train)

    train_acc = clf.score(X_train, y_train)
    val_acc = clf.score(X_val, y_val)
    logger.info("Train accuracy: %.2f%%", train_acc * 100)
    logger.info("Val accuracy:   %.2f%%", val_acc * 100)

    y_pred = clf.predict(X_val)
    active_labels = sorted(set(y_val))
    target_names = [LABELS[i] for i in active_labels]
    logger.info("\n%s", classification_report(y_val, y_pred, target_names=target_names))

    # Feature importances
    logger.info("Top features:")
    importances = sorted(
        zip(SPATIAL_FEATURE_NAMES, clf.feature_importances_),
        key=lambda x: x[1], reverse=True,
    )
    for name, imp in importances[:12]:
        bar = "#" * int(imp * 50)
        logger.info("  %30s  %.4f  %s", name, imp, bar)

    # Save
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, str(output))
    size_kb = output.stat().st_size / 1024
    logger.info("\nSaved (%.0f KB) → %s", size_kb, output)


if __name__ == "__main__":
    main()
