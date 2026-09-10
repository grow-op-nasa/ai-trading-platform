"""The BrokerConnection interface.

This is the seam for broker/exchange connectivity, the same role
`DataProvider` plays for market data (`src/data/base.py`): every
current and future broker (Alpaca today; Interactive Brokers or
anything else tomorrow) implements this one interface. Nothing outside
`src/broker/` should ever import a specific broker directly -- code
should depend on `BrokerConnection` so that swapping brokers never
requires touching strategies, risk sizing, or execution.

Connectivity/account state (`DECISIONS.md`, ADR-0023) and order
submission (ADR-0024) are both in scope now. Order management stops at
submit + check status -- no cancellation yet, and `OrderRequest`/
`BrokerOrder` (`models.py`) are deliberately independent of
`src/execution`'s `Order`/`Fill`, since a real order's asynchronous
lifecycle (pending, partial fill, rejection) doesn't fit
`PaperBroker`'s synchronous, instant-fill model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.broker.models import BrokerOrder, OrderRequest
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

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        """Submit a market order.

        Returns immediately with whatever status the broker reports
        back -- usually still pending, not yet filled. Call
        `get_order()` later to check on it.

        Raises:
            BrokerAuthenticationError: credentials were rejected.
            BrokerConnectionError: the broker couldn't be reached, or
                rejected the request for a reason other than
                authentication.
        """
        raise NotImplementedError

    @abstractmethod
    def get_order(self, broker_order_id: str) -> BrokerOrder:
        """Fetch the current status of a previously submitted order.

        Raises:
            BrokerAuthenticationError: credentials were rejected.
            BrokerConnectionError: the broker couldn't be reached, or
                no order exists with that id.
        """
        raise NotImplementedError
