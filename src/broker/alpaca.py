"""Alpaca implementation of BrokerConnection.

This is one interchangeable implementation of the `BrokerConnection`
interface (`base.py`). If we later add Interactive Brokers or another
venue, we write a new class here (or in a sibling module) implementing
the same `get_account()` signature -- nothing else in the codebase
should need to change.

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
"""

from __future__ import annotations

import os
from typing import Protocol

import requests

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.risk.models import AccountState

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL = "https://api.alpaca.markets"

_REQUEST_TIMEOUT_SECONDS = 10


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
        try:
            response = self._session.get(
                f"{self._base_url}/v2/account",
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            raise BrokerConnectionError(f"failed to reach Alpaca: {exc}") from exc

        if response.status_code in (401, 403):
            raise BrokerAuthenticationError(
                f"Alpaca rejected the provided credentials (HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise BrokerConnectionError(
                f"Alpaca returned HTTP {response.status_code}: {response.text}"
            )

        return _parse_account(response.json())

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
