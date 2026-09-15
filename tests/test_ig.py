"""Tests for the IG connection (src/broker/ig.py).

`IGBroker` is tested against an injected fake HTTP session, exactly
like `AlpacaBroker`/`IBKRBroker` -- no real IG account, no real
network. IG's session-based auth (login once, cache tokens) means most
tests here queue two responses: the `POST /session` login, then the
actual endpoint call.
"""

from __future__ import annotations

import requests
import pytest

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.broker.ig import IG_DEMO_BASE_URL, IG_LIVE_BASE_URL, IGBroker
from src.broker.models import OrderRequest, OrderSide, OrderStatus
from src.portfolio.models import AccountState


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        json_data: dict | None = None,
        text: str = "",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or str(json_data)
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json_data


def login_response(cst: str = "cst-token", security_token: str = "sec-token") -> FakeResponse:
    return FakeResponse(200, headers={"CST": cst, "X-SECURITY-TOKEN": security_token})


class FakeSession:
    """Returns queued responses in order (one per call, regardless of
    method), or raises a canned exception on every call.
    """

    def __init__(self, responses: list[FakeResponse] | None = None, raises: Exception | None = None):
        self.calls: list[tuple[str, str, dict, float]] = []
        self._responses = list(responses or [])
        self._raises = raises

    def get(self, url: str, headers: dict, timeout: float) -> FakeResponse:
        return self._call("get", url, headers, timeout)

    def post(self, url: str, headers: dict, json: dict, timeout: float) -> FakeResponse:
        return self._call("post", url, headers, timeout)

    def _call(self, method: str, url: str, headers: dict, timeout: float) -> FakeResponse:
        self.calls.append((method, url, headers, timeout))
        if self._raises is not None:
            raise self._raises
        return self._responses.pop(0)


def make_broker(session: FakeSession, **kwargs) -> IGBroker:
    kwargs.setdefault("api_key", "key")
    kwargs.setdefault("username", "user")
    kwargs.setdefault("password", "pass")
    return IGBroker(session=session, **kwargs)


# ---------------------------------------------------------------------------
# Interface conformance
# ---------------------------------------------------------------------------


def test_ig_broker_satisfies_broker_connection_interface():
    broker = make_broker(FakeSession())
    assert isinstance(broker, BrokerConnection)


# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------


def test_raises_when_no_credentials_given_and_none_in_environment(monkeypatch):
    monkeypatch.delenv("IG_API_KEY", raising=False)
    monkeypatch.delenv("IG_USERNAME", raising=False)
    monkeypatch.delenv("IG_PASSWORD", raising=False)
    with pytest.raises(BrokerAuthenticationError):
        IGBroker(session=FakeSession())


def test_accepts_credentials_passed_as_arguments(monkeypatch):
    monkeypatch.delenv("IG_API_KEY", raising=False)
    monkeypatch.delenv("IG_USERNAME", raising=False)
    monkeypatch.delenv("IG_PASSWORD", raising=False)
    IGBroker(api_key="key", username="user", password="pass", session=FakeSession())


def test_falls_back_to_environment_variables(monkeypatch):
    monkeypatch.setenv("IG_API_KEY", "env-key")
    monkeypatch.setenv("IG_USERNAME", "env-user")
    monkeypatch.setenv("IG_PASSWORD", "env-pass")
    IGBroker(session=FakeSession())


def test_raises_when_only_some_credentials_are_present(monkeypatch):
    monkeypatch.setenv("IG_API_KEY", "env-key")
    monkeypatch.delenv("IG_USERNAME", raising=False)
    monkeypatch.delenv("IG_PASSWORD", raising=False)
    with pytest.raises(BrokerAuthenticationError):
        IGBroker(session=FakeSession())


# ---------------------------------------------------------------------------
# Defaults -- demo by default
# ---------------------------------------------------------------------------


def test_defaults_to_demo_endpoint():
    broker = make_broker(FakeSession())
    assert broker._base_url == IG_DEMO_BASE_URL


def test_live_endpoint_requires_explicit_override():
    broker = make_broker(FakeSession(), base_url=IG_LIVE_BASE_URL)
    assert broker._base_url == IG_LIVE_BASE_URL


# ---------------------------------------------------------------------------
# get_account -- success path
# ---------------------------------------------------------------------------


