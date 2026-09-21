"""The neutral account-state and portfolio domain models.

Deliberately its own capability package, not owned by `src/broker`,
`src/risk`, or `src/execution` -- all three need this shape, in
different roles (brokers report it, `PositionSizer` consumes it,
`PaperBroker` constructs it), and none of them should have to depend on
either of the other two just to share it (`DECISIONS.md`, ADR-0031).

`AccountState` is a plain dataclass, no behavior beyond input
validation, and no broker-specific or risk-specific concept anywhere in
it -- unchanged since ADR-0031.

`Portfolio` (`DECISIONS.md`, ADR-0039, Sprint 7) is new: the richer,
still-neutral aggregate the portfolio-aware risk layer
(`src.risk.portfolio_risk.PortfolioRiskEngine`) needs -- cash plus the
open `Position`s (`position.py`) that make up exposure, not just
`AccountState`'s two summary numbers. `Portfolio.to_account_state()`
bridges back to the older, simpler shape for anything that only needs
that. `Portfolio` depends only on `src.portfolio.position` (its own
package) -- never on `src.broker`/`src.risk`/`src.execution`, same rule
`AccountState` has always followed (`tests/test_architecture.py`,
`test_portfolio_package_depends_on_nothing_else_in_this_codebase`).
Consequently `Portfolio` has no `apply_fill(fill)`-shaped method taking
an `src.execution.models.Fill` directly -- that would import across the
forbidden boundary. Instead `Portfolio` exposes `open_position()`/
`close_position()` on its own primitive terms, and
`src.execution.portfolio_sync.apply_fill_to_portfolio()` (execution is
allowed to depend on portfolio, never the reverse) is the small glue
function that reads a `Fill` and calls the right one.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pandas as pd

from src.portfolio.position import Position, PositionSide


@dataclass
class AccountState:
    """A snapshot of an account's current state.

    Args:
        equity: total account value (cash + open positions) -- the base
            every risk limit (`src.risk`) is computed as a fraction of.
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


