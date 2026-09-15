"""The BrokerConnection interface.

This is the seam for broker/exchange connectivity, the same role
`DataProvider` plays for market data (`src/data/base.py`): every
current and future broker (Alpaca today; Interactive Brokers or
anything else tomorrow) implements this one interface. Nothing outside
`src/broker/` should ever import a specific broker directly -- code
should depend on `BrokerConnection` so that swapping brokers never
requires touching strategies, risk sizing, or execution.

Connectivity/account state (`DECISIONS.md`, ADR-0023), order submission
(ADR-0024), and order cancellation (ADR-0025) are all in scope now.
`OrderRequest`/`BrokerOrder` (`models.py`) are deliberately independent
of `src/execution`'s `Order`/`Fill`, since a real order's asynchronous
lifecycle (pending, partial fill, rejection, cancellation) doesn't fit
`PaperBroker`'s synchronous, instant-fill model.

`get_account()` returns `src.portfolio.AccountState`, a neutral domain
model that lives outside both `src/broker` and `src/risk`
(`DECISIONS.md`, ADR-0031) -- broker infrastructure is foundational and
should not have to depend on the risk layer just to describe an
account.

Not every concrete broker implements every method identically, and a
`NotImplementedError` here does not always mean the same thing -- three
distinct cases show up across `src/broker`'s concrete implementations,
and each one's docstring/module comment says which applies: (1) **not
yet implemented** -- the broker's API could support this, but this
codebase hasn't built it yet (e.g. `IBKRBroker`/`TigerBroker`'s
`submit_order`, pending their own design round); (2) **intentionally
impossible given the broker's own API model** -- no design round would
change the answer, because the broker's data model doesn't have the
concept this method asks for (e.g. `IGBroker.get_order`/`cancel_order`:
a filled market order becomes a position, not a queryable order object,
so there is nothing to poll or cancel); (3) genuinely unsupported by
this interface entirely (not currently exercised by any broker in this
codebase, but distinct from the other two in principle). Treat a
`NotImplementedError`'s message as authoritative about which of these
applies -- do not assume case (1) (a gap to eventually fill) when a
broker actually means case (2) (a structural fact about how that broker
works).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.broker.models import BrokerOrder, OrderRequest
from src.portfolio.models import AccountState


class BrokerConnection(ABC):
    """Abstract base class for a live or paper broker connection."""

    @abstractmethod
    def get_account(self) -> AccountState:
        """Fetch the account's current state from the broker.

        Returns:
            A real `src.portfolio.AccountState` -- the same currency
            `PositionSizer` (`src/risk`) already consumes from
            `PaperBroker` (`src/execution`), so a live broker slots into
            the existing sizing pipeline without a new, parallel account
            model.

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

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        """Request cancellation of a previously submitted order.

        Returns `None` on success -- this confirms the broker *accepted*
        the cancellation request, not that the order actually ended up
        canceled. A real cancellation is asynchronous: the order may
        already have filled, or may fill in the brief window before the
        cancellation takes effect. Call `get_order()` afterward to see
        the resulting status.

        Raises:
            BrokerAuthenticationError: credentials were rejected.
            BrokerConnectionError: the broker couldn't be reached, no
                order exists with that id, or the order is no longer in
                a cancelable state (e.g. already filled).
        """
        raise NotImplementedError
