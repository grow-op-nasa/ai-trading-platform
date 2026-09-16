"""The neutral account/portfolio domain model.

`AccountState` lived in `src/risk` from Sprint 4 through Sprint 5's
broker work, which quietly pointed the dependency arrow backwards:
`src/broker` -- foundational connectivity infrastructure -- had to
import from `src/risk`, a layer that is supposed to *consume* account
information, not define its domain model (`DECISIONS.md`, ADR-0031).

`src/portfolio` depends on nothing else in this codebase. `src/broker`,
`src/risk`, and `src/execution` all depend on it for `AccountState`; it
depends on none of them. This is the same shape ADR-0002 already
established for `DataProvider` versus `MarketDataService` -- a shared
shape lives where nothing needs to reach backwards to get it.

Sprint 7 (`DECISIONS.md`, ADR-0039) adds `Portfolio` and `Position`
alongside `AccountState`, under the same rule -- still depends on
nothing else in this codebase.
"""

from src.portfolio.models import AccountState, Portfolio
from src.portfolio.position import Position, PositionLifecycle, PositionSide

__all__ = [
    "AccountState",
    "Portfolio",
    "Position",
    "PositionSide",
    "PositionLifecycle",
]
