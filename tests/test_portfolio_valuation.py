"""Tests for `src.analytics.valuation` -- Sprint 9 (`DECISIONS.md`,
ADR-0042).

Two concerns kept deliberately separate from `tests/test_analytics.py`:
read-only mark-to-market valuation of a `Portfolio` snapshot
(`PortfolioValuationService`), and the one sanctioned market-data
touchpoint in this package (`latest_price()`, via a fake
`MarketDataService`-shaped object -- never a real network call, Sprint
9 spec section 33).
"""

from __future__ import annotations

from uuid import uuid4

import pandas as pd
import pytest

from src.analytics.models import MetricStatus
from src.analytics.valuation import PortfolioValuationService, default_price_lookup, latest_price
from src.data.exceptions import DataProviderError, NoDataError
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionSide

ENTRY_TIME = pd.Timestamp("2024-01-01", tz="UTC")


def make_position(symbol, side, quantity, entry_price):
    return Position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        entry_timestamp=ENTRY_TIME,
        entry_signal_id=uuid4(),
    )


# ---------------------------------------------------------------------------
# PortfolioValuationService.value()
# ---------------------------------------------------------------------------


def test_value_with_no_positions_equals_cash():
    portfolio = Portfolio(cash=10_000.0)
    snapshot = PortfolioValuationService().value(portfolio, price_lookup=lambda s: 100.0)

    assert snapshot.cash == 10_000.0
    assert snapshot.market_value.status is MetricStatus.OK
    assert snapshot.market_value.value == 0.0
    assert snapshot.unrealized_pnl.value == 0.0
    assert snapshot.equity.value == 10_000.0
    assert snapshot.exposure.value == 0.0
    assert snapshot.realized_pnl == 0.0
    assert snapshot.positions == []
    assert snapshot.warnings == []


def test_value_long_position_hand_calculated():
    portfolio = Portfolio.reconstruct(
        cash=50_000.0,
        positions=[make_position("SPY", PositionSide.LONG, 10, 100.0)],
    )
    snapshot = PortfolioValuationService().value(portfolio, price_lookup=lambda s: 110.0)

    assert snapshot.market_value.value == pytest.approx(1_100.0)
    assert snapshot.unrealized_pnl.value == pytest.approx(100.0)
    assert snapshot.exposure.value == pytest.approx(1_100.0)
    assert snapshot.equity.value == pytest.approx(51_100.0)
    assert snapshot.total_pnl.value == pytest.approx(100.0)
    assert len(snapshot.positions) == 1
    position_valuation = snapshot.positions[0]
    assert position_valuation.symbol == "SPY"
    assert position_valuation.price_available is True
    assert position_valuation.market_value == pytest.approx(1_100.0)


def test_value_short_position_hand_calculated():
    portfolio = Portfolio.reconstruct(
        cash=50_000.0,
        positions=[make_position("SPY", PositionSide.SHORT, -5, 100.0)],
    )
    snapshot = PortfolioValuationService().value(portfolio, price_lookup=lambda s: 90.0)

    # Price dropped 10% -- a short position profits.
    assert snapshot.market_value.value == pytest.approx(-450.0)
    assert snapshot.unrealized_pnl.value == pytest.approx(50.0)
    assert snapshot.exposure.value == pytest.approx(450.0)
    assert snapshot.equity.value == pytest.approx(50_000.0 - 450.0)


def test_value_multiple_positions_cash_and_positions_mixed():
    portfolio = Portfolio.reconstruct(
        cash=20_000.0,
        positions=[
            make_position("AAPL", PositionSide.LONG, 10, 50.0),
            make_position("MSFT", PositionSide.SHORT, -4, 200.0),
        ],
    )
    prices = {"AAPL": 55.0, "MSFT": 190.0}
    snapshot = PortfolioValuationService().value(portfolio, price_lookup=lambda s: prices[s])

    # AAPL: market_value=550, unrealized=+50. MSFT: market_value=-760, unrealized=+40.
    assert snapshot.market_value.value == pytest.approx(550.0 - 760.0)
    assert snapshot.unrealized_pnl.value == pytest.approx(50.0 + 40.0)
    assert snapshot.exposure.value == pytest.approx(550.0 + 760.0)
    assert snapshot.equity.value == pytest.approx(20_000.0 + (550.0 - 760.0))
    assert {p.symbol for p in snapshot.positions} == {"AAPL", "MSFT"}


