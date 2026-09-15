"""A worked example: one real experiment run through the entire
research pipeline (`ROADMAP.md`, Sprint 6 close-out item 1).

`tests/test_pipeline_contract.py` already proves the pipeline
*composes*, using a synthetic fixture. This script proves the same
wiring is usable end to end by an actual person running an actual
command -- construct a strategy by name, run it against real candles,
size and fill through risk/execution, attribute performance, render a
research report, and persist the whole thing (including its
`ExperimentSpec`, ADR-0035) to the Experiment Registry.

    python scripts/run_experiment.py --symbol SPY --strategy ema_cross
    python scripts/run_experiment.py --symbol QQQ --strategy rsi_mean_reversion --period 1y

Deliberately a script, not a new `src/` module (`DECISIONS.md`,
ADR-0021 already flags a reusable orchestration layer -- a
"PaperTradingLoop" -- as deferred future work, not this round's job).
`run_experiment()` itself is a plain, network-free function so it stays
directly unit-testable (`tests/test_run_experiment_script.py`); only
`main()` touches the network, and only when this file is actually run.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import pandas as pd

from src.attribution.engine import PerformanceAttributor
from src.attribution.models import AttributionReport  # noqa: F401 -- re-exported type for ExperimentRunResult
from src.backtesting.engine import Backtester
from src.backtesting.models import BacktestResult
from src.execution.engine import PaperBroker
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.research.models import ResearchReport
from src.research.reporter import ResearchReporter
from src.risk.engine import PositionSizer
from src.risk.models import RiskLimits
from src.signals.models import SignalDirection
from src.strategies.registry import available_strategies, get_strategy_class

DEFAULT_STRATEGY = "ema_cross"
DEFAULT_SYMBOL = "SPY"
DEFAULT_PERIOD = "2y"
DEFAULT_ALLOCATION = 0.10


@dataclass
class ExperimentRunResult:
    """Everything one `run_experiment()` call produced, bundled for a
    caller (or a test) to inspect without re-deriving any of it."""

    experiment_id: int
    spec: ExperimentSpec
    result: BacktestResult
    attribution: AttributionReport
    report: ResearchReport


def _fill_signals_through_risk_and_execution(
    signals, candles: pd.DataFrame, sizer: PositionSizer, broker: PaperBroker, symbol: str
) -> None:
    """Drive `signals` through `PositionSizer` -> `PaperBroker` -- the
    Risk -> Execution -> Trade(Fill) leg of the pipeline that
    `Backtester.run()`'s own single-unit model (ADR-0011) doesn't
    exercise on its own. Mirrors
    `tests/test_pipeline_contract.py::run_signals_through_risk_and_execution`
    and `tests/test_integration_paper_trading.py` -- the same pattern,
    reused rather than reinvented, in a real (not test-only) caller.

    A sizing decision that isn't approved (e.g. it would breach
    `max_portfolio_exposure_pct`) is skipped rather than forced through
    -- `PositionSizer` already decided that trade shouldn't happen.
    """
    for signal in signals:
        price = float(candles.loc[signal.timestamp, "close"])
        if signal.direction is SignalDirection.FLAT:
            broker.submit_signal(signal, symbol, price)
            continue
        decision = sizer.size(signal, broker.account_state, price)
        if not decision.approved:
            continue
        broker.submit_signal(signal, symbol, price, sizing_decision=decision)


def run_experiment(
    *,
    strategy_name: str,
    symbol: str,
    candles: pd.DataFrame,
    strategy_params: dict | None = None,
    risk_limits: RiskLimits | None = None,
    interval: str = "1d",
    dataset_source: str = "yfinance",
    registry: ExperimentRegistry | None = None,
) -> ExperimentRunResult:
    """Wire one strategy + one candle set through the entire research
    pipeline and persist the result. Network-free -- `candles` is
    supplied by the caller, never fetched here (see `main()` for the
    network-touching CLI wrapper).

    Args:
        strategy_name: a name registered via `@register_strategy`
            (`src.strategies.registry.available_strategies()`).
        symbol: the instrument `candles` represents.
        candles: OHLCV data to run the strategy against.
        strategy_params: constructor keyword arguments besides
            `symbol`. Defaults to `{}` (the strategy's own defaults).
        risk_limits: position-sizing configuration. Defaults to
            `RiskLimits()`.
        interval: the candle interval `candles` represents, recorded on
            the `ExperimentSpec` only (not validated against the data).
        dataset_source: where `candles` came from, recorded on the
            `ExperimentSpec`.
        registry: an `ExperimentRegistry` to persist into. Defaults to
            one at `ExperimentRegistry`'s own default database path.

    Returns:
        An `ExperimentRunResult` bundling the experiment id, its spec,
        the backtest result, the attribution report, and the research
        report.

    Raises:
        KeyError: `strategy_name` isn't registered.
    """
    strategy_params = dict(strategy_params) if strategy_params else {}
    risk_limits = risk_limits or RiskLimits()
    registry = registry or ExperimentRegistry()

    strategy_cls = get_strategy_class(strategy_name)
    strategy = strategy_cls(symbol=symbol, **strategy_params)

    result = Backtester().run(strategy, candles)

    sizer = PositionSizer(risk_limits)
    broker = PaperBroker(starting_cash=100_000)
    _fill_signals_through_risk_and_execution(result.signals, candles, sizer, broker, symbol)

    attribution = PerformanceAttributor().run(result, candles)
    report = ResearchReporter().run(result, attribution)

    experiment_id = registry.log_experiment(
        changed={},
        metrics_before={},
        metrics_after=result.metrics,
        decision="INCONCLUSIVE",
        strategy_name=strategy.name,
        notes=f"scripts/run_experiment.py worked example -- {symbol}",
    )
    registry.save_signals(experiment_id, result.signals)

    spec = ExperimentSpec.capture(
        strategy,
        candles,
        risk_limits,
        symbol=symbol,
        interval=interval,
        dataset_source=dataset_source,
    )
    registry.save_spec(experiment_id, spec)

    return ExperimentRunResult(
        experiment_id=experiment_id,
        spec=spec,
        result=result,
        attribution=attribution,
        report=report,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one experiment through the full research pipeline and "
        "persist it to the Experiment Registry."
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"default: {DEFAULT_SYMBOL}")
    parser.add_argument(
        "--strategy",
        default=DEFAULT_STRATEGY,
        choices=sorted(available_strategies()),
        help=f"default: {DEFAULT_STRATEGY}",
    )
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help=f"yfinance-style relative period, e.g. 6mo, 1y, 2y (default: {DEFAULT_PERIOD})",
    )
    parser.add_argument(
        "--allocation",
        type=float,
        default=DEFAULT_ALLOCATION,
        help=f"fraction of equity per trade, e.g. 0.10 = 10%% (default: {DEFAULT_ALLOCATION})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Deferred import: only the actual CLI path touches the network or
    # requires yfinance to be installed -- run_experiment() itself does
    # not, and tests import this module without either.
    from src.data.service import MarketDataService

    candles = MarketDataService().get_history(args.symbol, period=args.period)

    run_result = run_experiment(
        strategy_name=args.strategy,
        symbol=args.symbol,
        candles=candles,
        risk_limits=RiskLimits(allocation_per_trade_pct=args.allocation),
        dataset_source="yfinance",
    )

    spec = run_result.spec
    result = run_result.result
    print(f"Experiment #{run_result.experiment_id} -- {args.strategy} on {args.symbol}")
    print(f"  Strategy version: {spec.strategy_version[:12]}...")
    print(f"  Dataset:          {spec.dataset_start.date()} to {spec.dataset_end.date()}")
    print(f"  Dataset fingerprint: {spec.dataset_fingerprint[:12]}...")
    print(f"  Trades: {len(result.trades)}")
    for key, value in result.metrics.items():
        print(f"  {key}: {value}")
    print()
    print(f"Findings ({run_result.report.findings.strategy_name}):")
    for line in run_result.report.findings.as_lines():
        print(f"  {line}")
    print()
    print(f"Narrative ({run_result.report.rendered_by}):")
    print(run_result.report.narrative)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
