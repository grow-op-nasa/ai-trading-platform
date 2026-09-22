"""End-to-end Sprint 10 integration test (`DECISIONS.md` ADR-0043) --
the single most important test in this sprint (Sprint 10 spec, section
46): synthetic canonical candles all the way through feature
engineering, label construction, chronological training, a persisted
model artifact, `AISignalStrategy`, `Backtester`, and
`AnalyticsService`, with no network access anywhere in the chain.

Gated on scikit-learn/joblib (see `tests/test_ai_model.py`'s module
docstring).
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("joblib")

from src.ai.artifacts import ModelArtifactStore
from src.ai.registry import ModelRegistry
from src.ai.training import MLTrainingService
from src.analytics.service import AnalyticsService
from src.backtesting.engine import Backtester
from src.data.base import Interval
from src.data.canonical import canonicalize_candles
from src.data.models import CandleDataset, SessionPolicy, ValidationReport
from src.experiments.spec import ExperimentSpec
from src.risk.models import RiskLimits
from src.strategies.ai_signal import AISignalStrategy
from src.utils.hashing import dataframe_fingerprint


def _make_candles(n: int = 300, seed: int = 99) -> pd.DataFrame:
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-0.02, 0.021)))
    dates = pd.date_range("2022-01-01", periods=n, freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": [rng.uniform(800.0, 1200.0) for _ in range(n)],
        },
        index=dates,
    )


def _make_dataset(candles: pd.DataFrame, symbol: str = "QQQ") -> CandleDataset:
    canonical = canonicalize_candles(candles)
    return CandleDataset(
        symbol=symbol,
        interval=Interval.DAY_1,
        provider="synthetic",
        candles=canonical,
        content_hash=dataframe_fingerprint(canonical),
        validation_report=ValidationReport(symbol=symbol, interval=Interval.DAY_1),
        session_policy=SessionPolicy.REGULAR,
        session_timezone="America/New_York",
        requested_start=canonical.index[0].date(),
        requested_end=canonical.index[-1].date(),
        dataset_start=canonical.index[0],
        dataset_end=canonical.index[-1],
    )


def test_full_pipeline_from_candles_to_analytics(tmp_path):
    candles = _make_candles()
    dataset = _make_dataset(candles)

    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    service = MLTrainingService(registry=registry, artifact_store=store)

    # 1. Train: features -> labels -> chronological split -> fit ->
    #    evaluate -> persist artifact + metadata.
    training_result = service.train(dataset)
    assert training_result.test_metrics["status"] == "ok"

    # 2. Build the AI strategy from the persisted, frozen model.
    strategy = AISignalStrategy(
        symbol="QQQ",
        model_id=training_result.model_id,
        min_probability=0.4,
        registry=registry,
    )

    # 3. Run it through the exact, unmodified Backtester -- no
    #    AI-specific branch anywhere in that class.
    backtest_result = Backtester().run(strategy, candles, dataset=dataset)
    assert backtest_result.dataset_identity == dataset.identity

    # 4. Feed the result through the exact, unmodified AnalyticsService
    #    -- trading performance, computed independently of the model's
    #    own classification-quality metrics from step 1.
    spec = ExperimentSpec.capture(
        strategy,
        candles,
        RiskLimits(),
        symbol="QQQ",
        interval="1d",
        dataset_source="synthetic",
    )
    assert spec.strategy_name == "ai_signal"
    assert spec.strategy_params["model_id"] == training_result.model_id

    analytics = AnalyticsService().analyze_backtest(backtest_result, spec=spec)
    assert analytics is not None
    # The analytics computation must succeed and produce the same
    # trade count the backtest itself reports -- no separate,
    # AI-specific P&L formula anywhere in this chain (Sprint 10 spec,
    # section 58).
    assert analytics.trade_count == len(backtest_result.trades)


def test_repeated_backtests_against_the_same_frozen_model_are_identical(tmp_path):
    # Sprint 10 spec, section 48: run the same trained model twice
    # against the same held-out dataset -- signals, trades, equity
    # curve, and analytics must be identical, with no training during
    # either run.
    candles = _make_candles()
    dataset = _make_dataset(candles)

    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    service = MLTrainingService(registry=registry, artifact_store=store)
    training_result = service.train(dataset)

    def run_once():
        strategy = AISignalStrategy(
            symbol="QQQ",
            model_id=training_result.model_id,
            min_probability=0.4,
            registry=registry,
        )
        result = Backtester().run(strategy, candles, dataset=dataset)
        analytics = AnalyticsService().analyze_backtest(result)
        return result, analytics

    result_a, analytics_a = run_once()
    result_b, analytics_b = run_once()

    assert [s.direction for s in result_a.signals] == [s.direction for s in result_b.signals]
    assert [s.timestamp for s in result_a.signals] == [s.timestamp for s in result_b.signals]

    # Compare trades on their economically meaningful fields only.
    # entry_signal_id/exit_signal_id are excluded deliberately: `Signal.id`
    # is `field(default_factory=uuid4)` (src/signals/models.py), so two
    # independent runs mint fresh random ids for otherwise-identical
    # signals even when the model and its predictions are fully
    # deterministic. That randomness is pre-existing Signal design, not
    # something Sprint 10 introduces or should mask by changing Signal --
    # so the test asserts on trade economics instead of raw identity.
    def _trade_economics(trades):
        return [
            (t.entry_time, t.exit_time, t.direction, t.entry_price, t.exit_price)
            for t in trades
        ]

    assert _trade_economics(result_a.trades) == _trade_economics(result_b.trades)
    pd.testing.assert_series_equal(result_a.equity_curve, result_b.equity_curve)
    assert analytics_a == analytics_b


def test_ai_strategy_runs_through_the_portfolio_aware_backtester(tmp_path):
    # Sprint 11 (DECISIONS.md, ADR-0044): the important architectural
    # milestone spec section 61 calls out -- a trained ML model's
    # AISignalStrategy must traverse the exact same portfolio-aware risk
    # path (PortfolioRiskEngine -> Portfolio -> Analytics) as any other
    # strategy, with zero AI-specific code anywhere in that path. This
    # exercises run_portfolio() rather than the legacy run() the other
    # tests in this module use.
    from src.backtesting.config import BacktestConfig
    from src.backtesting.stop_policy import ATRStopPolicy
    from src.risk.models import PortfolioRiskLimits, RiskLimits

    candles = _make_candles(n=400, seed=7)
    dataset = _make_dataset(candles, symbol="QQQ")

    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    training_result = MLTrainingService(registry=registry, artifact_store=store).train(dataset)

    strategy = AISignalStrategy(
        symbol="QQQ", model_id=training_result.model_id, min_probability=0.4, registry=registry
    )
    config = BacktestConfig(
        initial_cash=100_000.0,
        stop_policy=ATRStopPolicy(period=14, multiple=2.0),
        risk_limits=RiskLimits(allocation_per_trade_pct=0.2, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.01),
    )

    result = Backtester().run_portfolio({"QQQ": strategy}, {"QQQ": candles}, config, datasets={"QQQ": dataset})

    from src.backtesting.config import RiskMode

    assert result.risk_mode is RiskMode.PORTFOLIO_RISK
    assert result.final_portfolio is not None
    assert result.dataset_identities == {"QQQ": dataset.identity}
    # Every trade produced through this path carries a real, risk-approved
    # quantity -- never a fabricated unit size, and never requiring the
    # engine to know this strategy is AI-driven at all.
    for trade in result.trades:
        assert trade.quantity is not None
        assert trade.quantity > 0

    analytics = AnalyticsService().analyze_backtest(result)
    assert analytics.trade_count == len(result.trades)
    # If any trades closed, the dollar-based metrics must be populated --
    # the same analytics path a non-AI PORTFOLIO_RISK result gets.
    if result.trades:
        assert analytics.has_quantity_detail is True


def test_experiment_is_reproducible_from_its_recorded_spec(tmp_path):
    # The reproducibility claim Sprint 10 spec section 31 asks for:
    # given only what ExperimentSpec recorded (strategy name + params),
    # rebuilding the strategy and re-running it against the same
    # candles reproduces the same signals.
    candles = _make_candles()
    dataset = _make_dataset(candles)

    store = ModelArtifactStore(tmp_path)
    registry = ModelRegistry(tmp_path, artifact_store=store)
    training_result = MLTrainingService(registry=registry, artifact_store=store).train(dataset)

    original = AISignalStrategy(
        symbol="QQQ", model_id=training_result.model_id, min_probability=0.4, registry=registry
    )
    spec = ExperimentSpec.capture(
        original, candles, RiskLimits(), symbol="QQQ", interval="1d", dataset_source="synthetic"
    )

    from src.strategies.registry import get_strategy_class

    reconstructed_cls = get_strategy_class(spec.strategy_name)
    reconstructed = reconstructed_cls(symbol=spec.symbol, registry=registry, **spec.strategy_params)

    original_result = Backtester().run(original, candles)
    reconstructed_result = Backtester().run(reconstructed, candles)
    assert [s.direction for s in original_result.signals] == [
        s.direction for s in reconstructed_result.signals
    ]
