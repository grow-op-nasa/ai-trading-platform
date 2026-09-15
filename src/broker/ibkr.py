"""Interactive Brokers implementation of BrokerConnection.

This is the platform's second concrete `BrokerConnection` -- proof that
the interface (`base.py`) is actually swappable, not just designed to
be (`DECISIONS.md`, ADR-0026). Built against IB's **Client Portal Web
API**, not the TWS API: it's REST-based, so it fits the same injectable
HTTP-session pattern `alpaca.py` already uses, without introducing a
new socket-based transport model into the codebase.

Unlike Alpaca, there is no simple API-key/secret credential model for
an individual/retail IB account. Authentication happens against IB's
**Client Portal Gateway** -- a small Java process the user runs
locally and logs into via a browser (username, password, 2FA) -- and
every request in this module assumes that session already exists.
`IBKRBroker`'s constructor does no credential validation (there's
nothing to validate); a call simply fails with
`BrokerAuthenticationError` if the gateway reports the session isn't
authenticated.

There's also no separate paper/live *endpoint* the way Alpaca has
`ALPACA_PAPER_BASE_URL`/`ALPACA_LIVE_BASE_URL` -- for IB, paper vs.
live is determined entirely by which account was used to log into the
gateway, not by a URL this code chooses. `IBKR_GATEWAY_BASE_URL` is
just where the locally running gateway listens.

Scope this round is deliberately **connectivity and account state
only**, mirroring exactly how `AlpacaBroker` itself started
(ADR-0023): `submit_order`/`get_order`/`cancel_order` raise
`NotImplementedError`, not a `BrokerError`, because this is a known,
static gap in what this module supports today -- not a broker-side
failure a caller should retry or handle as a connectivity problem. IB's
real order-placement flow needs its own design pass later: orders are
placed against a numeric contract id (`conid`), not a plain symbol
string, and many orders come back with a "reply" (a risk/suitability
warning) that must be explicitly confirmed via a second call before
the order actually places.
"""

from __future__ import annotations

from typing import Protocol

import requests

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.broker.models import BrokerOrder, OrderRequest
from src.portfolio.models import AccountState

IBKR_GATEWAY_BASE_URL = "https://localhost:5000/v1/api"

_REQUEST_TIMEOUT_SECONDS = 10

_NOT_IMPLEMENTED_MESSAGE = (
    "IBKRBroker does not yet support {method}() -- Interactive Brokers order "
    "placement (contract id lookup, reply/confirmation handling) is deferred "
    "to a later round. See DECISIONS.md, ADR-0026."
)


class _Response(Protocol):
    """The minimal shape this module needs from an HTTP response --
    lets tests inject a fake without depending on `requests` internals.
    """

    status_code: int
    text: str

    def json(self) -> dict: ...


class _Session(Protocol):
    """The minimal shape this module needs from an HTTP client --
    `requests` itself satisfies this, and tests can inject a fake. Only
    `.get()` is needed today -- order submission, which would need
    `.post()`/`.delete()`, isn't implemented yet.
    """

    def get(self, url: str, headers: dict, timeout: float) -> _Response: ...


class IBKRBroker(BrokerConnection):
    """Connects to a locally running Interactive Brokers Client Portal
    Gateway.

    Args:
        base_url: where the Client Portal Gateway is listening. Defaults
            to `IBKR_GATEWAY_BASE_URL` (`https://localhost:5000/v1/api`,
            the gateway's own default). Unlike Alpaca, this does not
            select paper vs. live -- that's determined by which account
            is logged into the gateway.
        session: HTTP client to use. Defaults to the `requests` module
            itself; tests inject a fake satisfying `_Session`.
    """

    def __init__(
        self,
        base_url: str = IBKR_GATEWAY_BASE_URL,
        session: _Session | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session or requests

    def get_account(self) -> AccountState:
        account_id = self._resolve_account_id()
        data = self._request("get", f"/portfolio/{account_id}/summary")
        return _parse_account(data)

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="submit_order"))

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="get_order"))

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="cancel_order"))

    def _resolve_account_id(self) -> str:
        """Find which IB account to query, since every portfolio
        endpoint is scoped to a specific account id.

        Raises:
            BrokerConnectionError: the gateway reported no accounts at
                all for this session.
        """
        data = self._request("get", "/iserver/accounts")
        accounts = data.get("accounts") or []
        if not accounts:
            raise BrokerConnectionError("IB Gateway reported no accounts for this session")
        return data.get("selectedAccount") or accounts[0]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Shared HTTP call + error handling for every IB endpoint --
        the same shape `AlpacaBroker._request()` uses, kept as a
        separate implementation since the two brokers have no shared
        base class to put it on (each concrete broker owns its own
        network shape).
        """
        call = getattr(self._session, method)
        try:
            response = call(
                f"{self._base_url}{path}",
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.exceptions.RequestException as exc:
            raise BrokerConnectionError(f"failed to reach IB Gateway: {exc}") from exc

        if response.status_code in (401, 403):
            raise BrokerAuthenticationError(
                f"IB Gateway session is not authenticated (HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise BrokerConnectionError(
                f"IB Gateway returned HTTP {response.status_code}: {response.text}"
            )

        return response.json()

    def _headers(self) -> dict:
        # No API key/secret to send -- authentication is the Client
        # Portal Gateway's own browser-based session, not a header this
        # code constructs.
        return {}


def _parse_account(data: dict) -> AccountState:
    """Convert an IB `/portfolio/{accountId}/summary` response into an
    `AccountState`.

    `equity` comes from `netliquidation` -- required, since a summary
    response without it isn't one we can trust at all (fails loudly via
    `KeyError` rather than than silently defaulting to zero equity).
    `open_exposure` comes from `grosspositionvalue` (IB's own name for
    the sum of absolute value of all non-cash positions) -- optional
    and defaults to `0.0`, since a flat account may omit it.
    """
    equity = float(data["netliquidation"]["amount"])
    gross_position_value = data.get("grosspositionvalue") or {}
    open_exposure = abs(float(gross_position_value.get("amount") or 0.0))
    return AccountState(equity=equity, open_exposure=open_exposure)
