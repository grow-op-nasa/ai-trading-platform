"""Data shapes produced by the Backtesting Framework.

Plain dataclasses, no behavior beyond simple derived properties -- the
Backtester (`engine.py`) and metrics (`metrics.py`) do the actual work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pandas as pd

from src.backtesting.config import RiskMode
from src.backtesting.risk_audit import SignalOutcome
from src.data.models import DatasetIdentity
from src.portfolio.models import Portfolio
from src.signals.models import Signal


@dataclass
class Trade:
    """One completed position: opened in one direction, later closed.

    References the `Signal`s that opened and closed it by id rather
    than embedding the `Signal` objects directly (`DECISIONS.md`,
    ADR-0015) -- `Trade` stays a small, storable record, and whoever
    wants the full evidence behind a trade (Performance Attribution,
    the AI Research Reporter) looks it up via the Experiment Registry,
    which is what actually owns `Signal` storage (ADR-0016).

    Args:
        quantity: the actual approved share quantity this trade was
            opened at (`DECISIONS.md`, ADR-0044, Sprint 11) -- always
            positive, direction lives in `direction`, matching
            `src.execution.models.Order`'s own convention. `None` for
            every `RiskMode.LEGACY_UNIT` trade (the original Sprint 2
            one-unit execution model, `DECISIONS.md` ADR-0011, has no
            share-count concept at all) -- **never read `None` as "one
            unit"**; it means quantity genuinely was not tracked for
            this trade. Always populated for a `RiskMode.PORTFOLIO_RISK`
            trade, where it is exactly the quantity
            `PortfolioRiskEngine.decide()` approved and the simulated
            fill executed at.
    """

    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: int  # 1 = long, -1 = short
    entry_price: float
    exit_price: float
    entry_signal_id: UUID
    exit_signal_id: UUID | None = None  # None: closed at end-of-data, not by a signal
    quantity: float | None = None

    @property
    def pnl_per_unit(self) -> float:
        """Profit/loss per unit of position size, in price terms."""
        return self.direction * (self.exit_price - self.entry_price)

    @property
    def return_pct(self) -> float:
        """Return as a fraction of entry price (e.g. 0.05 = +5%)."""
        return self.direction * (self.exit_price - self.entry_price) / self.entry_price

    @property
    def gross_pnl(self) -> float | None:
        """Dollar profit/loss for this trade, before any costs
        (`quantity * pnl_per_unit`) -- `None` when `quantity` is
        unknown (a `RiskMode.LEGACY_UNIT` trade), never fabricated by
        assuming one unit (`DECISIONS.md`, ADR-0044). `src.analytics`
        is where this feeds into net/gross P&L, expectancy, and
        largest-winner/loser metrics when it's available."""
        if self.quantity is None:
            return None
        return self.quantity * self.pnl_per_unit


@dataclass
class BacktestResult:
    """Everything a single backtest run produced.

    Args:
        dataset_identity: which candle dataset this backtest actually
            ran against -- symbol, timeframe, content hash, session/
            timezone convention (Sprint 8 spec, section 11: "a backtest
            should be able to identify... dataset identity/hash").
            `None` by default, and left `None` whenever `Backtester.run()`
            is called without a `dataset` argument (e.g. every existing
            caller passing a hand-built or synthetic DataFrame directly)
            -- this field is additive, not a requirement placed on every
            backtest (`DECISIONS.md`, ADR-0041).
        risk_mode: which execution/sizing model produced this result
            (`DECISIONS.md`, ADR-0044, Sprint 11) --
            `RiskMode.LEGACY_UNIT` for every `Backtester.run()` result
            (the default, unconditionally, so every pre-Sprint-11
            caller and test is unaffected) or `RiskMode.PORTFOLIO_RISK`
            for a `Backtester.run_portfolio()` result. Never left to be
            inferred from whether `trades` happen to carry a `quantity`
            -- always stated explicitly (Sprint 11 spec, section 27).
        backtest_config: a JSON-safe description of the configuration
            that produced this result (`BacktestConfig.describe()`) --
            `None` for a `RiskMode.LEGACY_UNIT` result (there is no
            `BacktestConfig` in that path at all), always populated for
            `RiskMode.PORTFOLIO_RISK`.
        signal_outcomes: what became of every signal this run
            considered -- approved, risk-rejected, or never reaching
            Risk because a stop couldn't be computed
            (`src.backtesting.risk_audit.SignalOutcome`, Sprint 11 spec,
            sections 20-21, 48). Always `[]` for `RiskMode.LEGACY_UNIT`
            (that path has no risk engine to produce one).
        final_portfolio: the simulation `Portfolio` at the end of this
            run -- cash, open positions, and closed positions
            (`src.portfolio.models.Portfolio`), for
            `RiskMode.PORTFOLIO_RISK` only. `None` for
            `RiskMode.LEGACY_UNIT` (that path has no `Portfolio` at
            all) -- always an isolated, freshly-constructed simulation
            portfolio, never a live/paper-trading one (Sprint 11 spec,
            section 32).
        dataset_identities: per-symbol dataset identity for a
            multi-symbol `run_portfolio()` result (`{symbol:
            DatasetIdentity}`) -- the `run_portfolio()` analogue of
            `dataset_identity` above. `None` unless `datasets` was
            passed to `run_portfolio()`.
    """

    strategy_name: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)
    metrics: dict = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    dataset_identity: DatasetIdentity | None = None
    risk_mode: RiskMode = RiskMode.LEGACY_UNIT
    backtest_config: dict[str, Any] | None = None
    signal_outcomes: list[SignalOutcome] = field(default_factory=list)
    final_portfolio: Portfolio | None = None
    dataset_identities: dict[str, DatasetIdentity] | None = None

    def report(self) -> str:
        """A short, human-readable summary -- not a substitute for
        inspecting `trades` / `equity_curve` / `metrics` directly, just
        a quick-glance version for logs and the Experiment Registry."""
        lines = [
            f"Strategy: {self.strategy_name}",
            f"Trades: {self.metrics.get('total_trades', 0)}",
        ]
        if self.dataset_identity is not None:
            identity = self.dataset_identity
            lines.append(
                f"Dataset: {identity.symbol} {identity.interval.value}, "
                f"hash {identity.content_hash[:12]}"
            )
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
