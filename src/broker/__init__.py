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
handling) gets its own design round. IB also geo-restricts account
access for this platform's actual deployment, so `IBKRBroker` remains
an architecture proof without a usable real account behind it.

`IGBroker` (ADR-0028) is the platform's third concrete broker, and the
first second-broker candidate this platform's user can actually use
with a real account. Session-based auth (API key + username + password
-> login -> cached session tokens), connectivity + account state only
this round -- IG's own order model (a short-lived deal reference
confirmed into a permanent position, no ongoing order-status endpoint)
doesn't fit `BrokerOrder`/`OrderStatus` without its own design round
either.
"""

from src.broker.alpaca import ALPACA_LIVE_BASE_URL, ALPACA_PAPER_BASE_URL, AlpacaBroker
from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError, BrokerError
from src.broker.ibkr import IBKR_GATEWAY_BASE_URL, IBKRBroker
from src.broker.ig import IG_DEMO_BASE_URL, IG_LIVE_BASE_URL, IGBroker
from src.broker.models import BrokerOrder, OrderRequest, OrderSide, OrderStatus

__all__ = [
    "BrokerConnection",
    "AlpacaBroker",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_LIVE_BASE_URL",
    "IBKRBroker",
    "IBKR_GATEWAY_BASE_URL",
    "IGBroker",
    "IG_DEMO_BASE_URL",
    "IG_LIVE_BASE_URL",
    "BrokerError",
    "BrokerAuthenticationError",
    "BrokerConnectionError",
    "BrokerOrder",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
]
