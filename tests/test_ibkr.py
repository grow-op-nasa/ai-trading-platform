"""Tests for the Interactive Brokers connection (src/broker/ibkr.py).

`IBKRBroker` is tested against an injected fake HTTP session, exactly
like `AlpacaBroker` (`tests/test_broker.py`) -- no real Client Portal
Gateway, no real IBKR account. This is also the concrete proof behind
ADR-0026's claim: a second `BrokerConnection` implementation really can
be swapped in, satisfying the same interface `AlpacaBroker` does.
"""

from __future__ import annotations

import requests
import pytest

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.broker.ibkr import IBKR_GATEWAY_BASE_URL, IBKRBroker
from src.broker.models import OrderRequest, OrderSide
from src.portfolio.models import AccountState


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or str(json_data)

    def json(self) -> dict:
        return self._json_data


class FakeSession:
    """Returns queued responses in order (one per call), or raises a
    canned exception on every call -- standing in for `requests`.
    """

    def __init__(self, responses: list[FakeResponse] | None = None, raises: Exception | None = None):
        self.calls: list[tuple[str, str, dict, float]] = []
        self._responses = list(responses or [])
        self._raises = raises

    def get(self, url: str, headers: dict, timeout: float) -> FakeResponse:
        self.calls.append(("get", url, headers, timeout))
        if self._raises is not None:
            raise self._raises
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Interface conformance -- the actual point of ADR-0026
# ---------------------------------------------------------------------------


def test_ibkr_broker_satisfies_broker_connection_interface():
    broker = IBKRBroker(session=FakeSession())
    assert isinstance(broker, BrokerConnection)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_to_local_gateway_base_url():
    broker = IBKRBroker(session=FakeSession())
    assert broker._base_url == IBKR_GATEWAY_BASE_URL


def test_accepts_custom_base_url_override():
    broker = IBKRBroker(base_url="https://gateway.example.com/v1/api", session=FakeSession())
    assert broker._base_url == "https://gateway.example.com/v1/api"


def test_requires_no_credentials_to_construct():
    IBKRBroker(session=FakeSession())  # should not raise -- no api key/secret concept


# ---------------------------------------------------------------------------
# get_account -- success path
# ---------------------------------------------------------------------------


def test_get_account_resolves_selected_account_then_parses_summary():
    session = FakeSession(
        responses=[
            FakeResponse(200, {"accounts": ["U111", "U222"], "selectedAccount": "U111"}),
            FakeResponse(
                200,
                {
                    "netliquidation": {"amount": 100_000.0, "currency": "USD"},
                    "grosspositionvalue": {"amount": 15_000.0, "currency": "USD"},
                },
            ),
        ]
    )
    broker = IBKRBroker(session=session)

    account = broker.get_account()

    assert isinstance(account, AccountState)
    assert account.equity == pytest.approx(100_000.0)
    assert account.open_exposure == pytest.approx(15_000.0)

    accounts_call, summary_call = session.calls
    assert accounts_call[1].endswith("/iserver/accounts")
    assert summary_call[1].endswith("/portfolio/U111/summary")


def test_get_account_falls_back_to_first_account_when_none_selected():
    session = FakeSession(
        responses=[
            FakeResponse(200, {"accounts": ["U999"]}),
            FakeResponse(200, {"netliquidation": {"amount": 5_000.0}}),
        ]
    )
    broker = IBKRBroker(session=session)

    broker.get_account()

    _, summary_call = session.calls
    assert summary_call[1].endswith("/portfolio/U999/summary")


def test_get_account_defaults_missing_gross_position_value_to_zero():
    session = FakeSession(
        responses=[
            FakeResponse(200, {"accounts": ["U111"]}),
            FakeResponse(200, {"netliquidation": {"amount": 20_000.0}}),
        ]
    )
    broker = IBKRBroker(session=session)

    account = broker.get_account()

    assert account.equity == pytest.approx(20_000.0)
    assert account.open_exposure == 0.0


# ---------------------------------------------------------------------------
# get_account -- error paths
# ---------------------------------------------------------------------------


def test_get_account_raises_connection_error_when_no_accounts():
    session = FakeSession(responses=[FakeResponse(200, {"accounts": []})])
    broker = IBKRBroker(session=session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_get_account_raises_authentication_error_on_401():
    session = FakeSession(responses=[FakeResponse(401, text="not authenticated")])
    broker = IBKRBroker(session=session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_account()


def test_get_account_raises_authentication_error_on_403():
    session = FakeSession(responses=[FakeResponse(403, text="forbidden")])
    broker = IBKRBroker(session=session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_account()


def test_get_account_raises_connection_error_on_other_http_failure():
    session = FakeSession(responses=[FakeResponse(500, text="server error")])
    broker = IBKRBroker(session=session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_get_account_raises_connection_error_on_network_failure():
    session = FakeSession(raises=requests.exceptions.ConnectionError("no route to host"))
    broker = IBKRBroker(session=session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


# ---------------------------------------------------------------------------
# Order management -- deliberately not implemented this round
# ---------------------------------------------------------------------------


def test_submit_order_raises_not_implemented():
    broker = IBKRBroker(session=FakeSession())
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)

    with pytest.raises(NotImplementedError):
        broker.submit_order(request)


def test_get_order_raises_not_implemented():
    broker = IBKRBroker(session=FakeSession())

    with pytest.raises(NotImplementedError):
        broker.get_order("order-123")


def test_cancel_order_raises_not_implemented():
    broker = IBKRBroker(session=FakeSession())

    with pytest.raises(NotImplementedError):
        broker.cancel_order("order-123")
