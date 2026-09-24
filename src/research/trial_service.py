"""The Research Trial Service -- src/research/trial_service.py.

Sprint 13 spec, section 25 (Phase 6): a small, generic, reusable
application service composing

    MarketDataService -> Strategy -> PortfolioBacktestEngine (Risk,
    Execution) -> Analytics

into one bounded historical research trial. This is deliberately
**generic research infrastructure, not agent-specific code** -- the
long-term shape (Sprint 13 spec, section 25):

    CLI / Dashboard / Agent / Notebook / future API
                    |
                    v
        Research Trial Service  (this module)
                    |
                    v
        existing domain capabilities

`src.ai.agents.backtest_tool.RunHistoricalBacktestTool` is one caller of
this service, not a wrapper it depends on; `scripts/run_experiment.py`
remains its own, separate worked example and is never invoked by this
module or by the agent (Sprint 13 spec, section 24: "do not make the
agent shell out to scripts/run_experiment.py").

Deliberately does **not** persist anything to `src.experiments.
ExperimentRegistry` -- every call produces an in-memory
`ResearchTrialOutcome` only (Sprint 13 spec, section 40, "ephemeral
research trials"); a caller that wants to keep one is responsible for
its own explicit, human-facing save step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.ai.registry import ModelRegistry
from src.analytics.models import BacktestAnalytics
from src.analytics.service import AnalyticsService
from src.backtesting.config import BacktestConfig
from src.backtesting.engine import Backtester
from src.backtesting.execution_model import (
    ExecutionConfig,
    ExecutionTiming,
    PercentageFeeModel,
    PercentageSlippageModel,
)
from src.backtesting.models import BacktestResult
from src.backtesting.stop_policy import ATRStopPolicy
from src.data.base import Interval
from src.data.service import MarketDataService
from src.experiments.spec import ExperimentSpec
from src.risk.models import PortfolioRiskLimits, RiskLimits
from src.strategies.registry import get_strategy_class

DEFAULT_STOP_PERIOD = 14
DEFAULT_STOP_MULTIPLE = 2.0


@dataclass(frozen=True)
class ResearchTrialOutcome:
    """Everything one `ResearchTrialService.run_trial()` call produced --
    a plain, agent-agnostic bundle of existing platform result types
    (never a new parallel result shape).

    Args:
        trial_id: a fresh identifier for this trial (an event, not a
            reusable spec -- matches `AgentRun.run_id`'s own reasoning).
        result: the full `BacktestResult` (`RiskMode.PORTFOLIO_RISK`).
        spec: the `ExperimentSpec` capturing this trial's exact
            strategy/dataset/risk/execution identity.
        analytics: the `BacktestAnalytics` computed via the platform's
            own `AnalyticsService` -- never recomputed by a caller.
        model_id / feature_set_id / label_set_id: populated only when
            `strategy_name` was `"ai_signal"` (`None` otherwise).
    """

    trial_id: str
    result: BacktestResult
    spec: ExperimentSpec
    analytics: BacktestAnalytics
    model_id: str | None = None
    feature_set_id: str | None = None
    label_set_id: str | None = None


class ResearchTrialService:
    """Composes existing platform capabilities into one bounded
    historical research trial -- implements no backtest, risk,
    execution, or analytics logic of its own.

    Args:
        market_data_service: defaults to `MarketDataService()`.
        model_registry: defaults to `ModelRegistry()` -- consulted only
            when `strategy_name == "ai_signal"`, to surface
            `feature_set_id`/`label_set_id` provenance alongside the
            trial (Sprint 13 spec, section 55).
    """

    def __init__(
        self,
        market_data_service: MarketDataService | None = None,
        model_registry: ModelRegistry | None = None,
    ) -> None:
        self._data = market_data_service or MarketDataService()
        self._models = model_registry or ModelRegistry()

    def run_trial(
        self,
        *,
        strategy_name: str,
        strategy_params: dict[str, Any],
        symbol: str,
        interval: str,
        start: date,
        end: date,
        initial_cash: float = 100_000.0,
        allocation_per_trade_pct: float = 0.10,
        max_portfolio_exposure_pct: float = 0.50,
        risk_pct_per_trade: float = 0.01,
        max_symbol_exposure_pct: float | None = None,
        max_concurrent_positions: int | None = None,
        stop_period: int = DEFAULT_STOP_PERIOD,
        stop_multiple: float = DEFAULT_STOP_MULTIPLE,
        execution_timing: str = ExecutionTiming.SIGNAL_BAR_CLOSE.value,
        slippage_bps: float = 0.0,
        fee_bps: float = 0.0,
        fixed_fee: float = 0.0,
    ) -> ResearchTrialOutcome:
        """Run one historical, portfolio-aware backtest trial.

        Every argument is a plain primitive (not a domain object) so
        both a CLI and an agent tool can call this directly without
        constructing `RiskLimits`/`ExecutionConfig`/etc. themselves --
        this method is where those are built, once, from validated
        primitives.

        Raises:
            ValueError: `end` is in the future (this service only ever
                runs against historical data, Sprint 13 spec, section
                28), `start` is after `end`, or `strategy_name` isn't
                registered.
            KeyError: `strategy_name` isn't registered
                (`src.strategies.registry.get_strategy_class`).
            NoDataError: no candle data is available for the request.
            DataValidationError: the resulting dataset failed
                validation.
        """
        if end > date.today():
            raise ValueError(
                f"end ({end}) is in the future -- this service only runs "
                f"against historical data"
            )
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")

        interval_enum = Interval(interval)
        strategy_cls = get_strategy_class(strategy_name)
        strategy = strategy_cls(symbol=symbol, **strategy_params)

        dataset = self._data.get_dataset(symbol, start=start, end=end, interval=interval_enum)
        candles = dataset.candles

        risk_limits = RiskLimits(
            allocation_per_trade_pct=allocation_per_trade_pct,
            max_portfolio_exposure_pct=max_portfolio_exposure_pct,
        )
        portfolio_risk_limits = PortfolioRiskLimits(
            risk_pct_per_trade=risk_pct_per_trade,
            max_symbol_exposure_pct=max_symbol_exposure_pct,
            max_concurrent_positions=max_concurrent_positions,
        )
        stop_policy = ATRStopPolicy(period=stop_period, multiple=stop_multiple)
        execution_config = ExecutionConfig(
            timing=ExecutionTiming(execution_timing),
            slippage_model=PercentageSlippageModel(slippage_bps),
            fee_model=PercentageFeeModel(fee_bps=fee_bps, fixed_fee=fixed_fee),
        )
        config = BacktestConfig(
            initial_cash=initial_cash,
            risk_limits=risk_limits,
            portfolio_risk_limits=portfolio_risk_limits,
            stop_policy=stop_policy,
            execution_config=execution_config,
        )

        result = Backtester().run_portfolio(
            {symbol: strategy}, {symbol: candles}, config, datasets={symbol: dataset}
        )
        spec = ExperimentSpec.capture(
            strategy,
            candles,
            risk_limits,
            symbol=symbol,
            interval=interval_enum,
            dataset_source=dataset.provider,
            backtest_config=config.describe(),
        )
        analytics = AnalyticsService().analyze_backtest(result, spec=spec)

        model_id: str | None = None
        feature_set_id: str | None = None
        label_set_id: str | None = None
        if strategy_name == "ai_signal":
            model_id = strategy_params.get("model_id")
            if model_id:
                metadata = self._models.get_metadata(model_id)
                if metadata is not None:
                    feature_set_id = metadata.feature_set_id
                    label_set_id = metadata.label_spec_id

        return ResearchTrialOutcome(
            trial_id=uuid.uuid4().hex,
            result=result,
            spec=spec,
            analytics=analytics,
            model_id=model_id,
            feature_set_id=feature_set_id,
            label_set_id=label_set_id,
        )
