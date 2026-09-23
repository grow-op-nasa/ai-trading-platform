"""Explicit backtest configuration -- Sprint 11 (`DECISIONS.md`, ADR-0044).

Before this sprint, `Backtester.__init__(initial_cash, periods_per_year)`
held the platform's only backtest-level assumptions, and its execution
model was implicitly one thing: one unit of position size per signal
(`DECISIONS.md`, ADR-0011). Sprint 11 adds a second, genuinely different
execution model -- portfolio-aware risk sizing through the existing
`PortfolioRiskEngine` (`src.risk`) -- so which assumptions a given
result rests on can no longer be left implicit. `BacktestConfig` makes
that choice, and everything it depends on, an explicit, reproducible
value instead of a constructor default nobody has to think about.

Deliberately small: this is not a general platform-settings object.
It holds only what actually varies *research assumption* by *research
assumption* for one backtest run -- not logging config, not broker
credentials, not dashboard preferences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.backtesting.execution_model import ExecutionConfig
from src.backtesting.stop_policy import StopPolicy
from src.risk.models import PortfolioRiskLimits, RiskLimits

DEFAULT_INITIAL_CASH = 100_000.0


class RiskMode(str, Enum):
    """Which execution/sizing model a `BacktestResult` rests on
    (Sprint 11 spec, sections 4, 27) -- a `str` subclass, like
    `src.data.base.Interval` and `src.risk.models.RejectionReason`, so
    it serializes cleanly into `ExperimentSpec.backtest_config` and
    `BacktestResult` without a separate encode/decode step.

    `LEGACY_UNIT`: the original Sprint 2 model (`DECISIONS.md`,
        ADR-0011) -- one unit of position size per signal, no risk
        engine involved at all. Kept, unchanged, for backward
        compatibility (Sprint 11 spec, section 4) -- every experiment
        run before this sprint is a `LEGACY_UNIT` result, and
        `Backtester.run()` still produces one by default.
    `PORTFOLIO_RISK`: the new Sprint 11 model -- entries and exits sized
        and admitted by the real `PortfolioRiskEngine` against a real
        `Portfolio`, exactly as paper trading already does. Produced
        only by `Backtester.run_portfolio()`, never implicitly.
    """

    LEGACY_UNIT = "LEGACY_UNIT"
    PORTFOLIO_RISK = "PORTFOLIO_RISK"


@dataclass(frozen=True)
class BacktestConfig:
    """Explicit, reproducible configuration for one portfolio-aware
    backtest run (`Backtester.run_portfolio()`).

    Not used by the legacy `Backtester.run()` path at all -- that
    method's own constructor arguments (`initial_cash`,
    `periods_per_year`) are unchanged, and it always produces a
    `RiskMode.LEGACY_UNIT` result regardless of whether a
    `BacktestConfig` exists anywhere in the same process.

    Args:
        initial_cash: starting cash for the simulation `Portfolio`.
            Must be positive.
        risk_mode: which execution model this config describes.
            Defaults to `RiskMode.PORTFOLIO_RISK` -- a `BacktestConfig`
            is only ever built to run the portfolio-aware path (the
            legacy path doesn't take one at all), so defaulting it to
            anything else would be misleading. Kept as an explicit
            field anyway (rather than hardcoding `PORTFOLIO_RISK`
            everywhere `BacktestConfig` is used) so a caller can never
            be confused about which mode a given config, once
            persisted, actually described (Sprint 11 spec, section 27:
            "never rely on an implicit default").
        risk_limits: allocation/total-exposure limits, reused as-is by
            `PortfolioRiskEngine` (`src.risk.models.RiskLimits`).
            Defaults to `RiskLimits()`.
        portfolio_risk_limits: risk-per-trade and portfolio-constraint
            configuration (`src.risk.models.PortfolioRiskLimits`).
            Defaults to `PortfolioRiskLimits()`.
        stop_policy: how a stop price is derived for sizing
            (`src.backtesting.stop_policy.StopPolicy`). Required when
            `risk_mode` is `PORTFOLIO_RISK` -- `PortfolioRiskEngine`
            cannot size a trade without one, and this config must not
            invent a default stop policy silently (an ATR period/
            multiple choice is itself a research assumption, not a
            platform default to bury).
        periods_per_year: annualization override for Sharpe/volatility,
            same meaning as `Backtester.__init__`'s own parameter.
            Defaults to `None` (infer from the equity curve's own
            timestamp spacing, `DECISIONS.md` ADR-0038).
        execution_config: fill timing, slippage, and fee assumptions
            (Sprint 12, `DECISIONS.md` ADR-0045,
            `src.backtesting.execution_model.ExecutionConfig`). Defaults
            to `ExecutionConfig()` -- signal-bar-close timing, zero
            slippage, zero fees -- which reproduces Sprint 11's fills
            exactly (Sprint 12 spec, section 56: no existing result is
            silently reinterpreted just because this field now exists).

    Raises:
        ValueError: `initial_cash` isn't positive, or `risk_mode` is
            `PORTFOLIO_RISK` and `stop_policy` is `None`.
    """

    initial_cash: float = DEFAULT_INITIAL_CASH
    risk_mode: RiskMode = RiskMode.PORTFOLIO_RISK
    risk_limits: RiskLimits = field(default_factory=RiskLimits)
    portfolio_risk_limits: PortfolioRiskLimits = field(default_factory=PortfolioRiskLimits)
    stop_policy: StopPolicy | None = None
    periods_per_year: int | None = None
    execution_config: ExecutionConfig = field(default_factory=ExecutionConfig)

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError(f"initial_cash must be positive, got {self.initial_cash}")
        if self.risk_mode is RiskMode.PORTFOLIO_RISK and self.stop_policy is None:
            raise ValueError(
                "stop_policy is required when risk_mode is PORTFOLIO_RISK -- "
                "PortfolioRiskEngine cannot size a trade without a stop "
                "boundary, and this platform never invents one silently "
                "(DECISIONS.md, ADR-0044)"
            )

    def describe(self) -> dict[str, Any]:
        """A JSON-safe, reproducible description of this configuration
        -- what `ExperimentSpec.capture(backtest_config=...)` (Sprint 11
        spec, section 31) records so a researcher can answer "what risk
        assumptions produced this result" without re-reading code.
        """
        return {
            "initial_cash": self.initial_cash,
            "risk_mode": self.risk_mode.value,
            "risk_limits": {
                "allocation_per_trade_pct": self.risk_limits.allocation_per_trade_pct,
                "max_portfolio_exposure_pct": self.risk_limits.max_portfolio_exposure_pct,
            },
            "portfolio_risk_limits": {
                "risk_pct_per_trade": self.portfolio_risk_limits.risk_pct_per_trade,
                "max_symbol_exposure_pct": self.portfolio_risk_limits.max_symbol_exposure_pct,
                "max_concurrent_positions": self.portfolio_risk_limits.max_concurrent_positions,
                "min_quantity": self.portfolio_risk_limits.min_quantity,
            },
            "stop_policy": self.stop_policy.config if self.stop_policy is not None else None,
            "periods_per_year": self.periods_per_year,
            "execution": self.execution_config.describe(),
        }
