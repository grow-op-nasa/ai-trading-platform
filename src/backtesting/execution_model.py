"""Execution realism -- Sprint 12 (`DECISIONS.md`, ADR-0045).

Sprint 11 (ADR-0044) closed the *risk-sizing* gap between research and
paper trading: a backtest signal is now sized by the real
`PortfolioRiskEngine`, exactly as paper trading already is. It left one
gap deliberately open (its own module docstring, `portfolio_engine.py`):
every fill still happened at the signal's own bar close, instantly, at
that exact price -- no slippage, no fees, no timing gap between
"signal generated" and "order filled." This module closes *that* gap,
without touching risk sizing at all:

    Signal
        |
        v
    Approved Trade Intent  (src.risk -- unchanged, quantity frozen here)
        |
        v
    ExecutionModel.simulate(order, candles)
        |
        v
    reference price  (timing convention: which bar/field)
        |
        v
    slippage  (SlippageModel -- price-level friction)
        |
        v
    fee  (FeeModel -- notional-based cost)
        |
        v
    Fill  (src.execution.models.Fill -- the existing shape, extended)

**Execution decides *when*, *whether*, and *at what price/cost* an
already-approved order is filled. It never decides *whether* to trade,
*how much* risk is permitted, or *how large* a position is (Sprint 12
spec, section 2)** -- `ExecutionModel.simulate()` takes an `Order`
whose `quantity` was already fixed by `PortfolioRiskEngine`/
`RiskDecision.to_trade_intent()` and never changes it: an order is
either filled in full, at the quantity it was built with, or not filled
at all (`NO_EXECUTION_BAR`/`INSUFFICIENT_CASH_FOR_FEE`) -- partial
fills are explicitly future work (Sprint 12 spec, section 24).

**Reuses the existing `src.execution.models.Fill`/`Order`/`OrderSide`
shapes, never a second fill domain model** (Sprint 12 spec, section 4).
`Fill` gained four new, defaulted fields this sprint (see that module)
so a `Fill` produced here is a real, ordinary `Fill` -- indistinguishable
in shape from one `PaperBroker` already produces, just with its cost
fields actually populated.

**Deterministic, pure, no hidden state** (Sprint 12 spec, section 52):
`ExecutionModel.simulate()` is a pure function of `(order, candles,
config)` -- no randomness, no wall-clock reads, no mutation of anything
it's handed. Two calls with identical inputs always produce an
identical `Fill`.

**Candle-level only, market orders only** (Sprint 12 spec, sections
25-27): no limit/stop orders, no bid/ask ladders, no order-book depth,
no market-impact curves, no latency. `slippage` is the single, explicit
proxy for execution-price friction this sprint -- a future microstructure
sprint can model the bid/ask spread explicitly; this one does not
double-count it under a different name.

**Timing convention (Sprint 12 spec, section 5).** `ExecutionTiming.
SIGNAL_BAR_CLOSE` is the default -- and legacy-compatible -- convention:
fill at the signal's own bar close, identical to every pre-Sprint-12
result. `ExecutionTiming.NEXT_BAR_OPEN` is the new, more realistic
convention: a signal generated after bar N closes becomes eligible to
fill using bar N+1's open. Nothing switches a caller from one to the
other silently -- `ExecutionConfig.timing` defaults to
`SIGNAL_BAR_CLOSE` specifically so an existing `BacktestConfig()` with
no `execution_config` given reproduces Sprint 11 byte-for-byte (Sprint
12 spec, section 56).

**No look-ahead, structurally (Sprint 12 spec, section 6).** Under
`NEXT_BAR_OPEN`, the only future value ever read is bar N+1's `open` --
never its `high`/`low`/`close`, and never any bar beyond N+1. There is
no code path in `_reference_price()` that reads a column other than
`open` from the resolved reference bar, so this isn't a convention that
could be violated by a careless edit reaching for a "better" price.

**No next bar -> explicit `NO_EXECUTION_BAR`, never a same-bar-close
fallback (Sprint 12 spec, section 7).** A signal at the final candle
under `NEXT_BAR_OPEN` has no bar to reference. Silently falling back to
the current close would reintroduce exactly the look-ahead ambiguity
this module exists to remove (the signal-bar close is *known* only
because the signal itself was generated from it -- using it as a
"realistic" execution price defeats the point of modeling a timing
gap at all). `simulate()` returns `ExecutionOutcome(filled=False,
reason=NO_EXECUTION_BAR)` instead; `PortfolioBacktestEngine` treats
this as an unfilled order -- an entry never opens a position, a close
leaves its position open (picked up by the ordinary end-of-data
synthetic-close convention `portfolio_engine.py` already has).

**Session boundaries fall out for free (Sprint 12 spec, section 8).**
"The next bar" is always the next row in `candles`' own canonical
index (`candles.index.get_loc(order.timestamp) + 1`) -- never "the next
calendar minute" or any invented timestamp. A Friday-close signal's
next bar is whatever row canonical, session-aware market data
(`DECISIONS.md`, ADR-0041) actually has next, typically the following
session's open -- this module never fabricates an overnight or
premarket price to fill it sooner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from src.execution.models import Fill, Order, OrderSide

NO_EXECUTION_BAR = "NO_EXECUTION_BAR"
INSUFFICIENT_CASH_FOR_FEE = "INSUFFICIENT_CASH_FOR_FEE"


class ExecutionTiming(str, Enum):
    """Which bar/field an order's reference execution price comes from.

    A `str` subclass, like `src.backtesting.config.RiskMode` and
    `src.data.base.Interval` -- serializes cleanly into
    `ExecutionConfig.describe()`/experiment provenance with no separate
    encode/decode step.

    `SIGNAL_BAR_CLOSE`: the signal's own bar close -- identical to
        every pre-Sprint-12 fill (`DECISIONS.md`, ADR-0011/ADR-0044).
        The default, so an `ExecutionConfig()` with no arguments
        reproduces Sprint 11 exactly.
    `NEXT_BAR_OPEN`: the next bar's open -- the new, realistic
        convention this sprint introduces (Sprint 12 spec, section 5).
        Never used unless a caller opts in explicitly.
    """

    SIGNAL_BAR_CLOSE = "SIGNAL_BAR_CLOSE"
    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"


@dataclass(frozen=True)
class ReferencePrice:
    """The frictionless price `ExecutionModel` resolved for one order,
    before slippage or fees -- what "the market was doing" at the
    chosen timing convention's reference point.

    Args:
        timestamp: which bar this price came from -- the signal's own
            bar (`SIGNAL_BAR_CLOSE`) or the following bar
            (`NEXT_BAR_OPEN`). This is the fill's actual timestamp,
            distinct from `order.timestamp` (the originating signal's
            timestamp) whenever the two conventions differ.
        price: the reference price itself -- a bar's `close` or `open`,
            read directly off canonical candle data, never derived or
            interpolated.
    """

    timestamp: pd.Timestamp
    price: float


@dataclass(frozen=True)
class ExecutionOutcome:
    """The result of one `ExecutionModel.simulate()` call.

    Args:
        filled: whether the order was filled at all. Always either
            fully filled (at `order.quantity`, the amount Risk already
            approved -- Sprint 12 spec, section 19) or not filled --
            there is no partial-fill outcome this sprint (section 24).
        fill: the resulting `Fill`, when `filled` is `True`.
        reason: `NO_EXECUTION_BAR` (no reference bar exists -- section
            7) or `INSUFFICIENT_CASH_FOR_FEE` (the fee would drive cash
            negative -- section 21), when `filled` is `False`.

    Raises:
        ValueError: `filled` is `True` without a `fill`, or `False`
            without a `reason` -- an outcome must always explain
            itself one way or the other.
    """

    filled: bool
    fill: Fill | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.filled and self.fill is None:
            raise ValueError("a filled ExecutionOutcome must carry a fill")
        if not self.filled and self.reason is None:
            raise ValueError("an unfilled ExecutionOutcome must carry a reason")


@runtime_checkable
class SlippageModel(Protocol):
    """A deterministic mapping from a reference price + order side to a
    fill price. Never random, never dependent on anything but its own
    inputs (Sprint 12 spec, section 12)."""

    def apply(self, reference_price: float, side: OrderSide) -> float:
        """Return the fill price after slippage.

        For positive slippage: a `BUY` fills at or above
        `reference_price` (paying more); a `SELL` fills at or below it
        (receiving less) -- slippage may never *improve* execution
        (Sprint 12 spec, section 11).
        """
        ...

    @property
    def config(self) -> dict[str, Any]:
        """A JSON-safe description of this model's type and parameters,
        for experiment provenance (`ExecutionConfig.describe()`)."""
        ...


class PercentageSlippageModel:
    """Slippage as a fixed fraction of the reference price, in basis
    points (Sprint 12 spec, section 9).

    Args:
        slippage_bps: basis points (1 bp = 0.01%) of adverse price
            movement applied to every fill. `0` (the default) means no
            slippage at all -- `fill_price == reference_price` exactly
            (section 10), a valid, explicitly-supported configuration,
            never a hard-coded minimum.

    Raises:
        ValueError: `slippage_bps` is negative -- slippage is a cost,
            never a source of favorable execution (section 11).
    """

    def __init__(self, slippage_bps: float = 0.0) -> None:
        if slippage_bps < 0:
            raise ValueError(f"slippage_bps must be non-negative, got {slippage_bps}")
        self._slippage_bps = slippage_bps

    @property
    def config(self) -> dict[str, Any]:
        return {"type": "percentage", "slippage_bps": self._slippage_bps}

    def apply(self, reference_price: float, side: OrderSide) -> float:
        factor = self._slippage_bps / 10_000.0
        if side is OrderSide.BUY:
            return reference_price * (1 + factor)
        return reference_price * (1 - factor)


@runtime_checkable
class FeeModel(Protocol):
    """A deterministic mapping from a fill's notional value to a dollar
    fee. Broker-independent -- this is a generic research friction
    model, never a specific broker's real fee schedule (Sprint 12 spec,
    section 13)."""

    def fee(self, notional: float) -> float:
        """Return the dollar fee for one fill of `notional =
        quantity * fill_price` (Sprint 12 spec, section 14)."""
        ...

    @property
    def config(self) -> dict[str, Any]:
        """A JSON-safe description of this model's type and parameters."""
        ...


