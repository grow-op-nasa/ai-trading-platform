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
lifecycle (pending, partial fill, rejection) doesn't fit
`PaperBroker`'s synchronous, instant-fill model, and importing across
that boundary would run the dependency the wrong direction. Order
management stops at submit + status check -- no cancellation yet.
"""

from src.broker.alpaca import ALPACA_LIVE_BASE_URL, ALPACA_PAPER_BASE_URL, AlpacaBroker
from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError, BrokerError
from src.broker.models import BrokerOrder, OrderRequest, OrderSide, OrderStatus

__all__ = [
    "BrokerConnection",
    "AlpacaBroker",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_LIVE_BASE_URL",
    "BrokerError",
    "BrokerAuthenticationError",
    "BrokerConnectionError",
    "BrokerOrder",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
]
