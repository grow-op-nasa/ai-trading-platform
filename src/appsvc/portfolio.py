"""Stored paper-portfolio state and explicit valuation (Sprint 19).

`stored_state` is a read-only view of the experiment registry's saved portfolio: cash, positions AT COST (entry price),
and realised P&L from closed positions. It is a stored snapshot, not live state, and it carries no market price.
`value_now` is an explicit operator action: it asks the existing `PortfolioValuationService` to mark open positions to
the latest price via `MarketDataService`; if any open position has no price, the aggregate equity is UNDEFINED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.appsvc.paths import AppPaths


@dataclass(frozen=True)
class StoredPosition:
    symbol: str
    side: str
    quantity: float
    entry_price: float
    entry_timestamp: str
    cost_basis: float
    stop_price: float | None


@dataclass(frozen=True)
class StoredPortfolio:
    experiment_id: int
    cash: float
    positions: tuple[StoredPosition, ...]
    realized_pnl: float
    closed_count: int


def experiment_has_portfolio(paths: AppPaths, experiment_id: int) -> bool:
    return stored_state(paths, experiment_id) is not None


def stored_state(paths: AppPaths, experiment_id: int) -> StoredPortfolio | None:
    if not paths.registry_exists():
        return None
    from src.experiments.registry import ExperimentRegistry
    pf = ExperimentRegistry(paths.registry).get_portfolio(experiment_id)
    if pf is None:
        return None
    pos = tuple(StoredPosition(p.symbol, getattr(p.side, "value", str(p.side)), float(p.quantity), float(p.entry_price),
                               str(p.entry_timestamp), abs(float(p.quantity)) * float(p.entry_price),
                               getattr(p, "stop_price", None))
                for p in pf.positions.values())
    realized = sum(p.realized_pnl for p in pf.closed_positions if p.realized_pnl is not None)
    return StoredPortfolio(experiment_id, float(pf.cash), pos, float(realized), len(pf.closed_positions))


def value_now(paths: AppPaths, experiment_id: int, *, price_lookup=None):
    """EXPLICIT: one price request per open symbol (none if there are no open positions).
    Returns (PortfolioSnapshot | None, valued_at_utc_iso)."""
    from src.analytics.valuation import PortfolioValuationService, default_price_lookup
    from src.experiments.registry import ExperimentRegistry
    if not paths.registry_exists():
        return None, ""
    pf = ExperimentRegistry(paths.registry).get_portfolio(experiment_id)
    if pf is None:
        return None, ""
    snap = PortfolioValuationService().value(pf, price_lookup=price_lookup or default_price_lookup())
    return snap, datetime.now(timezone.utc).isoformat(timespec="seconds")
