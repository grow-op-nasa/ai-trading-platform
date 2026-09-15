"""Tests for the Tiger Brokers (Tiger Trade) connection (src/broker/tiger.py).

`TigerBroker` is tested against an injected fake `_TradeClient` object
(a stand-in for `tigeropen`'s real `TradeClient`), never a fake HTTP
session -- the injectable seam here is the SDK client itself
(`DECISIONS.md`, ADR-0030). No test in this module ever imports or
requires `tigeropen` to be installed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.broker.models import OrderRequest, OrderSide
from src.broker.tiger import TigerBroker
from src.portfolio.models import AccountState


def make_portfolio(net_liquidation: float, gross_position_value: float | None) -> SimpleNamespace:
    summary = SimpleNamespace(
        net_liquidation=net_liquidation,
        gross_position_value=gross_position_value,
    )
    return SimpleNamespace(summary=summary)


class FakeTradeClient:
    """Returns a queued portfolio list, or raises a canned exception --
    standing in for `tigeropen.trade.trade_client.TradeClient`.
    """

    def __init__(self, portfolios: list | None = None, raises: Exception | None = None):
        self.calls: list[tuple[bool, bool]] = []
        self._portfolios = portfolios if portfolios is not None else []
        self._raises = raises

    def get_assets(self, segment: bool, market_value: bool) -> list:
        self.calls.append((segment, market_value))
        if self._raises is not None:
            raise self._raises
        return self._portfolios


# ---------------------------------------------------------------------------
# Interface conformance -- the actual point of ADR-0030
# ---------------------------------------------------------------------------


def test_tiger_broker_satisfies_broker_connection_interface():
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=FakeTradeClient()
    )
    assert isinstance(broker, BrokerConnection)


# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------


def test_requires_tiger_id():
    with pytest.raises(BrokerAuthenticationError):
        TigerBroker(
            tiger_id=None, private_key_path="/tmp/key.pem", account="acct-1", client=FakeTradeClient()
        )


def test_requires_private_key_path():
    with pytest.raises(BrokerAuthenticationError):
        TigerBroker(tiger_id="t-1", private_key_path=None, account="acct-1", client=FakeTradeClient())


def test_requires_account():
    with pytest.raises(BrokerAuthenticationError):
        TigerBroker(
            tiger_id="t-1", private_key_path="/tmp/key.pem", account=None, client=FakeTradeClient()
        )


def test_falls_back_to_environment_variables(monkeypatch):
    monkeypatch.setenv("TIGER_ID", "env-tiger-id")
    monkeypatch.setenv("TIGER_PRIVATE_KEY_PATH", "/env/key.pem")
    monkeypatch.setenv("TIGER_ACCOUNT", "env-account")

    broker = TigerBroker(client=FakeTradeClient())

    assert broker._tiger_id == "env-tiger-id"
    assert broker._private_key_path == "/env/key.pem"
    assert broker._account == "env-account"


def test_arguments_take_precedence_over_environment_variables(monkeypatch):
    monkeypatch.setenv("TIGER_ID", "env-tiger-id")
    monkeypatch.setenv("TIGER_PRIVATE_KEY_PATH", "/env/key.pem")
    monkeypatch.setenv("TIGER_ACCOUNT", "env-account")

    broker = TigerBroker(
        tiger_id="arg-tiger-id",
        private_key_path="/arg/key.pem",
        account="arg-account",
        client=FakeTradeClient(),
    )

    assert broker._tiger_id == "arg-tiger-id"
    assert broker._private_key_path == "/arg/key.pem"
    assert broker._account == "arg-account"


def test_defaults_sandbox_debug_to_false():
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=FakeTradeClient()
    )
    assert broker._sandbox_debug is False


# ---------------------------------------------------------------------------
# get_account -- success path
# ---------------------------------------------------------------------------


def test_get_account_parses_net_liquidation_and_gross_position_value():
    client = FakeTradeClient(portfolios=[make_portfolio(100_000.0, 15_000.0)])
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=client
    )

    account = broker.get_account()

    assert isinstance(account, AccountState)
    assert account.equity == pytest.approx(100_000.0)
    assert account.open_exposure == pytest.approx(15_000.0)
    assert client.calls == [(False, True)]


def test_get_account_defaults_missing_gross_position_value_to_zero():
    client = FakeTradeClient(portfolios=[make_portfolio(50_000.0, None)])
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=client
    )

    account = broker.get_account()

    assert account.equity == pytest.approx(50_000.0)
    assert account.open_exposure == 0.0


def test_get_account_uses_first_portfolio_when_multiple_present():
    client = FakeTradeClient(
        portfolios=[make_portfolio(10_000.0, 1_000.0), make_portfolio(999.0, 999.0)]
    )
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=client
    )

    account = broker.get_account()

    assert account.equity == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# get_account -- error paths
# ---------------------------------------------------------------------------


def test_get_account_raises_connection_error_when_no_portfolios():
    client = FakeTradeClient(portfolios=[])
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=client
    )

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


def test_get_account_wraps_any_client_exception_in_connection_error():
    client = FakeTradeClient(raises=RuntimeError("signature verification failed"))
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=client
    )

    with pytest.raises(BrokerConnectionError):
        broker.get_account()


# ---------------------------------------------------------------------------
# Order management -- deliberately not implemented this round
# ---------------------------------------------------------------------------


def test_submit_order_raises_not_implemented():
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=FakeTradeClient()
    )
    request = OrderRequest(symbol="AAPL", side=OrderSide.BUY, quantity=10)

    with pytest.raises(NotImplementedError):
        broker.submit_order(request)


def test_get_order_raises_not_implemented():
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=FakeTradeClient()
    )

    with pytest.raises(NotImplementedError):
        broker.get_order("order-123")


def test_cancel_order_raises_not_implemented():
    broker = TigerBroker(
        tiger_id="t-1", private_key_path="/tmp/key.pem", account="acct-1", client=FakeTradeClient()
    )

    with pytest.raises(NotImplementedError):
        broker.cancel_order("order-123")
