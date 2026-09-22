"""Tests for `src/ai/splitting.py` (Sprint 10, `DECISIONS.md` ADR-0043).

Network-free, sklearn-free -- this module only ever touches a
`pd.Index`, never label values, so these tests hand-build small index
fixtures rather than full candle data.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.ai.splitting import SplitSpec, chronological_split, walk_forward_splits


def _index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="D", name="timestamp")


def test_split_spec_rejects_fractions_that_dont_sum_to_one():
    with pytest.raises(ValueError, match="must equal 1.0"):
        SplitSpec(train_frac=0.5, val_frac=0.2, test_frac=0.2)


def test_split_spec_rejects_non_positive_fraction():
    with pytest.raises(ValueError):
        SplitSpec(train_frac=0.0, val_frac=0.5, test_frac=0.5)


def test_chronological_order_train_before_validation_before_test():
    index = _index(100)
    split = chronological_split(index, SplitSpec(), purge_bars=0)
    assert split.train[-1] < split.validation[0]
    assert split.validation[-1] < split.test[0]


def test_split_proportions_are_approximately_respected():
    index = _index(100)
    split = chronological_split(index, SplitSpec(0.6, 0.2, 0.2), purge_bars=0)
    assert len(split.train) == 60
    assert len(split.validation) == 20
    assert len(split.test) == 20


def test_purge_removes_rows_at_each_internal_boundary():
    index = _index(100)
    purge = 5
    split = chronological_split(index, SplitSpec(0.6, 0.2, 0.2), purge_bars=purge)
    # Without purge, train would be index[:60] and validation index[60:80].
    # With a purge of 5, train's last 5 rows and validation's last 5
    # rows must be gone.
    assert len(split.train) == 60 - purge
    assert len(split.validation) == 20 - purge
    assert len(split.test) == 20  # no split follows test -- nothing to purge


def test_purge_guarantees_no_horizon_overlap_across_boundaries():
    # The property that actually matters (Sprint 10 spec, section 11):
    # a training row's label horizon must never reach into validation.
    # If purge_bars == horizon_bars, the last training timestamp plus
    # horizon_bars must land at or before validation's first timestamp
    # minus one bar -- i.e. strictly before it, never inside it.
    index = _index(100)
    horizon_bars = 7
    split = chronological_split(index, SplitSpec(0.6, 0.2, 0.2), purge_bars=horizon_bars)

    train_positions = index.get_indexer(split.train)
    val_positions = index.get_indexer(split.validation)
    last_train_pos = train_positions[-1]
    first_val_pos = val_positions[0]

    # The last training row's outcome window (t .. t+horizon) must end
    # strictly before validation begins.
    assert last_train_pos + horizon_bars < first_val_pos


def test_zero_purge_leaves_splits_contiguous():
    index = _index(30)
    split = chronological_split(index, SplitSpec(), purge_bars=0)
    assert len(split.train) + len(split.validation) + len(split.test) == 30


def test_negative_purge_raises():
    with pytest.raises(ValueError):
        chronological_split(_index(10), purge_bars=-1)


def test_empty_index_raises():
    with pytest.raises(ValueError, match="empty"):
        chronological_split(pd.DatetimeIndex([]))


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


def test_walk_forward_folds_are_expanding_and_never_shrink():
    index = _index(100)
    folds = walk_forward_splits(index, min_train_bars=40, test_bars=10, purge_bars=0)
    assert len(folds) >= 2
    for earlier, later in zip(folds, folds[1:]):
        assert len(later.train_index) > len(earlier.train_index)
        # Every fold's training set is a genuine prefix of the next
        # fold's -- never reshuffled, never dropping earlier history.
        assert list(later.train_index[: len(earlier.train_index)]) == list(
            earlier.train_index
        )


def test_walk_forward_test_windows_never_overlap_and_advance_in_time():
    index = _index(100)
    folds = walk_forward_splits(index, min_train_bars=40, test_bars=10, purge_bars=0)
    for earlier, later in zip(folds, folds[1:]):
        assert earlier.test_index[-1] < later.test_index[0]


def test_walk_forward_respects_purge_between_train_and_test():
    index = _index(100)
    purge = 5
    folds = walk_forward_splits(index, min_train_bars=40, test_bars=10, purge_bars=purge)
    assert folds  # sanity check
    for fold in folds:
        train_positions = index.get_indexer(fold.train_index)
        test_positions = index.get_indexer(fold.test_index)
        assert train_positions[-1] + purge < test_positions[0]


def test_walk_forward_respects_max_folds():
    index = _index(200)
    folds = walk_forward_splits(index, min_train_bars=40, test_bars=10, max_folds=3)
    assert len(folds) == 3


def test_walk_forward_returns_empty_list_when_dataset_too_short():
    index = _index(20)
    folds = walk_forward_splits(index, min_train_bars=40, test_bars=10)
    assert folds == []


def test_walk_forward_rejects_non_positive_arguments():
    index = _index(50)
    with pytest.raises(ValueError):
        walk_forward_splits(index, min_train_bars=0, test_bars=10)
    with pytest.raises(ValueError):
        walk_forward_splits(index, min_train_bars=10, test_bars=0)
    with pytest.raises(ValueError):
        walk_forward_splits(index, min_train_bars=10, test_bars=5, purge_bars=-1)
