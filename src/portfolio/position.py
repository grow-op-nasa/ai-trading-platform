"""The platform-level `Position` domain model -- Sprint 7 (`DECISIONS.md`,
ADR-0039).

Deliberately its own module in the neutral `src/portfolio` package, not
`src/execution` -- Sprint 7's risk and portfolio-constraint layers need
a broker-independent view of "what does the platform currently hold,"
the same way `AccountState` (`models.py`) already gives a
broker-independent view of "what is the account's cash/equity."
`src/execution/models.py` keeps its own, separate, lighter `Position`
(quantity/entry_price/entry_signal_id only) exactly as it was --
`PaperBroker`'s tested internals are not touched this sprint (extend
rather than rewrite). The two are related but distinct: execution's
`Position` is a fill-bookkeeping record `PaperBroker` owns for its own
simulation; this `Position` is the richer, neutral record the risk and
portfolio-constraint engines reason about (stop price, lifecycle,
realized/unrealized P&L). `Portfolio.apply_fill()` (`models.py`) is what
turns one into the other -- see that method's docstring for exactly how
the two are kept in sync.

Two lifecycle states only: `OPEN` and `CLOSED`. The conceptual `FLAT`
state the Sprint 7 spec describes ("FLAT -> OPEN -> CLOSED") is
represented by *absence* -- there is no stored `Position` for a symbol
until one is opened, so "FLAT" is simply "no entry in
`Portfolio.positions` for this symbol," not a third enum member. A
`Position` object inherently describes something that has already been
opened; giving it a `FLAT` member would mean either a nonsensical
half-populated instance or dead code that never constructs one. No
partial-fill/scaling states are added either -- `PaperBroker` supports
only whole-position open and whole-position close (one open position
per symbol at a time, `DECISIONS.md` ADR-0022), so `Position` doesn't
carry lifecycle states for operations the platform can't yet perform.
See ADR-0039 for the explicit `POSITION_SCALING_NOT_SUPPORTED` rejection
this implies in `src.risk.portfolio_risk.PortfolioRiskEngine`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import pandas as pd


class PositionSide(Enum):
    """Which direction a `Position` is held in.

    Distinct from `src.signals.models.SignalDirection` (`LONG`/`SHORT`/
    `FLAT`) the same way `src.execution.models.OrderSide` is: a
    `Position` that exists is always either long or short -- there is no
    `FLAT` side, matching the lifecycle note above.
    """

    LONG = "LONG"
    SHORT = "SHORT"


class PositionLifecycle(Enum):
    """A `Position`'s lifecycle state. See the module docstring for why
    there is no third `FLAT` member."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class Position:
    """One position the platform currently holds (or held), independent
    of any particular broker.

    Args:
        symbol: which instrument.
        side: `LONG` or `SHORT`.
        quantity: signed magnitude -- positive for `LONG`, negative for
            `SHORT`, matching `src.execution.models.Position`'s existing
            convention so the two stay easy to reason about side by
            side. Always non-zero.
        entry_price: the price this position was opened at.
        entry_timestamp: when it was opened -- the originating signal's
            own timestamp (`DECISIONS.md`, ADR-0038: full precision,
            never truncated to a date).
        stop_price: the risk boundary this position was sized against,
            if any (`src.risk.portfolio_risk.PortfolioRiskEngine`). Not
            an order resting at a broker and not a guarantee of the
            realized exit price -- see that module's docstring for the
            exact assumption (`DECISIONS.md`, ADR-0039).
        current_price: the most recently known market price, if the
            caller has supplied one. `None` by default -- this platform
            has no live mark-to-market yet (`DECISIONS.md`, ADR-0022);
            `unrealized_pnl`/`exposure` fall back to `entry_price` when
            this is `None`, exactly like `PaperBroker.account_state`
            already does. Nothing in Sprint 7 sets this automatically.
        lifecycle: `OPEN` or `CLOSED`.
        realized_pnl: set once the position is closed
            (`Portfolio.apply_fill()`); `None` while `OPEN`.
        entry_signal_id: the `Signal.id` that opened this position, for
            traceability (matching `src.execution.models.Position`).

    Raises:
        ValueError: `quantity` is zero, `entry_price`/`current_price` is
            not positive, or `quantity`'s sign disagrees with `side`.
    """

    symbol: str
    side: PositionSide
    quantity: float
    entry_price: float
    entry_timestamp: pd.Timestamp
    entry_signal_id: UUID
    stop_price: float | None = None
    current_price: float | None = None
    lifecycle: PositionLifecycle = PositionLifecycle.OPEN
    realized_pnl: float | None = None

    def __post_init__(self) -> None:
        if self.quantity == 0:
            raise ValueError("quantity must not be zero -- a Position always holds something")
        if self.side is PositionSide.LONG and self.quantity < 0:
            raise ValueError(f"a LONG position must have positive quantity, got {self.quantity}")
        if self.side is PositionSide.SHORT and self.quantity > 0:
            raise ValueError(f"a SHORT position must have negative quantity, got {self.quantity}")
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got {self.entry_price}")
        if self.current_price is not None and self.current_price <= 0:
            raise ValueError(f"current_price must be positive, got {self.current_price}")

    @property
    def valuation_price(self) -> float:
        """The price used to value this position -- `current_price` if
        known, otherwise the frozen `entry_price` (`DECISIONS.md`,
        ADR-0022's no-mark-to-market limitation, carried forward)."""
        return self.current_price if self.current_price is not None else self.entry_price

    @property
    def exposure(self) -> float:
        """`abs(quantity * valuation_price)` -- this position's
        contribution to gross portfolio exposure (`DECISIONS.md`,
        ADR-0039)."""
        return abs(self.quantity) * self.valuation_price

    @property
    def unrealized_pnl(self) -> float | None:
        """Mark-to-market P&L if `current_price` is known, else `None`
        -- this is never fabricated from `entry_price` alone, unlike
        `valuation_price`/`exposure`, since claiming an unrealized P&L
        without a real current price would overstate what this platform
        actually knows (`DECISIONS.md`, ADR-0039)."""
        if self.current_price is None:
            return None
        return (self.current_price - self.entry_price) * self.quantity

    def close(self, exit_price: float) -> "Position":
        """Return a new, `CLOSED` copy of this position with
        `realized_pnl` computed against `exit_price`.

        Does not mutate `self` -- `Portfolio.apply_fill()` calls this to
        build the record it moves into `closed_positions`, leaving
        nothing in `positions` still referencing the pre-close object
        (`DECISIONS.md`, ADR-0039).

        Raises:
            ValueError: this position is already `CLOSED`, or
                `exit_price` is not positive.
        """
        if self.lifecycle is PositionLifecycle.CLOSED:
            raise ValueError(f"{self.symbol!r} position is already CLOSED")
        if exit_price <= 0:
            raise ValueError(f"exit_price must be positive, got {exit_price}")
        realized_pnl = (exit_price - self.entry_price) * self.quantity
        return Position(
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            entry_price=self.entry_price,
            entry_timestamp=self.entry_timestamp,
            entry_signal_id=self.entry_signal_id,
            stop_price=self.stop_price,
            current_price=self.current_price,
            lifecycle=PositionLifecycle.CLOSED,
            realized_pnl=realized_pnl,
        )
