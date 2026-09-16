"""Data shapes for position sizing and portfolio-aware risk (src/risk).

Plain dataclasses, no behavior beyond input validation --
`PositionSizer` (`engine.py`) and `PortfolioRiskEngine`
(`portfolio_risk.py`) do the actual math. `AccountState` moved to
`src/portfolio` (`DECISIONS.md`, ADR-0031) -- import it from there.

`RiskLimits`/`SizingDecision` are Sprint 4's original, allocation-only
pair (ADR-0021/ADR-0032) -- unchanged by Sprint 7, still consumed by
`PositionSizer`. `PortfolioRiskLimits`/`RejectionReason`/`RiskDecision`
are new this sprint (ADR-0039): genuine, stop-based risk sizing plus
portfolio constraints, a distinct concept living in distinct classes
rather than folded into (or replacing) the allocation-only pair -- see
each class's own docstring for why.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from src.signals.models import SignalDirection


@dataclass
class RiskLimits:
    """Configurable capital-allocation parameters -- one instance can be
    shared across every sizing decision for an account, or varied per
    strategy/run.

    Args:
        allocation_per_trade_pct: fraction of account equity to deploy
            on a single trade (e.g. `0.10` = 10%). Applied identically
            regardless of `Signal.confidence` -- see `DECISIONS.md`,
            ADR-0021, for why a fixed fraction was chosen over a
            confidence-scaled one for this first version.

            **This is capital allocation, not maximum loss.** It answers
            "how much of the account is committed to this position,"
            never "how much could this trade lose." `RiskLimits` itself
            still has no stop-loss/risk-distance field (`DECISIONS.md`,
            ADR-0032) -- a trade sized at `allocation_per_trade_pct`
            alone can still lose far more or less than that percentage
            of equity, depending on how far price moves against it;
            this setting caps exposure, not loss. Genuine, stop-based
            risk sizing now exists (`PortfolioRiskLimits`, below,
            `DECISIONS.md` ADR-0039) as a distinct concept in a distinct
            class -- `PositionSizer`'s allocation-only model is
            unchanged and still the right tool when a trade has no
            stop-loss concept at all.
        max_portfolio_exposure_pct: fraction of account equity allowed
            to be committed to open positions at any one time (e.g.
            `0.50` = 50%). A new position is sized down -- or rejected
            outright if there's no headroom left -- rather than ever
            exceeding this.

    Raises:
        ValueError: either percentage is outside `(0, 1]`.
    """

    allocation_per_trade_pct: float = 0.10
    max_portfolio_exposure_pct: float = 0.50

    def __post_init__(self) -> None:
        if not 0 < self.allocation_per_trade_pct <= 1:
            raise ValueError(
                "allocation_per_trade_pct must be in (0, 1], got "
                f"{self.allocation_per_trade_pct}"
            )
        if not 0 < self.max_portfolio_exposure_pct <= 1:
            raise ValueError(
                "max_portfolio_exposure_pct must be in (0, 1], got "
                f"{self.max_portfolio_exposure_pct}"
            )


@dataclass
class SizingDecision:
    """The outcome of one `PositionSizer.size()` call.

    Check `approved`, not just whether `position_size` is truthy --
    rejection is meant to be an explicit branch at call sites, not
    something inferred from a zero.
    """

    approved: bool
    position_size: float
    capital_allocated: float
    reason: str


@dataclass
class PortfolioRiskLimits:
    """Configuration for `PortfolioRiskEngine` (`portfolio_risk.py`,
    `DECISIONS.md` ADR-0039) -- genuine, stop-based risk sizing plus the
    portfolio-level constraints layered on top of it.

    Deliberately a separate class from `RiskLimits`, not new fields
    added to it: `RiskLimits.allocation_per_trade_pct` and
    `max_portfolio_exposure_pct` are *reused* by `PortfolioRiskEngine`
    (for the allocation-limit and total-exposure-limit checks
    respectively, section 28.2 of the Sprint 7 spec) rather than
    duplicated here -- a `PortfolioRiskEngine` is constructed with both
    a `RiskLimits` and a `PortfolioRiskLimits`. This also keeps
    `tests/test_risk.py::test_risk_limits_is_not_a_maximum_loss_model`
    literally true forever: `RiskLimits` still has no loss-based field,
    because the loss-based fields live here instead, not because they
    don't exist.

    Args:
        risk_pct_per_trade: fraction of account equity intentionally
            placed at risk before the stop -- e.g. `0.005` (0.5%) of
            $10,000 equity is a $50 maximum intended loss, independent
            of how much capital the position actually deploys (that's
            `allocation_per_trade_pct`'s job). See `portfolio_risk.py`
            for the full sizing formula.
        max_symbol_exposure_pct: fraction of account equity a single
            symbol's exposure may reach. `None` (the default) means no
            per-symbol limit is enforced -- only the shared
            `RiskLimits.max_portfolio_exposure_pct` total-exposure
            ceiling applies.
        max_concurrent_positions: maximum number of distinct symbols
            allowed an open position at once. `None` (the default)
            means no limit is enforced.
        min_quantity: the smallest tradable quantity/increment. `1` (the
            default) matches `PaperBroker`'s integer-share contract --
            fractional shares are not introduced this sprint.

    Raises:
        ValueError: `risk_pct_per_trade` is outside `(0, 1]`;
            `max_symbol_exposure_pct` is given and outside `(0, 1]`;
            `max_concurrent_positions` is given and less than `1`; or
            `min_quantity` is less than `1`.
    """

    risk_pct_per_trade: float = 0.01
    max_symbol_exposure_pct: float | None = None
    max_concurrent_positions: int | None = None
    min_quantity: int = 1

    def __post_init__(self) -> None:
        if not 0 < self.risk_pct_per_trade <= 1:
            raise ValueError(
                f"risk_pct_per_trade must be in (0, 1], got {self.risk_pct_per_trade}"
            )
        if self.max_symbol_exposure_pct is not None and not 0 < self.max_symbol_exposure_pct <= 1:
            raise ValueError(
                f"max_symbol_exposure_pct must be in (0, 1] when given, got "
                f"{self.max_symbol_exposure_pct}"
            )
        if self.max_concurrent_positions is not None and self.max_concurrent_positions < 1:
            raise ValueError(
                f"max_concurrent_positions must be at least 1 when given, got "
                f"{self.max_concurrent_positions}"
            )
        if self.min_quantity < 1:
            raise ValueError(f"min_quantity must be at least 1, got {self.min_quantity}")


class RejectionReason(str, Enum):
    """Why a `PortfolioRiskEngine.decide()` call rejected a trade, or --
    reused for the same underlying concepts -- which constraint(s)
    reduced an *approved* trade's quantity below its theoretical
    `risk_quantity` (`RiskDecision.limiting_constraint`). A stable,
    serializable enum (`str` subclass, like `Interval`,
    `DECISIONS.md` ADR-0038) rather than a free-form string, per the
    Sprint 7 spec's explicit requirement (section 28.8) -- suitable for
    logging, tests, and experiment provenance without risking a typo'd
    string silently failing to match.

    `NO_POSITION_TO_CLOSE` (Sprint 7 cleanup, `DECISIONS.md` ADR-0040)
    is deliberately distinct from every member above: those all
    describe a *new-position* sizing request being reduced or refused,
    a question `PortfolioRiskEngine.decide()` answers. This one is
    returned only by `decide_close()`, for a close/exit intent naming a
    symbol the portfolio has no open position in -- never conflated
    with a risk-limit rejection, since there is no risk budget,
    capital, or exposure question to even ask when there is no
    position to close.
    """

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_STOP = "INVALID_STOP"
    ZERO_STOP_DISTANCE = "ZERO_STOP_DISTANCE"
    INSUFFICIENT_RISK_BUDGET = "INSUFFICIENT_RISK_BUDGET"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    ALLOCATION_LIMIT = "ALLOCATION_LIMIT"
    MAX_PORTFOLIO_EXPOSURE = "MAX_PORTFOLIO_EXPOSURE"
    MAX_SYMBOL_EXPOSURE = "MAX_SYMBOL_EXPOSURE"
    MAX_CONCURRENT_POSITIONS = "MAX_CONCURRENT_POSITIONS"
    QUANTITY_BELOW_MINIMUM = "QUANTITY_BELOW_MINIMUM"
    POSITION_SCALING_NOT_SUPPORTED = "POSITION_SCALING_NOT_SUPPORTED"
    UNSUPPORTED_POSITION_OPERATION = "UNSUPPORTED_POSITION_OPERATION"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    NO_POSITION_TO_CLOSE = "NO_POSITION_TO_CLOSE"


class CapitalConstraintModel(str, Enum):
    """Whether `RiskDecision.capital_quantity` reflects a real,
    computed capital ceiling or simply isn't modeled for this trade
    (Sprint 7 cleanup, `DECISIONS.md` ADR-0040).

    Introduced because `capital_quantity: int | None` is ambiguous on
    its own: `None` already means "not applicable" for a `SHORT` (no
    margin/collateral model exists, `portfolio_risk.py`'s module
    docstring), but `None` could also be misread as "no limit, treat as
    unlimited" by a careless caller. This enum makes that distinction
    machine-visible and typed instead of relying on a reader correctly
    inferring intent from an absent number. `capital_quantity is None`
    must never be interpreted as "capital-unlimited" -- it must always
    be read alongside `capital_model`, which states plainly whether a
    capital constraint was evaluated at all.

    `MODELED`: `capital_quantity` is a real, computed ceiling (today,
        every `LONG` trade -- `PaperBroker` requires cash up front to
        buy).
    `NOT_MODELED`: no capital/margin constraint was evaluated for this
        trade (today, every `SHORT` trade -- `PaperBroker` credits cash
        immediately on a short open with no margin/collateral
        requirement at all, so there is nothing to compute). This is a
        documented absence of modeling, not a claim that real
        short-selling is capital-free or that a broker would allow it
        without margin.
    """

    MODELED = "MODELED"
    NOT_MODELED = "NOT_MODELED"


@dataclass(frozen=True)
class RiskDecision:
    """The structured outcome of one `PortfolioRiskEngine.decide()` call
    (`DECISIONS.md`, ADR-0039) -- immutable, and detailed enough that a
    researcher can reconstruct exactly why a trade was approved (at what
    quantity) or rejected (for what reason) without needing a debug log
    (Sprint 7 spec, section 17, "Auditability").

    Also produced by `PortfolioRiskEngine.decide_close()` (Sprint 7
    cleanup, `DECISIONS.md` ADR-0040) for a close/exit intent -- see
    `is_close` below for how a close decision's fields differ from an
    entry decision's.

    Args:
        approved: whether this trade may proceed at all.
        symbol: the instrument this decision is about.
        direction: `LONG` or `SHORT` for an entry decision (`decide()`
            never sizes a `FLAT` signal, the same restriction
            `PositionSizer.size()` already has); always `FLAT` for a
            close decision (`is_close=True`, `decide_close()` never
            accepts anything else).
        entry_price: the proposed entry price this decision was
            computed against.
        stop_price: the proposed stop price this decision was computed
            against. See `portfolio_risk.py`'s module docstring for
            exactly what assumption this represents (and does not).
        equity_used: the account equity this decision's percentages
            were computed against.
        risk_pct: `PortfolioRiskLimits.risk_pct_per_trade` at decision
            time.
        risk_amount: `equity_used * risk_pct` -- the maximum intended
            dollar loss this decision sized against.
        requested_quantity: the quantity the risk budget alone would
            justify, before any portfolio constraint is applied. Equal
            to `risk_quantity` in this round -- kept as its own field
            per the Sprint 7 spec (section 28.7) so a future
            caller-specified quantity override doesn't require widening
            this shape; there is no such override yet.
        risk_quantity: `floor(risk_amount / abs(entry_price - stop_price))`
            -- Stage A's theoretical quantity (section 28.1).
        capital_quantity: the maximum quantity available cash affords,
            or `None` when this constraint doesn't apply to this trade
            (a `SHORT` under the current no-margin-model simulation --
            see `portfolio_risk.py`). **`None` here must never be read
            as "capital-unlimited"** -- it means no capital ceiling was
            computed for this trade at all; always check `capital_model`
            alongside it to know whether that absence is because the
            constraint genuinely isn't modeled (`NOT_MODELED`) or
            because this is a close decision where capital doesn't
            apply either (also `None`, `capital_model` likewise `None`
            -- see `is_close`).
        capital_model: (Sprint 7 cleanup, `DECISIONS.md` ADR-0040)
            `CapitalConstraintModel.MODELED` when `capital_quantity` is
            a real, computed ceiling (every `LONG` entry decision);
            `CapitalConstraintModel.NOT_MODELED` when no capital
            constraint was evaluated at all (every `SHORT` entry
            decision, since `PaperBroker` requires no margin/collateral
            to open one); `None` for a close decision, where capital
            affordability isn't a question `decide_close()` asks in the
            first place.
        allocation_quantity: the maximum quantity
            `RiskLimits.allocation_per_trade_pct` of equity affords.
            Always populated -- this limit is always configured.
        portfolio_exposure_quantity: the maximum quantity that keeps
            projected total exposure within
            `RiskLimits.max_portfolio_exposure_pct` of equity. Always
            populated -- this limit is always configured.
        symbol_exposure_quantity: the maximum quantity that keeps this
            symbol's projected exposure within
            `PortfolioRiskLimits.max_symbol_exposure_pct` of equity, or
            `None` when that limit isn't configured at all.
        final_approved_quantity: the quantity execution is permitted to
            submit. `0` when `approved` is `False`. Never greater than
            `risk_quantity` (`REQUIRED ARCHITECTURAL INVARIANT`, Sprint
            7 spec section 29) -- constraints may only reduce it.
        limiting_constraint: when `approved` is `True` and
            `final_approved_quantity < risk_quantity`, the
            `RejectionReason` member(s) whose independently-computed
            quantity tied for that minimum (a tuple, since more than one
            constraint can bind at once, section 28.7) -- `None` when
            the trade was approved at the full `risk_quantity` (nothing
            limited it) or when the trade was rejected outright (see
            `rejection_reason` instead).
        rejection_reason: the single, precedence-ordered
            `RejectionReason` this trade was rejected for, or `None`
            when `approved` is `True`.
        resulting_exposure: the portfolio's total gross exposure after
            this decision -- unchanged from before the call when
            rejected; `portfolio.total_exposure` at decision time plus
            `final_approved_quantity * entry_price` when approved.
        explanation: a short, human-readable summary -- for logs and
            quick reading, not a substitute for the structured fields
            above.
        is_close: (Sprint 7 cleanup, `DECISIONS.md` ADR-0040) `True`
            when this decision came from `decide_close()`, `False` for
            every `decide()` entry decision (the default, so every
            pre-cleanup call site that builds or matches on a
            `RiskDecision` is unaffected). A close decision is **not**
            a new-position sizing decision that happens to pass: it
            carries no risk budget (`risk_pct`/`risk_amount` are `0.0`),
            no portfolio-constraint ceilings (`capital_quantity`,
            `allocation_quantity`, `portfolio_exposure_quantity`,
            `symbol_exposure_quantity`, and `capital_model` are all
            `None` -- none of those questions apply to reducing
            exposure), and `entry_price`/`stop_price` echo the
            *existing* position's own recorded entry/stop (for
            traceability) rather than a newly-proposed risk boundary.
            See `PortfolioRiskEngine.decide_close()` for the full
            rationale.
        signal_id: (Sprint 7 cleanup, `DECISIONS.md` ADR-0040) the
            originating `Signal.id`, so a `RiskDecision` is traceable
            back to the signal that produced it without a separate
            lookup table. `None` only if a decision is constructed
            without one (not done by `PortfolioRiskEngine` itself,
            which always populates it) -- optional purely so this
            addition doesn't break any pre-existing direct construction
            of `RiskDecision`.
    """

    approved: bool
    symbol: str
    direction: SignalDirection
    entry_price: float
    stop_price: float
    equity_used: float
    risk_pct: float
    risk_amount: float
    requested_quantity: int
    risk_quantity: int
    capital_quantity: int | None
    allocation_quantity: int | None
    portfolio_exposure_quantity: int | None
    symbol_exposure_quantity: int | None
    final_approved_quantity: int
    limiting_constraint: tuple[RejectionReason, ...] | None
    rejection_reason: RejectionReason | None
    resulting_exposure: float | None
    explanation: str
    is_close: bool = False
    signal_id: UUID | None = None
    capital_model: CapitalConstraintModel | None = None

    def as_sizing_decision(self) -> SizingDecision:
        """Adapt this decision to the older `SizingDecision` shape so it
        can be handed to `PaperBroker.submit_signal(sizing_decision=...)`
        unchanged -- Sprint 7 connects to the existing execution layer
        this way specifically so `PaperBroker`'s tested interface never
        has to learn about `RiskDecision` at all (Sprint 7 spec, section
        14, "without redesigning the broker interfaces"). This remains
        the adapter `PaperBroker` actually consumes -- unchanged by the
        Sprint 7 cleanup; `to_trade_intent()` below is a separate,
        richer audit/boundary object, not a replacement for this one.
        """
        return SizingDecision(
            approved=self.approved,
            position_size=float(self.final_approved_quantity),
            capital_allocated=float(self.final_approved_quantity) * self.entry_price,
            reason=self.explanation,
        )

    def to_trade_intent(self, quantity: int | None = None) -> "ApprovedTradeIntent":
        """Formalize the `RiskDecision` -> Execution handoff as an
        explicit `ApprovedTradeIntent` (Sprint 7 cleanup, `DECISIONS.md`
        ADR-0040) -- the boundary object Execution should be handed
        instead of re-deriving what it needs from a raw `RiskDecision`.

        Composes rather than duplicates: `ApprovedTradeIntent` carries
        this `RiskDecision` by reference (`risk_decision`), plus the
        handful of fields Execution actually needs at the top level, so
        there is exactly one authoritative record of *why* a trade was
        approved (this `RiskDecision`) and one explicit statement of
        *what* Execution is cleared to submit (the intent).

        Args:
            quantity: the quantity Execution intends to submit. Defaults
                to `final_approved_quantity` (the normal path -- most
                callers should not pass this at all). Passing an
                explicit value exists only so an execution-specific
                reduction (e.g. a broker's own lot-size rounding) can be
                recorded on the intent -- it can never be used to
                request *more* than Risk approved.

        Returns:
            An `ApprovedTradeIntent` for `quantity` units of this
            decision's `symbol`/`direction` at `entry_price`.

        Raises:
            ValueError: this decision was not approved (`approved` is
                `False` -- there is nothing to hand to Execution), or
                `quantity` exceeds `final_approved_quantity`. This is
                the **hard invariant** Execution must never violate
                (`execution_quantity <= risk_approved_quantity`,
                Sprint 7 cleanup) enforced at the one place the
                approved quantity is turned into something Execution
                acts on -- not by convention, by construction.
        """
        if not self.approved:
            raise ValueError(
                "cannot build an ApprovedTradeIntent from a rejected RiskDecision "
                f"(rejection_reason={self.rejection_reason})"
            )
        final_quantity = self.final_approved_quantity if quantity is None else quantity
        if final_quantity > self.final_approved_quantity:
            raise ValueError(
                f"execution quantity {final_quantity} exceeds the risk-approved "
                f"quantity {self.final_approved_quantity} -- execution may never "
                f"increase what Risk approved (DECISIONS.md, ADR-0040)"
            )
        return ApprovedTradeIntent(
            symbol=self.symbol,
            direction=self.direction,
            quantity=final_quantity,
            entry_price=self.entry_price,
            stop_price=self.stop_price,
            signal_id=self.signal_id,
            risk_decision=self,
        )


@dataclass(frozen=True)
class ApprovedTradeIntent:
    """The explicit handoff from Risk to Execution (Sprint 7 cleanup,
    `DECISIONS.md` ADR-0040): "this specific trade, at this quantity, is
    cleared to be carried out." Produced only via
    `RiskDecision.to_trade_intent()`, never constructed standalone, so
    it can never exist without a backing, approved `RiskDecision`.

    Deliberately composes rather than duplicates `RiskDecision`'s
    business data: the few fields Execution actually needs are exposed
    at the top level for convenience, but the full audit record --
    every intermediate quantity, the limiting constraint, the
    originating equity/risk percentages -- stays in `risk_decision`,
    referenced, not copied. There is exactly one authoritative place
    that data lives.

    This coexists with, and does not replace, `RiskDecision.
    as_sizing_decision()` -- `PaperBroker.submit_signal()` still
    consumes the older `SizingDecision` shape unchanged (Sprint 7 spec,
    section 14). `ApprovedTradeIntent` is the more complete, explicit
    contract for reasoning about and auditing the Risk/Execution
    boundary itself, not a new execution-layer input this sprint wires
    `PaperBroker` up to consume.

    Args:
        symbol: the instrument to trade.
        direction: `LONG` or `SHORT` for an entry, `FLAT` for a close
            (mirrors `risk_decision.direction`).
        quantity: the quantity Execution is cleared to submit. Never
            greater than `risk_decision.final_approved_quantity` --
            enforced in `RiskDecision.to_trade_intent()`, not just
            documented here.
        entry_price: the reference price this intent was approved
            against.
        stop_price: the reference stop price this intent was approved
            against (see `risk_decision.is_close` for what this means
            on a close).
        signal_id: the originating `Signal.id`, for audit traceability
            from signal through to whatever Execution eventually does
            with this intent.
        risk_decision: the full `RiskDecision` this intent was derived
            from -- the single authoritative record of why.
    """

    symbol: str
    direction: SignalDirection
    quantity: int
    entry_price: float
    stop_price: float
    signal_id: UUID | None
    risk_decision: RiskDecision
