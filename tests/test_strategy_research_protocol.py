"""Chronological validation protocol and walk-forward folds (src/strategy_research/protocol.py, ADR-0061)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategy_research.protocol import MIN_SEGMENT_BARS, ProtocolError, ValidationProtocol, walk_forward_folds


def index(n: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2020-01-01", periods=n, tz="UTC")


def test_segments_are_consecutive_non_overlapping_and_cover_every_bar():
    p = ValidationProtocol(100, 50, 50)
    b = p.boundaries(index(200))
    assert (b["development"]["start_pos"], b["development"]["end_pos"]) == (0, 100)
    assert (b["validation"]["start_pos"], b["validation"]["end_pos"]) == (100, 150)
    assert (b["holdout"]["start_pos"], b["holdout"]["end_pos"]) == (150, 200)
    idx = index(200)
    assert b["development"]["last_bar"] < b["validation"]["first_bar"] < b["validation"]["last_bar"] < b["holdout"]["first_bar"]
    assert b["development"]["first_bar"] == idx[0].isoformat() and b["holdout"]["last_bar"] == idx[-1].isoformat()
    assert sum(v["bars"] for v in b.values()) == 200


def test_from_fractions_leaves_the_remainder_as_the_holdout():
    p = ValidationProtocol.from_fractions(401, dev=0.5, validation=0.25)
    assert (p.dev_bars, p.validation_bars, p.holdout_bars) == (200, 100, 101)
    assert p.total_bars == 401


@pytest.mark.parametrize("dev,val", [(0, 0.25), (0.5, 0), (0.8, 0.3), (1.0, 0.1), (-0.1, 0.2)])
def test_invalid_fractions_are_rejected(dev, val):
    with pytest.raises(ProtocolError):
        ValidationProtocol.from_fractions(500, dev, val)


def test_each_segment_needs_a_minimum_number_of_bars():
    with pytest.raises(ProtocolError):
        ValidationProtocol(MIN_SEGMENT_BARS - 1, 50, 50)
    with pytest.raises(ProtocolError):
        ValidationProtocol.from_fractions(40)


def test_boundaries_require_exactly_the_covered_number_of_bars():
    with pytest.raises(ProtocolError):
        ValidationProtocol(100, 50, 50).boundaries(index(199))


def test_protocol_round_trips():
    p = ValidationProtocol(120, 60, 60)
    assert ValidationProtocol.from_dict(p.to_dict()) == p


def test_walk_forward_test_windows_never_overlap_and_start_where_training_ends():
    folds = walk_forward_folds(400, train_bars=100, test_bars=50)
    assert len(folds) == 6
    for f in folds:
        assert f.test_start == f.train_end and f.train_end - f.train_start == 100 and f.test_end - f.test_start == 50
    for a, b in zip(folds, folds[1:]):
        assert b.test_start == a.test_end            # test windows tile the data with no overlap
        assert b.train_end > a.train_end             # advances; no test bar can sit inside its own or an earlier training window
    assert folds[-1].test_end <= 400


def test_no_test_bar_is_inside_its_own_training_window():
    for f in walk_forward_folds(500, 120, 40):
        assert set(range(f.train_start, f.train_end)).isdisjoint(range(f.test_start, f.test_end))


def test_walk_forward_rejects_data_too_short_for_one_fold_and_tiny_windows():
    with pytest.raises(ProtocolError):
        walk_forward_folds(120, 100, 50)
    with pytest.raises(ProtocolError):
        walk_forward_folds(500, 10, 50)
    with pytest.raises(ProtocolError):
        walk_forward_folds(500, 100, 5)
