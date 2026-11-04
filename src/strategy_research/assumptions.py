"""Explicit, recorded research assumptions -- one place, identical for every strategy (Sprint 23, ADR-0061).

Every strategy in a research run receives the same capital, risk sizing, stop policy, fill timing and cost model.
The costs are generic research frictions (basis points of price/notional), not any broker's real fee schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.backtesting.config import BacktestConfig, RiskMode
from src.backtesting.execution_model import (
    ExecutionConfig,
    ExecutionTiming,
    PercentageFeeModel,
    PercentageSlippageModel,
)
from src.backtesting.stop_policy import ATRStopPolicy
from src.risk.models import PortfolioRiskLimits, RiskLimits

DEFAULT_SLIPPAGE_BPS = 2.0
DEFAULT_FEE_BPS = 1.0
STRESS_COST_MULTIPLE = 3.0
EXECUTION_MODEL_VERSION = "sprint12-adr0045"


@dataclass(frozen=True)
class ResearchAssumptions:
    """Capital, sizing, stop, timing and cost assumptions shared by every research run.

    Fills happen at the *next bar's open* (never the signal bar's close) with adverse slippage and a percentage fee.
    Position size comes from the existing ``PortfolioRiskEngine`` (``risk_pct_per_trade`` of equity at risk to an ATR
    stop); ``allocation_per_trade_pct`` and ``max_portfolio_exposure_pct`` are the caps that engine applies.
    """

    initial_cash: float = 100_000.0
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    fee_bps: float = DEFAULT_FEE_BPS
    fixed_fee: float = 0.0
    risk_pct_per_trade: float = 0.01
    allocation_per_trade_pct: float = 1.0
    max_portfolio_exposure_pct: float = 1.0
    stop_atr_period: int = 14
    stop_atr_multiple: float = 2.0

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        for name in ("slippage_bps", "fee_bps", "fixed_fee"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    def to_backtest_config(self, cost_multiple: float = 1.0) -> BacktestConfig:
        """The portfolio-risk backtest configuration; ``cost_multiple`` scales every friction (0 gives the gross run)."""
        if cost_multiple < 0:
            raise ValueError("cost_multiple must be non-negative")
        return BacktestConfig(
            initial_cash=self.initial_cash,
            risk_mode=RiskMode.PORTFOLIO_RISK,
            risk_limits=RiskLimits(
                allocation_per_trade_pct=self.allocation_per_trade_pct,
                max_portfolio_exposure_pct=self.max_portfolio_exposure_pct,
            ),
            portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=self.risk_pct_per_trade),
            stop_policy=ATRStopPolicy(period=self.stop_atr_period, multiple=self.stop_atr_multiple),
            execution_config=ExecutionConfig(
                timing=ExecutionTiming.NEXT_BAR_OPEN,
                slippage_model=PercentageSlippageModel(self.slippage_bps * cost_multiple),
                fee_model=PercentageFeeModel(self.fee_bps * cost_multiple, self.fixed_fee * cost_multiple),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_cash": self.initial_cash, "slippage_bps": self.slippage_bps, "fee_bps": self.fee_bps,
            "fixed_fee": self.fixed_fee, "risk_pct_per_trade": self.risk_pct_per_trade,
            "allocation_per_trade_pct": self.allocation_per_trade_pct,
            "max_portfolio_exposure_pct": self.max_portfolio_exposure_pct,
            "stop_atr_period": self.stop_atr_period, "stop_atr_multiple": self.stop_atr_multiple,
            "fill_timing": ExecutionTiming.NEXT_BAR_OPEN.value, "execution_model_version": EXECUTION_MODEL_VERSION,
            "stress_cost_multiple": STRESS_COST_MULTIPLE,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ResearchAssumptions":
        keys = ("initial_cash", "slippage_bps", "fee_bps", "fixed_fee", "risk_pct_per_trade",
                "allocation_per_trade_pct", "max_portfolio_exposure_pct", "stop_atr_period", "stop_atr_multiple")
        return cls(**{k: raw[k] for k in keys if k in raw})

    def describe_costs(self) -> str:
        return (f"Simulated research frictions, not broker fees: {self.slippage_bps:g} bps adverse slippage and "
                f"{self.fee_bps:g} bps fee per fill (plus ${self.fixed_fee:g} fixed), fills at the next bar's open.")
