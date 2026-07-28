"""The Backtesting Framework.

Not a strategy -- the framework. Runs any object satisfying the
`Strategy` interface (`src/strategies/base.py`) against a set of
candles and answers: run strategy -> collect trades -> calculate
metrics -> generate report.

    from src.backtesting import Backtester

    result = Backtester().run(my_strategy, candles)
    print(result.report())

A single, simplified execution model today: one unit of position size
per signal (-1/0/1), no transaction costs or slippage, entries/exits at
the candle's close price. Realistic execution modeling (partial fills,
costs, slippage) is future work for `src/execution/`, not this
framework's job.
"""

from __future__ import annotations

import pandas as pd

from src.backtesting.metrics import calculate_metrics
from src.backtesting.models import BacktestResult, Trade
from src.strategies.base import SIGNAL_COLUMN, Strategy

DEFAULT_INITIAL_CASH = 100_000.0


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
                DataFrame containing the required signal column.
        """
        prepared = strategy.prepare(candles)
        signals = strategy.generate_signals(prepared)

        if SIGNAL_COLUMN not in signals.columns:
            raise ValueError(
                f"{strategy.name}.generate_signals() must return a DataFrame "
                f"with a '{SIGNAL_COLUMN}' column"
            )

        position = signals[SIGNAL_COLUMN].fillna(0).astype(int)
        trades = self._extract_trades(candles, position)
        equity_curve = self._compute_equity_curve(candles, position)
        metrics = calculate_metrics(trades, equity_curve, self._initial_cash)

        return BacktestResult(
            strategy_name=strategy.name,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
        )

    def _extract_trades(self, candles: pd.DataFrame, position: pd.Series) -> list[Trade]:
        """Turn a position series into a list of completed Trades.

        A trade opens whenever position moves away from 0 (or flips
        sign) and closes whenever it moves back to 0 (or flips again),
        using the candle's close price for both entry and exit. Any
        position still open at the end of the series is closed at the
        final candle's close.
        """
        trades: list[Trade] = []
        current_direction = 0
        entry_time = None
        entry_price = None

        for timestamp, pos in position.items():
            pos = int(pos)
            if pos == current_direction:
                continue

            if current_direction != 0:
                exit_price = float(candles.loc[timestamp, "close"])
                trades.append(
                    Trade(
                        entry_time=entry_time,
                        exit_time=timestamp,
                        direction=current_direction,
                        entry_price=entry_price,
                        exit_price=exit_price,
                    )
                )

            if pos != 0:
                entry_time = timestamp
                entry_price = float(candles.loc[timestamp, "close"])

            current_direction = pos

        if current_direction != 0:
            last_timestamp = candles.index[-1]
            trades.append(
                Trade(
                    entry_time=entry_time,
                    exit_time=last_timestamp,
                    direction=current_direction,
                    entry_price=entry_price,
                    exit_price=float(candles.loc[last_timestamp, "close"]),
                )
            )

        return trades

    def _compute_equity_curve(self, candles: pd.DataFrame, position: pd.Series) -> pd.Series:
        """Simulated equity over time, given the position series.

        Position at time t is applied to the return realized from
        t-1 to t (position is shifted forward one bar) so a signal
        computed using bar t's close can't "see" bar t's own return --
        it can only act on bar t+1's move.
        """
        price_returns = candles["close"].pct_change().fillna(0)
        strategy_returns = position.shift(1).fillna(0) * price_returns
        equity = (1 + strategy_returns).cumprod() * self._initial_cash
        return equity.rename("equity")
