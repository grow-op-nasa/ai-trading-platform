"""Integration test: a real strategy's signals through `PositionSizer`
and `PaperBroker`, driven by real candles.

Sprint 4's remaining `ROADMAP.md` item -- proving the full pipeline
(`EMACrossStrategy` -> `PositionSizer` -> `PaperBroker`) composes
correctly end to end, rather than each piece only being verified
against the others in isolation, as in `tests/test_risk.py` and
`tests/test_execution.py`. Adds no new production code -- `src/risk`
and `src/execution` remain standalone modules (`DECISIONS.md`,
ADR-0021/ADR-0022); this test is the proof that wiring them together,
the way a future caller eventually will, actually works.

Deliberately does not hand-predict exact EMA crossover values or
signal counts -- the candle shape below is already proven (in
`tests/test_ema_cross_strategy.py`) to produce at least one LONG
followed later by a FLAT with `fast=2, slow=4`; every assertion here is
derived from whatever `Fill`s that run actually produces, not from a
hardcoded expectation of the underlying EMA math.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.execution.engine import PaperBroker
from src.risk.engine import PositionSizer
from src.risk.models import RiskLimits
from src.signals.models import Signal, SignalDirection
from src.strategies.ema_cross import EMACrossStrategy

SYMBOL = "SPY"

# Known-good crossover shape: dips, spikes sharply, then declines --
# guaranteed to produce at least one LONG followed later by a FLAT with
# fast=2, slow=4 (see tests/test_ema_cross_strategy.py).
CLOSES = [110, 108, 106, 104, 102, 100, 105, 110, 115, 120, 110, 100, 90, 80]


def make_candles(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


def run_signals_through_pipeline(signals, candles, sizer, broker, symbol=SYMBOL):
    """Feed each signal through `PositionSizer` (for `LONG`/`SHORT`) and
    `PaperBroker.submit_signal()`, exactly as a future live/paper
    trading loop would. Returns `(signal, decision_or_none, fill_or_none)`
    tuples, in order, for the caller to assert against.
    """
    steps = []
    for signal in signals:
        price = float(candles.loc[signal.timestamp, "close"])
        if signal.direction is SignalDirection.FLAT:
            fill = broker.submit_signal(signal, symbol, price)
            steps.append((signal, None, fill))
        else:
            decision = sizer.size(signal, broker.account_state, price)
            if not decision.approved:
                steps.append((signal, decision, None))
                continue
            fill = broker.submit_signal(signal, symbol, price, sizing_decision=decision)
            steps.append((signal, decision, fill))
    return steps


def test_full_pipeline_runs_without_error():
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(fast=2, slow=4)
    signals = strategy.generate_signals(strategy.prepare(candles))

    assert any(s.direction is SignalDirection.LONG for s in signals)
    assert any(s.direction is SignalDirection.FLAT for s in signals)

    sizer = PositionSizer(RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50))
    broker = PaperBroker(starting_cash=100_000)

    steps = run_signals_through_pipeline(signals, candles, sizer, broker)

    assert len(steps) == len(signals)
    for signal, decision, fill in steps:
        assert fill is not None  # nothing gets rejected in this single-symbol, single-position flow
        if signal.direction is not SignalDirection.FLAT:
            assert decision.approved is True


def test_first_open_is_sized_at_ten_percent_of_starting_equity():
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(fast=2, slow=4)
    signals = strategy.generate_signals(strategy.prepare(candles))

    sizer = PositionSizer(RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50))
    broker = PaperBroker(starting_cash=100_000)
    steps = run_signals_through_pipeline(signals, candles, sizer, broker)

    _, decision, fill = next(s for s in steps if s[0].direction is SignalDirection.LONG)
    # The first LONG happens before any prior trade, so equity is still
    # exactly starting_cash.
    assert decision.capital_allocated == pytest.approx(100_000 * 0.10)
    assert fill.order.quantity == pytest.approx(decision.position_size)


def test_closing_realizes_pnl_matching_the_actual_fill_prices():
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(fast=2, slow=4)
    signals = strategy.generate_signals(strategy.prepare(candles))

    sizer = PositionSizer(RiskLimits())
    broker = PaperBroker(starting_cash=100_000)
    steps = run_signals_through_pipeline(signals, candles, sizer, broker)

    # Signals strictly alternate (no two consecutive share a direction,
    # proven at the strategy level), so the first FLAT is guaranteed to
    # be the close for the first LONG.
    _, long_decision, long_fill = next(s for s in steps if s[0].direction is SignalDirection.LONG)
    _, _, flat_fill = next(s for s in steps if s[0].direction is SignalDirection.FLAT)

    expected_pnl = long_decision.position_size * (flat_fill.fill_price - long_fill.fill_price)
    # Computed from just these two fills' own cash_delta, independent of
    # whatever happens later in the series -- valid regardless of how
    # many further signals this run produces.
    cash_after_round_trip = 100_000 + long_fill.cash_delta + flat_fill.cash_delta

    assert cash_after_round_trip == pytest.approx(100_000 + expected_pnl)


def test_account_state_after_a_round_trip_resizes_the_next_signal_off_updated_equity():
    # Proves the loop actually closes: a signal sized AFTER a completed
    # round trip should be sized off the UPDATED equity (reflecting
    # realized P&L from that round trip), not the original starting_cash.
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(fast=2, slow=4)
    signals = strategy.generate_signals(strategy.prepare(candles))

    limits = RiskLimits(risk_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50)
    sizer = PositionSizer(limits)
    broker = PaperBroker(starting_cash=100_000)
    run_signals_through_pipeline(signals, candles, sizer, broker)

    equity_after_round_trip = broker.account_state.equity
    assert equity_after_round_trip != 100_000  # the round trip had a nonzero price move

    next_signal = Signal(
        timestamp=pd.Timestamp("2024-02-01"), direction=SignalDirection.LONG, confidence=0.9
    )
    decision = sizer.size(next_signal, broker.account_state, price=50.0)

    assert decision.capital_allocated == pytest.approx(equity_after_round_trip * 0.10)
    assert decision.capital_allocated != pytest.approx(100_000 * 0.10)


def test_signal_ids_are_traceable_through_the_whole_pipeline():
    candles = make_candles(CLOSES)
    strategy = EMACrossStrategy(fast=2, slow=4)
    signals = strategy.generate_signals(strategy.prepare(candles))

    sizer = PositionSizer(RiskLimits())
    broker = PaperBroker(starting_cash=100_000)
    steps = run_signals_through_pipeline(signals, candles, sizer, broker)

    for signal, _, fill in steps:
        assert fill.order.signal_id == signal.id
        assert fill.order.timestamp == signal.timestamp
