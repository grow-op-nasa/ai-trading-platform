"""Tests for the Signal Framework (src/signals)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.signals.models import Signal, SignalDirection


def make_signal(**overrides) -> Signal:
    defaults = dict(
        timestamp=pd.Timestamp("2024-01-01"),
        symbol="SPY",
        direction=SignalDirection.LONG,
        confidence=0.8,
        metadata={"reason": "test"},
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_signal_direction_has_exactly_long_short_flat():
    assert {d.value for d in SignalDirection} == {"LONG", "SHORT", "FLAT"}


def test_signal_accepts_confidence_at_boundaries():
    make_signal(confidence=0.0)
    make_signal(confidence=1.0)


def test_signal_rejects_confidence_above_one():
    with pytest.raises(ValueError):
        make_signal(confidence=1.01)


def test_signal_rejects_confidence_below_zero():
    with pytest.raises(ValueError):
        make_signal(confidence=-0.01)


def test_signal_has_no_price_field():
    # A Signal is a decision, not a market event or an order -- it
    # deliberately carries no price (DECISIONS.md, ADR-0015).
    signal = make_signal()
    assert not hasattr(signal, "price")


def test_signal_id_is_auto_generated_and_unique():
    first = make_signal()
    second = make_signal()
    assert first.id != second.id


def test_signal_id_can_be_reconstructed_explicitly():
    original = make_signal()
    reconstructed = make_signal(id=original.id)
    assert reconstructed.id == original.id


def test_signal_is_immutable():
    signal = make_signal()
    with pytest.raises(Exception):
        signal.confidence = 0.5  # type: ignore[misc]


def test_signal_metadata_defaults_to_empty_dict():
    signal = Signal(
        timestamp=pd.Timestamp("2024-01-01"),
        symbol="SPY",
        direction=SignalDirection.FLAT,
        confidence=0.5,
    )
    assert signal.metadata == {}


def test_two_signals_with_identical_fields_still_have_different_ids():
    ts = pd.Timestamp("2024-01-01")
    first = Signal(timestamp=ts, symbol="SPY", direction=SignalDirection.LONG, confidence=0.9)
    second = Signal(timestamp=ts, symbol="SPY", direction=SignalDirection.LONG, confidence=0.9)
    assert first.id != second.id


# ---------------------------------------------------------------------------
# symbol -- first-class field (DECISIONS.md, ADR-0033)
# ---------------------------------------------------------------------------


def test_signal_carries_the_symbol_it_was_created_for():
    signal = make_signal(symbol="QQQ")
    assert signal.symbol == "QQQ"


def test_signal_symbol_is_not_stored_in_metadata():
    # symbol must be its own typed field, not a metadata workaround.
    signal = make_signal(symbol="GLD", metadata={"reason": "test"})
    assert "symbol" not in signal.metadata
    assert signal.symbol == "GLD"


def test_two_signals_for_different_symbols_are_distinguishable_without_context():
    # The whole point of ADR-0033: a Signal read back in isolation (no
    # surrounding backtest/strategy context) still says which
    # instrument it's about.
    ts = pd.Timestamp("2024-01-01")
    spy_signal = Signal(timestamp=ts, symbol="SPY", direction=SignalDirection.LONG, confidence=0.9)
    qqq_signal = Signal(timestamp=ts, symbol="QQQ", direction=SignalDirection.LONG, confidence=0.9)
    assert spy_signal.symbol != qqq_signal.symbol
