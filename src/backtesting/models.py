"""Data shapes produced by the Backtesting Framework.

Plain dataclasses, no behavior beyond simple derived properties -- the
Backtester (`engine.py`) and metrics (`metrics.py`) do the actual work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Trade:
    """One completed position: opened in one direction, later closed."""

    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: int  # 1 = long, -1 = short
    entry_price: float
    exit_price: float

    @property
    def pnl_per_unit(self) -> float:
        """Profit/loss per unit of position size, in price terms."""
        return self.direction * (self.exit_price - self.entry_price)

    @property
    def return_pct(self) -> float:
        """Return as a fraction of entry price (e.g. 0.05 = +5%)."""
        return self.direction * (self.exit_price - self.entry_price) / self.entry_price


@dataclass
class BacktestResult:
    """Everything a single backtest run produced."""

    strategy_name: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    metrics: dict = field(default_factory=dict)

    def report(self) -> str:
        """A short, human-readable summary -- not a substitute for
        inspecting `trades` / `equity_curve` / `metrics` directly, just
        a quick-glance version for logs and the Experiment Registry."""
        lines = [
            f"Strategy: {self.strategy_name}",
            f"Trades: {self.metrics.get('total_trades', 0)}",
        ]
        win_rate = self.metrics.get("win_rate")
        if win_rate is not None:
            lines.append(f"Win rate: {win_rate:.1%}")
        sharpe = self.metrics.get("sharpe")
        if sharpe is not None:
            lines.append(f"Sharpe: {sharpe:.2f}")
        total_return = self.metrics.get("total_return_pct")
        if total_return is not None:
            lines.append(f"Total return: {total_return:.2%}")
        max_dd = self.metrics.get("max_drawdown_pct")
        if max_dd is not None:
            lines.append(f"Max drawdown: {max_dd:.2%}")
        return "\n".join(lines)
