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
from src.broker.models import BrokerOrder, OrderRequest, OrderSide, OrderStatus
from src.risk.models import AccountState


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or str(json_data)

    def json(self) -> dict:
        return self._json_data


class FakeSession:
    """Records the calls it received and returns a canned response (or
    raises a canned exception), standing in for `requests`."""

    def __init__(self, response: FakeResponse | None = None, raises: Exception | None = None):
        self.calls: list[tuple[str, str, dict, float]] = []
        self._response = response
        self._raises = raises

    def get(self, url: str, headers: dict, timeout: float) -> FakeResponse:
        self.calls.append(("get", url, headers, timeout))
        if self._raises is not None:
            raise self._raises
        return self._response

    def post(self, url: str, headers: dict, json: dict, timeout: float) -> FakeResponse:
        self.calls.append(("post", url, headers, timeout))
        self.last_json_body = json
        if self._raises is not None:
            raise self._raises
        return self._response

    def delete(self, url: str, headers: dict, timeout: float) -> FakeResponse:
        self.calls.append(("delete", url, headers, timeout))
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

    method, url, headers, _ = session.calls[0]
    assert method == "get"
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


# ---------------------------------------------------------------------------
# OrderRequest validation
# ---------------------------------------------------------------------------


def test_order_request_rejects_zero_quantity():
    with pytest.raises(ValueError):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=0)


def test_order_request_rejects_negative_quantity():
    with pytest.raises(ValueError):
        OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=-5)


def test_order_request_accepts_positive_quantity():
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)
    assert request.quantity == 10


# ---------------------------------------------------------------------------
# submit_order -- success path
# ---------------------------------------------------------------------------


def _alpaca_order_response(**overrides) -> dict:
    data = {
        "id": "order-123",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "10",
        "status": "new",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
    data.update(overrides)
    return data


def test_submit_order_sends_expected_request_body():
    session = FakeSession(response=FakeResponse(200, _alpaca_order_response()))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)

    broker.submit_order(request)

    method, url, _, _ = session.calls[0]
    assert method == "post"
    assert url.endswith("/v2/orders")
    assert session.last_json_body == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }


def test_submit_order_sell_side_maps_to_lowercase_sell():
    session = FakeSession(response=FakeResponse(200, _alpaca_order_response(side="sell")))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)
    request = OrderRequest(symbol="AAPL", side=OrderSide.SELL, quantity=10)

    broker.submit_order(request)

    assert session.last_json_body["side"] == "sell"


def test_submit_order_returns_parsed_broker_order():
    session = FakeSession(response=FakeResponse(200, _alpaca_order_response()))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)

    order = broker.submit_order(request)

    assert isinstance(order, BrokerOrder)
    assert order.broker_order_id == "order-123"
    assert order.symbol == "AAPL"
    assert order.side is OrderSide.BUY
    assert order.quantity == pytest.approx(10.0)
    assert order.status is OrderStatus.PENDING
    assert order.filled_quantity == pytest.approx(0.0)
    assert order.filled_avg_price is None


# ---------------------------------------------------------------------------
# get_order -- success path
# ---------------------------------------------------------------------------


def test_get_order_requests_expected_url():
    session = FakeSession(response=FakeResponse(200, _alpaca_order_response()))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    broker.get_order("order-123")

    method, url, _, _ = session.calls[0]
    assert method == "get"
    assert url.endswith("/v2/orders/order-123")


def test_get_order_returns_parsed_broker_order_with_fill_info():
    session = FakeSession(
        response=FakeResponse(
            200,
            _alpaca_order_response(
                status="filled", filled_qty="10", filled_avg_price="150.25"
            ),
        )
    )
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    order = broker.get_order("order-123")

    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == pytest.approx(10.0)
    assert order.filled_avg_price == pytest.approx(150.25)


# ---------------------------------------------------------------------------
# Order status mapping
# ---------------------------------------------------------------------------


def test_status_mapping_covers_representative_alpaca_statuses():
    cases = {
        "new": OrderStatus.PENDING,
        "accepted": OrderStatus.PENDING,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "rejected": OrderStatus.REJECTED,
        "canceled": OrderStatus.CANCELED,
        "expired": OrderStatus.CANCELED,
    }
    for raw_status, expected in cases.items():
        session = FakeSession(response=FakeResponse(200, _alpaca_order_response(status=raw_status)))
        broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

        order = broker.get_order("order-123")

        assert order.status is expected, f"expected {raw_status!r} -> {expected}"


def test_get_order_raises_connection_error_on_unrecognized_status():
    session = FakeSession(response=FakeResponse(200, _alpaca_order_response(status="mystery")))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerConnectionError):
        broker.get_order("order-123")


# ---------------------------------------------------------------------------
# submit_order / get_order -- error paths (mirroring get_account's)
# ---------------------------------------------------------------------------


def test_submit_order_raises_authentication_error_on_401():
    session = FakeSession(response=FakeResponse(401, text="unauthorized"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerAuthenticationError):
        broker.submit_order(request)


def test_submit_order_raises_connection_error_on_other_http_failure():
    session = FakeSession(response=FakeResponse(422, text="unprocessable"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerConnectionError):
        broker.submit_order(request)


def test_submit_order_raises_connection_error_on_network_failure():
    session = FakeSession(raises=requests.exceptions.ConnectionError("no route to host"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerConnectionError):
        broker.submit_order(request)


def test_get_order_raises_authentication_error_on_403():
    session = FakeSession(response=FakeResponse(403, text="forbidden"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_order("order-123")


def test_get_order_raises_connection_error_on_network_failure():
    session = FakeSession(raises=requests.exceptions.ConnectionError("no route to host"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerConnectionError):
        broker.get_order("order-123")


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


def test_cancel_order_sends_delete_to_expected_url():
    session = FakeSession(response=FakeResponse(204))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    broker.cancel_order("order-123")

    method, url, _, _ = session.calls[0]
    assert method == "delete"
    assert url.endswith("/v2/orders/order-123")


def test_cancel_order_returns_none_on_success():
    session = FakeSession(response=FakeResponse(204))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    result = broker.cancel_order("order-123")

    assert result is None


def test_cancel_order_raises_authentication_error_on_401():
    session = FakeSession(response=FakeResponse(401, text="unauthorized"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerAuthenticationError):
        broker.cancel_order("order-123")


def test_cancel_order_raises_connection_error_on_other_http_failure():
    session = FakeSession(response=FakeResponse(422, text="order is no longer cancelable"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerConnectionError):
        broker.cancel_order("order-123")


def test_cancel_order_raises_connection_error_on_network_failure():
    session = FakeSession(raises=requests.exceptions.ConnectionError("no route to host"))
    broker = AlpacaBroker(api_key="key", api_secret="secret", session=session)

    with pytest.raises(BrokerConnectionError):
        broker.cancel_order("order-123")
