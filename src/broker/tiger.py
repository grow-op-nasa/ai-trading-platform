"""Tiger Brokers (Tiger Trade) implementation of BrokerConnection.

The platform's fourth concrete broker (`DECISIONS.md`, ADR-0030), and a
real, usable account for this platform's user (real US/HK/SG equities,
closer to Alpaca's asset class than IG's CFDs).

Tiger's auth is a fourth distinct shape in this codebase: every request
must be cryptographically signed with an RSA private key (PKCS#1), not
just a header, session token, or established browser session. Rather
than hand-roll that signing scheme against raw HTTP -- real risk of a
subtly wrong signature that only surfaces against a real account --
`TigerBroker` wraps Tiger's own official `tigeropen` Python SDK, which
already implements it correctly. This is the first broker in this
codebase built on a vendor SDK instead of talking `requests` directly;
the injectable seam here is the SDK's `TradeClient` object (`_TradeClient`
Protocol, below), not an HTTP session the way `alpaca.py`/`ibkr.py`/
`ig.py` all use.

`tigeropen` is imported lazily, only inside `_build_client()`, and is
deliberately **not** added to `requirements.txt` -- the same "optional
heavy dependency" pattern `src/research`'s `ClaudeNarrativeRenderer`
already uses for `anthropic` (`DECISIONS.md`, ADR-0020). Importing this
module, or constructing a `TigerBroker` with an injected fake `client`
(as every test in `tests/test_tiger.py` does), never requires
`tigeropen` to be installed.

Scope this round is **connectivity and account state only**, the same
posture every broker in this codebase has started with:
`submit_order`/`get_order`/`cancel_order` raise `NotImplementedError`,
even though Tiger's own API appears to support a genuine order-status
and cancellation lifecycle (`get_order`/`get_orders`/`cancel_order`)
more cleanly than IG's does -- proving the SDK-wrapper approach and
account-state mapping first is this round's job, not building order
management on top of an unproven foundation.
"""

from __future__ import annotations

import os
from typing import Protocol

from src.broker.base import BrokerConnection
from src.broker.exceptions import BrokerAuthenticationError, BrokerConnectionError
from src.broker.models import BrokerOrder, OrderRequest
from src.portfolio.models import AccountState

_NOT_IMPLEMENTED_MESSAGE = (
    "TigerBroker does not yet support {method}() -- this round is scoped "
    "to connectivity and account state only, proving the tigeropen SDK "
    "wrapper works before building order management on top of it. See "
    "DECISIONS.md, ADR-0030."
)


class _TradeClient(Protocol):
    """The minimal shape this module needs from tigeropen's
    `TradeClient` -- lets tests inject a fake without depending on
    `tigeropen` or a real network call.
    """

    def get_assets(self, segment: bool, market_value: bool) -> list: ...


class TigerBroker(BrokerConnection):
    """Connects to Tiger Brokers via the official `tigeropen` SDK.

    Args:
        tiger_id: Tiger developer/account id. Falls back to `TIGER_ID`.
        private_key_path: path to the RSA private key (PKCS#1) used to
            sign requests. Falls back to `TIGER_PRIVATE_KEY_PATH`.
        account: which Tiger account to trade against -- a real paper
            account or a real live account; unlike Alpaca/IG, there's
            no separate URL for paper vs. live, the same "the account
            itself decides" shape `IBKRBroker` already has. Falls back
            to `TIGER_ACCOUNT`.
        sandbox_debug: use Tiger's sandbox environment instead of a real
            account. Defaults to `False` -- Tiger's own documentation
            recommends testing against a real paper account over the
            sandbox.
        client: the underlying SDK client to use. Defaults to
            constructing a real `tigeropen.trade.trade_client.TradeClient`
            (see the module docstring on the lazy import); tests inject
            a fake satisfying `_TradeClient`.

    Raises:
        BrokerAuthenticationError: no tiger_id, private_key_path, or
            account was found in either the arguments or the
            environment.
    """

    def __init__(
        self,
        tiger_id: str | None = None,
        private_key_path: str | None = None,
        account: str | None = None,
        sandbox_debug: bool = False,
        client: _TradeClient | None = None,
    ) -> None:
        self._tiger_id = tiger_id or os.environ.get("TIGER_ID")
        self._private_key_path = private_key_path or os.environ.get("TIGER_PRIVATE_KEY_PATH")
        self._account = account or os.environ.get("TIGER_ACCOUNT")
        if not self._tiger_id or not self._private_key_path or not self._account:
            raise BrokerAuthenticationError(
                "TIGER_ID, TIGER_PRIVATE_KEY_PATH, and TIGER_ACCOUNT must all "
                "be set, as arguments or environment variables"
            )
        self._sandbox_debug = sandbox_debug
        self._client = client or self._build_client()

    def get_account(self) -> AccountState:
        try:
            portfolios = self._client.get_assets(segment=False, market_value=True)
        except Exception as exc:
            # tigeropen's own exception taxonomy hasn't been verified in
            # depth this round (DECISIONS.md, ADR-0030) -- everything is
            # mapped to BrokerConnectionError rather than guessing at a
            # finer-grained BrokerAuthenticationError distinction.
            raise BrokerConnectionError(f"failed to reach Tiger: {exc}") from exc

        if not portfolios:
            raise BrokerConnectionError("Tiger reported no portfolio accounts for this session")

        return _parse_account(portfolios[0])

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="submit_order"))

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="get_order"))

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED_MESSAGE.format(method="cancel_order"))

    def _build_client(self) -> _TradeClient:
        """Lazily imports `tigeropen` and constructs a real
        `TradeClient` -- only reached when no fake `client` was
        injected, so neither this module nor its tests require
        `tigeropen` to be installed.
        """
        from tigeropen.tiger_open_config import get_client_config
        from tigeropen.trade.trade_client import TradeClient

        client_config = get_client_config(
            private_key_path=self._private_key_path,
            tiger_id=self._tiger_id,
            account=self._account,
        )
        client_config.sandbox_debug = self._sandbox_debug
        return TradeClient(client_config)


def _parse_account(portfolio) -> AccountState:
    """Convert one `tigeropen` `PortfolioAccount` into an `AccountState`.

    `equity` maps to `summary.net_liquidation`. `open_exposure` maps to
    `summary.gross_position_value` -- the sum of absolute market value
    of all non-cash positions, exactly the meaning `AlpacaBroker`
    already gives `open_exposure`. This is the most direct account
    mapping of any broker integrated so far: Tiger exposes gross
    position value as its own field, rather than needing an
    approximation the way IG's margin-based `deposit` does.
    """
    summary = portfolio.summary
    equity = float(summary.net_liquidation)
    open_exposure = abs(float(summary.gross_position_value or 0.0))
    return AccountState(equity=equity, open_exposure=open_exposure)
