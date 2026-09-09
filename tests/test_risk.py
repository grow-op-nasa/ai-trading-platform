"""Tests for position sizing (src/risk).

`PositionSizer.size()` is tested against `Signal`/`AccountState`
objects built directly -- no strategy or backtest involved, since
sizing is deliberately standalone this round (`DECISIONS.md`, ADR-0021).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.risk.engine import PositionSizer
from src.risk.models import AccountState, RiskLimits, SizingDecision
from src.signals.models import Signal, SignalDirection


def make_signal(direction: SignalDirection = SignalDirection.LONG) -> Signal:
    return Signal(timestamp=pd.Timestamp("2024-01-01"), direction=direction, confidence=0.8)


# ---------------------------------------------------------------------------
# RiskLimits validation
# ---------------------------------------------------------------------------


def test_risk_limits_defaults():
    limits = RiskLimits()
    assert limits.risk_per_trade_pct == 0.10
    assert limits.max_portfolio_exposure_pct == 0.50


def test_risk_limits_rejects_out_of_range_risk_per_trade_pct():
    for bad_value in (0, -0.1, 1.5):
        with pytest.raises(ValueError):
            RiskLimits(risk_per_trade_pct=bad_value)


def test_risk_limits_rejects_out_of_range_max_portfolio_exposure_pct():
    for bad_value in (0, -0.1, 1.5):
        with pytest.raises(ValueError):
            RiskLimits(max_portfolio_exposure_pct=bad_value)


def test_risk_limits_accepts_boundary_value_one():
    RiskLimits(risk_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0)  # should not raise


# ---------------------------------------------------------------------------
# AccountState validation
# ---------------------------------------------------------------------------


def test_account_state_rejects_non_positive_equity():
    with pytest.raises(ValueError):
        AccountState(equity=0)
    with pytest.raises(ValueError):
        AccountState(equity=-100)


def test_account_state_rejects_negative_open_exposure():
    with pytest.raises(ValueError):
        AccountState(equity=10_000, open_exposure=-1)


def test_account_state_defaults_open_exposure_to_zero():
    account = AccountState(equity=10_000)
    assert account.open_exposure == 0.0


# ---------------------------------------------------------------------------
# PositionSizer -- input validation
# ---------------------------------------------------------------------------


def test_size_rejects_flat_signal():
    sizer = PositionSizer()
    account = AccountState(equity=10_000)
    with pytest.raises(ValueError):
        sizer.size(make_signal(SignalDirection.FLAT), account, price=100.0)


def test_size_rejects_non_positive_price():
    sizer = PositionSizer()
    account = AccountState(equity=10_000)
    for bad_price in (0, -50.0):
        with pytest.raises(ValueError):
            sizer.size(make_signal(), account, price=bad_price)


# ---------------------------------------------------------------------------
# PositionSizer -- full-size case
# ---------------------------------------------------------------------------


def test_full_size_when_no_open_exposure():
    limits = RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50)
    sizer = PositionSizer(limits)
    account = AccountState(equity=100_000, open_exposure=0.0)

    decision = sizer.size(make_signal(), account, price=100.0)

    assert decision.approved is True
    assert decision.capital_allocated == pytest.approx(10_000.0)  # 10% of 100k
    assert decision.position_size == pytest.approx(100.0)  # 10,000 / 100
    assert "full per-trade risk" in decision.reason


def test_long_and_short_are_sized_identically():
    limits = RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50)
    account = AccountState(equity=100_000, open_exposure=0.0)

    long_decision = PositionSizer(limits).size(
        make_signal(SignalDirection.LONG), account, price=100.0
    )
    short_decision = PositionSizer(limits).size(
        make_signal(SignalDirection.SHORT), account, price=100.0
    )

    assert long_decision.capital_allocated == short_decision.capital_allocated
    assert long_decision.position_size == short_decision.position_size


# ---------------------------------------------------------------------------
# PositionSizer -- sized-down case
# ---------------------------------------------------------------------------


def test_sized_down_when_desired_capital_exceeds_remaining_headroom():
    # Equity 100k, max exposure 50% = 50k allowed. 45k already
    # committed -> only 5k of headroom left, less than the desired 10k.
    limits = RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50)
    sizer = PositionSizer(limits)
    account = AccountState(equity=100_000, open_exposure=45_000)

    decision = sizer.size(make_signal(), account, price=100.0)

    assert decision.approved is True
    assert decision.capital_allocated == pytest.approx(5_000.0)
    assert decision.position_size == pytest.approx(50.0)
    assert "sized down" in decision.reason


# ---------------------------------------------------------------------------
# PositionSizer -- rejected case
# ---------------------------------------------------------------------------


def test_rejected_when_exposure_limit_already_reached():
    limits = RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50)
    sizer = PositionSizer(limits)
    account = AccountState(equity=100_000, open_exposure=50_000)  # exactly at the cap

    decision = sizer.size(make_signal(), account, price=100.0)

    assert decision.approved is False
    assert decision.position_size == 0.0
    assert decision.capital_allocated == 0.0
    assert "exposure limit reached" in decision.reason


def test_rejected_when_exposure_already_over_the_limit():
    limits = RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50)
    sizer = PositionSizer(limits)
    account = AccountState(equity=100_000, open_exposure=60_000)  # somehow over the cap

    decision = sizer.size(make_signal(), account, price=100.0)

    assert decision.approved is False
    assert decision.position_size == 0.0


# ---------------------------------------------------------------------------
# PositionSizer -- defaults
# ---------------------------------------------------------------------------


def test_position_sizer_uses_default_limits_when_none_given():
    sizer = PositionSizer()
    account = AccountState(equity=100_000)

    decision = sizer.size(make_signal(), account, price=100.0)

    assert decision.capital_allocated == pytest.approx(10_000.0)  # default 10%


def test_sizing_decision_is_a_plain_dataclass_instance():
    sizer = PositionSizer()
    account = AccountState(equity=100_000)
    decision = sizer.size(make_signal(), account, price=100.0)
    assert isinstance(decision, SizingDecision)
