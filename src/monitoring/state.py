"""Platform state adapters -- src/monitoring/state.py (ADR-0050).

The monitoring layer never reaches for raw data. It reads a
`PlatformState` snapshot assembled from existing application services:

    ExperimentRegistry  -> paper portfolio / equity curve / trades / spec
    MarketDataService   -> canonical datasets (the *only* market-data door)

Anything unavailable is `None` -- never fabricated -- and the checks
that need it report NOT_EVALUABLE. Market data is observed once per
target here and shared by the freshness, integrity and valuation
checks, so they all agree about what the provider returned.

This module does not import yfinance, anthropic or streamlit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Protocol

import pandas as pd

from src.data.base import Interval
from src.data.exceptions import DataProviderError, DataValidationError
from src.data.service import MarketDataService
from src.execution.models import Fill, Order
from src.experiments.registry import ExperimentRegistry
from src.experiments.spec import ExperimentSpec
from src.portfolio.models import Portfolio
from src.reconciliation.models import FillReconciliation
from src.risk.models import PortfolioRiskLimits, RiskDecision, RiskLimits
from src.signals.models import Signal


@dataclass(frozen=True)
class MarketDataTarget:
    symbol: str
    interval: Interval

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.interval.value}"


@dataclass
class MarketDataObservation:
    """What `MarketDataService.get_dataset()` returned for one target."""

    symbol: str
    interval: Interval
    ok: bool
    observed_at: datetime
    error_code: str | None = None
    error_message: str | None = None
    rows: int = 0
    dataset_start: pd.Timestamp | None = None
    dataset_end: pd.Timestamp | None = None
    content_hash: str | None = None
    gap_count: int = 0
    suspicious_count: int = 0
    last_close: float | None = None

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.interval.value}"


@dataclass
class ExecutionRecords:
    """Orders/fills (and optionally the quantity Risk approved per signal)
    supplied by a paper session. The experiment registry does not persist
    these, so the registry-backed source leaves this `None`."""

    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    approved_quantity_by_signal_id: dict[str, float] | None = None


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    artifact_hash: str
    recomputed_artifact_hash: str | None
    symbol: str | None = None
    interval: str | None = None


@dataclass
class PlatformState:
    as_of: datetime
    source: str
    experiment_id: int | None = None
    spec: ExperimentSpec | None = None
    portfolio: Portfolio | None = None
    equity_curve: pd.Series | None = None
    trades: list | None = None
    signals: list[Signal] | None = None
    risk_limits: RiskLimits | None = None
    risk_config_error: str | None = None
    portfolio_risk_limits: PortfolioRiskLimits | None = None
    risk_decisions: list[RiskDecision] | None = None
    execution: ExecutionRecords | None = None
    reconciliations: list[FillReconciliation] | None = None
    market_data_targets: list[MarketDataTarget] = field(default_factory=list)
    market_data: dict[str, MarketDataObservation] = field(default_factory=dict)
    market_data_unavailable_reason: str | None = None
    models: dict[str, ModelInfo | None] = field(default_factory=dict)
    models_unavailable_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        """Provenance of what this run looked at (no secrets)."""
        return {
            "source": self.source,
            "experiment_id": self.experiment_id,
            "as_of": self.as_of.isoformat(),
            "has_portfolio": self.portfolio is not None,
            "open_positions": self.portfolio.position_count if self.portfolio is not None else None,
            "has_equity_curve": self.equity_curve is not None,
            "has_trades": self.trades is not None,
            "has_execution_records": self.execution is not None,
            "market_data_targets": [t.key for t in self.market_data_targets],
            "notes": list(self.notes),
        }


class StateSource(Protocol):
    def load(self, now: datetime) -> PlatformState: ...


class StaticStateSource:
    """Returns a prebuilt `PlatformState` -- deterministic tests and
    explicit demo fixtures (clearly separate from real monitoring)."""

    def __init__(self, state: PlatformState) -> None:
        self._state = state

    def load(self, now: datetime) -> PlatformState:
        return self._state


ModelLookup = Callable[[str], "ModelInfo | None"]


class RegistryStateSource:
    """Reads the platform's persisted paper results from an
    `ExperimentRegistry`: the most recent experiment that saved a paper
    portfolio (or an explicit `experiment_id`).

    The registry persists one paper-portfolio snapshot per experiment,
    not a live trading session: this is the real paper-trading state the
    platform currently has, and it is reported as such. With nothing
    saved, `load()` returns an empty state whose notes say so."""

    def __init__(
        self,
        registry: ExperimentRegistry,
        *,
        experiment_id: int | None = None,
        model_lookup: ModelLookup | None = None,
    ) -> None:
        self._registry = registry
        self._experiment_id = experiment_id
        self._model_lookup = model_lookup

    def _pick(self) -> int | None:
        if self._experiment_id is not None:
            return self._experiment_id
        for exp in reversed(self._registry.list_experiments()):
            if exp.id is not None and self._registry.get_portfolio(exp.id) is not None:
                return exp.id
        return None

    def load(self, now: datetime) -> PlatformState:
        exp_id = self._pick()
        if exp_id is None:
            return PlatformState(
                as_of=now,
                source="experiment_registry",
                notes=["no experiment with a saved paper portfolio exists in the registry"],
            )
        state = PlatformState(as_of=now, source="experiment_registry", experiment_id=exp_id)
        state.spec = self._registry.get_spec(exp_id)
        state.portfolio = self._registry.get_portfolio(exp_id)
        curve = self._registry.get_equity_curve(exp_id)
        state.equity_curve = curve if len(curve) else None
        state.trades = self._registry.get_trades(exp_id)
        state.signals = self._registry.get_signals(exp_id)
        if state.spec is not None:
            state.market_data_targets.append(MarketDataTarget(state.spec.symbol, state.spec.interval))
            raw = state.spec.risk_config or {}
            try:
                allowed = {k: raw[k] for k in ("allocation_per_trade_pct", "max_portfolio_exposure_pct") if k in raw}
                state.risk_limits = RiskLimits(**allowed) if allowed else None
            except (TypeError, ValueError) as exc:
                state.risk_config_error = f"{type(exc).__name__}: {exc}"
            model_id = (state.spec.strategy_params or {}).get("model_id")
            if model_id:
                if self._model_lookup is None:
                    state.models_unavailable_reason = "no model registry lookup configured"
                else:
                    state.models[str(model_id)] = self._model_lookup(str(model_id))
        if state.portfolio is not None:
            known = {t.symbol for t in state.market_data_targets}
            interval = state.spec.interval if state.spec is not None else Interval.DAY_1
            for symbol in sorted(state.portfolio.positions):
                if symbol not in known:
                    state.market_data_targets.append(MarketDataTarget(symbol, interval))
        state.notes.append(
            "paper state is the persisted portfolio snapshot of this experiment, not a live trading session"
        )
        return state


def observe_market_data(
    service: MarketDataService,
    target: MarketDataTarget,
    now: datetime,
    lookback_days: int,
) -> MarketDataObservation:
    """Fetch one canonical dataset through `MarketDataService` (the only
    sanctioned door) and reduce it to an observation. Never raises."""
    end: date = now.date()
    start = end - timedelta(days=lookback_days)
    try:
        dataset = service.get_dataset(target.symbol, start=start, end=end, interval=target.interval)
    except (DataProviderError, DataValidationError, ValueError) as exc:
        return MarketDataObservation(
            symbol=target.symbol,
            interval=target.interval,
            ok=False,
            observed_at=now,
            error_code=type(exc).__name__,
            error_message=str(exc)[:200],
        )
    except Exception as exc:  # noqa: BLE001 -- monitoring must never crash on a provider surprise
        return MarketDataObservation(
            symbol=target.symbol,
            interval=target.interval,
            ok=False,
            observed_at=now,
            error_code=f"UNEXPECTED:{type(exc).__name__}",
            error_message=str(exc)[:200],
        )
    candles = dataset.candles
    report = dataset.validation_report
    return MarketDataObservation(
        symbol=target.symbol,
        interval=target.interval,
        ok=True,
        observed_at=now,
        rows=len(candles),
        dataset_start=dataset.dataset_start if len(candles) else None,
        dataset_end=dataset.dataset_end if len(candles) else None,
        content_hash=dataset.content_hash or None,
        gap_count=report.gaps.count,
        suspicious_count=len(report.suspicious),
        last_close=float(candles["close"].iloc[-1]) if len(candles) else None,
    )
