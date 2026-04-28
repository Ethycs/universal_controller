"""Train a RandomForest on DOM code features (stage 2 classifier).

Input:  data/training/storybook_samples.json (relative to UC root, with code_features field)
Output: models/dom_classifier/code_classifier.pkl (relative to UC root)

The code classifier uses 34 structural/semantic features extracted from
DOM subtrees. It runs as stage 2 after the raster classifier to filter
false positives with high precision.

Usage:
  pixi run python scripts/train_code_classifier.py
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

# DOM classifier schema (matches event_harvester.dom_classifier and ml/dom_inference.js)
DEFAULT_LABELS = [
    "search", "chat_input", "form_field", "modal",
    "login_form", "button", "navigation", "data_display",
]

CODE_FEATURE_NAMES = [
    "n_input", "n_textarea", "n_button", "n_select", "n_a", "n_iframe",
    "interactive_ratio", "depth", "child_count",
    "has_role", "has_aria_label", "has_placeholder", "has_contenteditable",
    "role_textbox", "role_dialog", "role_search", "role_navigation", "role_form",
    "kw_chat", "kw_search", "kw_login", "kw_modal", "kw_nav", "kw_form", "kw_feed",
    "word_count", "has_send", "has_search_text", "has_login_text", "has_live_region",
    "is_fixed", "is_bottom_right", "rel_width", "rel_height",
]

logger = logging.getLogger("train_code_classifier")

LABEL_TO_IDX = {label: idx for idx, label in enumerate(DEFAULT_LABELS)}


def load_samples(path: str) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Load samples that have code_features, return (X, y, raw_labels)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    X_list = []
    y_list = []
    raw_labels = []

    for sample in data:
        label = sample.get("label", "")
        if label not in LABEL_TO_IDX:
            continue

        feats = sample.get("code_features")
        if not feats:
            continue

        row = [feats.get(f, 0) for f in CODE_FEATURE_NAMES]
        X_list.append(row)
        y_list.append(LABEL_TO_IDX[label])
        raw_labels.append(label)

    return np.array(X_list, dtype=np.float32), np.array(y_list), raw_labels


def main():
    parser = argparse.ArgumentParser(description="Train stage 2 code classifier")
    parser.add_argument(
        "--input",
        default="data/training/storybook_samples.json",
        help="Training data path",
    )
    parser.add_argument(
        "--output",
        default="models/dom_classifier/code_classifier.pkl",
        help="Output pickle path",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    logger.info("Loading samples from %s", args.input)
    X, y, raw_labels = load_samples(args.input)
    logger.info("Loaded %d samples with code features", len(X))

    if len(X) < 10:
        logger.error(
            "Too few samples with code_features (%d). "
            "Re-run the scraper to collect code features:\n"
            "  pixi run python scripts/scrape_storybooks.py",
            len(X),
        )
        return

    dist = Counter(raw_labels)
    for label, count in dist.most_common():
        logger.info("  %s: %d", label, count)

    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42,
        stratify=y if len(set(y)) > 1 else None,
    )
    logger.info("Train: %d, Val: %d", len(X_train), len(X_val))

    logger.info("Training RandomForest on %d features...", X.shape[1])
    clf = RandomForestClassifier(
        n_estimators=100,
        random_state=42,
        class_weight="balanced",
    )
    clf.fit(X_train, y_train)

    train_acc = clf.score(X_train, y_train)
    val_acc = clf.score(X_val, y_val)
    logger.info("Train accuracy: %.2f%%", train_acc * 100)
    logger.info("Val accuracy:   %.2f%%", val_acc * 100)

    y_pred = clf.predict(X_val)
    active_labels = sorted(set(y_val))
    target_names = [DEFAULT_LABELS[i] for i in active_labels]
    logger.info("\n%s", classification_report(y_val, y_pred, target_names=target_names))

    # Feature importances
    logger.info("Top features:")
    importances = sorted(
        zip(CODE_FEATURE_NAMES, clf.feature_importances_),
        key=lambda x: x[1], reverse=True,
    )
    for name, imp in importances[:10]:
        bar = "#" * int(imp * 50)
        logger.info("  %25s  %.4f  %s", name, imp, bar)

    # Save
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, str(output))
    logger.info("\nSaved → %s", output)


if __name__ == "__main__":
    main()
