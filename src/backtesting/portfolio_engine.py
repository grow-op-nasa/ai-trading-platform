"""Portfolio-aware backtesting -- Sprint 11 (`DECISIONS.md`, ADR-0044).

Closes the architectural gap between research sizing and paper-trading
sizing: before this sprint, `Backtester.run()` sized every trade at one
unit regardless of account size, risk, or what else was already open
(`DECISIONS.md`, ADR-0011), while paper trading already ran every
signal through `PortfolioRiskEngine` -> a real `Portfolio` (Sprint 7,
ADR-0039). `PortfolioBacktestEngine` (invoked via
`Backtester.run_portfolio()`) is the same risk-sizing/portfolio-
admission path, replayed against historical candles instead of live
ones -- so a strategy comparison in research now means something a
paper-trading comparison would materially agree with.

**Composes `PortfolioRiskEngine`, never reimplements it.** This module
constructs a real `src.risk.portfolio_risk.PortfolioRiskEngine` and
calls `.decide()`/`.decide_close()` for every entry/exit -- it holds no
`risk_amount`/`risk_quantity`/`allocation_quantity`-shaped computation
of its own anywhere. The one thing it *does* compute -- a stop price --
is deliberately factored out to `src.backtesting.stop_policy`, a
sizing *input* Risk itself has no opinion about producing.

**A fresh, isolated simulation `Portfolio` every run** (Sprint 11 spec,
section 32) -- never a live or paper-trading one. `run()` constructs
its own `Portfolio(cash=config.initial_cash)` and nothing else ever
touches it.

**Execution-aware fills (Sprint 12, `DECISIONS.md` ADR-0045).** Every
approved order is simulated through an
`src.backtesting.execution_model.ExecutionModel` -- signal-bar-close
timing with zero slippage/fees by default (byte-for-byte identical to
Sprint 11's own fills), or a realistic next-bar-open/slippage/fee
configuration when a caller opts in via `BacktestConfig.
execution_config`. Risk sizing is entirely unaffected either way: the
quantity `PortfolioRiskEngine` approves is fixed *before* Execution
ever runs, and Execution may only fill that exact quantity in full or
not at all (`NO_EXECUTION_BAR`/`INSUFFICIENT_CASH_FOR_FEE`) -- never
scale it up or down. Partial fills, limit/stop orders, and order-book
simulation remain out of scope (Sprint 12 spec, sections 24-26).

**One open position per symbol, no scaling, full close only** -- the
same limitation `Portfolio`/`PortfolioRiskEngine` already enforce
(`DECISIONS.md`, ADR-0039): a signal requesting an unsupported
same-symbol operation (a same-symbol reversal while already open, or a
partial close) is rejected by the risk engine itself
(`RejectionReason.POSITION_SCALING_NOT_SUPPORTED`/
`UNSUPPORTED_POSITION_OPERATION`) and recorded as a rejected
`SignalOutcome` -- this module adds no reversal-handling logic of its
own, because doing so would be exactly the scaling extension Sprint 11
spec section 34 rules out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from src.backtesting.config import BacktestConfig, RiskMode
from src.backtesting.execution_model import (
    INSUFFICIENT_CASH_FOR_FEE,
    ExecutionModel,
    ExecutionOutcome,
)
from src.backtesting.metrics import calculate_metrics
from src.backtesting.models import BacktestResult, Trade
from src.backtesting.risk_audit import SignalOutcome
from src.execution.models import Order, OrderSide
from src.portfolio.models import Portfolio
from src.portfolio.position import PositionSide
from src.risk.portfolio_risk import PortfolioRiskEngine
from src.signals.models import Signal, SignalDirection
from src.strategies.base import Strategy

if TYPE_CHECKING:  # pragma: no cover -- type-checking only, avoids a runtime
    # dependency from src.backtesting on src.data beyond what already exists
    # (BacktestResult already carries DatasetIdentity).
    from src.data.models import CandleDataset


class PortfolioBacktestEngine:
    """Runs one or more strategies, each against its own symbol's
    candles, through a single shared, portfolio-aware risk-sizing
    simulation.

    Args:
        config: the `BacktestConfig` describing this run's cash,
            risk limits, and stop policy. Must have
            `risk_mode == RiskMode.PORTFOLIO_RISK` -- this engine has
            no other mode to run.

    Raises:
        ValueError: `config.risk_mode` is not `RiskMode.PORTFOLIO_RISK`.
    """

    def __init__(self, config: BacktestConfig) -> None:
        if config.risk_mode is not RiskMode.PORTFOLIO_RISK:
            raise ValueError(
                f"PortfolioBacktestEngine only runs RiskMode.PORTFOLIO_RISK, "
                f"got {config.risk_mode}"
            )
        self._config = config
        self._risk_engine = PortfolioRiskEngine(
            config.risk_limits, config.portfolio_risk_limits
        )
        # BacktestConfig.__post_init__ already guarantees this is not None
        # for PORTFOLIO_RISK -- no fallback constructed here.
        self._stop_policy = config.stop_policy
        # Sprint 12 (DECISIONS.md, ADR-0045): every order this engine
        # approves is simulated through this model before it ever
        # touches `portfolio`. `config.execution_config` defaults to
        # signal-bar-close/zero-cost, so a caller who never heard of
        # Sprint 12 gets byte-for-byte Sprint 11 behavior.
        self._execution_model = ExecutionModel(config.execution_config)

    def run(
        self,
        strategies: dict[str, Strategy],
        candles: dict[str, pd.DataFrame],
        datasets: "dict[str, CandleDataset] | None" = None,
    ) -> BacktestResult:
        """Run every `strategies[symbol]` against `candles[symbol]`,
        processing the combined, chronologically-sorted signal stream
        against one shared `Portfolio`.

        Args:
            strategies: one `Strategy` per symbol. Each strategy runs
                independently against its own candles (unaware any
                other symbol exists) -- only the resulting signals are
                combined, exactly the composition Sprint 11 spec
                section 29 requires so no signal source, including a
                model-driven one, needs any special handling here at
                all.
            candles: OHLCV data per symbol, keyed identically to
                `strategies`.
            datasets: optional per-symbol `CandleDataset` provenance
                (`src.data.MarketDataService.get_dataset()`), purely
                additive like `Backtester.run()`'s own `dataset`
                parameter.

        Raises:
            ValueError: `strategies` and `candles` don't share exactly
                the same symbol keys, `strategies` is empty, or some
                strategy's `generate_signals()` doesn't return a
                `list[Signal]`.
        """
        if not strategies:
            raise ValueError("run_portfolio() requires at least one symbol")
        if set(strategies) != set(candles):
            raise ValueError(
                f"strategies and candles must share exactly the same symbol "
                f"keys -- got strategies={sorted(strategies)}, "
                f"candles={sorted(candles)}"
            )

        signals = self._generate_all_signals(strategies, candles)
        signals_by_timestamp = self._group_signals_by_timestamp(signals)

        portfolio = Portfolio(cash=self._config.initial_cash)
        merged_index = self._merged_timeline(candles)
        ffilled_closes = {
            symbol: df["close"].reindex(merged_index, method="ffill")
            for symbol, df in candles.items()
        }

        trades: list[Trade] = []
        outcomes: list[SignalOutcome] = []
        open_trades: dict[str, dict] = {}
        equity_index: list[pd.Timestamp] = []
        equity_values: list[float] = []

        # A single forward pass over the merged bar timeline: at each
        # timestamp, first apply whatever signals land exactly there
        # (mutating `portfolio` via real fills only), then read off
        # that bar's mark-to-market equity. This keeps trade extraction
        # and the equity curve as one coherent replay of the same
        # events, rather than two passes that could disagree, and
        # avoids ever recomputing a full portfolio valuation from
        # scratch or rescanning the entire candle set per signal
        # (Sprint 11 spec, section 60) -- work here is O(bars) for
        # valuation plus O(signals) for event handling, not O(bars) per
        # signal.
        for timestamp in merged_index:
            for signal in signals_by_timestamp.get(timestamp, ()):
                outcome, trade = self._process_signal(
                    signal, candles, portfolio, open_trades
                )
                if outcome is not None:
                    outcomes.append(outcome)
                if trade is not None:
                    trades.append(trade)
            equity_index.append(timestamp)
            equity_values.append(self._mark_to_market(portfolio, ffilled_closes, timestamp))

        # Positions still open when the data ends are given a synthetic
        # closing Trade for reporting purposes only -- the same
        # end-of-data convention Backtester._extract_trades() already
        # uses for the legacy path. The simulation Portfolio itself is
        # left genuinely open (final_portfolio reflects real state, not
        # a fabricated close) -- only the Trade record used for
        # analytics/reporting treats it as closed-for-measurement.
        trades.extend(self._synthesize_closing_trades(open_trades, candles))
        trades.sort(key=lambda t: t.entry_time)

        equity_curve = pd.Series(
            equity_values,
            index=pd.DatetimeIndex(equity_index, name="timestamp"),
            name="equity",
        )

        metrics = calculate_metrics(
            trades,
            equity_curve,
            self._config.initial_cash,
            periods_per_year=self._config.periods_per_year,
        )

        strategy_names = sorted({s.name for s in strategies.values()})
        strategy_name = strategy_names[0] if len(strategy_names) == 1 else "+".join(strategy_names)

        dataset_identities = None
        if datasets is not None:
            dataset_identities = {symbol: ds.identity for symbol, ds in datasets.items()}

        return BacktestResult(
            strategy_name=strategy_name,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            signals=signals,
            risk_mode=RiskMode.PORTFOLIO_RISK,
            backtest_config=self._config.describe(),
            signal_outcomes=outcomes,
            final_portfolio=portfolio,
            dataset_identities=dataset_identities,
        )

    def _generate_all_signals(
        self, strategies: dict[str, Strategy], candles: dict[str, pd.DataFrame]
    ) -> list[Signal]:
        all_signals: list[Signal] = []
        for symbol, strategy in strategies.items():
            prepared = strategy.prepare(candles[symbol])
            raw_signals = strategy.generate_signals(prepared)
            if not isinstance(raw_signals, list) or not all(
                isinstance(s, Signal) for s in raw_signals
            ):
                raise ValueError(
                    f"{strategy.name}.generate_signals() must return a list[Signal] "
                    f"(symbol={symbol!r})"
                )
            all_signals.extend(raw_signals)
        return sorted(all_signals, key=lambda s: s.timestamp)

    def _group_signals_by_timestamp(
        self, signals: list[Signal]
    ) -> dict[pd.Timestamp, list[Signal]]:
        grouped: dict[pd.Timestamp, list[Signal]] = {}
        for signal in signals:
            grouped.setdefault(signal.timestamp, []).append(signal)
        # Deterministic tie-break for same-timestamp signals across
        # different symbols -- alphabetical by symbol, so a run is
        # reproducible regardless of dict/strategy iteration order.
        for group in grouped.values():
            group.sort(key=lambda s: s.symbol)
        return grouped

    def _merged_timeline(self, candles: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
        indices = list(candles.values())
        merged = indices[0].index
        for df in indices[1:]:
            merged = merged.union(df.index)
        return merged.sort_values()

    def _process_signal(
        self,
        signal: Signal,
        candles: dict[str, pd.DataFrame],
        portfolio: Portfolio,
        open_trades: dict[str, dict],
    ) -> tuple[SignalOutcome | None, Trade | None]:
        symbol = signal.symbol
        symbol_candles = candles.get(symbol)
        if symbol_candles is None or signal.timestamp not in symbol_candles.index:
            # Mirrors Backtester._extract_trades()'s existing convention:
            # a signal referencing a timestamp/symbol this run wasn't
            # given data for is skipped, not raised on -- there is no
            # decision to audit here, only a data-alignment mismatch.
            return None, None

        price = float(symbol_candles.loc[signal.timestamp, "close"])

        if signal.direction is SignalDirection.FLAT:
            decision = self._risk_engine.decide_close(signal, portfolio)
            if not decision.approved:
                outcome = SignalOutcome(signal=signal, accepted=False, risk_decision=decision)
                return outcome, None

            existing_position = portfolio.positions[symbol]
            order_side = OrderSide.SELL if existing_position.quantity > 0 else OrderSide.BUY
            order = Order(
                symbol=symbol,
                side=order_side,
                quantity=abs(existing_position.quantity),
                signal_id=signal.id,
                timestamp=signal.timestamp,
            )
            execution_outcome = self._execution_model.simulate(order, symbol_candles)
            unaffordable_reason = self._unaffordable_reason(portfolio, execution_outcome)
            reason = execution_outcome.reason or unaffordable_reason
            if reason is not None:
                outcome = SignalOutcome(
                    signal=signal,
                    accepted=False,
                    risk_decision=decision,
                    execution_unavailable_reason=reason,
                )
                return outcome, None

            fill = execution_outcome.fill
            portfolio.close_position(symbol, exit_price=fill.fill_price, fee=fill.fee)
            outcome = SignalOutcome(signal=signal, accepted=True, risk_decision=decision)
            open_trade = open_trades.pop(symbol, None)
            trade = None
            if open_trade is not None:
                trade = Trade(
                    entry_time=open_trade["entry_time"],
                    exit_time=signal.timestamp,
                    direction=open_trade["direction"],
                    entry_price=open_trade["entry_price"],
                    exit_price=fill.reference_price,
                    entry_signal_id=open_trade["entry_signal_id"],
                    exit_signal_id=signal.id,
                    quantity=open_trade["quantity"],
                    entry_fill_price=open_trade["entry_fill_price"],
                    exit_fill_price=fill.fill_price,
                    entry_fee=open_trade["entry_fee"],
                    exit_fee=fill.fee,
                )
            return outcome, trade

        # LONG / SHORT entry: derive the sizing stop, then ask Risk.
        history = symbol_candles.loc[: signal.timestamp]
        stop_result = self._stop_policy.stop_price(signal, history, entry_price=price)
        if not stop_result.available:
            outcome = SignalOutcome(
                signal=signal,
                accepted=False,
                risk_decision=None,
                stop_unavailable_reason=stop_result.reason,
            )
            return outcome, None

        decision = self._risk_engine.decide(
            signal, portfolio, entry_price=price, stop_price=stop_result.stop_price
        )
        if not decision.approved:
            outcome = SignalOutcome(signal=signal, accepted=False, risk_decision=decision)
            return outcome, None

        intent = decision.to_trade_intent()
        side = PositionSide.LONG if signal.direction is SignalDirection.LONG else PositionSide.SHORT
        order_side = OrderSide.BUY if side is PositionSide.LONG else OrderSide.SELL
        order = Order(
            symbol=symbol,
            side=order_side,
            quantity=intent.quantity,
            signal_id=signal.id,
            timestamp=signal.timestamp,
        )
        execution_outcome = self._execution_model.simulate(order, symbol_candles)
        unaffordable_reason = self._unaffordable_reason(portfolio, execution_outcome)
        reason = execution_outcome.reason or unaffordable_reason
        if reason is not None:
            outcome = SignalOutcome(
                signal=signal,
                accepted=False,
                risk_decision=decision,
                execution_unavailable_reason=reason,
            )
            return outcome, None

        fill = execution_outcome.fill
        signed_quantity = intent.quantity if side is PositionSide.LONG else -intent.quantity
        portfolio.open_position(
            symbol=symbol,
            side=side,
            quantity=signed_quantity,
            entry_price=fill.fill_price,
            entry_timestamp=signal.timestamp,
            entry_signal_id=signal.id,
            stop_price=stop_result.stop_price,
            fee=fill.fee,
        )
        outcome = SignalOutcome(signal=signal, accepted=True, risk_decision=decision)
        open_trades[symbol] = {
            "entry_time": signal.timestamp,
            "entry_price": fill.reference_price,
            "entry_fill_price": fill.fill_price,
            "entry_fee": fill.fee,
            "direction": 1 if side is PositionSide.LONG else -1,
            "entry_signal_id": signal.id,
            "quantity": intent.quantity,
        }
        return outcome, None

    def _unaffordable_reason(
        self, portfolio: Portfolio, execution_outcome: ExecutionOutcome
    ) -> str | None:
        """`INSUFFICIENT_CASH_FOR_FEE` when a genuinely filled order's
        real cash effect (price plus fee, Sprint 12 spec, section 21)
        would drive `portfolio.cash` negative -- `None` otherwise
        (including when `execution_outcome` itself didn't fill at all;
        that case is already `NO_EXECUTION_BAR`, handled by the caller).
        Checked here rather than inside `Portfolio.open_position()`/
        `close_position()` so an unaffordable fill becomes an ordinary,
        auditable rejected `SignalOutcome` instead of a raised
        exception -- Execution reports what it could not do the same
        structured way Risk and the stop policy already do, rather than
        crashing the run (Sprint 12 spec, section 21: "make this
        explicit rather than silently letting cash go negative")."""
        if not execution_outcome.filled:
            return None
        if portfolio.cash + execution_outcome.fill.cash_delta < 0:
            return INSUFFICIENT_CASH_FOR_FEE
        return None

    def _mark_to_market(
        self,
        portfolio: Portfolio,
        ffilled_closes: dict[str, pd.Series],
        timestamp: pd.Timestamp,
    ) -> float:
        """Read-only valuation: never assigns `Position.current_price`
        or calls any `Portfolio` mutator (Sprint 11 spec, section 23) --
        every price comes from a precomputed, independent
        forward-filled close series, the same "never mutate to value"
        posture `src.analytics.valuation.PortfolioValuationService`
        already established for live paper-portfolio valuation."""
        equity = portfolio.cash
        for symbol, position in portfolio.positions.items():
            price_series = ffilled_closes.get(symbol)
            price = None
            if price_series is not None:
                candidate = price_series.loc[timestamp]
                if not pd.isna(candidate):
                    price = float(candidate)
            if price is None:
                # Defensive only: a position can't exist for a symbol
                # before that symbol's own first candle, so this should
                # never actually trigger in a coherent run.
                price = position.valuation_price
            equity += position.quantity * price
        return equity

    def _synthesize_closing_trades(
        self, open_trades: dict[str, dict], candles: dict[str, pd.DataFrame]
    ) -> list[Trade]:
        trades: list[Trade] = []
        for symbol, open_trade in open_trades.items():
            symbol_candles = candles[symbol]
            last_time = symbol_candles.index[-1]
            last_price = float(symbol_candles["close"].iloc[-1])
            trades.append(
                Trade(
                    entry_time=open_trade["entry_time"],
                    exit_time=last_time,
                    direction=open_trade["direction"],
                    entry_price=open_trade["entry_price"],
                    exit_price=last_price,
                    entry_signal_id=open_trade["entry_signal_id"],
                    exit_signal_id=None,
                    quantity=open_trade["quantity"],
                    entry_fill_price=open_trade["entry_fill_price"],
                    # No real exit fill ever occurred -- this is a
                    # reporting-only synthesis at the final candle's raw
                    # close (unchanged from Sprint 11), not a simulated
                    # execution, so there is no exit fill price or fee
                    # to report (Sprint 12, DECISIONS.md ADR-0045).
                    exit_fill_price=None,
                    entry_fee=open_trade["entry_fee"],
                    exit_fee=0.0,
                )
            )
        return trades
