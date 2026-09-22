"""Time-aware splitting -- src/ai/splitting.py.

The single most important correctness rule in Sprint 10
(`ROADMAP.md`, `DECISIONS.md` ADR-0043): a time-series training
pipeline must never let a later observation influence an earlier
decision. This module is the one place that rule is actually enforced
in code, so every caller (`src.ai.training.MLTrainingService`) gets it
by construction rather than by remembering to do it right each time.

Two related but distinct tools:

    chronological_split(index, spec, purge_bars)
        One train -> validation -> test split, in that time order,
        with a `purge_bars`-wide embargo dropped at each internal
        boundary (Sprint 10 spec, sections 10-11).

    walk_forward_splits(index, min_train_bars, test_bars, purge_bars)
        A simple expanding-window evaluator: fold *i* trains on
        everything up to a cutoff and evaluates the next window, fold
        *i+1* trains on everything up to a *later* cutoff (never
        shrinking the training window, never reusing a later fold's
        data for an earlier one) (Sprint 10 spec, section 12).

Neither function ever shuffles, and neither ever looks at label values
-- both operate purely on `index` positions, which is what makes the
"no future data" guarantee structural rather than a matter of the
caller remembering to pass `shuffle=False`.

Why purging matters: a label at row `t` encodes `close[t + horizon]`
(`src.ai.labels`), so a training row within `horizon_bars` of a split
boundary has an outcome window that reaches *into* the next split.
Dropping the last `purge_bars` rows of every split but the last
removes that overlap -- Sprint 10 spec, section 11's exact requirement
("no training observation may contain a label whose outcome period
overlaps the validation or test period"), with `purge_bars` set to the
label's own `horizon_bars` by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SplitSpec:
    """Chronological train/validation/test proportions -- one place
    they're configured, not re-hardcoded in every module that needs a
    split (Sprint 10 spec, section 10).

    Args:
        train_frac / val_frac / test_frac: fractions of the dataset's
            row *count* (not calendar time) assigned to each split,
            before purging. Must be positive and sum to `1.0` (within
            floating-point tolerance).

    Raises:
        ValueError: any fraction is `<= 0`, or the three don't sum to
            `1.0`.
    """

    train_frac: float = 0.6
    val_frac: float = 0.2
    test_frac: float = 0.2

    def __post_init__(self) -> None:
        for name, value in (
            ("train_frac", self.train_frac),
            ("val_frac", self.val_frac),
            ("test_frac", self.test_frac),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        total = self.train_frac + self.val_frac + self.test_frac
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"train_frac + val_frac + test_frac must equal 1.0, got {total}"
            )


@dataclass(frozen=True)
class SplitIndices:
    """The three chronological, non-overlapping (and purge-separated)
    slices `chronological_split()` produces. Each is a `pd.Index` --
    a subset of the input index, order preserved -- ready to select
    rows out of `TrainingTable.X`/`.y` via `.loc[...]`."""

    train: pd.Index
    validation: pd.Index
    test: pd.Index


def chronological_split(
    index: pd.Index, spec: SplitSpec | None = None, purge_bars: int = 0
) -> SplitIndices:
    """Split `index` (assumed already sorted ascending -- true of every
    canonical dataset and every `TrainingTable`) into train/validation/
    test, purging `purge_bars` rows off the end of train and of
    validation.

    Args:
        index: the chronologically ordered index to split (typically
            `TrainingTable.X.index`).
        spec: split proportions; defaults to `SplitSpec()`.
        purge_bars: rows to drop immediately before the
            train/validation and validation/test boundaries (Sprint 10
            spec, section 11). Pass the label's own `horizon_bars` --
            that's the exact width a label's outcome window can reach
            past its own row. `0` disables purging (only correct for a
            `LabelSpec` whose `horizon_bars` genuinely doesn't apply,
            e.g. a non-forward-looking target added later).

    Raises:
        ValueError: `purge_bars` is negative, or `index` is empty.
    """
    if purge_bars < 0:
        raise ValueError(f"purge_bars must be >= 0, got {purge_bars}")
    if len(index) == 0:
        raise ValueError("index must not be empty")

    spec = spec or SplitSpec()
    n = len(index)
    train_end = round(n * spec.train_frac)
    val_end = min(train_end + round(n * spec.val_frac), n)
    train_end = min(train_end, n)

    train_idx = index[: max(0, train_end - purge_bars)]
    val_start = train_end
    val_idx = index[val_start : max(val_start, val_end - purge_bars)]
    test_idx = index[val_end:]

    return SplitIndices(train=train_idx, validation=val_idx, test=test_idx)


@dataclass(frozen=True)
class WalkForwardFold:
    """One expanding-window fold: train on everything up to this fold's
    cutoff, evaluate strictly on the window right after it.

    Args:
        fold: 0-based fold number, in chronological order.
        train_index: every row this fold trains on -- always a prefix
            of the full index, and always a superset of the previous
            fold's `train_index` (the "expanding" part).
        test_index: the out-of-sample window this fold evaluates
            against -- strictly later than every timestamp in
            `train_index`, purged the same way `chronological_split()`
            purges its own boundary.
    """

    fold: int
    train_index: pd.Index
    test_index: pd.Index


def walk_forward_splits(
    index: pd.Index,
    min_train_bars: int,
    test_bars: int,
    purge_bars: int = 0,
    max_folds: int | None = None,
) -> list[WalkForwardFold]:
    """Build a simple expanding-window walk-forward evaluation plan.

    Fold 0 trains on the first `min_train_bars` rows (minus
    `purge_bars`) and evaluates on the next `test_bars` rows. Fold 1
    trains on everything up through the end of fold 0's test window
    (minus `purge_bars`) and evaluates on the *next* `test_bars` rows,
    and so on -- never shuffled, never reusing a later window's rows to
    train an earlier fold (Sprint 10 spec, section 12).

    Args:
        index: the chronologically ordered index to fold.
        min_train_bars: smallest allowed training window -- fold 0's
            test window starts right after this many rows.
        test_bars: size of each fold's evaluation window.
        purge_bars: rows dropped off the end of each fold's training
            window, the same embargo `chronological_split()` applies
            (pass the label's `horizon_bars`).
        max_folds: stop after this many folds even if more would fit.
            `None` (default) generates every fold that fits.

    Returns:
        A list of `WalkForwardFold`, oldest first. Empty if `index` is
        too short to produce even one fold -- not an error, since a
        short synthetic dataset legitimately can't walk-forward at all.

    Raises:
        ValueError: `min_train_bars`, `test_bars` isn't a positive
            integer, or `purge_bars` is negative.
    """
    if min_train_bars <= 0:
        raise ValueError(f"min_train_bars must be positive, got {min_train_bars}")
    if test_bars <= 0:
        raise ValueError(f"test_bars must be positive, got {test_bars}")
    if purge_bars < 0:
        raise ValueError(f"purge_bars must be >= 0, got {purge_bars}")

    n = len(index)
    folds: list[WalkForwardFold] = []
    fold_num = 0
    test_start = min_train_bars

    while test_start + test_bars <= n:
        train_end = test_start - purge_bars
        if train_end <= 0:
            break
        folds.append(
            WalkForwardFold(
                fold=fold_num,
                train_index=index[:train_end],
                test_index=index[test_start : test_start + test_bars],
            )
        )
        fold_num += 1
        test_start += test_bars
        if max_folds is not None and fold_num >= max_folds:
            break

    return folds
