"""Alpaca implementation of BrokerConnection.

This is one interchangeable implementation of the `BrokerConnection`
interface (`base.py`). If we later add Interactive Brokers or another
venue, we write a new class here (or in a sibling module) implementing
the same interface -- nothing else in the codebase should need to
change.

Defaults to Alpaca's **paper-trading** endpoint, not live trading --
this platform is a research and paper-trading tool today, and
connecting to real money should always be an explicit, deliberate
override (`base_url=ALPACA_LIVE_BASE_URL`), never an accident of a
default value. See `DECISIONS.md`, ADR-0023.

Credentials come from `api_key`/`api_secret` arguments, or the
`ALPACA_API_KEY`/`ALPACA_API_SECRET` environment variables if the
arguments aren't given -- the same convention `src/research`'s
`ANTHROPIC_API_KEY` already uses. Missing credentials raise immediately
in `__init__`, before any network call is attempted.

Order submission (`submit_order`/`get_order`, ADR-0024) is market-order
only, matching `PaperBroker`'s own simplicity. `cancel_order`
(ADR-0025) requests cancellation and returns `None` -- it confirms the
broker *accepted* the request, not that the order actually ended up
canceled, since a real cancellation is asynchronous. Call `get_order()`
afterward to see the resulting status.
"""

from __future__ import annotations

import os
from typing import Protocol

import requests

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.broker.models import BrokerOrder, OrderRequest, OrderSide, OrderStatus
from src.portfolio.models import AccountState

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL = "https://api.alpaca.markets"

_REQUEST_TIMEOUT_SECONDS = 10

# Alpaca's raw order `status` strings, mapped onto our own OrderStatus.
# Deliberately explicit rather than a fallback guess -- an unrecognized
# status raises (`_parse_order`) instead of silently defaulting, the
# same "never fake a pass" posture `atp doctor` follows.
_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.PENDING,
    "accepted": OrderStatus.PENDING,
    "pending_new": OrderStatus.PENDING,
    "accepted_for_bidding": OrderStatus.PENDING,
    "calculated": OrderStatus.PENDING,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "rejected": OrderStatus.REJECTED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "stopped": OrderStatus.CANCELED,
    "suspended": OrderStatus.CANCELED,
}


class _Response(Protocol):
    """The minimal shape this module needs from an HTTP response --
    lets tests inject a fake without depending on `requests` internals.
    """

    status_code: int
    text: str

    def json(self) -> dict: ...


class _Session(Protocol):
    """The minimal shape this module needs from an HTTP client --
    `requests` itself satisfies this, and tests can inject a fake.
    """

    def get(self, url: str, headers: dict, timeout: float) -> _Response: ...

    def post(self, url: str, headers: dict, json: dict, timeout: float) -> _Response: ...

    def delete(self, url: str, headers: dict, timeout: float) -> _Response: ...


class AlpacaBroker(BrokerConnection):
    """Connects to Alpaca's REST API.

    Args:
        api_key: Alpaca API key ID. Falls back to `ALPACA_API_KEY`.
        api_secret: Alpaca API secret key. Falls back to
            `ALPACA_API_SECRET`.
        base_url: which Alpaca environment to talk to. Defaults to
            `ALPACA_PAPER_BASE_URL` -- pass `ALPACA_LIVE_BASE_URL`
            explicitly to connect to a real, live-money account.
        session: HTTP client to use. Defaults to the `requests` module
            itself; tests inject a fake satisfying `_Session`.

    Raises:
        BrokerAuthenticationError: no API key/secret was found in
            either the arguments or the environment.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = ALPACA_PAPER_BASE_URL,
        session: _Session | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY")
        self._api_secret = api_secret or os.environ.get("ALPACA_API_SECRET")
        if not self._api_key or not self._api_secret:
            raise BrokerAuthenticationError(
                "ALPACA_API_KEY and ALPACA_API_SECRET must be set, as "
                "arguments or environment variables"
            )
        self._base_url = base_url.rstrip("/")
        self._session = session or requests

    def get_account(self) -> AccountState:
        return _parse_account(self._request("get", "/v2/account"))

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        body = {
            "symbol": request.symbol,
            "qty": str(request.quantity),
            "side": "buy" if request.side is OrderSide.BUY else "sell",
            "type": "market",
            "time_in_force": "day",
        }
        return _parse_order(self._request("post", "/v2/orders", json=body))

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        return _parse_order(self._request("get", f"/v2/orders/{broker_order_id}"))

    def cancel_order(self, broker_order_id: str) -> None:
        self._request("delete", f"/v2/orders/{broker_order_id}", parse_json=False)

    def _request(self, method: str, path: str, parse_json: bool = True, **kwargs) -> dict | None:
        """Shared HTTP call + error handling for every Alpaca endpoint.

        Consolidates what used to live only inside `get_account()`:
        network failures and non-2xx responses both become one of our
        own `BrokerError` subclasses, never a raw `requests` exception
        or an unchecked status code. `parse_json=False` skips the
        `.json()` call for endpoints like cancel, which return `204 No
        Content` with no body to parse.
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
            raise BrokerConnectionError(f"failed to reach Alpaca: {exc}") from exc

        if response.status_code in (401, 403):
            raise BrokerAuthenticationError(
                f"Alpaca rejected the provided credentials (HTTP {response.status_code})"
            )
        if response.status_code not in (200, 201, 204):
            raise BrokerConnectionError(
                f"Alpaca returned HTTP {response.status_code}: {response.text}"
            )

        return response.json() if parse_json else None

    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self._api_key, "APCA-API-SECRET-KEY": self._api_secret}


def _parse_account(data: dict) -> AccountState:
    """Convert Alpaca's `/v2/account` response into an `AccountState`.

    `open_exposure` maps to the sum of Alpaca's long and short market
    value (both taken as absolute values, matching `AccountState`'s own
    convention of an always-non-negative committed-capital figure) --
    fields are treated as optional and default to `0.0`, since not
    every Alpaca account type reports both.
    """
    equity = float(data["equity"])
    long_exposure = abs(float(data.get("long_market_value") or 0.0))
    short_exposure = abs(float(data.get("short_market_value") or 0.0))
    return AccountState(equity=equity, open_exposure=long_exposure + short_exposure)


def _parse_order(data: dict) -> BrokerOrder:
    """Convert one of Alpaca's order responses into a `BrokerOrder`.

    Raises:
        BrokerConnectionError: Alpaca reported a `status` string we
            don't recognize. We'd rather fail loudly than guess wrong
            about whether an order filled.
    """
    raw_status = data["status"]
    status = _STATUS_MAP.get(raw_status)
    if status is None:
        raise BrokerConnectionError(f"Alpaca returned an unrecognized order status: {raw_status!r}")

    filled_avg_price = data.get("filled_avg_price")
    return BrokerOrder(
        broker_order_id=data["id"],
        symbol=data["symbol"],
        side=OrderSide.BUY if data["side"] == "buy" else OrderSide.SELL,
        quantity=float(data["qty"]),
        status=status,
        filled_quantity=float(data.get("filled_qty") or 0.0),
        filled_avg_price=float(filled_avg_price) if filled_avg_price else None,
    )
