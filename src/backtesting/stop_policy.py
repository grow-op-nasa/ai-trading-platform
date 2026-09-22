"""Stop-price abstraction for portfolio-aware backtesting -- Sprint 11
(`DECISIONS.md`, ADR-0044).

`src.risk.portfolio_risk.PortfolioRiskEngine.decide()` requires a stop
price because its sizing formula is genuinely stop-based (`risk_amount /
abs(entry_price - stop_price)`). `Signal` (`src.signals.models`)
deliberately carries no stop price -- a strategy expresses *direction*
("what position should the portfolio move toward"), never a price
boundary (`DECISIONS.md`, ADR-0015's own module docstring). Rather than
widen `Signal`'s core contract for one consumer, this module supplies
the missing input as its own small, swappable policy:

    StopPolicy
        |
        v
    stop_price(signal, history, entry_price) -> StopResult

A `StopPolicy` is a *sizing* input, nothing else. It is not:
    - a guaranteed exit price -- nothing in this platform simulates a
      resting stop order (`portfolio_risk.py`'s own module docstring
      makes the identical point about `stop_price` itself);
    - responsible for closing a trade -- only `PortfolioBacktestEngine`
      (via `PortfolioRiskEngine.decide_close()`) ever closes anything;
    - a broker stop-order model of any kind.
It answers exactly one question: "how far away, in price terms, is the
boundary this position's size should be computed against?"

**Past-only, by construction of the caller, not just this file's
convention.** `PortfolioBacktestEngine` always calls `stop_price()` with
`history` already truncated to `candles.loc[:signal.timestamp]` --
every row up to and including the signal's own bar, nothing beyond it.
A `StopPolicy` implementation that only ever reads `history` (never a
wider frame smuggled in some other way) is therefore leakage-safe by
the shape of its own input, not merely by its author's discipline.
`tests/test_backtesting_stop_policy.py`'s leakage-regression test
proves this directly: two histories identical through `t`, differing
only afterward, must produce identical stops at `t`.

**Unavailable is a first-class outcome, never invented.** ATR has a
`period`-bar warmup (a rolling window, `src.indicators.formulas.
average_true_range`) -- Sprint 10's `src.ai.dataset` established the
same principle for its own indicator-derived features: an
indicator that isn't ready yet is an explicit `NaN`, and a caller that
can't act on `NaN` must say so explicitly rather than substitute
something plausible-looking. `StopResult.available=False` is exactly
that explicit "I cannot answer" -- `PortfolioBacktestEngine` turns it
into a rejected signal (`STOP_UNAVAILABLE`), never a silent fallback to
unit sizing (Sprint 11 spec, section 9: that would bypass Risk
entirely).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

from src.indicators.engine import IndicatorEngine
from src.signals.models import Signal, SignalDirection

DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_MULTIPLE = 2.0


@dataclass(frozen=True)
class StopResult:
    """The outcome of one `StopPolicy.stop_price()` call.

    Args:
        available: whether a valid stop could be computed at all.
        stop_price: the computed stop, or `None` when `available` is
            `False`. When `available` is `True`, always on the correct
            side of the signal's entry (below for `LONG`, above for
            `SHORT`) and never equal to it -- a policy that cannot
            guarantee this must report `available=False` instead of an
            invalid price (`PortfolioRiskEngine.decide()` would reject
            an invalid stop anyway, but a policy should never rely on
            that as its own validation).
        reason: a short, human-readable explanation, set whenever
            `available` is `False` (e.g. "ATR warmup unavailable" or
            "ATR is zero -- cannot form a valid stop distance").
    """

    available: bool
    stop_price: float | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.available and self.stop_price is None:
            raise ValueError("an available StopResult must carry a stop_price")
        if not self.available and self.reason is None:
            raise ValueError("an unavailable StopResult must carry a reason")


@runtime_checkable
class StopPolicy(Protocol):
    """The contract every stop policy must satisfy -- structural typing,
    like `src.strategies.base.Strategy`."""

    def stop_price(
        self, signal: Signal, history: pd.DataFrame, entry_price: float
    ) -> StopResult:
        """Compute a sizing-boundary stop for `signal`.

        Args:
            signal: the `LONG`/`SHORT` signal being sized. Never `FLAT`
                -- closing is not sized (mirrors `PortfolioRiskEngine.
                decide()`'s own restriction).
            history: `signal.symbol`'s own OHLCV candles, already
                truncated by the caller to rows at or before
                `signal.timestamp` -- see this module's docstring for
                why that truncation is what makes every implementation
                past-only by construction, not by convention.
            entry_price: the proposed entry price (the signal's own bar
                close, by this platform's established convention).

        Returns:
            A `StopResult`. Never raises for "not enough history yet"
            or "the resulting stop would be degenerate" -- those are
            `StopResult.available=False` outcomes, not exceptions.
        """
        ...

    @property
    def config(self) -> dict:
        """A JSON-safe description of this policy's type and parameters
        -- e.g. `{"type": "atr", "period": 14, "multiple": 2.0}`. Used
        for experiment provenance (`ExperimentSpec.backtest_config`,
        Sprint 11 spec section 31) so a reproduced run can state exactly
        which stop policy and parameters produced a given result."""
        ...


class ATRStopPolicy:
    """A stop `multiple` Average True Range units away from entry.

    `LONG`: `stop = entry_price - multiple * ATR`.
    `SHORT`: `stop = entry_price + multiple * ATR`.

    ATR is computed via the existing `IndicatorEngine` (`src.indicators`)
    -- never a reimplemented formula (Sprint 11 spec, section 6) -- so
    this policy's ATR is identical to every strategy's own `ATR(14)`,
    the same "one authoritative formula" guarantee `IndicatorEngine`
    already gives every strategy.

    Args:
        period: the ATR lookback window. Defaults to 14, matching
            `src.indicators.formulas.average_true_range`'s own default
            and Sprint 10's feature set (`src.ai.features.FeatureSpec`).
        multiple: how many ATRs away from entry the stop sits. Defaults
            to 2.0 -- wide enough that ordinary intrabar noise rarely
            trips it, narrow enough to still bound risk meaningfully.

    Raises:
        ValueError: `period` isn't a positive integer, or `multiple`
            isn't positive.
    """

    def __init__(
        self, period: int = DEFAULT_ATR_PERIOD, multiple: float = DEFAULT_ATR_MULTIPLE
    ) -> None:
        if period < 1:
            raise ValueError(f"period must be a positive integer, got {period}")
        if multiple <= 0:
            raise ValueError(f"multiple must be positive, got {multiple}")
        self._period = period
        self._multiple = multiple

    @property
    def config(self) -> dict:
        return {"type": "atr", "period": self._period, "multiple": self._multiple}

    def stop_price(
        self, signal: Signal, history: pd.DataFrame, entry_price: float
    ) -> StopResult:
        if signal.direction is SignalDirection.FLAT:
            raise ValueError(
                "cannot compute a stop for a FLAT signal -- FLAT closes a "
                "position, it doesn't open one"
            )

        # ATR is recomputed on this (sparse-signal-bounded, not
        # candle-set-bounded) prefix rather than precomputed once over
        # the full series: signals are sparse by construction
        # (DECISIONS.md, ADR-0015), so the total work across a run is
        # proportional to len(signals) * average-history-length, not to
        # len(candles) for every signal (Sprint 11 spec, section 60) --
        # and, more importantly, it keeps "past-only" a property of what
        # this policy is physically handed, not of restraint exercised
        # inside the formula.
        atr_series = IndicatorEngine(history).calculate("ATR", period=self._period)
        if atr_series.empty:
            return StopResult(available=False, stop_price=None, reason="no history available yet")

        atr_value = float(atr_series.iloc[-1])
        if pd.isna(atr_value):
            return StopResult(
                available=False,
                stop_price=None,
                reason=(
                    f"ATR({self._period}) warmup unavailable -- fewer than "
                    f"{self._period} candles of history at this signal"
                ),
            )
        if atr_value <= 0:
            return StopResult(
                available=False,
                stop_price=None,
                reason=f"ATR({self._period}) is {atr_value} -- cannot form a valid stop distance",
            )

        distance = self._multiple * atr_value
        if signal.direction is SignalDirection.LONG:
            stop = entry_price - distance
        else:
            stop = entry_price + distance

        if stop <= 0:
            return StopResult(
                available=False,
                stop_price=None,
                reason=f"computed stop {stop} is not a positive price",
            )

        return StopResult(available=True, stop_price=stop)
