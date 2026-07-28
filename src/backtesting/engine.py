"""The Backtesting Framework.

Not a strategy -- the framework. Runs any object satisfying the
`Strategy` interface (`src/strategies/base.py`) against a set of
candles and answers: run strategy -> collect trades -> calculate
metrics -> generate report.

    from src.backtesting import Backtester

    result = Backtester().run(my_strategy, candles)
    print(result.report())

A single, simplified execution model today: one unit of position size
per signal, no transaction costs or slippage, entries/exits at the
candle's close price. Realistic execution modeling (partial fills,
costs, slippage) is future work for `src/execution/`, not this
framework's job.

As of `DECISIONS.md` ADR-0015 (superseding ADR-0011), strategies emit a
sparse list of `Signal` objects -- one per decision point, not one per
candle -- rather than a dense DataFrame column. A signal's direction is
held from its own bar forward until the next signal supersedes it. No
lookahead: the *return* realized on the bar a signal fires on is never
attributed to that signal (see `_compute_equity_curve`), even though
the signal's own bar close is used as its trade's entry/exit price --
the same convention ADR-0011 established, just expressed over a sparse
signal list instead of a dense column.
"""

from __future__ import annotations

import pandas as pd

from src.backtesting.metrics import calculate_metrics
from src.backtesting.models import BacktestResult, Trade
from src.signals.models import Signal, SignalDirection
from src.strategies.base import Strategy

DEFAULT_INITIAL_CASH = 100_000.0

_DIRECTION_TO_UNITS = {
    SignalDirection.LONG: 1,
    SignalDirection.SHORT: -1,
    SignalDirection.FLAT: 0,
}


class Backtester:
    """Runs a `Strategy` against candles and produces a `BacktestResult`.

    Args:
        initial_cash: starting notional, used to express the equity
            curve and total return in dollar/percentage terms.
    """

    def __init__(self, initial_cash: float = DEFAULT_INITIAL_CASH) -> None:
        self._initial_cash = initial_cash

    def run(self, strategy: Strategy, candles: pd.DataFrame) -> BacktestResult:
        """Run `strategy` against `candles` and return the full result.

        Raises:
            ValueError: `strategy.generate_signals()` doesn't return a
                `list[Signal]`.
        """
        prepared = strategy.prepare(candles)
        raw_signals = strategy.generate_signals(prepared)

        if not isinstance(raw_signals, list) or not all(
            isinstance(s, Signal) for s in raw_signals
        ):
            raise ValueError(
                f"{strategy.name}.generate_signals() must return a list[Signal]"
            )

        signals = sorted(raw_signals, key=lambda s: s.timestamp)

        trades = self._extract_trades(candles, signals)
        position = self._build_position_series(candles, signals)
        equity_curve = self._compute_equity_curve(candles, position)
        metrics = calculate_metrics(trades, equity_curve, self._initial_cash)

        return BacktestResult(
            strategy_name=strategy.name,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            signals=signals,
        )

    def _extract_trades(self, candles: pd.DataFrame, signals: list[Signal]) -> list[Trade]:
        """Turn a sparse, time-ordered signal list into completed Trades.

        A trade opens at a non-FLAT signal's own bar and closes at the
        next signal that changes direction (using that signal's own bar
        as the exit), or at the final candle if no closing signal ever
        arrives (`exit_signal_id=None` in that case). Signals whose
        timestamp isn't an actual candle in this run are skipped rather
        than raising -- a strategy analyzing a wider history than it
        was ultimately backtested against shouldn't crash the run.
        """
        trades: list[Trade] = []
        open_trade: dict | None = None

        for signal in signals:
            if signal.timestamp not in candles.index:
                continue

            new_direction = _DIRECTION_TO_UNITS[signal.direction]
            price = float(candles.loc[signal.timestamp, "close"])

            if open_trade is not None and new_direction != open_trade["direction"]:
                trades.append(
                    Trade(
                        entry_time=open_trade["entry_time"],
                        exit_time=signal.timestamp,
                        direction=open_trade["direction"],
                        entry_price=open_trade["entry_price"],
                        exit_price=price,
                        entry_signal_id=open_trade["entry_signal_id"],
                        exit_signal_id=signal.id,
                    )
                )
                open_trade = None

            if new_direction != 0 and open_trade is None:
                open_trade = {
                    "entry_time": signal.timestamp,
                    "entry_price": price,
                    "direction": new_direction,
                    "entry_signal_id": signal.id,
                }

        if open_trade is not None:
            last_time = candles.index[-1]
            trades.append(
                Trade(
                    entry_time=open_trade["entry_time"],
                    exit_time=last_time,
                    direction=open_trade["direction"],
                    entry_price=open_trade["entry_price"],
                    exit_price=float(candles["close"].iloc[-1]),
                    entry_signal_id=open_trade["entry_signal_id"],
                    exit_signal_id=None,
                )
            )

        return trades

    def _build_position_series(self, candles: pd.DataFrame, signals: list[Signal]) -> pd.Series:
        """Dense per-bar position, derived from the sparse signal list.

        Holds each signal's direction from its own bar forward until
        the next signal. This is deliberately the *unshifted* position
        -- equivalent to ADR-0011's original dense `signal` column --
        the no-lookahead adjustment happens once, in
        `_compute_equity_curve`, not here.
        """
        position = pd.Series(0, index=candles.index, dtype=int)
        for signal in signals:
            if signal.timestamp not in candles.index:
                continue
            position.loc[signal.timestamp:] = _DIRECTION_TO_UNITS[signal.direction]
        return position

    def _compute_equity_curve(self, candles: pd.DataFrame, position: pd.Series) -> pd.Series:
        """Simulated equity over time, given the (unshifted) position series.

        Position at time t is applied to the return realized from
        t-1 to t (position is shifted forward one bar) so a signal
        computed using bar t's close can't "see" bar t's own return --
        it can only act on bar t+1's move.
        """
        price_returns = candles["close"].pct_change().fillna(0)
        strategy_returns = position.shift(1).fillna(0) * price_returns
        equity = (1 + strategy_returns).cumprod() * self._initial_cash
        return equity.rename("equity")