def test_get_account_logs_in_then_parses_preferred_account():
    session = FakeSession(
        responses=[
            login_response(),
            FakeResponse(
                200,
                {
                    "accounts": [
                        {"accountId": "A1", "preferred": False, "balance": {"balance": 1000.0, "deposit": 0.0}},
                        {"accountId": "A2", "preferred": True, "balance": {"balance": 50_000.0, "deposit": 5_000.0}},
                    ]
                },
            ),
        ]
    )
    broker = make_broker(session)

    account = broker.get_account()

    assert isinstance(account, AccountState)
    assert account.equity == pytest.approx(50_000.0)
    assert account.open_exposure == pytest.approx(5_000.0)

    login_call, accounts_call = session.calls
    assert login_call[0] == "post"
    assert login_call[1].endswith("/session")
    assert accounts_call[0] == "get"
    assert accounts_call[1].endswith("/accounts")


def test_get_account_uses_explicit_account_id_override():
    session = FakeSession(
        responses=[
            login_response(),
            FakeResponse(
                200,
                {
                    "accounts": [
                        {"accountId": "A1", "preferred": True, "balance": {"balance": 1000.0}},
                        {"accountId": "A2", "preferred": False, "balance": {"balance": 50_000.0}},
                    ]
                },
            ),
        ]
    )
    broker = make_broker(session, account_id="A2")

    account = broker.get_account()

    assert account.equity == pytest.approx(50_000.0)


def test_get_account_falls_back_to_first_account_when_none_preferred():
    session = FakeSession(
        responses=[
            login_response(),
            FakeResponse(
                200,
                {"accounts": [{"accountId": "A1", "balance": {"balance": 2_500.0}}]},
            ),
        ]
    )
    broker = make_broker(session)

    account = broker.get_account()

    assert account.equity == pytest.approx(2_500.0)


def test_get_account_defaults_missing_deposit_to_zero():
    session = FakeSession(
        responses=[
            login_response(),
            FakeResponse(200, {"accounts": [{"accountId": "A1", "balance": {"balance": 2_500.0}}]}),
        ]
    )
    broker = make_broker(session)

    account = broker.get_account()

    assert account.open_exposure == 0.0


def test_login_is_cached_across_multiple_calls():
    session = FakeSession(
        responses=[
            login_response(),
            FakeResponse(200, {"accounts": [{"accountId": "A1", "balance": {"balance": 1.0}}]}),
            FakeResponse(200, {"accounts": [{"accountId": "A1", "balance": {"balance": 1.0}}]}),
        ]
    )
    broker = make_broker(session)

    broker.get_account()
    broker.get_account()

    login_calls = [call for call in session.calls if call[1].endswith("/session")]
    assert len(login_calls) == 1


# ---------------------------------------------------------------------------
# get_account -- error paths
# ---------------------------------------------------------------------------


