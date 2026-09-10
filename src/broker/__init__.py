"""Broker connectivity -- Sprint 5.

Every broker implements one interface, `BrokerConnection`, analogous to
`src/data`'s `DataProvider` -- a new broker is an extension, not a
rewrite (`DECISIONS.md`, ADR-0000). `get_account()` returns a real
`src.risk.AccountState`, the same currency `PositionSizer` already
consumes from `PaperBroker` (`src/execution`), so a live broker slots
into the existing sizing pipeline without a new account model.

Connectivity + account state shipped first (ADR-0023). Order
submission (`submit_order`/`get_order`) followed in ADR-0024, using its
own `OrderRequest`/`BrokerOrder` shapes (`models.py`) rather than
`src/execution`'s `Order`/`Fill` -- a real order's asynchronous
lifecycle (pending, partial fill, rejection, cancellation) doesn't fit
`PaperBroker`'s synchronous, instant-fill model, and importing across
that boundary would run the dependency the wrong direction.
`cancel_order` (ADR-0025) rounds out order management: it returns
`None`, confirming the broker accepted the cancellation request, not
that the order actually ended up canceled -- call `get_order()`
afterward for the resulting status.

`IBKRBroker` (ADR-0026) is the platform's second concrete broker --
proof `BrokerConnection` is actually swappable, not just designed to
be. It only implements `get_account()` so far; `submit_order`/
`get_order`/`cancel_order` raise `NotImplementedError` until Interactive
Brokers' order-placement flow (contract id lookup, reply/confirmation
handling) gets its own design round.
"""

from src.broker.alpaca import ALPACA_LIVE_BASE_URL, ALPACA_PAPER_BASE_URL, AlpacaBroker
from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError, BrokerError
from src.broker.ibkr import IBKR_GATEWAY_BASE_URL, IBKRBroker
from src.broker.models import BrokerOrder, OrderRequest, OrderSide, OrderStatus

__all__ = [
    "BrokerConnection",
    "AlpacaBroker",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_LIVE_BASE_URL",
    "IBKRBroker",
    "IBKR_GATEWAY_BASE_URL",
    "BrokerError",
    "BrokerAuthenticationError",
    "BrokerConnectionError",
    "BrokerOrder",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
]
