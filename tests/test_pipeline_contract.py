"""The end-to-end pipeline contract test -- Sprint 6 (`DECISIONS.md`,
ADR-0035).

One deterministic run through the entire research pipeline named in
`ROADMAP.md`'s target chain:

    Strategy -> Signal -> Backtest -> Risk -> Execution -> Trade
             -> Performance -> Attribution -> Experiment Registry
             -> (spec) -> reconstruction -> Research Report

Deliberately one thorough test, not thirty superficial ones -- every
existing module already has its own unit tests; what nothing previously
proved is that all of them compose together, that an experiment's
outputs actually land in the registry connected to each other, and that
the whole thing can be reconstructed from what was stored. That
composition claim is what this test exists to protect.

Uses the same known-good EMA-crossover candle shape already proven (in
`tests/test_ema_cross_strategy.py`, reused by
`tests/test_integration_paper_trading.py`) to produce at least one LONG
followed by a FLAT with `fast=2, slow=4` -- no assertion here
hand-predicts EMA values; everything is derived from whatever the run
actually produces.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.attribution.engine import PerformanceAttributor
from src.backtesting.engine import Backtester
from src.execution.engine import PaperBroker
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.research.reporter import ResearchReporter
from src.risk.engine import PositionSizer
from src.risk.models import RiskLimits
from src.signals.models import SignalDirection
from src.strategies.ema_cross import EMACrossStrategy

SYMBOL = "SPY"
CLOSES = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]


def make_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


def run_signals_through_risk_and_execution(signals, candles, sizer, broker, symbol=SYMBOL):
    """Drive `signals` through `PositionSizer` -> `PaperBroker`, exactly
    as `tests/test_integration_paper_trading.py` does -- the Risk ->
    Execution -> Trade(Fill) leg of the pipeline that `Backtester`'s own
    single-unit model (ADR-0011) doesn't exercise on its own.
    """
    fills = []
    for signal in signals:
        price = float(candles.loc[signal.timestamp, "close"])
        if signal.direction is SignalDirection.FLAT:
            fills.append(broker.submit_signal(signal, symbol, price))
        else:
            decision = sizer.size(signal, broker.account_state, price)
            assert decision.approved, f"expected {signal.direction} to be approved: {decision.reason}"
            fills.append(broker.submit_signal(signal, symbol, price, sizing_decision=decision))
    return fills


def test_full_research_pipeline_composes_and_is_reproducible(tmp_path):
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(symbol=SYMBOL, fast=2, slow=4, confidence=0.65)

    # Strategy -> Signal -> Backtest -> Trade -> Performance
    result = Backtester().run(strategy, candles)
    assert result.signals, "sanity check: this candle shape must produce signals"
    assert result.trades, "sanity check: this candle shape must produce at least one trade"
    assert all(s.symbol == SYMBOL for s in result.signals)

    # Signal -> Risk -> Execution -> Trade(Fill), independently of
    # Backtester's own simplified sizing -- proves the same signals also
    # compose through the paper-trading leg of the pipeline.
    risk_limits = RiskLimits(allocation_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50)
    sizer = PositionSizer(risk_limits)
    broker = PaperBroker(starting_cash=100_000)
    fills = run_signals_through_risk_and_execution(result.signals, candles, sizer, broker)
    assert len(fills) == len(result.signals)

    # Result -> Attribution
    attribution = PerformanceAttributor().run(result, candles, trend_fast=2, trend_slow=4)
    assert attribution.total_trades == len(result.trades)

    # Result -> Research Report
    report = ResearchReporter().run(result, attribution)
    assert report.findings is not None
    assert report.rendered_by in ("fallback", "claude")

    # Outputs -> Experiment Registry, including the new ADR-0035 spec.
    registry = ExperimentRegistry(db_path=tmp_path / "experiments.db")
    experiment_id = registry.log_experiment(
        changed={"fast": [12, 2], "slow": [26, 4]},
        metrics_before={},
        metrics_after=result.metrics,
        decision="KEEP",
        strategy_name=strategy.name,
    )
    registry.save_signals(experiment_id, result.signals)

    spec = ExperimentSpec.capture(
        strategy,
        candles,
        risk_limits,
        symbol=SYMBOL,
        interval="1d",
        dataset_source="test-fixture",
    )
    registry.save_spec(experiment_id, spec)

    # ---- Integrity: experiment records preserve what produced them ----
    fetched_signals = registry.get_signals(experiment_id)
    assert {s.id for s in fetched_signals} == {s.id for s in result.signals}
    assert len(fetched_signals) == len(result.signals)
    assert all(s.symbol == SYMBOL for s in fetched_signals)

    fetched_spec = registry.get_spec(experiment_id)
    assert fetched_spec == spec
    assert fetched_spec.strategy_name == "ema_cross"
    assert fetched_spec.strategy_params == {"fast": 2, "slow": 4, "confidence": 0.65}

    # ---- Reproducibility: reconstruct the experiment from its spec ----
    assert fetched_spec.verify_strategy_version() is True
    assert fetched_spec.verify_dataset(candles) is True

    rebuilt_strategy = fetched_spec.reconstruct_strategy()
    assert isinstance(rebuilt_strategy, EMACrossStrategy)
    assert rebuilt_strategy.symbol == SYMBOL

    rebuilt_signals = rebuilt_strategy.generate_signals(rebuilt_strategy.prepare(candles))

    # Deterministic: a strategy reconstructed purely from its stored
    # name + params, run against the same (fingerprint-verified) candles,
    # reproduces the same decisions -- same timestamps, directions,
    # confidences, symbol -- as the original run. Signal `id`s are
    # expected to differ (each Signal mints its own UUID at construction
    # time); everything that constitutes the actual decision must not.
    def decision_shape(signals):
        return [(s.timestamp, s.symbol, s.direction, s.confidence) for s in signals]

    assert decision_shape(rebuilt_signals) == decision_shape(result.signals)


def test_a_changed_strategy_implementation_is_detected_by_verify_strategy_version(tmp_path):
    # The exact scenario ADR-0035 exists for: an experiment recorded
    # against one version of a strategy must not silently claim to match
    # a *different* implementation later.
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(symbol=SYMBOL, fast=2, slow=4)
    spec = ExperimentSpec.capture(
        strategy,
        candles,
        RiskLimits(),
        symbol=SYMBOL,
        interval="1d",
        dataset_source="test-fixture",
    )

    # Simulate the implementation having changed since capture: the spec
    # recorded a version that no longer matches the registered class's
    # actual current source.
    stale_spec = ExperimentSpec(
        strategy_name=spec.strategy_name,
        strategy_version="0" * 64,  # not a real hash of anything
        strategy_params=spec.strategy_params,
        symbol=spec.symbol,
        interval=spec.interval,
        dataset_start=spec.dataset_start,
        dataset_end=spec.dataset_end,
        dataset_source=spec.dataset_source,
        dataset_fingerprint=spec.dataset_fingerprint,
        risk_config=spec.risk_config,
    )

    assert stale_spec.verify_strategy_version() is False
    # Dataset identity is unaffected -- these are independent checks.
    assert stale_spec.verify_dataset(candles) is True


def test_a_changed_dataset_is_detected_by_verify_dataset(tmp_path):
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(symbol=SYMBOL, fast=2, slow=4)
    spec = ExperimentSpec.capture(
        strategy,
        candles,
        RiskLimits(),
        symbol=SYMBOL,
        interval="1d",
        dataset_source="test-fixture",
    )

    revised_candles = candles.copy()
    revised_candles.loc[revised_candles.index[0], "close"] = 12345.0

    assert spec.verify_dataset(revised_candles) is False