def test_value_missing_price_makes_aggregates_undefined_but_shows_full_breakdown():
    portfolio = Portfolio.reconstruct(
        cash=10_000.0,
        positions=[
            make_position("AAPL", PositionSide.LONG, 10, 50.0),
            make_position("GHOST", PositionSide.LONG, 5, 20.0),
        ],
    )

    def price_lookup(symbol: str) -> float | None:
        return 55.0 if symbol == "AAPL" else None

    snapshot = PortfolioValuationService().value(portfolio, price_lookup=price_lookup)

    assert snapshot.market_value.status is MetricStatus.UNDEFINED
    assert snapshot.unrealized_pnl.status is MetricStatus.UNDEFINED
    assert snapshot.equity.status is MetricStatus.UNDEFINED
    assert snapshot.exposure.status is MetricStatus.UNDEFINED
    assert snapshot.total_pnl.status is MetricStatus.UNDEFINED
    assert len(snapshot.warnings) == 1
    assert "GHOST" in snapshot.warnings[0]

    by_symbol = {p.symbol: p for p in snapshot.positions}
    assert by_symbol["AAPL"].price_available is True
    assert by_symbol["AAPL"].market_value == pytest.approx(550.0)
    assert by_symbol["GHOST"].price_available is False
    assert by_symbol["GHOST"].market_value is None
    assert by_symbol["GHOST"].warning is not None

    # cash/realized_pnl never depend on a market price -- always known.
    assert snapshot.cash == 10_000.0
    assert snapshot.realized_pnl == 0.0


def test_value_realized_pnl_comes_from_closed_positions_regardless_of_pricing():
    closed_winner = make_position("AAPL", PositionSide.LONG, 10, 50.0).close(60.0)
    closed_loser = make_position("MSFT", PositionSide.LONG, 5, 200.0).close(190.0)
    portfolio = Portfolio.reconstruct(
        cash=10_000.0, positions=[], closed_positions=[closed_winner, closed_loser]
    )

    snapshot = PortfolioValuationService().value(portfolio, price_lookup=lambda s: None)

    # realized_pnl: (60-50)*10=100, (190-200)*5=-50 -> net 50.
    assert snapshot.realized_pnl == pytest.approx(50.0)
    # No open positions -- nothing depends on price_lookup at all, so
    # the aggregates stay fully defined even though price_lookup
    # returns None for everything.
    assert snapshot.market_value.status is MetricStatus.OK
    assert snapshot.equity.status is MetricStatus.OK
    assert snapshot.equity.value == pytest.approx(10_000.0)
    assert snapshot.total_pnl.value == pytest.approx(50.0)


def test_value_never_mutates_the_portfolio():
    position = make_position("SPY", PositionSide.LONG, 10, 100.0)
    portfolio = Portfolio.reconstruct(cash=50_000.0, positions=[position])

    service = PortfolioValuationService()
    first = service.value(portfolio, price_lookup=lambda s: 110.0)
    second = service.value(portfolio, price_lookup=lambda s: 110.0)

    # Calling value() (even twice) never sets current_price or touches
    # cash -- Portfolio's own state is exactly what it was before.
    assert portfolio.cash == 50_000.0
    assert portfolio.positions["SPY"].current_price is None
    assert portfolio.positions["SPY"].quantity == 10
    assert len(portfolio.closed_positions) == 0

    # And the two snapshots agree -- valuation is a pure read.
    assert first.equity.value == pytest.approx(second.equity.value)
    assert first.market_value.value == pytest.approx(second.market_value.value)


# ---------------------------------------------------------------------------
# latest_price() -- the one sanctioned MarketDataService touchpoint
# ---------------------------------------------------------------------------


class _FakeMarketDataService:
    """Stands in for `MarketDataService` without any network access --
    `latest_price()` only calls `.get_history(symbol, period, interval)`."""

    def __init__(self, candles: pd.DataFrame | None = None, error: Exception | None = None):
        self._candles = candles
        self._error = error

    def get_history(self, symbol: str, period: str = "5d", interval: str = "1d") -> pd.DataFrame:
        if self._error is not None:
            raise self._error
        return self._candles


def test_latest_price_returns_the_most_recent_close():
    candles = pd.DataFrame({"close": [100.0, 105.0, 110.0]})
    service = _FakeMarketDataService(candles=candles)
    assert latest_price("SPY", service) == 110.0


def test_latest_price_returns_none_on_no_data_error():
    service = _FakeMarketDataService(error=NoDataError("no data"))
    assert latest_price("SPY", service) is None


def test_latest_price_returns_none_on_data_provider_error():
    service = _FakeMarketDataService(error=DataProviderError("boom"))
    assert latest_price("SPY", service) is None


def test_latest_price_returns_none_for_empty_candles():
    service = _FakeMarketDataService(candles=pd.DataFrame({"close": []}))
    assert latest_price("SPY", service) is None


def test_default_price_lookup_constructs_a_real_market_data_service(monkeypatch):
    # src.dashboard is not allowed to construct MarketDataService itself
    # (tests/test_architecture.py) -- default_price_lookup() is the one
    # place that does, on the dashboard's behalf.
    import src.analytics.valuation as valuation_module

    class _StubService:
        def get_history(self, symbol, period="5d", interval="1d"):
            return pd.DataFrame({"close": [42.0]})

    monkeypatch.setattr(valuation_module, "MarketDataService", _StubService)
    price_lookup = default_price_lookup()
    assert price_lookup("SPY") == 42.0
