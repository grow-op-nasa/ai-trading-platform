"""Broker connectivity -- Sprint 5.

Every broker implements one interface, `BrokerConnection`, analogous to
`src/data`'s `DataProvider` -- a new broker is an extension, not a
rewrite (`DECISIONS.md`, ADR-0000). `get_account()` returns a real
`src.risk.AccountState`, the same currency `PositionSizer` already
consumes from `PaperBroker` (`src/execution`), so a live broker slots
into the existing sizing pipeline without a new account model.

Deliberately scoped to connectivity and account state only this round
-- order submission against a real broker is a separate, later step,
since a real order's lifecycle (pending, partial fill, rejection)
doesn't fit `src/execution`'s synchronous, instant-fill `Order`/`Fill`
model. See `DECISIONS.md`, ADR-0023.
"""

from src.broker.alpaca import ALPACA_LIVE_BASE_URL, ALPACA_PAPER_BASE_URL, AlpacaBroker
from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError, BrokerError

__all__ = [
    "BrokerConnection",
    "AlpacaBroker",
    "ALPACA_PAPER_BASE_URL",
    "ALPACA_LIVE_BASE_URL",
    "BrokerError",
    "BrokerAuthenticationError",
    "BrokerConnectionError",
]
