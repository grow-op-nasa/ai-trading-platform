"""Model-quality evaluation -- src/ai/evaluation.py.

Answers "how well did the model classify outcomes?" -- a strictly
different question from "how well did the resulting trading policy
perform?", which is `src.backtesting.Backtester` + `src.analytics.
AnalyticsService`'s job, not this module's (Sprint 10 spec, sections
22, 58, 67: classification accuracy is never "strategy performance").

    from src.ai.evaluation import classification_metrics

    metrics = classification_metrics(y_true, y_pred, y_proba, classes=["LONG","SHORT","FLAT"])
    metrics["accuracy"]
    metrics["signal_coverage"]       # trading-relevant, but still a
                                      # property of *predictions*, not
                                      # of any simulated P&L

Every metric here reduces to scikit-learn's own implementations
(`accuracy_score`, `balanced_accuracy_score`,
`precision_recall_fscore_support`, `confusion_matrix`, `log_loss`) --
this module's job is only to package them consistently and to make an
undefined case (an empty split) an explicit, typed result rather than
a crash or a fabricated `0.0` (Sprint 10 spec, section 21: "where
metrics are undefined, represent that explicitly").
"""

from __future__ import annotations

import warnings
from typing import Any

import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    log_loss,
    precision_recall_fscore_support,
)

DEFAULT_CLASSES = ("LONG", "SHORT", "FLAT")


def _class_distribution(labels: pd.Series, classes: tuple[str, ...]) -> dict[str, dict]:
    n = len(labels)
    counts = labels.value_counts()
    return {
        cls: {
            "count": int(counts.get(cls, 0)),
            "proportion": float(counts.get(cls, 0)) / n if n else 0.0,
        }
        for cls in classes
    }


def classification_metrics(
    y_true: pd.Series,
    y_pred: Any,
    y_proba: pd.DataFrame | None = None,
    classes: tuple[str, ...] = DEFAULT_CLASSES,
) -> dict:
    """Classification-quality metrics for one evaluation split.

    Args:
        y_true: ground-truth labels for the split.
        y_pred: this model's predicted labels for the same rows, same
            order (typically `AIModel.predict(X)`'s output).
        y_proba: this model's per-class probabilities for the same
            rows (`AIModel.predict_proba(X)`'s output), columns in
            `classes` order. Optional -- `log_loss` is omitted (`None`)
            without it, everything else is still computed.
        classes: the full label space, in a fixed order used
            everywhere a per-class value is reported. Defaults to
            `DEFAULT_CLASSES` (`src.ai.labels`'s three values).

    Returns:
        A dict. If `y_true` is empty (an empty split -- Sprint 10 spec,
        section 21), returns `{"status": "undefined", "reason": ...,
        "n_samples": 0}` instead of computing anything -- never a
        fabricated `0.0`/`nan` accuracy for a split that was never
        evaluated. Otherwise includes `"status": "ok"` plus:

        - `n_samples`
        - `accuracy`, `balanced_accuracy`
        - `precision`, `recall`: `{class: float}`, zero for a class
          with no predicted/true members rather than raising
          (`zero_division=0`).
        - `confusion_matrix`: `{"labels": [...], "matrix": [[...]]}`.
        - `log_loss`: `float`, or `None` if `y_proba` wasn't given.
        - `true_class_distribution` / `predicted_class_distribution`:
          `{class: {"count": int, "proportion": float}}` -- the
          trading-relevant diagnostic quantities Sprint 10 spec section
          22 asks for (LONG/SHORT/FLAT proportions), computed from
          predictions for `predicted_class_distribution`.
        - `signal_coverage`: proportion of predictions that are *not*
          `"FLAT"` -- how often this model/threshold combination would
          actually want a position, as a model-level diagnostic (not a
          trading-performance claim).
    """
    n = len(y_true)
    if n == 0:
        return {"status": "undefined", "reason": "empty evaluation split", "n_samples": 0}

    y_true_list = list(y_true)
    y_pred_list = list(y_pred)
    classes_list = list(classes)

    precision, recall, _f1, _support = precision_recall_fscore_support(
        y_true_list, y_pred_list, labels=classes_list, zero_division=0
    )
    matrix = confusion_matrix(y_true_list, y_pred_list, labels=classes_list)

    log_loss_value = None
    if y_proba is not None:
        proba_ordered = y_proba[classes_list].to_numpy()
        log_loss_value = float(log_loss(y_true_list, proba_ordered, labels=classes_list))

    predicted_distribution = _class_distribution(pd.Series(y_pred_list), classes)

    # balanced_accuracy_score has no `labels` parameter (unlike
    # precision_recall_fscore_support/confusion_matrix above), so it
    # infers its label set from y_true alone. On a split whose y_true
    # happens to be missing one of the three classes -- routine on the
    # small evaluation windows a walk-forward fold or a short holdout
    # can produce -- a model that still predicts that missing class
    # elsewhere makes sklearn emit "y_pred contains classes not in
    # y_true" and exclude it from the average. That's the *correct*
    # behavior (there's no true-positive rate to compute for a class
    # with zero true instances in this split), not a bug; the warning
    # is just redundant with what true_class_distribution/
    # predicted_class_distribution below already report structurally.
    # Suppressed narrowly by exact message so any other, unrelated
    # warning from this call still surfaces.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="y_pred contains classes not in y_true",
            category=UserWarning,
        )
        balanced_accuracy = balanced_accuracy_score(y_true_list, y_pred_list)

    return {
        "status": "ok",
        "n_samples": n,
        "accuracy": float(accuracy_score(y_true_list, y_pred_list)),
        "balanced_accuracy": float(balanced_accuracy),
        "precision": dict(zip(classes_list, (float(p) for p in precision))),
        "recall": dict(zip(classes_list, (float(r) for r in recall))),
        "confusion_matrix": {"labels": classes_list, "matrix": matrix.tolist()},
        "log_loss": log_loss_value,
        "true_class_distribution": _class_distribution(pd.Series(y_true_list), classes),
        "predicted_class_distribution": predicted_distribution,
        "signal_coverage": 1.0 - predicted_distribution.get("FLAT", {"proportion": 0.0})["proportion"],
    }
