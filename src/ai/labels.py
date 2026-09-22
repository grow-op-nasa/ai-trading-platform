"""Label construction -- src/ai/labels.py.

Deliberately its own module, not a step hidden inside
`src.ai.features` (Sprint 10 spec, section 8): a feature at timestamp
`t` describes information available *at* `t`; a label describes an
*outcome after* `t`. Keeping them in separate classes with separate
inputs/outputs is what makes it possible to later swap in a different
horizon, a different threshold, or even a regression target without
touching a single feature.

    from src.ai.labels import LabelBuilder, LabelSpec

    builder = LabelBuilder(LabelSpec(horizon_bars=5, neutral_threshold=0.01))
    labels = builder.build(candles)   # "LONG"/"SHORT"/"FLAT"/NaN Series
    label_id = builder.label_spec_id()

For each timestamp `t` (Sprint 10 spec, section 7):

    future_return = close[t + horizon_bars] / close[t] - 1

    future_return >  neutral_threshold  -> "LONG"
    future_return < -neutral_threshold  -> "SHORT"
    otherwise                            -> "FLAT"

The final `horizon_bars` rows of any series have no future close to
compute `future_return` from -- those rows are `NaN` in the returned
Series (a real "unknown", not a fabricated `"FLAT"`), left for
`src.ai.dataset` to drop once features are known too (Sprint 10 spec,
section 9).

Label values are the same three strings `SignalDirection` uses
(`"LONG"`/`"SHORT"`/`"FLAT"`, `src.signals.models.SignalDirection`) --
not a fourth, ML-specific vocabulary -- so a trained model's predicted
class maps onto `SignalDirection` in `src.strategies.ai_signal`
without an intermediate translation table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd

from src.utils.hashing import sha256_hex

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"


@dataclass(frozen=True)
class LabelSpec:
    """The label definition itself is part of the experiment (Sprint 10
    spec, section 7) -- not a constant buried inside a training script.

    Args:
        horizon_bars: how many bars ahead `future_return` looks.
            Configurable and explicit; there is no implicit default
            "the model just knows" -- callers must decide this, the
            same way `RSIMeanReversionStrategy` requires an explicit
            `period` rather than assuming one.
        neutral_threshold: the symmetric fractional-return band around
            zero that maps to `"FLAT"` -- e.g. `0.01` means a forward
            return in `(-1%, +1%)` is "no clear direction", not a weak
            long or short. Must be `>= 0`; `0.0` is legal (every
            nonzero forward return gets a directional label).

    Raises:
        ValueError: `horizon_bars` isn't a positive integer, or
            `neutral_threshold` is negative.
    """

    horizon_bars: int = 5
    neutral_threshold: float = 0.01

    def __post_init__(self) -> None:
        if not isinstance(self.horizon_bars, int) or self.horizon_bars <= 0:
            raise ValueError(
                f"horizon_bars must be a positive integer, got {self.horizon_bars!r}"
            )
        if self.neutral_threshold < 0:
            raise ValueError(
                f"neutral_threshold must be >= 0, got {self.neutral_threshold}"
            )

    def to_dict(self) -> dict:
        return {"horizon_bars": self.horizon_bars, "neutral_threshold": self.neutral_threshold}


class LabelBuilder:
    """Builds the 3-class forward-return label Series from candles.

    Args:
        spec: label configuration. Defaults to `LabelSpec()`.
    """

    def __init__(self, spec: LabelSpec | None = None) -> None:
        self._spec = spec or LabelSpec()

    @property
    def spec(self) -> LabelSpec:
        return self._spec

    def label_spec_id(self) -> str:
        """Deterministic hex digest of this builder's `spec` (Sprint 10
        spec, section 36) -- a 5-bar-horizon model and a 20-bar-horizon
        model never share an identity, even with everything else equal.
        """
        return sha256_hex(json.dumps(self._spec.to_dict(), sort_keys=True).encode("utf-8"))

    def build(self, candles: pd.DataFrame) -> pd.Series:
        """Compute the label for every row of `candles`.

        Returns an `object`-dtype Series aligned to `candles`' index,
        values in `{"LONG", "SHORT", "FLAT"}` plus `NaN` for the final
        `horizon_bars` rows (Sprint 10 spec, section 9) -- never a
        fabricated fourth class and never a filled-in guess for a row
        with no real future outcome yet.

        Raises:
            ValueError: `candles` has no `close` column.
        """
        if "close" not in candles.columns:
            raise ValueError("candles is missing required column: 'close'")

        horizon = self._spec.horizon_bars
        threshold = self._spec.neutral_threshold
        close = candles["close"]

        future_close = close.shift(-horizon)
        future_return = future_close / close - 1

        labels = pd.Series(FLAT, index=candles.index, dtype=object)
        labels[future_return > threshold] = LONG
        labels[future_return < -threshold] = SHORT
        # Rows with no future close at all (the last `horizon` rows)
        # have an undefined outcome, not a "FLAT" one -- overwrite them
        # back to NaN after the vectorized comparisons above (which
        # would otherwise read NaN > threshold as False and leave a
        # wrong "FLAT").
        labels[future_return.isna()] = pd.NA

        return labels.rename("label")
