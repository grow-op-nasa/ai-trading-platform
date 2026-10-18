"""Sprint 22: strategies DECLARE the history they need (ADR-0059). Contract tests for `src.strategies.history`."""

from __future__ import annotations

import math
import random

import pandas as pd
import pytest

import src.strategies  # noqa: F401  registers built-ins
from src.ai.features import FeatureBuilder, FeatureSpec
from src.indicators.formulas import average_true_range, exponential_moving_average
from src.strategies import history as H
from src.strategies.ema_cross import EMACrossStrategy
from src.strategies.ema_cross_vol_filter import EMACrossVolFilterStrategy
from src.strategies.registry import available_strategies, get_strategy_class
from src.strategies.rsi_mean_reversion import RSIMeanReversionStrategy


def _candles(n: int = 400, seed: int = 3) -> pd.DataFrame:
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-0.02, 0.021)))
    idx = pd.date_range("2022-01-03", periods=n, freq="B", tz="UTC", name="timestamp")
    return pd.DataFrame({"open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [rng.uniform(900, 1100) for _ in range(n)]}, index=idx)


# ------------------------------------------------------------------ value object
def test_requirement_validation_rejects_bad_values():
    comp = H.component("x", 10, "because")
    for bad in (0, -1, 1.5, True, "5", H.MAX_DECLARABLE_BARS + 1):
        with pytest.raises(H.HistoryRequirementError):
            H.HistoryRequirement(bad, (comp,))
    with pytest.raises(H.HistoryRequirementError):
        H.HistoryRequirement(5, (comp,))                      # smaller than its largest component
    with pytest.raises(H.HistoryRequirementError):
        H.HistoryRequirement(10, ())
    with pytest.raises(H.HistoryRequirementError):
        H.HistoryComponent("x", 3, "")
    with pytest.raises(H.HistoryRequirementError):
        H.HistoryRequirement(10, (comp,), unit="calendar_days")


def test_round_trip_and_explanation():
    req = H.combine(H.ema_component(8), H.ema_component(21))
    assert H.HistoryRequirement.from_dict(req.to_dict()) == req
    assert str(req.min_bars) in req.explain() and "EMA(21)" in req.explain()
    with pytest.raises(H.HistoryRequirementError):
        H.HistoryRequirement.from_dict({"min_bars": 10})
    with pytest.raises(H.HistoryRequirementError):
        H.HistoryRequirement.from_dict("nope")


def test_combine_takes_the_largest_component():
    req = H.combine(H.ema_component(5), H.atr_component(30), H.rsi_component(2))
    assert req.min_bars == max(c.bars for c in req.components)


# ------------------------------------------------------------------ lookup never guesses
def test_undeclared_and_invalid_are_reported_not_defaulted():
    class Bare:
        pass

    class Raises:
        def history_requirement(self):
            raise RuntimeError("boom")

    class WrongType:
        def history_requirement(self):
            return 60

    assert H.requirement_of(Bare()).status == H.UNDECLARED
    assert H.requirement_of(Raises()).status == H.INVALID
    assert H.requirement_of(WrongType()).status == H.INVALID
    for obj in (Bare(), Raises(), WrongType()):
        a = H.assess_strategy(obj, 10_000)
        assert not a.satisfied and a.required is None       # never satisfied, whatever the data volume


def test_assessment_boundaries():
    look = H.requirement_of(EMACrossStrategy("X", fast=8, slow=21))
    n = look.requirement.min_bars
    assert H.assess_history(look, n).satisfied
    short = H.assess_history(look, n - 1)
    assert not short.satisfied and short.shortfall == 1 and short.required == n and short.available == n - 1


# ------------------------------------------------------------------ derivations are the documented ones
@pytest.mark.parametrize("span", [1, 2, 8, 21, 50, 200])
def test_ema_bars_meets_tolerance_and_is_minimal(span):
    alpha = 2 / (span + 1)
    k = H.ema_bars(span) - 1
    if alpha >= 1:                      # span 1: the EMA is the price itself, nothing to converge
        assert H.ema_bars(span) == 1
        return
    assert (1 - alpha) ** k <= H.SEED_WEIGHT_TOLERANCE + 1e-12
    if k > 0:
        assert (1 - alpha) ** (k - 1) > H.SEED_WEIGHT_TOLERANCE


@pytest.mark.parametrize("period", [1, 2, 7, 14, 30])
def test_rsi_bars_meets_tolerance(period):
    k = H.rsi_bars(period) - 2
    if period == 1:                     # alpha 1: no smoothing, only the first difference's extra price
        assert H.rsi_bars(period) == 2
        return
    assert (1 - 1 / period) ** k <= H.SEED_WEIGHT_TOLERANCE + 1e-12


def test_empirical_ema_seed_influence_is_within_tolerance_at_declared_bars():
    """Measured, not assumed: perturb the first close; by the declared bar count the EMA moves by <=1% of the perturbation."""
    base = _candles()
    for span in (8, 21, 55):
        bumped = base.copy()
        bumped.iloc[0, bumped.columns.get_loc("close")] += 50.0
        n = H.ema_bars(span)
        a = exponential_moving_average(base, span).iloc[n - 1]
        b = exponential_moving_average(bumped, span).iloc[n - 1]
        assert abs(a - b) <= 0.01 * 50.0 + 1e-9


def test_atr_declared_bars_equals_first_defined_row():
    c = _candles(60)
    for period in (5, 14, 30):
        first = int(average_true_range(c, period).notna().values.argmax()) + 1
        assert H.atr_bars(period) == first


# ------------------------------------------------------------------ the shipped strategies
def test_builtin_strategies_are_parameter_aware():
    a = EMACrossStrategy("X", fast=8, slow=21).history_requirement().min_bars
    b = EMACrossStrategy("X", fast=50, slow=200).history_requirement().min_bars
    assert b > a and b == H.ema_bars(200)
    r14 = RSIMeanReversionStrategy("X", period=14).history_requirement().min_bars
    r30 = RSIMeanReversionStrategy("X", period=30).history_requirement().min_bars
    assert r30 > r14
    # thresholds are not periods: changing them never changes the requirement
    assert RSIMeanReversionStrategy("X", period=14, oversold=10, overbought=99).history_requirement().min_bars == r14
    v = EMACrossVolFilterStrategy("X", fast=8, slow=21, atr_period=100).history_requirement()
    assert v.min_bars == 100 and any(c.name == "ATR(100)" for c in v.components)


def test_strategies_with_a_dominant_ema_or_atr_term():
    assert EMACrossVolFilterStrategy("X").history_requirement().min_bars == H.ema_bars(21)
    assert EMACrossStrategy("X", fast=2, slow=3).history_requirement().min_bars == H.ema_bars(3)


@pytest.mark.parametrize("strategy", [EMACrossStrategy("X"), EMACrossVolFilterStrategy("X"),
                                      RSIMeanReversionStrategy("X")], ids=lambda s: s.name)
def test_declared_bars_cover_every_indicator_column_becoming_defined(strategy):
    """Contract: declared warm-up >= the measured first row where every indicator column the strategy decides on is defined."""
    c = _candles()
    prepared = strategy.prepare(c)
    cols = [col for col in prepared.columns if col not in c.columns]
    assert cols
    first_defined = int(prepared[cols].notna().all(axis=1).values.argmax()) + 1
    assert strategy.history_requirement().min_bars >= first_defined


def test_ai_feature_requirement_covers_measured_feature_formation():
    spec = FeatureSpec()
    builder = FeatureBuilder(spec)
    feats = builder.build(_candles())
    first = int(feats.notna().all(axis=1).values.argmax()) + 1
    comps = (*(H.pct_change_component(n) for n in spec.return_periods), H.ema_component(spec.ema_fast),
             H.ema_component(spec.ema_slow), H.rsi_component(spec.rsi_period), H.atr_component(spec.atr_period),
             H.macd_component(spec.macd_fast, spec.macd_slow, spec.macd_signal), H.rolling_component("volume_avg", spec.volume_window))
    assert H.combine(*comps).min_bars >= first


def test_every_registered_production_strategy_declares_a_valid_requirement():
    """No registered built-in/promoted strategy may be undeclared (ai_signal needs a model, covered in its own test)."""
    for name in available_strategies():
        cls = get_strategy_class(name)
        if not cls.__module__.startswith("src.strategies"):
            continue                       # test-registered fixtures are declared in their own modules
        assert callable(getattr(cls, "history_requirement", None)), name


def test_no_heuristic_remains_in_the_scanner():
    import src.scanner.engine as engine
    assert not hasattr(engine, "scanner_min_bars") and not hasattr(engine, "_PERIOD_NAME")
    assert not hasattr(engine, "MIN_BARS_FLOOR") and not hasattr(engine, "MIN_BARS_CAP")
    assert math.isfinite(H.SEED_WEIGHT_TOLERANCE)


# ------------------------------------------------------------------ experiment provenance
def test_experiment_spec_records_the_declared_requirement_against_the_window_it_ran_on():
    from src.data.base import Interval
    from src.experiments.spec import ExperimentSpec
    from src.risk.models import RiskLimits

    s = EMACrossStrategy("X", fast=8, slow=21)
    ok = ExperimentSpec.capture(s, _candles(200), RiskLimits(), symbol="X", interval=Interval.DAY_1, dataset_source="t")
    h = ok.backtest_config["history"]
    assert h["status"] == "DECLARED" and h["satisfied"] and h["required"] == s.history_requirement().min_bars
    short = ExperimentSpec.capture(s, _candles(10), RiskLimits(), symbol="X", interval=Interval.DAY_1, dataset_source="t")
    assert short.backtest_config["history"]["satisfied"] is False and short.backtest_config["history"]["shortfall"] > 0

    class Bare:
        name, params = "bare", {}

    undeclared = ExperimentSpec.capture(Bare(), _candles(200), RiskLimits(), symbol="X", interval=Interval.DAY_1,
                                        dataset_source="t", strategy_params={})
    assert undeclared.backtest_config["history"]["status"] == "UNDECLARED" and undeclared.backtest_config["history"]["satisfied"] is False


def test_other_backtest_config_keys_survive_the_history_annotation():
    from src.data.base import Interval
    from src.experiments.spec import ExperimentSpec
    from src.risk.models import RiskLimits

    spec = ExperimentSpec.capture(EMACrossStrategy("X"), _candles(200), RiskLimits(), symbol="X", interval=Interval.DAY_1,
                                  dataset_source="t", backtest_config={"execution": {"timing": "SIGNAL_BAR_CLOSE"}})
    assert spec.backtest_config["execution"] == {"timing": "SIGNAL_BAR_CLOSE"} and "history" in spec.backtest_config
