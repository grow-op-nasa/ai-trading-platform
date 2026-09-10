"""IG implementation of BrokerConnection.

The platform's third concrete broker (`DECISIONS.md`, ADR-0028) -- and,
unlike `IBKRBroker` (ADR-0026), one this platform's actual user can use
with a real account, since IG doesn't geo-restrict access the way
Interactive Brokers does. Built against IG's REST Trading API, which
(like Alpaca, unlike IB) is plain HTTP with no locally running gateway
process required.

Credentials and auth are a genuine third shape in this codebase.
Alpaca uses a static `API_KEY`/`API_SECRET` header pair, valid forever
until revoked. IG instead requires an API key *plus* a username and
password, exchanged once via `POST /session` for short-lived session
tokens (`CST` / `X-SECURITY-TOKEN`) that must be attached to every
subsequent request. `IGBroker` logs in lazily on first use and caches
those tokens for the lifetime of the instance -- there is no
session-refresh logic here (IG's tokens are valid for hours, not
seconds), so a long-lived `IGBroker` instance may eventually need to be
reconstructed to re-authenticate; that's a known, accepted gap for this
round (see ADR-0028).

`IG_DEMO_BASE_URL`/`IG_LIVE_BASE_URL` select demo vs. live the same
explicit way Alpaca's `ALPACA_PAPER_BASE_URL`/`ALPACA_LIVE_BASE_URL`
do -- unlike IB, where paper vs. live is determined by which account is
logged into the gateway, not a URL.

`submit_order` (ADR-0029) places a market order and resolves it
**synchronously**: `POST /positions/otc` returns a short-lived
`dealReference`, which is immediately confirmed via
`GET /confirms/{dealReference}` into `ACCEPTED`/`REJECTED` plus a
permanent `dealId` -- so the `BrokerOrder` `submit_order()` returns is
already final (`FILLED` or `REJECTED`), not a pending state a caller
needs to check on later. `get_order`/`cancel_order` stay
`NotImplementedError` deliberately: there is no live endpoint to
re-query an IG market order's status after the fact (a filled order
simply becomes a position, not a persistent order object), and nothing
about a resolved market order can be canceled. Pretending otherwise --
e.g. approximating status via a position lookup -- would guess at a
distinction (closed-because-filled vs. never-existed) IG's API doesn't
actually let this code tell apart.
"""

from __future__ import annotations

import os
from typing import Mapping, Protocol

import requests

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.broker.models import BrokerOrder, OrderRequest, OrderSide, OrderStatus
from src.risk.models import AccountState

IG_DEMO_BASE_URL = "https://demo-api.ig.com/gateway/deal"
IG_LIVE_BASE_URL = "https://api.ig.com/gateway/deal"

_REQUEST_TIMEOUT_SECONDS = 10

_NOT_IMPLEMENTED_MESSAGE = (
    "IGBroker does not support {method}() for market orders -- "
    "submit_order() already resolves synchronously (ACCEPTED+OPEN or "
    "REJECTED) via IG's deal confirmation, so there is no later status to "
    "poll for and nothing left to cancel. See DECISIONS.md, ADR-0029."
)


class _Response(Protocol):
    """The minimal shape this module needs from an HTTP response --
    lets tests inject a fake without depending on `requests` internals.
    `headers` is needed here (unlike Alpaca/IB) because IG's session
    tokens come back as response headers, not JSON body fields.
    """

    status_code: int
    text: str
    headers: Mapping[str, str]

    def json(self) -> dict: ...


class _Session(Protocol):
    """The minimal shape this module needs from an HTTP client --
    `requests` itself satisfies this, and tests can inject a fake.
    """

    def get(self, url: str, headers: dict, timeout: float) -> _Response: ...

    def post(self, url: str, headers: dict, json: dict, timeout: float) -> _Response: ...


