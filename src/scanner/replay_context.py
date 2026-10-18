"""Serialisable inputs that make a scan decision replayable (Sprint 22, ADR-0059): the private risk context (limits, stop
policy, saved portfolio, decision price).

Pure functions over plain data; imports nothing from the broker, execution, AI, UI or provider layers.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd


# ------------------------------------------------------------------------------------------------ risk context
def describe_risk_context(risk: Any, price: float) -> dict[str, Any]:
    """What the read-only risk assessment was given, as plain data. Failures are recorded, never raised."""
    if risk is None:
        return {"assessed": False}
    try:
        engine = risk.risk
        portfolio = risk.portfolio
        return {
            "assessed": True,
            "price": float(price),
            "limits": asdict(engine._risk_limits),
            "portfolio_limits": asdict(engine._portfolio_limits),
            "stop": dict(risk.stop_policy.config),
            "portfolio": {"cash": float(portfolio.cash),
                          "positions": [_position_dict(p) for p in portfolio.positions.values()]},
            "note": "open positions only; closed-position history is not an input to the risk decision",
        }
    except Exception as exc:  # noqa: BLE001 - recorded as unavailable, which replay then reports honestly
        return {"assessed": True, "unavailable": f"{type(exc).__name__}: {exc}"}


def _position_dict(p: Any) -> dict[str, Any]:
    return {"symbol": p.symbol, "side": p.side.value, "quantity": float(p.quantity), "entry_price": float(p.entry_price),
            "entry_timestamp": pd.Timestamp(p.entry_timestamp).isoformat(), "entry_signal_id": str(p.entry_signal_id),
            "stop_price": None if p.stop_price is None else float(p.stop_price),
            "current_price": None if p.current_price is None else float(p.current_price)}


def rebuild_risk_inputs(ctx: dict[str, Any]):
    """(PortfolioRiskEngine, ATRStopPolicy, Portfolio, price) from a stored description; raises ValueError if unusable."""
    if not ctx or not ctx.get("assessed") or ctx.get("unavailable") or "limits" not in ctx:
        raise ValueError(ctx.get("unavailable") if ctx and ctx.get("unavailable") else "no risk context was stored")
    from uuid import UUID

    from src.backtesting.stop_policy import ATRStopPolicy
    from src.portfolio.models import Portfolio
    from src.portfolio.position import Position, PositionSide
    from src.risk.models import PortfolioRiskLimits, RiskLimits
    from src.risk.portfolio_risk import PortfolioRiskEngine

    engine = PortfolioRiskEngine(RiskLimits(**ctx["limits"]), PortfolioRiskLimits(**ctx["portfolio_limits"]))
    stop = ctx["stop"]
    if stop.get("type") != "atr":
        raise ValueError(f"unsupported stop policy {stop.get('type')!r}")
    policy = ATRStopPolicy(period=int(stop["period"]), multiple=float(stop["multiple"]))
    positions = [Position(symbol=p["symbol"], side=PositionSide(p["side"]), quantity=p["quantity"], entry_price=p["entry_price"],
                          entry_timestamp=pd.Timestamp(p["entry_timestamp"]), entry_signal_id=UUID(p["entry_signal_id"]),
                          stop_price=p["stop_price"], current_price=p["current_price"])
                 for p in ctx["portfolio"]["positions"]]
    return engine, policy, Portfolio.reconstruct(cash=float(ctx["portfolio"]["cash"]), positions=positions), float(ctx["price"])
