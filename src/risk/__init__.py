"""Position sizing and portfolio-level exposure limits.

Given a `Signal` and the account's current state, decides how large a
position to take -- and whether to take one at all, if doing so would
breach a configured exposure limit. Deliberately standalone from
`Backtester` and any broker/execution layer for now (`DECISIONS.md`,
ADR-0021). `AccountState` is a neutral domain model this package
consumes, not owns -- it lives in `src/portfolio` (ADR-0031) and is
re-exported here for convenience.

Sprint 7 (`DECISIONS.md`, ADR-0039) adds `PortfolioRiskEngine`: genuine,
stop-based risk sizing plus portfolio constraints, standalone from
(not a replacement for) `PositionSizer`'s allocation-only model -- see
`portfolio_risk.py`'s module docstring. A Sprint 7 cleanup pass
(`DECISIONS.md`, ADR-0040) added `PortfolioRiskEngine.decide_close()`
(the symmetric close/exit-intent counterpart to `decide()`),
`CapitalConstraintModel` (makes a `SHORT`'s unmodeled capital ceiling
machine-visible rather than an ambiguous `None`), and
`ApprovedTradeIntent` (the explicit `RiskDecision` -> Execution
handoff, via `RiskDecision.to_trade_intent()`).
"""

from src.portfolio.models import AccountState
from src.risk.engine import PositionSizer
from src.risk.models import (
    ApprovedTradeIntent,
    CapitalConstraintModel,
    PortfolioRiskLimits,
    RejectionReason,
    RiskDecision,
    RiskLimits,
    SizingDecision,
)
from src.risk.portfolio_risk import PortfolioRiskEngine

__all__ = [
    "PositionSizer",
    "AccountState",
    "RiskLimits",
    "SizingDecision",
    "PortfolioRiskEngine",
    "PortfolioRiskLimits",
    "RejectionReason",
    "RiskDecision",
    "ApprovedTradeIntent",
    "CapitalConstraintModel",
]