class PercentageFeeModel:
    """A fee proportional to notional, plus an optional fixed
    per-order component (Sprint 12 spec, section 13).

    Args:
        fee_bps: basis points of `notional` charged per fill. `0` (the
            default) means no percentage fee.
        fixed_fee: a flat dollar amount charged per fill, in addition
            to the percentage component. `0.0` (the default) means no
            fixed component.

    Raises:
        ValueError: either argument is negative.
    """

    def __init__(self, fee_bps: float = 0.0, fixed_fee: float = 0.0) -> None:
        if fee_bps < 0:
            raise ValueError(f"fee_bps must be non-negative, got {fee_bps}")
        if fixed_fee < 0:
            raise ValueError(f"fixed_fee must be non-negative, got {fixed_fee}")
        self._fee_bps = fee_bps
        self._fixed_fee = fixed_fee

    @property
    def config(self) -> dict[str, Any]:
        return {"type": "percentage", "fee_bps": self._fee_bps, "fixed_fee": self._fixed_fee}

    def fee(self, notional: float) -> float:
        return notional * (self._fee_bps / 10_000.0) + self._fixed_fee


@dataclass(frozen=True)
class ExecutionConfig:
    """Explicit, reproducible execution assumptions for one backtest
    run (Sprint 12 spec, sections 28-29, 54) -- the execution-layer
    analogue of `src.backtesting.config.BacktestConfig`'s risk fields.

    Two configs with equivalent timing/slippage/fees always `describe()`
    identically; any material change (a different timing mode, a
    different `slippage_bps`, a different fee) always produces a
    different description -- this is what gives an execution
    configuration a stable identity for experiment provenance.

    Args:
        timing: which bar/field the reference execution price comes
            from. Defaults to `ExecutionTiming.SIGNAL_BAR_CLOSE` --
            the legacy-compatible convention (Sprint 12 spec, section
            56: no existing result is silently reinterpreted).
        slippage_model: how a reference price becomes a fill price.
            Defaults to zero slippage (`PercentageSlippageModel(0.0)`).
        fee_model: how a fill's notional becomes a dollar cost.
            Defaults to zero fees (`PercentageFeeModel(0.0, 0.0)`).
    """

    timing: ExecutionTiming = ExecutionTiming.SIGNAL_BAR_CLOSE
    slippage_model: SlippageModel = field(default_factory=lambda: PercentageSlippageModel(0.0))
    fee_model: FeeModel = field(default_factory=lambda: PercentageFeeModel(0.0, 0.0))

    def describe(self) -> dict[str, Any]:
        """A JSON-safe, reproducible description -- what
        `BacktestConfig.describe()` embeds under `"execution"` so a
        researcher can answer "what execution assumptions produced this
        result" without re-reading code (Sprint 12 spec, sections
        29-31)."""
        return {
            "timing": self.timing.value,
            "slippage": self.slippage_model.config,
            "fee": self.fee_model.config,
        }


