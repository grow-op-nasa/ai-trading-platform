"""The BrokerConnection interface.

This is the seam for broker/exchange connectivity, the same role
`DataProvider` plays for market data (`src/data/base.py`): every
current and future broker (Alpaca today; Interactive Brokers or
anything else tomorrow) implements this one interface. Nothing outside
`src/broker/` should ever import a specific broker directly -- code
should depend on `BrokerConnection` so that swapping brokers never
requires touching strategies, risk sizing, or execution.

Deliberately scoped to connectivity and account state only this round
(`DECISIONS.md`, ADR-0023) -- order submission against a real broker is
a separate, later step, since a real order's lifecycle (pending,
partial fill, rejection) doesn't fit `src/execution`'s existing
synchronous, instant-fill `Order`/`Fill` model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.risk.models import AccountState


class BrokerConnection(ABC):
    """Abstract base class for a live or paper broker connection."""

    @abstractmethod
    def get_account(self) -> AccountState:
        """Fetch the account's current state from the broker.

        Returns:
            A real `src.risk.AccountState` -- the same currency
            `PositionSizer` already consumes from `PaperBroker`
            (`src/execution`), so a live broker slots into the existing
            sizing pipeline without a new, parallel account model.

        Raises:
            BrokerAuthenticationError: credentials were rejected.
            BrokerConnectionError: the broker couldn't be reached at all.
        """
        raise NotImplementedError