def test_get_account_raises_connection_error_when_no_accounts():
    session = FakeSession(responses=[login_response(), FakeResponse(200, {"accounts": []})])
    broker = make_broker(session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_get_account_raises_connection_error_for_unknown_account_id():
    session = FakeSession(
        responses=[
            login_response(),
            FakeResponse(200, {"accounts": [{"accountId": "A1", "balance": {"balance": 1.0}}]}),
        ]
    )
    broker = make_broker(session, account_id="does-not-exist")

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_login_raises_authentication_error_on_401():
    session = FakeSession(responses=[FakeResponse(401, text="unauthorized")])
    broker = make_broker(session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_account()


def test_login_raises_authentication_error_on_403():
    session = FakeSession(responses=[FakeResponse(403, text="forbidden")])
    broker = make_broker(session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_account()


def test_login_raises_connection_error_on_other_http_failure():
    session = FakeSession(responses=[FakeResponse(500, text="server error")])
    broker = make_broker(session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_login_raises_connection_error_when_tokens_missing_from_response():
    session = FakeSession(responses=[FakeResponse(200, headers={})])
    broker = make_broker(session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_get_account_raises_connection_error_on_network_failure():
    session = FakeSession(raises=requests.exceptions.ConnectionError("no route to host"))
    broker = make_broker(session)

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_get_account_raises_authentication_error_when_session_expires_mid_use():
    session = FakeSession(responses=[login_response(), FakeResponse(401, text="session expired")])
    broker = make_broker(session)

    with pytest.raises(BrokerAuthenticationError):
        broker.get_account()


# ---------------------------------------------------------------------------
# submit_order -- success path (resolves synchronously)
# ---------------------------------------------------------------------------


def _accounts_response(currency: str = "USD") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "accounts": [
                {
                    "accountId": "A1",
                    "preferred": True,
                    "currency": currency,
                    "balance": {"balance": 50_000.0, "deposit": 0.0},
                }
            ]
        },
    )


def _confirmation_response(
    deal_status: str = "ACCEPTED",
    status: str = "OPEN",
    direction: str = "BUY",
    size: float = 10.0,
    level: float | None = 150.0,
    deal_id: str = "deal-123",
) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "dealId": deal_id,
            "dealReference": "ref-123",
            "dealStatus": deal_status,
            "status": status,
            "epic": "CS.D.EURUSD.MINI.IP",
            "direction": direction,
            "size": size,
            "level": level,
        },
    )


def test_submit_order_sends_expected_body_with_account_currency():
    session = FakeSession(
        responses=[
            login_response(),
            _accounts_response(currency="GBP"),
            FakeResponse(200, {"dealReference": "ref-123"}),
            _confirmation_response(),
        ]
    )
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    broker.submit_order(request)

    login_call, accounts_call, submit_call, confirm_call = session.calls
    assert submit_call[0] == "post"
    assert submit_call[1].endswith("/positions/otc")
    assert confirm_call[0] == "get"
    assert confirm_call[1].endswith("/confirms/ref-123")


def test_submit_order_returns_filled_broker_order_on_accepted():
    session = FakeSession(
        responses=[
            login_response(),
            _accounts_response(),
            FakeResponse(200, {"dealReference": "ref-123"}),
            _confirmation_response(deal_status="ACCEPTED", size=10.0, level=150.0),
        ]
    )
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    order = broker.submit_order(request)

    assert order.broker_order_id == "deal-123"
    assert order.symbol == "CS.D.EURUSD.MINI.IP"
    assert order.side is OrderSide.BUY
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == pytest.approx(10.0)
    assert order.filled_avg_price == pytest.approx(150.0)


def test_submit_order_returns_rejected_broker_order_on_rejected():
    session = FakeSession(
        responses=[
            login_response(),
            _accounts_response(),
            FakeResponse(200, {"dealReference": "ref-123"}),
            _confirmation_response(deal_status="REJECTED", level=None),
        ]
    )
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    order = broker.submit_order(request)

    assert order.status is OrderStatus.REJECTED
    assert order.filled_quantity == pytest.approx(0.0)
    assert order.filled_avg_price is None


def test_submit_order_maps_sell_side_correctly():
    session = FakeSession(
        responses=[
            login_response(),
            _accounts_response(),
            FakeResponse(200, {"dealReference": "ref-123"}),
            _confirmation_response(direction="SELL"),
        ]
    )
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.SELL, quantity=10)

    order = broker.submit_order(request)

    assert order.side is OrderSide.SELL


def test_submit_order_raises_connection_error_on_unrecognized_deal_status():
    session = FakeSession(
        responses=[
            login_response(),
            _accounts_response(),
            FakeResponse(200, {"dealReference": "ref-123"}),
            _confirmation_response(deal_status="MYSTERY"),
        ]
    )
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerConnectionError):
        broker.submit_order(request)


# ---------------------------------------------------------------------------
# submit_order -- error paths
# ---------------------------------------------------------------------------


def test_submit_order_raises_connection_error_when_no_accounts_for_currency():
    session = FakeSession(responses=[login_response(), FakeResponse(200, {"accounts": []})])
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerConnectionError):
        broker.submit_order(request)


def test_submit_order_raises_authentication_error_on_401():
    session = FakeSession(
        responses=[login_response(), _accounts_response(), FakeResponse(401, text="unauthorized")]
    )
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerAuthenticationError):
        broker.submit_order(request)


def test_submit_order_raises_connection_error_on_other_http_failure():
    session = FakeSession(
        responses=[login_response(), _accounts_response(), FakeResponse(500, text="server error")]
    )
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerConnectionError):
        broker.submit_order(request)


def test_submit_order_raises_connection_error_on_network_failure():
    session = FakeSession(raises=requests.exceptions.ConnectionError("no route to host"))
    broker = make_broker(session)
    request = OrderRequest(symbol="CS.D.EURUSD.MINI.IP", side=OrderSide.BUY, quantity=10)

    with pytest.raises(BrokerConnectionError):
        broker.submit_order(request)


# ---------------------------------------------------------------------------
# get_order / cancel_order -- deliberately not implemented
# ---------------------------------------------------------------------------


def test_get_order_raises_not_implemented():
    broker = make_broker(FakeSession())

    with pytest.raises(NotImplementedError):
        broker.get_order("deal-123")


def test_cancel_order_raises_not_implemented():
    broker = make_broker(FakeSession())

    with pytest.raises(NotImplementedError):
        broker.cancel_order("deal-123")
