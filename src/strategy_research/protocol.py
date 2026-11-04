"""Chronological validation protocol -- development / validation / holdout (Sprint 23, ADR-0061).

Boundaries are explicit bar timestamps, persisted with every run. Data is never shuffled. The development and
validation segments are the only ones any selection logic may see; the holdout is evaluated once, afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

MIN_SEGMENT_BARS = 20


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationProtocol:
    """Three consecutive, non-overlapping segments of the evaluated bars, identified by bar position.

    ``dev_bars`` / ``validation_bars`` are the sizes of the first two segments; every remaining bar is the holdout.
    """

    dev_bars: int
    validation_bars: int
    holdout_bars: int

    def __post_init__(self) -> None:
        for name in ("dev_bars", "validation_bars", "holdout_bars"):
            if getattr(self, name) < MIN_SEGMENT_BARS:
                raise ProtocolError(f"{name} must be at least {MIN_SEGMENT_BARS} bars, got {getattr(self, name)}")

    @classmethod
    def from_fractions(cls, total_bars: int, dev: float = 0.5, validation: float = 0.25) -> "ValidationProtocol":
        if not (0 < dev < 1 and 0 < validation < 1 and dev + validation < 1):
            raise ProtocolError("fractions must be positive and leave a non-empty holdout")
        d = int(total_bars * dev)
        v = int(total_bars * validation)
        return cls(d, v, total_bars - d - v)

    @property
    def total_bars(self) -> int:
        return self.dev_bars + self.validation_bars + self.holdout_bars

    def boundaries(self, index: pd.DatetimeIndex) -> dict[str, dict[str, Any]]:
        """Exact first/last bar timestamps of each segment for ``index`` (which must be exactly ``total_bars`` long)."""
        if len(index) != self.total_bars:
            raise ProtocolError(f"protocol covers {self.total_bars} bars but the data has {len(index)}")
        a, b = self.dev_bars, self.dev_bars + self.validation_bars
        spans = {"development": (0, a), "validation": (a, b), "holdout": (b, self.total_bars)}
        return {k: {"first_bar": index[i].isoformat(), "last_bar": index[j - 1].isoformat(), "bars": j - i,
                    "start_pos": i, "end_pos": j} for k, (i, j) in spans.items()}

    def to_dict(self) -> dict[str, Any]:
        return {"dev_bars": self.dev_bars, "validation_bars": self.validation_bars, "holdout_bars": self.holdout_bars}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ValidationProtocol":
        return cls(int(raw["dev_bars"]), int(raw["validation_bars"]), int(raw["holdout_bars"]))


@dataclass(frozen=True)
class WalkForwardFold:
    train_start: int
    train_end: int      # exclusive
    test_start: int
    test_end: int       # exclusive


def walk_forward_folds(total_bars: int, train_bars: int, test_bars: int) -> list[WalkForwardFold]:
    """Rolling folds: train [i, i+train), test [i+train, i+train+test), advancing by ``test_bars``.

    Test windows never overlap one another and every test window begins exactly where its training window ends,
    so no test bar is ever inside any earlier or its own training window.
    """
    if train_bars < MIN_SEGMENT_BARS or test_bars < MIN_SEGMENT_BARS:
        raise ProtocolError(f"train_bars and test_bars must each be at least {MIN_SEGMENT_BARS}")
    folds: list[WalkForwardFold] = []
    i = 0
    while i + train_bars + test_bars <= total_bars:
        folds.append(WalkForwardFold(i, i + train_bars, i + train_bars, i + train_bars + test_bars))
        i += test_bars
    if not folds:
        raise ProtocolError(f"{total_bars} bars cannot hold one fold of {train_bars} train + {test_bars} test bars")
    return folds
