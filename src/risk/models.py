"""Data shapes for position sizing (src/risk).

Plain dataclasses, no behavior beyond input validation -- `PositionSizer`
(`engine.py`) does the actual math.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    """Configurable risk parameters -- one instance can be shared across
    every sizing decision for an account, or varied per strategy/run.

    Args:
        risk_per_trade_pct: fraction of account equity to deploy on a
            single trade (e.g. `0.10` = 10%). Applied identically
            regardless of `Signal.confidence` -- see `DECISIONS.md`,
            ADR-0021, for why a fixed fraction was chosen over a
            confidence-scaled one for this first version.
        max_portfolio_exposure_pct: fraction of account equity allowed
            to be committed to open positions at any one time (e.g.
            `0.50` = 50%). A new position is sized down -- or rejected
            outright if there's no headroom left -- rather than ever
            exceeding this.

    Raises:
        ValueError: either percentage is outside `(0, 1]`.
    """

    risk_per_trade_pct: float = 0.10
    max_portfolio_exposure_pct: float = 0.50

    def __post_init__(self) -> None:
        if not 0 < self.risk_per_trade_pct <= 1:
            raise ValueError(
                f"risk_per_trade_pct must be in (0, 1], got {self.risk_per_trade_pct}"
            )
        if not 0 < self.max_portfolio_exposure_pct <= 1:
            raise ValueError(
                "max_portfolio_exposure_pct must be in (0, 1], got "
                f"{self.max_portfolio_exposure_pct}"
            )


@dataclass
class AccountState:
    """A snapshot of account state at the moment a sizing decision is
    needed -- deliberately minimal, since no broker or live portfolio
    tracker exists yet (Sprint 5). The caller is responsible for
    constructing this from whatever it currently knows.

    Args:
        equity: total account value (cash + open positions) -- the base
            every risk limit is computed as a fraction of.
        open_exposure: total dollar amount already committed to open
            positions, across however many the caller is tracking.

    Raises:
        ValueError: `equity` isn't positive, or `open_exposure` is
            negative.
    """

    equity: float
    open_exposure: float = 0.0

    def __post_init__(self) -> None:
        if self.equity <= 0:
            raise ValueError(f"equity must be positive, got {self.equity}")
        if self.open_exposure < 0:
            raise ValueError(
                f"open_exposure cannot be negative, got {self.open_exposure}"
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
