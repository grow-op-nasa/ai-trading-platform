"""Data shapes for position sizing (src/risk).

Plain dataclasses, no behavior beyond input validation -- `PositionSizer`
(`engine.py`) does the actual math. `AccountState` moved to
`src/portfolio` (`DECISIONS.md`, ADR-0031) -- import it from there.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    """Configurable capital-allocation parameters -- one instance can be
    shared across every sizing decision for an account, or varied per
    strategy/run.

    Args:
        allocation_per_trade_pct: fraction of account equity to deploy
            on a single trade (e.g. `0.10` = 10%). Applied identically
            regardless of `Signal.confidence` -- see `DECISIONS.md`,
            ADR-0021, for why a fixed fraction was chosen over a
            confidence-scaled one for this first version.

            **This is capital allocation, not maximum loss.** It answers
            "how much of the account is committed to this position,"
            never "how much could this trade lose." There is no
            stop-loss/risk-distance model in this codebase yet -- true
            risk-based sizing (position size derived from a maximum
            acceptable loss divided by the distance to a stop) is a
            distinct, unbuilt capability (`DECISIONS.md`, ADR-0032). A
            trade sized at `allocation_per_trade_pct` can still lose
            far more or less than that percentage of equity, depending
            on how far price moves against it -- this setting caps
            exposure, not loss.
        max_portfolio_exposure_pct: fraction of account equity allowed
            to be committed to open positions at any one time (e.g.
            `0.50` = 50%). A new position is sized down -- or rejected
            outright if there's no headroom left -- rather than ever
            exceeding this.

    Raises:
        ValueError: either percentage is outside `(0, 1]`.
    """

    allocation_per_trade_pct: float = 0.10
    max_portfolio_exposure_pct: float = 0.50

    def __post_init__(self) -> None:
        if not 0 < self.allocation_per_trade_pct <= 1:
            raise ValueError(
                "allocation_per_trade_pct must be in (0, 1], got "
                f"{self.allocation_per_trade_pct}"
            )
        if not 0 < self.max_portfolio_exposure_pct <= 1:
            raise ValueError(
                "max_portfolio_exposure_pct must be in (0, 1], got "
                f"{self.max_portfolio_exposure_pct}"
            )


@dataclass
class SizingDecision:
    """The outcome of one `PositionSizer.size()` call.

    Check `approved`, not just whether `position_size` is truthy --
    rejection is meant to be an explicit branch at call sites, not
    something inferred from a zero.
    """

    approved: bool
    position_size: float
    capital_allocated: float
    reason: str