class ExecutionModel:
    """Deterministic, candle-level execution simulation (Sprint 12
    spec, sections 4-8).

    Args:
        config: the timing/slippage/fee assumptions this model
            simulates against. Defaults to `ExecutionConfig()` -- the
            zero-cost, signal-bar-close baseline (Sprint 12 spec,
            section 34: "research needs a clean control").
    """

    def __init__(self, config: ExecutionConfig | None = None) -> None:
        self._config = config or ExecutionConfig()

    @property
    def config(self) -> ExecutionConfig:
        return self._config

    def simulate(self, order: Order, candles: pd.DataFrame) -> ExecutionOutcome:
        """Simulate filling `order` against `candles` -- `order.symbol`'s
        own canonical OHLCV data (Sprint 12 spec, section 49: this
        method never fetches, normalizes, or invents candle data of its
        own; it only reads the DataFrame it's handed).

        Args:
            order: the already-risk-approved order to fill. Its
                `quantity` is never altered here -- fully filled at
                that exact quantity, or not filled at all.
            candles: the full canonical OHLCV history for `order.symbol`
                (not truncated to the order's own timestamp -- this
                method needs to see one bar *past* it under
                `NEXT_BAR_OPEN`; see the module docstring for why that
                single, explicit `open` read is not a look-ahead
                violation).

        Returns:
            An `ExecutionOutcome` -- filled (with a `Fill`) or not
            (with a `NO_EXECUTION_BAR`/`INSUFFICIENT_CASH_FOR_FEE`
            reason). Never raises for "no next bar" or "fee makes this
            unaffordable" -- those are structured outcomes, not
            exceptions, matching this platform's established
            `StopResult`/`RiskDecision` convention.
        """
        reference = self._reference_price(order, candles)
        if reference is None:
            return ExecutionOutcome(filled=False, reason=NO_EXECUTION_BAR)

        fill_price = self._config.slippage_model.apply(reference.price, order.side)
        notional = order.quantity * fill_price
        fee = self._config.fee_model.fee(notional)
        cash_delta = (
            -notional - fee if order.side is OrderSide.BUY else notional - fee
        )

        fill = Fill(
            order=order,
            fill_price=fill_price,
            cash_delta=cash_delta,
            reference_price=reference.price,
            fill_timestamp=reference.timestamp,
            slippage_amount=fill_price - reference.price,
            fee=fee,
        )
        return ExecutionOutcome(filled=True, fill=fill)

    def _reference_price(self, order: Order, candles: pd.DataFrame) -> ReferencePrice | None:
        if order.timestamp not in candles.index:
            return None

        if self._config.timing is ExecutionTiming.SIGNAL_BAR_CLOSE:
            price = float(candles.loc[order.timestamp, "close"])
            return ReferencePrice(timestamp=order.timestamp, price=price)

        # NEXT_BAR_OPEN: the next row in candles' own canonical index --
        # never an invented calendar timestamp (module docstring, "session
        # boundaries fall out for free"). candles.index is unique and
        # sorted (the canonical market-data contract, DECISIONS.md
        # ADR-0041), so get_loc() always returns a plain int here.
        position = candles.index.get_loc(order.timestamp)
        next_position = position + 1
        if next_position >= len(candles.index):
            return None
        next_timestamp = candles.index[next_position]
        price = float(candles.loc[next_timestamp, "open"])
        return ReferencePrice(timestamp=next_timestamp, price=price)
