"""Tests for the platform-level `Position` model and the `Portfolio`
aggregate (Sprint 7, `DECISIONS.md` ADR-0039).

`AccountState`'s own tests are untouched (`tests/test_portfolio.py`) --
this file covers only what Sprint 7 adds: `Position`'s own validation
and computed properties, and `Portfolio`'s lifecycle, exposure, and
multi-position behavior.
"""

from __future__ import annotations

from uuid import uuid4

import pandas as pd
import pytest

from src.portfolio import AccountState, Portfolio, Position, PositionLifecycle, PositionSide

ENTRY_TS = pd.Timestamp("2024-01-01 09:30:00")


def make_position(
    symbol: str = "SPY",
    side: PositionSide = PositionSide.LONG,
    quantity: float = 10.0,
    entry_price: float = 100.0,
    **kwargs,
) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        entry_timestamp=ENTRY_TS,
        entry_signal_id=uuid4(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Position validation
# ---------------------------------------------------------------------------


def test_position_rejects_zero_quantity():
    with pytest.raises(ValueError):
        make_position(quantity=0.0)


def test_position_rejects_long_with_negative_quantity():
    with pytest.raises(ValueError):
        make_position(side=PositionSide.LONG, quantity=-5.0)


def test_position_rejects_short_with_positive_quantity():
    with pytest.raises(ValueError):
        make_position(side=PositionSide.SHORT, quantity=5.0)


def test_position_rejects_non_positive_entry_price():
    with pytest.raises(ValueError):
        make_position(entry_price=0.0)


def test_position_defaults_to_open_with_no_stop_or_realized_pnl():
    position = make_position()
    assert position.lifecycle is PositionLifecycle.OPEN
    assert position.stop_price is None
    assert position.realized_pnl is None
    assert position.current_price is None


# ---------------------------------------------------------------------------
# Position computed properties
# ---------------------------------------------------------------------------


def test_valuation_price_falls_back_to_entry_price_with_no_mark_to_market():
    position = make_position(entry_price=150.0)
    assert position.valuation_price == 150.0
    assert position.unrealized_pnl is None  # no current_price supplied -- never fabricated


def test_valuation_price_uses_current_price_when_supplied():
    position = make_position(entry_price=150.0, current_price=160.0)
    assert position.valuation_price == 160.0
    assert position.unrealized_pnl == pytest.approx(100.0)  # (160-150)*10


def test_exposure_is_absolute_regardless_of_side():
    long_position = make_position(side=PositionSide.LONG, quantity=10.0, entry_price=100.0)
    short_position = make_position(side=PositionSide.SHORT, quantity=-10.0, entry_price=100.0)
    assert long_position.exposure == 1_000.0
    assert short_position.exposure == 1_000.0


def test_close_computes_realized_pnl_for_long_and_short():
    long_position = make_position(side=PositionSide.LONG, quantity=10.0, entry_price=100.0)
    closed_long = long_position.close(exit_price=110.0)
    assert closed_long.lifecycle is PositionLifecycle.CLOSED
    assert closed_long.realized_pnl == pytest.approx(100.0)

    short_position = make_position(side=PositionSide.SHORT, quantity=-10.0, entry_price=100.0)
    closed_short = short_position.close(exit_price=90.0)
    assert closed_short.realized_pnl == pytest.approx(100.0)  # price dropped, short profits


def test_close_does_not_mutate_the_original_position():
    position = make_position()
    closed = position.close(exit_price=110.0)
    assert position.lifecycle is PositionLifecycle.OPEN  # original untouched
    assert closed.lifecycle is PositionLifecycle.CLOSED


def test_closing_an_already_closed_position_raises():
    position = make_position()
    closed = position.close(exit_price=110.0)
    with pytest.raises(ValueError):
        closed.close(exit_price=120.0)


# ---------------------------------------------------------------------------
# Portfolio construction and basic properties
# ---------------------------------------------------------------------------


def test_portfolio_rejects_negative_cash():
    with pytest.raises(ValueError):
        Portfolio(cash=-1.0)


def test_portfolio_allows_zero_cash():
    portfolio = Portfolio(cash=0.0)
    assert portfolio.cash == 0.0
    assert portfolio.equity == 0.0


def test_new_portfolio_has_no_positions():
    portfolio = Portfolio(cash=100_000.0)
    assert portfolio.positions == {}
    assert portfolio.total_exposure == 0.0
    assert portfolio.position_count == 0
    assert portfolio.has_open_position("SPY") is False


# ---------------------------------------------------------------------------
# Opening a position
# ---------------------------------------------------------------------------


def test_opening_a_long_position_debits_cash_and_leaves_equity_unchanged():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.LONG, quantity=10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    assert portfolio.cash == pytest.approx(99_000.0)
    assert portfolio.equity == pytest.approx(100_000.0)
    assert portfolio.total_exposure == pytest.approx(1_000.0)
    assert portfolio.has_open_position("SPY") is True


def test_opening_a_short_position_credits_cash_and_leaves_equity_unchanged():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.SHORT, quantity=-10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    assert portfolio.cash == pytest.approx(101_000.0)
    assert portfolio.equity == pytest.approx(100_000.0)
    assert portfolio.total_exposure == pytest.approx(1_000.0)


def test_opening_when_already_held_raises():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.LONG, quantity=10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    with pytest.raises(ValueError):
        portfolio.open_position(
            symbol="SPY", side=PositionSide.LONG, quantity=5.0, entry_price=105.0,
            entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
        )


def test_positions_property_returns_a_defensive_copy():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.LONG, quantity=10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    positions = portfolio.positions
    del positions["SPY"]
    assert "SPY" in portfolio.positions  # internal state untouched


# ---------------------------------------------------------------------------
# 18. Multi-position support (SPY/QQQ/GLD simultaneously)
# ---------------------------------------------------------------------------


def test_multiple_concurrent_positions_are_all_representable_simultaneously():
    portfolio = Portfolio(cash=1_000_000.0)
    for symbol, price in (("SPY", 400.0), ("QQQ", 350.0), ("GLD", 180.0)):
        portfolio.open_position(
            symbol=symbol, side=PositionSide.LONG, quantity=10.0, entry_price=price,
            entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
        )

    assert portfolio.position_count == 3
    assert set(portfolio.positions) == {"SPY", "QQQ", "GLD"}
    assert portfolio.symbol_exposure("SPY") == pytest.approx(4_000.0)
    assert portfolio.symbol_exposure("QQQ") == pytest.approx(3_500.0)
    assert portfolio.symbol_exposure("GLD") == pytest.approx(1_800.0)
    assert portfolio.total_exposure == pytest.approx(4_000.0 + 3_500.0 + 1_800.0)


def test_symbol_exposure_for_an_unheld_symbol_is_zero_not_none():
    portfolio = Portfolio(cash=100_000.0)
    assert portfolio.symbol_exposure("NONEXISTENT") == 0.0


# ---------------------------------------------------------------------------
# 19. Closing a position
# ---------------------------------------------------------------------------


def test_closing_a_position_raises_when_none_is_open():
    portfolio = Portfolio(cash=100_000.0)
    with pytest.raises(ValueError):
        portfolio.close_position("SPY", exit_price=100.0)


def test_closing_a_long_position_realizes_profit_and_releases_exposure():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.LONG, quantity=10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    closed = portfolio.close_position("SPY", exit_price=110.0)

    assert closed.lifecycle is PositionLifecycle.CLOSED
    assert closed.realized_pnl == pytest.approx(100.0)
    assert portfolio.cash == pytest.approx(100_100.0)  # paid 1,000 to open, received 1,100 to close
    assert "SPY" not in portfolio.positions
    assert portfolio.has_open_position("SPY") is False
    assert portfolio.total_exposure == 0.0
    assert portfolio.symbol_exposure("SPY") == 0.0
    assert closed in portfolio.closed_positions


def test_closing_a_short_position_realizes_profit_and_releases_exposure():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.SHORT, quantity=-10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    closed = portfolio.close_position("SPY", exit_price=90.0)

    assert closed.realized_pnl == pytest.approx(100.0)
    assert portfolio.cash == pytest.approx(100_100.0)
    assert "SPY" not in portfolio.positions
    assert portfolio.total_exposure == 0.0


def test_closing_one_of_several_positions_leaves_the_others_untouched():
    portfolio = Portfolio(cash=1_000_000.0)
    for symbol, price in (("SPY", 400.0), ("QQQ", 350.0), ("GLD", 180.0)):
        portfolio.open_position(
            symbol=symbol, side=PositionSide.LONG, quantity=10.0, entry_price=price,
            entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
        )

    portfolio.close_position("QQQ", exit_price=360.0)

    assert portfolio.position_count == 2
    assert set(portfolio.positions) == {"SPY", "GLD"}
    assert portfolio.symbol_exposure("QQQ") == 0.0
    # Closed QQQ no longer contributes to total exposure -- only SPY+GLD remain.
    assert portfolio.total_exposure == pytest.approx(4_000.0 + 1_800.0)


def test_closed_positions_history_accumulates_in_order():
    portfolio = Portfolio(cash=1_000_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.LONG, quantity=10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    portfolio.open_position(
        symbol="QQQ", side=PositionSide.LONG, quantity=10.0, entry_price=200.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    portfolio.close_position("SPY", exit_price=110.0)
    portfolio.close_position("QQQ", exit_price=190.0)

    closed = portfolio.closed_positions
    assert [p.symbol for p in closed] == ["SPY", "QQQ"]
    assert all(p.lifecycle is PositionLifecycle.CLOSED for p in closed)


def test_closed_positions_property_returns_a_defensive_copy():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.LONG, quantity=10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )
    portfolio.close_position("SPY", exit_price=110.0)

    history = portfolio.closed_positions
    history.clear()
    assert len(portfolio.closed_positions) == 1  # internal state untouched


# ---------------------------------------------------------------------------
# Bridge to the legacy AccountState shape
# ---------------------------------------------------------------------------


def test_to_account_state_reflects_equity_and_total_exposure():
    portfolio = Portfolio(cash=100_000.0)
    portfolio.open_position(
        symbol="SPY", side=PositionSide.LONG, quantity=10.0, entry_price=100.0,
        entry_timestamp=ENTRY_TS, entry_signal_id=uuid4(),
    )

    account = portfolio.to_account_state()

    assert isinstance(account, AccountState)
    assert account.equity == pytest.approx(portfolio.equity)
    assert account.open_exposure == pytest.approx(portfolio.total_exposure)