class IGBroker(BrokerConnection):
    """Connects to IG's REST Trading API.

    Args:
        api_key: IG API key. Falls back to `IG_API_KEY`.
        username: IG account username/identifier. Falls back to
            `IG_USERNAME`.
        password: IG account password. Falls back to `IG_PASSWORD`.
        base_url: which IG environment to talk to. Defaults to
            `IG_DEMO_BASE_URL` -- pass `IG_LIVE_BASE_URL` explicitly to
            connect to a real, live-money account.
        account_id: which IG account to use, if the login has access to
            more than one. Falls back to `IG_ACCOUNT_ID`, or IG's own
            "preferred" account if neither is given.
        session: HTTP client to use. Defaults to the `requests` module
            itself; tests inject a fake satisfying `_Session`.

    Raises:
        BrokerAuthenticationError: no API key, username, or password
            was found in either the arguments or the environment.
    """

    def __init__(
        self,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        base_url: str = IG_DEMO_BASE_URL,
        account_id: str | None = None,
        session: _Session | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("IG_API_KEY")
        self._username = username or os.environ.get("IG_USERNAME")
        self._password = password or os.environ.get("IG_PASSWORD")
        if not self._api_key or not self._username or not self._password:
            raise BrokerAuthenticationError(
                "IG_API_KEY, IG_USERNAME, and IG_PASSWORD must all be set, "
                "as arguments or environment variables"
            )
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id or os.environ.get("IG_ACCOUNT_ID")
        self._session = session or requests
        self._cst: str | None = None
        self._security_token: str | None = None

    def get_account(self) -> AccountState:
        return _parse_account(self._get_selected_account())

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        currency_code = self._get_selected_account().get("currency")
        if not currency_code:
            raise BrokerConnectionError("IG account has no currency set")

        body = {
            "currencyCode": currency_code,
            "direction": request.side.value,
            "epic": request.symbol,
            "expiry": "-",
            "forceOpen": True,
            "guaranteedStop": False,
            "level": None,
            "limitDistance": None,
            "limitLevel": None,
            "orderType": "MARKET",
            "quoteId": None,
            "size": request.quantity,
            "stopDistance": None,
            "stopLevel": None,
            "trailingStop": False,
            "trailingStopIncrement": None,
        }
        submitted = self._request("post", "/positions/otc", version="2", json=body)
        deal_reference = submitted["dealReference"]
        confirmation = self._request("get", f"/confirms/{deal_reference}", version="1")
        return _parse_confirmation(confirmation)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="get_order"))

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="cancel_order"))

    def _get_selected_account(self) -> dict:
        """Fetch this session's accounts and pick which one to use.

        Shared by `get_account()` (to build the `AccountState`) and
        `submit_order()` (to read the account's own currency for the
        order body) -- one account-resolution path, not two.

        Raises:
            BrokerConnectionError: the session has no accounts at all.
        """
        data = self._request("get", "/accounts", version="1")
        accounts = data.get("accounts") or []
        if not accounts:
            raise BrokerConnectionError("IG reported no accounts for this session")
        return self._select_account(accounts)

    def _select_account(self, accounts: list[dict]) -> dict:
        """Pick which of the logged-in session's accounts to use.

        Raises:
            BrokerConnectionError: an explicit `account_id` was given
                but doesn't match any account in this session.
        """
        if self._account_id:
            for account in accounts:
                if account.get("accountId") == self._account_id:
                    return account
            raise BrokerConnectionError(
                f"IG account {self._account_id!r} not found in this session"
            )
        for account in accounts:
            if account.get("preferred"):
                return account
        return accounts[0]

    def _ensure_authenticated(self) -> None:
        if self._cst is None or self._security_token is None:
            self._login()

    def _login(self) -> None:
        headers = {
            "X-IG-API-KEY": self._api_key,
            "VERSION": "2",
            "Accept": "application/json; charset=UTF-8",
        }
        try:
            response = self._session.post(
                f"{self._base_url}/session",
                headers=headers,
                json={"identifier": self._username, "password": self._password},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            raise BrokerConnectionError(f"failed to reach IG: {exc}") from exc

        if response.status_code in (401, 403):
            raise BrokerAuthenticationError(
                f"IG rejected the provided credentials (HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise BrokerConnectionError(
                f"IG returned HTTP {response.status_code}: {response.text}"
            )

        cst = response.headers.get("CST")
        security_token = response.headers.get("X-SECURITY-TOKEN")
        if not cst or not security_token:
            raise BrokerConnectionError("IG login response did not include session tokens")

        self._cst = cst
        self._security_token = security_token

    def _request(self, method: str, path: str, version: str = "1", **kwargs) -> dict:
        """Shared HTTP call + error handling for every IG endpoint --
        the same shape `AlpacaBroker._request()`/`IBKRBroker._request()`
        use, kept as a separate implementation since each concrete
        broker owns its own network shape. Ensures a session exists
        (logging in lazily on first use) before every call.
        """
        self._ensure_authenticated()
        call = getattr(self._session, method)
        try:
            response = call(
                f"{self._base_url}{path}",
                headers=self._headers(version),
                timeout=_REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.exceptions.RequestException as exc:
            raise BrokerConnectionError(f"failed to reach IG: {exc}") from exc

        if response.status_code in (401, 403):
            raise BrokerAuthenticationError(
                f"IG session is not authenticated (HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise BrokerConnectionError(
                f"IG returned HTTP {response.status_code}: {response.text}"
            )

        return response.json()

    def _headers(self, version: str) -> dict:
        return {
            "X-IG-API-KEY": self._api_key,
            "CST": self._cst,
            "X-SECURITY-TOKEN": self._security_token,
            "VERSION": version,
            "Accept": "application/json; charset=UTF-8",
        }


def _parse_account(account: dict) -> AccountState:
    """Convert one entry from IG's `/accounts` response into an
    `AccountState`.

    `equity` maps to `balance.balance` -- the account's total value.
    `open_exposure` maps to `balance.deposit` -- the margin currently
    committed to open positions. This is a deliberate approximation:
    IG accounts trade CFDs and spread bets, which are margined rather
    than fully paid for the way Alpaca's equities are, so "deposit"
    (margin used) is the closest honest analogue to `open_exposure`'s
    "capital currently committed" meaning -- not the full notional
    value of open positions, which IG's balance summary doesn't expose
    directly. Optional and defaults to `0.0` if absent.
    """
    balance = account.get("balance") or {}
    equity = float(balance["balance"])
    open_exposure = abs(float(balance.get("deposit") or 0.0))
    return AccountState(equity=equity, open_exposure=open_exposure)


def _parse_confirmation(data: dict) -> BrokerOrder:
    """Convert an IG `/confirms/{dealReference}` response into a
    `BrokerOrder`.

    A market order resolves synchronously here: `dealStatus ==
    "ACCEPTED"` means the position opened and the order is fully
    `FILLED` at the confirmed `level`; `"REJECTED"` means it never
    filled at all. There is no partial-fill or pending state to
    represent for a market order confirmed this way.

    Raises:
        BrokerConnectionError: IG reported a `dealStatus` we don't
            recognize. We'd rather fail loudly than guess wrong about
            whether the order filled.
    """
    deal_status = data["dealStatus"]
    size = float(data.get("size") or 0.0)

    if deal_status == "REJECTED":
        status = OrderStatus.REJECTED
        filled_quantity = 0.0
        filled_avg_price = None
    elif deal_status == "ACCEPTED":
        status = OrderStatus.FILLED
        filled_quantity = size
        level = data.get("level")
        filled_avg_price = float(level) if level is not None else None
    else:
        raise BrokerConnectionError(f"IG returned an unrecognized deal status: {deal_status!r}")

    return BrokerOrder(
        broker_order_id=data.get("dealId") or data["dealReference"],
        symbol=data["epic"],
        side=OrderSide.BUY if data["direction"] == "BUY" else OrderSide.SELL,
        quantity=size,
        status=status,
        filled_quantity=filled_quantity,
        filled_avg_price=filled_avg_price,
    )