class Portfolio:
    """Cash plus open positions -- the neutral aggregate Sprint 7's risk
    and portfolio-constraint layers reason about (`DECISIONS.md`,
    ADR-0039).

    A regular class, not a dataclass, matching `PaperBroker`'s own
    convention (`src.execution.engine`) -- `Portfolio` has real
    invariants to protect (its positions dict must stay internally
    consistent with `cash`), not just data to hold.

    Args:
        cash: available capital. Must be non-negative -- a fully
            invested portfolio can legitimately hold `0.0`.

    Raises:
        ValueError: `cash` is negative.
    """

    def __init__(self, cash: float) -> None:
        if cash < 0:
            raise ValueError(f"cash cannot be negative, got {cash}")
        self.cash = cash
        self._positions: dict[str, Position] = {}
        self._closed_positions: list[Position] = []

    @classmethod
    def reconstruct(
        cls,
        cash: float,
        positions: list[Position] | None = None,
        closed_positions: list[Position] | None = None,
    ) -> "Portfolio":
        """Rebuild a `Portfolio` directly from already-known state --
        for restoring a previously-saved snapshot (Sprint 9,
        `DECISIONS.md` ADR-0042: `src.experiments.registry.
        ExperimentRegistry.get_portfolio()`), not for live trading.

        Deliberately distinct from `open_position()`/`close_position()`:
        those simulate a *new* fill against the portfolio's current
        state (deriving `cash` from an entry/exit price, rejecting a
        symbol that's already open or already closed) -- exactly the
        wrong operations for loading state that was already correct
        the moment it was saved. This bypasses that simulation and
        trusts `cash`/`positions`/`closed_positions` as given.

        Args:
            cash: the exact cash balance to restore.
            positions: open positions to restore, keyed internally by
                their own `symbol`. Defaults to none.
            closed_positions: closed positions to restore, in order.
                Defaults to none.

        Raises:
            ValueError: `cash` is negative, or two entries in
                `positions` share a `symbol`.
        """
        portfolio = cls(cash=cash)
        for position in positions or []:
            if position.symbol in portfolio._positions:
                raise ValueError(
                    f"{position.symbol!r} appears more than once in positions -- "
                    f"a Portfolio holds at most one open position per symbol"
                )
            portfolio._positions[position.symbol] = position
        portfolio._closed_positions.extend(closed_positions or [])
        return portfolio

    @property
    def positions(self) -> dict[str, Position]:
        """A defensive copy -- callers can't mutate internal state
        through it (matches `PaperBroker.positions`)."""
        return dict(self._positions)

    @property
    def closed_positions(self) -> list[Position]:
        """A defensive copy of every position closed so far, in the
        order they closed -- the platform's realized-P&L history."""
        return list(self._closed_positions)

    @property
    def equity(self) -> float:
        """Cash plus each open position's signed contribution at its
        `valuation_price` -- the same formula
        `PaperBroker.account_state.equity` uses (`DECISIONS.md`,
        ADR-0022): unchanged immediately after opening a position
        (long or short), since cash and position value move opposite
        and equal amounts at entry; only a subsequent close (realizing
        P&L into cash) or a supplied `current_price` change it further.
        """
        return self.cash + sum(
            position.quantity * position.valuation_price
            for position in self._positions.values()
        )

    @property
    def total_exposure(self) -> float:
        """Gross exposure across every open position -- the sum of each
        position's own `exposure` (`abs(quantity * valuation_price)`),
        never netted long against short (`DECISIONS.md`, ADR-0039)."""
        return sum(position.exposure for position in self._positions.values())

    def symbol_exposure(self, symbol: str) -> float:
        """This symbol's own exposure -- `0.0` if it has no open
        position, never `None`; a symbol not held contributes nothing,
        which is a fact, not a missing value."""
        position = self._positions.get(symbol)
        return position.exposure if position is not None else 0.0

    @property
    def position_count(self) -> int:
        """How many distinct symbols currently have an open position --
        what `max_concurrent_positions`
        (`src.risk.models.PortfolioRiskLimits`) is measured against."""
        return len(self._positions)

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def open_position(
        self,
        symbol: str,
        side: PositionSide,
        quantity: float,
        entry_price: float,
        entry_timestamp: pd.Timestamp,
        entry_signal_id: UUID,
        stop_price: float | None = None,
    ) -> Position:
        """Open a new position and debit/credit `cash` accordingly.

        `quantity` is signed (positive for `LONG`, negative for
        `SHORT`, matching `Position`'s own convention) -- cash moves by
        `-quantity * entry_price` uniformly for both: a `LONG` (positive
        quantity) debits cash (buying), a `SHORT` (negative quantity)
        credits it (selling first), with no separate branch needed.

        Raises:
            ValueError: `symbol` already has an open position --
                `Portfolio` does not support scaling into an existing
                position this sprint, the same limitation
                `PaperBroker` already has (`DECISIONS.md`, ADR-0022,
                ADR-0039); a caller reaching this should have already
                been rejected upstream with
                `RejectionReason.POSITION_SCALING_NOT_SUPPORTED`
                (`src.risk.portfolio_risk.PortfolioRiskEngine`).
        """
        if symbol in self._positions:
            raise ValueError(
                f"{symbol!r} already has an open position -- Portfolio does "
                f"not support scaling into an existing position this sprint "
                f"(DECISIONS.md, ADR-0039)"
            )
        position = Position(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_timestamp=entry_timestamp,
            entry_signal_id=entry_signal_id,
            stop_price=stop_price,
        )
        self.cash += -quantity * entry_price
        self._positions[symbol] = position
        return position

    def close_position(self, symbol: str, exit_price: float) -> Position:
        """Fully close `symbol`'s open position at `exit_price`: credit/
        debit `cash`, move the position (now `CLOSED`, with
        `realized_pnl` set) into `closed_positions`, and remove it from
        `positions` -- it no longer contributes to `total_exposure` or
        `symbol_exposure()` from this call onward.

        Cash moves by `+quantity * exit_price` using the position's own
        stored signed quantity -- closing a `LONG` (positive quantity)
        credits cash (selling), closing a `SHORT` (negative quantity)
        debits it (buying to cover), again with no separate branch.

        Raises:
            ValueError: `symbol` has no open position.
        """
        existing = self._positions.get(symbol)
        if existing is None:
            raise ValueError(f"no open position in {symbol!r} to close")
        closed = existing.close(exit_price)
        self.cash += existing.quantity * exit_price
        del self._positions[symbol]
        self._closed_positions.append(closed)
        return closed

    def to_account_state(self) -> AccountState:
        """Bridge to the older, simpler `AccountState` shape -- for any
        caller (e.g. the legacy `PositionSizer`) that only needs
        equity/open_exposure, not the full position-level detail."""
        return AccountState(equity=self.equity, open_exposure=self.total_exposure)
