"""Typed, validated configuration for one paper-trading target (Sprint 18, ADR-0053).

Everything is explicit: the operator names the paper account (an experiment id with a saved portfolio), the
registered strategy and its parameters, the instrument, the timeframe, the risk limits, the stop policy and the
execution-cost assumptions. Nothing is defaulted silently, and unknown keys are rejected (typo protection).
`execution_mode` must be the literal string "paper"; there is no setting that selects any other adapter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.backtesting.execution_model import (
    ExecutionConfig,
    ExecutionTiming,
    PercentageFeeModel,
    PercentageSlippageModel,
)
from src.backtesting.stop_policy import ATRStopPolicy
from src.data.base import Interval
from src.risk.models import PortfolioRiskLimits, RiskLimits

PAPER_MODE = "paper"
SUPPORTED_INTERVALS = (Interval.DAY_1,)  # closed-bar semantics are implemented for daily bars only

_TOP_KEYS = {"execution_mode", "experiment_id", "strategy_name", "strategy_params", "symbol", "interval",
             "lookback_days", "risk", "stop", "execution"}
_REQUIRED = ("execution_mode", "experiment_id", "strategy_name", "symbol", "risk", "stop", "execution")
_RISK_KEYS = {"allocation_per_trade_pct", "max_portfolio_exposure_pct", "risk_pct_per_trade",
              "max_symbol_exposure_pct", "max_concurrent_positions", "min_quantity"}
_RISK_REQUIRED = ("allocation_per_trade_pct", "max_portfolio_exposure_pct", "risk_pct_per_trade")
_STOP_KEYS = {"type", "period", "multiple"}
_EXEC_KEYS = {"timing", "slippage_bps", "fee_bps", "fixed_fee"}


class PaperConfigError(ValueError):
    """The paper-run configuration is missing, malformed or asks for something unsupported."""


@dataclass(frozen=True)
class PaperRunConfig:
    execution_mode: str
    experiment_id: int
    strategy_name: str
    symbol: str
    risk: dict[str, Any]
    stop: dict[str, Any]
    execution: dict[str, Any]
    strategy_params: dict[str, Any] = field(default_factory=dict)
    interval: str = "1d"
    lookback_days: int = 400

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PaperRunConfig":
        if not isinstance(raw, dict):
            raise PaperConfigError("configuration must be a JSON object")
        unknown = sorted(set(raw) - _TOP_KEYS)
        if unknown:
            raise PaperConfigError(f"unknown configuration key(s): {unknown}")
        missing = [k for k in _REQUIRED if k not in raw]
        if missing:
            raise PaperConfigError(f"missing required configuration key(s): {missing}")
        cfg = cls(
            execution_mode=raw["execution_mode"], experiment_id=raw["experiment_id"],
            strategy_name=raw["strategy_name"], symbol=raw["symbol"], risk=dict(raw["risk"] or {}),
            stop=dict(raw["stop"] or {}), execution=dict(raw["execution"] or {}),
            strategy_params=dict(raw.get("strategy_params") or {}), interval=raw.get("interval", "1d"),
            lookback_days=raw.get("lookback_days", 400),
        )
        cfg.validate()
        return cfg

    @classmethod
    def from_file(cls, path: str | Path) -> "PaperRunConfig":
        p = Path(path)
        if not p.is_file():
            raise PaperConfigError(f"configuration file not found: {p}")
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperConfigError(f"configuration file is not valid JSON: {exc}") from exc
        return cls.from_dict(raw)

    # ------------------------------------------------------------------ validation
    def validate(self) -> None:
        if self.execution_mode != PAPER_MODE:
            raise PaperConfigError(
                f"execution_mode must be exactly {PAPER_MODE!r}; this runtime is paper-only and has no other mode"
            )
        if isinstance(self.experiment_id, bool) or not isinstance(self.experiment_id, int) or self.experiment_id < 1:
            raise PaperConfigError("experiment_id must be a positive integer naming an existing paper portfolio")
        for name in ("strategy_name", "symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise PaperConfigError(f"{name} must be a non-empty string")
        if not isinstance(self.strategy_params, dict):
            raise PaperConfigError("strategy_params must be an object")
        try:
            interval = Interval(self.interval)
        except ValueError as exc:
            raise PaperConfigError(f"unknown interval {self.interval!r}") from exc
        if interval not in SUPPORTED_INTERVALS:
            raise PaperConfigError(
                f"interval {self.interval!r} is not supported by the paper runtime yet (supported: "
                f"{[i.value for i in SUPPORTED_INTERVALS]}); closed-bar handling exists for daily bars only"
            )
        if isinstance(self.lookback_days, bool) or not isinstance(self.lookback_days, int) \
                or not 30 <= self.lookback_days <= 3650:
            raise PaperConfigError("lookback_days must be an integer between 30 and 3650")
        self._check_keys("risk", self.risk, _RISK_KEYS, _RISK_REQUIRED)
        self._check_keys("stop", self.stop, _STOP_KEYS, ("type", "period", "multiple"))
        self._check_keys("execution", self.execution, _EXEC_KEYS, ("timing", "slippage_bps", "fee_bps", "fixed_fee"))
        try:
            self.risk_limits()
            self.portfolio_limits()
            self.stop_policy()
            self.execution_config()
        except PaperConfigError:
            raise
        except (ValueError, TypeError) as exc:
            raise PaperConfigError(f"invalid risk/stop/execution settings: {exc}") from exc

    @staticmethod
    def _check_keys(section: str, values: dict, allowed: set, required: tuple) -> None:
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise PaperConfigError(f"unknown {section} key(s): {unknown}")
        missing = [k for k in required if k not in values]
        if missing:
            raise PaperConfigError(f"{section} must explicitly set: {missing}")

    # ------------------------------------------------------------------ typed views
    def interval_enum(self) -> Interval:
        return Interval(self.interval)

    def risk_limits(self) -> RiskLimits:
        return RiskLimits(allocation_per_trade_pct=float(self.risk["allocation_per_trade_pct"]),
                          max_portfolio_exposure_pct=float(self.risk["max_portfolio_exposure_pct"]))

    def portfolio_limits(self) -> PortfolioRiskLimits:
        kw: dict[str, Any] = {"risk_pct_per_trade": float(self.risk["risk_pct_per_trade"])}
        for k in ("max_symbol_exposure_pct", "max_concurrent_positions", "min_quantity"):
            if k in self.risk and self.risk[k] is not None:
                kw[k] = self.risk[k]
        return PortfolioRiskLimits(**kw)

    def stop_policy(self) -> ATRStopPolicy:
        if self.stop.get("type") != "atr":
            raise PaperConfigError("stop.type must be 'atr' (the only stop policy the platform implements)")
        return ATRStopPolicy(period=int(self.stop["period"]), multiple=float(self.stop["multiple"]))

    def execution_config(self) -> ExecutionConfig:
        timing = self.execution["timing"]
        if timing != ExecutionTiming.SIGNAL_BAR_CLOSE.value:
            raise PaperConfigError(
                "execution.timing must be 'SIGNAL_BAR_CLOSE': NEXT_BAR_OPEN needs a bar that does not exist yet "
                "at decision time, so it cannot be paper-executed honestly"
            )
        return ExecutionConfig(
            timing=ExecutionTiming.SIGNAL_BAR_CLOSE,
            slippage_model=PercentageSlippageModel(float(self.execution["slippage_bps"])),
            fee_model=PercentageFeeModel(float(self.execution["fee_bps"]), float(self.execution["fixed_fee"])),
        )

    # ------------------------------------------------------------------ identity
    def describe(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode, "experiment_id": self.experiment_id,
            "strategy_name": self.strategy_name, "strategy_params": self.strategy_params, "symbol": self.symbol,
            "interval": self.interval, "lookback_days": self.lookback_days, "risk": self.risk,
            "stop": self.stop_policy().config, "execution": self.execution_config().describe(),
        }

    def identity_hash(self) -> str:
        blob = json.dumps(self.describe(), sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
