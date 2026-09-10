"""Tests for broker connectivity (src/broker).

`AlpacaBroker` is tested against an injected fake HTTP session, so
these tests run instantly and never hit the network -- the same
"unit-test the interface/parsing, leave the live vendor to a future
integration suite" posture `tests/test_market_data.py` takes toward
`YFinanceProvider`.
"""

from __future__ import annotations

import requests
import pytest

from src.broker.alpaca import ALPACA_LIVE_BASE_URL, ALPACA_PAPER_BASE_URL, AlpacaBroker
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.risk.models import AccountState


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or str(json_data)

    def json(self) -> dict:
        return self._json_data


class FakeSession:
    """Records the call it received and returns a canned response (or
    raises a canned exception), standing in for `requests`."""

    def __init__(self, response: FakeResponse | None = None, raises: Exception | None = None):
        self.calls: list[tuple[str, dict, float]] = []
        self._response = response
        self._raises = raises

    def get(self, url: str, headers: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, headers, timeout))
        if self._raises is not None:
            raise self._raises
        return self._response


# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------


def test_raises_when_no_credentials_given_and_none_in_environment(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(BrokerAuthenticationError):
        AlpacaBroker()


def test_accepts_credentials_passed_as_arguments(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    AlpacaBroker(api_key="key", api_secret="secret")  # should not raise


def test_falls_back_to_environment_variables(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "env-secret")
    AlpacaBroker()  # should not raise


def test_raises_when_only_one_of_key_or_secret_is_present(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(BrokerAuthenticationError):
        AlpacaBroker()


# ---------------------------------------------------------------------------
# Defaults -- paper trading by default
# ---------------------------------------------------------------------------


def test_defaults_to_paper_trading_endpoint():
    broker = AlpacaBroker(api_key="key", api_secret="secret")
    assert broker._base_url == ALPACA_PAPER_BASE_URL


def test_live_endpoint_requires_explicit_override():
    broker = AlpacaBroker(api_key="key", api_secret="secret", base_url=ALPACA_LIVE_BASE_URL)
    assert broker._base_url == ALPACA_LIVE_BASE_URL


# ---------------------------------------------------------------------------
# get_account -- success path
# ---------------------------------------------------------------------------


def test_get_account_parses_equity_and_exposure():
    session = FakeSession(
        response=FakeResponse(
            200,
            {"equity": "100000.00", "long_market_value": "15000.00", "short_market_value": "0"},
        )
    )
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    account = broker.get_account()

    assert isinstance(account, AccountState)
    assert account.equity == pytest.approx(100_000.0)
    assert account.open_exposure == pytest.approx(15_000.0)


def test_get_account_sums_long_and_short_exposure():
    session = FakeSession(
        response=FakeResponse(
            200,
            {"equity": "50000.00", "long_market_value": "10000.00", "short_market_value": "-5000.00"},
        )
    )
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    account = broker.get_account()

    assert account.open_exposure == pytest.approx(15_000.0)  # abs(10000) + abs(-5000)


def test_get_account_defaults_missing_exposure_fields_to_zero():
    session = FakeSession(response=FakeResponse(200, {"equity": "20000.00"}))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    account = broker.get_account()

    assert account.equity == pytest.approx(20_000.0)
    assert account.open_exposure == 0.0


def test_get_account_sends_credentials_as_headers():
    session = FakeSession(response=FakeResponse(200, {"equity": "1000.00"}))
    broker = AlpacaBroker(api_key="my-key", api_secret="my-secret", session=session)

    broker.get_account()

    url, headers, _ = session.calls[0]
    assert url.endswith("/v2/account")
    assert headers["APCA-API-KEY-ID"] == "my-key"
    assert headers["APCA-API-SECRET-KEY"] == "my-secret"


# ---------------------------------------------------------------------------
# get_account -- error paths
# ---------------------------------------------------------------------------


def test_get_account_raises_authentication_error_on_401():
    session = FakeSession(response=FakeResponse(401, text="unauthorized"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_account()


def test_get_account_raises_authentication_error_on_403():
    session = FakeSession(response=FakeResponse(403, text="forbidden"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_account()


def test_get_account_raises_connection_error_on_other_http_failure():
    session = FakeSession(response=FakeResponse(500, text="server error"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_get_account_raises_connection_error_on_network_failure():
    session = FakeSession(raises=requests.exceptions.ConnectionError("no route to host"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()
