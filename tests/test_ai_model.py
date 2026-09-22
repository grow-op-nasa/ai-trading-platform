"""Tests for `src/ai/model.py` (Sprint 10, `DECISIONS.md` ADR-0043).

Gated on scikit-learn being importable (`pytest.importorskip`), the
same established pattern `tests/test_dashboard_smoke.py` uses for
Streamlit (`DECISIONS.md`, ADR-0042): scikit-learn is a real,
`requirements.txt`-pinned project dependency, but isn't installed in
the sandbox this suite is sometimes run in -- this module (and every
other Sprint 10 test file that needs a real fit) skips cleanly here
and runs for real on the dev machine, rather than either crashing the
whole suite or being stubbed into something that no longer tests the
real library.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

pytest.importorskip("sklearn")

from src.ai.model import LogisticRegressionModel, ModelConfig, build_model, model_class_for


def _make_xy(n: int = 120, seed: int = 3):
    rng = random.Random(seed)
    X = pd.DataFrame(
        {
            "f1": [rng.uniform(-1, 1) for _ in range(n)],
            "f2": [rng.uniform(-1, 1) for _ in range(n)],
            "f3": [rng.uniform(-1, 1) for _ in range(n)],
        },
        index=pd.RangeIndex(n),
    )
    # A genuinely learnable relationship, not pure noise -- f1 mostly
    # drives the class, so a fit model should do meaningfully better
    # than chance without needing this test to hand-verify accuracy.
    y = pd.Series(
        ["LONG" if v > 0.15 else "SHORT" if v < -0.15 else "FLAT" for v in X["f1"]],
        index=X.index,
        name="label",
    )
    return X, y


def test_model_fits_and_predicts():
    X, y = _make_xy()
    model = LogisticRegressionModel(ModelConfig(random_state=1))
    model.fit(X, y)
    predictions = model.predict(X)
    assert len(predictions) == len(X)
    assert set(predictions) <= {"LONG", "SHORT", "FLAT"}


def test_predict_proba_columns_are_the_classes_and_rows_sum_to_one():
    X, y = _make_xy()
    model = LogisticRegressionModel(ModelConfig(random_state=1)).fit(X, y)
    proba = model.predict_proba(X)
    assert set(proba.columns) == set(model.classes_)
    assert (proba.sum(axis=1).round(6) == 1.0).all()


def test_fit_is_deterministic_given_a_fixed_random_state():
    X, y = _make_xy()
    model_a = LogisticRegressionModel(ModelConfig(random_state=7)).fit(X, y)
    model_b = LogisticRegressionModel(ModelConfig(random_state=7)).fit(X, y)
    pd.testing.assert_frame_equal(model_a.predict_proba(X), model_b.predict_proba(X))


def test_predict_before_fit_raises():
    X, _ = _make_xy()
    model = LogisticRegressionModel()
    with pytest.raises(RuntimeError):
        model.predict(X)
    with pytest.raises(RuntimeError):
        model.predict_proba(X)


def test_schema_mismatch_missing_column_fails_loudly():
    X, y = _make_xy()
    model = LogisticRegressionModel().fit(X, y)
    with pytest.raises(ValueError, match="schema"):
        model.predict(X.drop(columns=["f2"]))


def test_schema_mismatch_reordered_columns_fails_loudly():
    X, y = _make_xy()
    model = LogisticRegressionModel().fit(X, y)
    reordered = X[["f2", "f1", "f3"]]
    with pytest.raises(ValueError, match="schema"):
        model.predict(reordered)


def test_scaler_is_fit_only_on_the_data_passed_to_fit_not_refit_on_predict():
    # The structural leakage guard (Sprint 10 spec, section 42): fitting
    # on a small X then calling predict() on a wildly different-scaled X
    # must not change the model's own learned scaling -- if it did,
    # predict_proba() on the same rows twice (once alone, once alongside
    # very different data) would disagree.
    X, y = _make_xy()
    model = LogisticRegressionModel(ModelConfig(random_state=1)).fit(X, y)
    baseline = model.predict_proba(X)

    far_away = X.copy() * 1000  # would badly distort a scaler if refit
    model.predict_proba(far_away)  # must not mutate the fitted pipeline

    after = model.predict_proba(X)
    pd.testing.assert_frame_equal(baseline, after)


def test_empty_training_set_raises():
    X, y = _make_xy()
    model = LogisticRegressionModel()
    with pytest.raises(ValueError):
        model.fit(X.iloc[0:0], y.iloc[0:0])


def test_to_artifact_and_from_artifact_round_trip_predictions():
    X, y = _make_xy()
    model = LogisticRegressionModel(ModelConfig(random_state=1)).fit(X, y)
    artifact = model.to_artifact()

    restored = LogisticRegressionModel.from_artifact(artifact, model.config)
    pd.testing.assert_frame_equal(model.predict_proba(X), restored.predict_proba(X))
    assert restored.feature_columns == model.feature_columns
    assert restored.classes_ == model.classes_


def test_build_model_dispatches_by_model_type():
    model = build_model(ModelConfig(model_type="logistic_regression"))
    assert isinstance(model, LogisticRegressionModel)


def test_build_model_rejects_unknown_model_type():
    with pytest.raises(KeyError):
        build_model(ModelConfig(model_type="not_a_real_model_type"))


def test_model_class_for_rejects_unknown_model_type():
    with pytest.raises(KeyError):
        model_class_for("not_a_real_model_type")
