"""The neutral account-state domain model.

Deliberately its own capability package, not owned by `src/broker`,
`src/risk`, or `src/execution` -- all three need this shape, in
different roles (brokers report it, `PositionSizer` consumes it,
`PaperBroker` constructs it), and none of them should have to depend on
either of the other two just to share it (`DECISIONS.md`, ADR-0031).

Plain dataclass, no behavior beyond input validation, and no
broker-specific or risk-specific concept anywhere in it.
"""

from __future__ import annotations

from dataclasses import dataclass


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
