"""True, stop-based position sizing plus portfolio constraints --
`PortfolioRiskEngine` (`DECISIONS.md`, ADR-0039, Sprint 7).

    from src.portfolio import Portfolio
    from src.risk import PortfolioRiskEngine, RiskLimits
    from src.risk.models import PortfolioRiskLimits

    engine = PortfolioRiskEngine(RiskLimits(), PortfolioRiskLimits(risk_pct_per_trade=0.005))
    decision = engine.decide(signal, portfolio, entry_price=500.0, stop_price=495.0)
    if decision.approved:
        broker.submit_signal(signal, signal.symbol, fill_price=500.0,
                              sizing_decision=decision.as_sizing_decision())

Deliberately standalone from `PositionSizer` (`engine.py`) -- that
class's allocation-only model is untouched and still the right tool for
a trade with no stop-loss concept at all (`DECISIONS.md`, ADR-0021).
This engine answers a different question: given a defined loss
boundary (a stop), how large a position does the *risk budget* justify,
and how much of that does the *portfolio* actually have room for.

**Two-stage decision model (Sprint 7 spec, section 28.1):**

    Stage A -- risk sizing:
        risk_amount = equity * risk_pct
        price_risk_per_unit = abs(entry_price - stop_price)
        risk_quantity = floor(risk_amount / price_risk_per_unit)

    Stage B -- portfolio/capital constraints, each computed
    independently from the same proposed trade:
        approved_quantity = min(
            risk_quantity, capital_quantity, allocation_quantity,
            portfolio_exposure_quantity, symbol_exposure_quantity,
        )

`risk_quantity` is a ceiling, never a target -- no constraint may ever
increase the approved quantity above it (`REQUIRED ARCHITECTURAL
INVARIANT`, section 29). Constraints that aren't configured (an unset
`PortfolioRiskLimits.max_symbol_exposure_pct`, or a `SHORT` under this
platform's no-margin-model simulation) contribute `None`, not an
arbitrary large sentinel, and are excluded from the `min()`.

**Stop-loss semantics (Sprint 7 spec, section 15) -- read this before
trusting `risk_amount`/`risk_quantity` as a real-money guarantee.** A
stop here is a *sizing* input: the price distance the position is
willing to lose before the thesis is considered wrong. It is
deliberately **not**:
    - a guaranteed fill price -- `PaperBroker` has no resting stop
      orders and cannot simulate one triggering;
    - a guaranteed maximum realized loss -- an actual exit (a `FLAT`
      signal, filled by `PaperBroker`) can happen at any price, better
      or worse than `stop_price`, including through it (no slippage or
      gap modeling exists, `DECISIONS.md` ADR-0011/ADR-0022);
    - broker stop-order behavior of any kind;
    - slippage-free execution.
`risk_amount` is therefore a *theoretical* maximum loss under the
assumption that an exit at exactly `stop_price` is achievable. This
platform does not enforce that assumption and does not claim to.

**Short-sale capital/margin semantics -- explicitly NOT_MODELED, never
"unlimited" (Sprint 7 spec, section 6; made machine-visible in the
Sprint 7 cleanup, `DECISIONS.md` ADR-0040).** `PaperBroker` models a
`SHORT` open as an immediate cash credit with no margin/collateral
requirement at all (`engine.py`'s own docstring, `DECISIONS.md`
ADR-0022) -- there is genuinely no capital/margin model to compute a
ceiling from for a `SHORT`. This engine represents that absence
explicitly rather than leaving it to be inferred from a bare `None`:
`RiskDecision.capital_quantity` is `None` for a `SHORT` *and*
`RiskDecision.capital_model` is `CapitalConstraintModel.NOT_MODELED` --
the pair together say "no capital ceiling was computed here," not "no
capital ceiling applies" or "capital is unlimited for this trade."
Broker-realistic short-sale margin, borrow availability, and financing
requirements are simply outside what this engine calculates. A `SHORT`
trade is **not** thereby claimed to be capital-affordable in any
broker-realistic sense -- what a `SHORT` *is* still constrained by,
exactly as for a `LONG`: the risk budget and stop distance (Stage A),
total exposure, symbol exposure, allocation limits (all of Stage B
except the capital ceiling), and the concurrent-position limit. Only
the capital/margin ceiling itself is unmodeled for a `SHORT` -- every
other check in this module runs identically regardless of direction.

**Position scaling is not supported (Sprint 7 spec, section 9/28.3).**
`PaperBroker` allows exactly one open position per symbol at a time and
has no mechanism to add to or partially reduce one (`DECISIONS.md`,
ADR-0022) -- a `decide()` call for a symbol that already has an open
position is rejected with `POSITION_SCALING_NOT_SUPPORTED` rather than
silently pretending the operation would succeed.

**Entry/increase intent vs. exit/close intent are architecturally
distinct (Sprint 7 cleanup, `DECISIONS.md` ADR-0040).** A close is not
a new-position risk-sizing decision that happens to always pass -- it
is a different question entirely, answered by a different method:

    Entry / increase intent                Exit / close intent
    LONG / SHORT signal                    FLAT / exit signal
        |                                       |
        v                                       v
    decide()                               decide_close()
        |                                       |
        v                                       v
    Risk sizing (Stage A)                  Position lookup
        |                                       |
        v                                       v
    Portfolio constraints (Stage B)        Close (full quantity only)
        |                                       |
        v                                       v
    Approved order                         Approved close order
                                            (or NO_POSITION_TO_CLOSE)

`decide()` still raises `ValueError` on a `FLAT` signal -- unchanged
from before the cleanup, and deliberately not "fixed" by silently
routing `FLAT` through new-position sizing, since that would be exactly
the conflation this distinction exists to prevent. `decide_close()` is
the new, separate, symmetric method for exit intent: it never computes
a risk budget, never applies `max_portfolio_exposure_pct`/
`max_symbol_exposure_pct` (a close can proceed even when the portfolio
is already over either limit -- reducing exposure can never be the
thing that breaches an exposure limit), and never increases exposure.
It looks up whether `portfolio` holds an open position in the signal's
symbol: none -> `RejectionReason.NO_POSITION_TO_CLOSE` (never
fabricated as a close order, and never confused with a risk-limit
rejection -- there is no risk question to ask); a position exists ->
approved for the position's full quantity. `PaperBroker`/`Portfolio`
support only whole-position closes this sprint (no partial reduction,
`DECISIONS.md` ADR-0022) -- requesting anything other than the full
existing quantity is rejected with `RejectionReason.
UNSUPPORTED_POSITION_OPERATION`, never silently coerced to the full
amount or fabricated as partial support that doesn't exist.
`decide_close()` never submits an order itself, exactly like `decide()`
-- both return a `RiskDecision` for a caller to act on; the actual
close order still goes through `PaperBroker.submit_signal()` directly
(unchanged since before Sprint 7), with `apply_fill_to_portfolio()`
syncing `Portfolio` from the resulting `Fill`, exactly as for an entry.

**Responsibility boundary (Sprint 7 cleanup, `DECISIONS.md` ADR-0040).**
This module answers exactly one question -- "what trade is permitted,
and at what quantity?" -- and nothing else. `PortfolioRiskEngine` does
not, and structurally cannot (`tests/test_architecture.py::
test_risk_modules_do_not_import_src_broker_or_src_execution`), import
or call `PaperBroker`, a live broker adapter, or anything in
`src.execution` -- it never submits orders, never waits for fills, and
never mutates `Portfolio` as a side effect of a decision (`Portfolio`
is only ever read here, via `portfolio.cash`/`equity`/`total_exposure`/
`symbol_exposure()`/`has_open_position()`/`position_count`, never
written to). `RiskDecision.to_trade_intent()` (`models.py`) is the
explicit, formalized handoff to Execution: it turns an approved
decision into an `ApprovedTradeIntent` and enforces, in code, that the
quantity Execution is handed can never exceed what was approved here.
"""

from __future__ import annotations

import math

from src.portfolio.models import Portfolio
from src.risk.models import (
    CapitalConstraintModel,
    PortfolioRiskLimits,
    RejectionReason,
    RiskDecision,
    RiskLimits,
)
from src.signals.models import Signal, SignalDirection


class PortfolioRiskEngine:
    """Turns a `Signal` + `Portfolio` + proposed entry/stop into a
    `RiskDecision` (`DECISIONS.md`, ADR-0039).

    Args:
        risk_limits: reused from `src.risk.models.RiskLimits` --
            `allocation_per_trade_pct` and `max_portfolio_exposure_pct`
            are consumed here for the allocation-limit and
            total-exposure-limit checks (Sprint 7 spec section 28.2,
            steps 4 and 5). Defaults to `RiskLimits()`.
        portfolio_limits: the genuinely new Sprint 7 configuration --
            `risk_pct_per_trade`, `max_symbol_exposure_pct`,
            `max_concurrent_positions`, `min_quantity`. Defaults to
            `PortfolioRiskLimits()`.
    """

    def __init__(
        self,
        risk_limits: RiskLimits | None = None,
        portfolio_limits: PortfolioRiskLimits | None = None,
    ) -> None:
        self._risk_limits = risk_limits or RiskLimits()
        self._portfolio_limits = portfolio_limits or PortfolioRiskLimits()

    def decide(
        self,
        signal: Signal,
        portfolio: Portfolio,
        entry_price: float,
        stop_price: float,
    ) -> RiskDecision:
        """Decide whether -- and how large a -- position `signal` may
        open, against `portfolio`'s current state.

        Args:
            signal: the `LONG`/`SHORT` signal being sized.
            portfolio: the portfolio's current cash, positions, and
                exposure.
            entry_price: the proposed entry price.
            stop_price: the proposed stop price -- must be on the
                correct side of `entry_price` for `signal.direction`
                (`LONG`: `stop_price < entry_price`; `SHORT`:
                `stop_price > entry_price`), and not equal to it.

        Returns:
            A `RiskDecision` -- always, for every rejection case
            described in the Sprint 7 spec (section 28.8): this method
            does not raise for a business-rule rejection, only for a
            call that doesn't make sense at all (see Raises).

        Raises:
            ValueError: `signal.direction` is `FLAT` -- closing isn't
                sized, the same restriction `PositionSizer.size()` has.
        """
        if signal.direction is SignalDirection.FLAT:
            raise ValueError(
                "cannot risk-size a FLAT signal -- FLAT closes a position, "
                "it doesn't open one"
            )

        symbol = signal.symbol
        direction = signal.direction
        equity = portfolio.equity
        risk_pct = self._portfolio_limits.risk_pct_per_trade
        # Capital/margin modeling depends only on direction, known
        # up front -- computed once so every RiskDecision this call
        # returns (rejected or approved) carries it (DECISIONS.md,
        # ADR-0040): SHORTs have no capital ceiling to compute at all
        # (module docstring), never to be misread as "unlimited."
        capital_model = (
            CapitalConstraintModel.NOT_MODELED
            if direction is SignalDirection.SHORT
            else CapitalConstraintModel.MODELED
        )

        def reject(reason: RejectionReason, **computed) -> RiskDecision:
            return RiskDecision(
                approved=False,
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_price=stop_price,
                equity_used=equity,
                risk_pct=risk_pct,
                risk_amount=computed.get("risk_amount", 0.0),
                requested_quantity=computed.get("risk_quantity", 0),
                risk_quantity=computed.get("risk_quantity", 0),
                capital_quantity=computed.get("capital_quantity"),
                allocation_quantity=computed.get("allocation_quantity"),
                portfolio_exposure_quantity=computed.get("portfolio_exposure_quantity"),
                symbol_exposure_quantity=computed.get("symbol_exposure_quantity"),
                final_approved_quantity=0,
                limiting_constraint=None,
                rejection_reason=reason,
                resulting_exposure=portfolio.total_exposure,
                explanation=f"rejected: {reason.value}",
                signal_id=signal.id,
                capital_model=capital_model,
            )

        # -- Stage 1: input validity (section 28.2, step 1) -----------------
        if not symbol:
            return reject(RejectionReason.INVALID_INPUT)
        if entry_price <= 0:
            return reject(RejectionReason.INVALID_INPUT)
        if stop_price <= 0:
            return reject(RejectionReason.INVALID_STOP)
        if stop_price == entry_price:
            return reject(RejectionReason.ZERO_STOP_DISTANCE)
        if direction is SignalDirection.LONG and stop_price >= entry_price:
            return reject(RejectionReason.INVALID_STOP)
        if direction is SignalDirection.SHORT and stop_price <= entry_price:
            return reject(RejectionReason.INVALID_STOP)

        # -- Position scaling is not supported (section 9, 28.3) -----------
        if portfolio.has_open_position(symbol):
            return reject(RejectionReason.POSITION_SCALING_NOT_SUPPORTED)

        # -- Stage A: risk sizing (section 4, 28.1, 28.2 step 2) ------------
        risk_amount = equity * risk_pct
        price_risk_per_unit = abs(entry_price - stop_price)
        risk_quantity = math.floor(risk_amount / price_risk_per_unit)

        if risk_quantity < 1:
            return reject(
                RejectionReason.INSUFFICIENT_RISK_BUDGET,
                risk_amount=risk_amount,
                risk_quantity=risk_quantity,
            )

        # -- Stage B: independently-computed portfolio/capital ceilings ----
        if direction is SignalDirection.LONG:
            capital_quantity = math.floor(portfolio.cash / entry_price)
        else:
            # SHORT: no margin/collateral model exists (module docstring).
            capital_quantity = None

        max_allocation_value = equity * self._risk_limits.allocation_per_trade_pct
        allocation_quantity = math.floor(max_allocation_value / entry_price)

        max_total_exposure = equity * self._risk_limits.max_portfolio_exposure_pct
        portfolio_headroom = max(0.0, max_total_exposure - portfolio.total_exposure)
        portfolio_exposure_quantity = math.floor(portfolio_headroom / entry_price)

        if self._portfolio_limits.max_symbol_exposure_pct is not None:
            max_symbol_exposure = equity * self._portfolio_limits.max_symbol_exposure_pct
            symbol_headroom = max(0.0, max_symbol_exposure - portfolio.symbol_exposure(symbol))
            symbol_exposure_quantity = math.floor(symbol_headroom / entry_price)
        else:
            symbol_exposure_quantity = None

        reducible = [
            (RejectionReason.INSUFFICIENT_CAPITAL, capital_quantity),
            (RejectionReason.ALLOCATION_LIMIT, allocation_quantity),
            (RejectionReason.MAX_PORTFOLIO_EXPOSURE, portfolio_exposure_quantity),
            (RejectionReason.MAX_SYMBOL_EXPOSURE, symbol_exposure_quantity),
        ]
        applicable = [(reason, qty) for reason, qty in reducible if qty is not None]
        final_quantity = min([risk_quantity] + [qty for _, qty in applicable])

        computed = dict(
            risk_amount=risk_amount,
            risk_quantity=risk_quantity,
            capital_quantity=capital_quantity,
            allocation_quantity=allocation_quantity,
            portfolio_exposure_quantity=portfolio_exposure_quantity,
            symbol_exposure_quantity=symbol_exposure_quantity,
        )

        if final_quantity < 1:
            # risk_quantity already passed the >=1 gate above, so the
            # binding reason here is always one of the reducible ones --
            # first in precedence order (list order above) among ties.
            reason = next(r for r, qty in applicable if qty == final_quantity)
            return reject(reason, **computed)

        # -- Minimum tradable quantity (section 28.5) -----------------------
        min_quantity = self._portfolio_limits.min_quantity
        if final_quantity < min_quantity:
            return reject(RejectionReason.QUANTITY_BELOW_MINIMUM, **computed)

        # -- Concurrent-position limit (section 28.2 step 7) -----------------
        # `symbol` is guaranteed not already held (the scaling check above
        # already returned otherwise), so this is always a genuinely new
        # position -- it always consumes a slot when this check applies.
        max_positions = self._portfolio_limits.max_concurrent_positions
        if max_positions is not None and portfolio.position_count >= max_positions:
            return reject(RejectionReason.MAX_CONCURRENT_POSITIONS, **computed)

        # -- Approved ---------------------------------------------------------
        limiting_constraint = (
            tuple(r for r, qty in applicable if qty == final_quantity)
            if final_quantity < risk_quantity
            else None
        )
        resulting_exposure = portfolio.total_exposure + final_quantity * entry_price
        explanation = f"approved {final_quantity} unit(s) of {symbol} (risk_quantity={risk_quantity}"
        explanation += (
            f", limited by {', '.join(r.value for r in limiting_constraint)})"
            if limiting_constraint
            else ")"
        )

        return RiskDecision(
            approved=True,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            equity_used=equity,
            risk_pct=risk_pct,
            risk_amount=risk_amount,
            requested_quantity=risk_quantity,
            risk_quantity=risk_quantity,
            capital_quantity=capital_quantity,
            allocation_quantity=allocation_quantity,
            portfolio_exposure_quantity=portfolio_exposure_quantity,
            symbol_exposure_quantity=symbol_exposure_quantity,
            final_approved_quantity=final_quantity,
            limiting_constraint=limiting_constraint,
            rejection_reason=None,
            resulting_exposure=resulting_exposure,
            explanation=explanation,
            signal_id=signal.id,
            capital_model=capital_model,
        )

    def decide_close(
        self,
        signal: Signal,
        portfolio: Portfolio,
        quantity: float | None = None,
    ) -> RiskDecision:
        """Decide whether an exit/close intent may proceed against
        `portfolio`'s current state (Sprint 7 cleanup, `DECISIONS.md`
        ADR-0040) -- the symmetric counterpart to `decide()` for the
        opposite kind of intent. See this module's own docstring for
        the full entry-vs-close architectural distinction.

        A close is looked up and permitted, never risk-sized: no risk
        budget is computed, and the portfolio's total/symbol exposure
        limits (`RiskLimits.max_portfolio_exposure_pct`,
        `PortfolioRiskLimits.max_symbol_exposure_pct`) are never
        consulted -- a close can never be the thing that breaches an
        exposure limit, and remains permitted even when the portfolio
        is already above one. Reducing exposure is what this method
        approves; it never approves anything that would increase it.

        Args:
            signal: the `FLAT` signal identifying which symbol to
                close. Any other `direction` raises -- this method is
                exclusively the close/exit path, the strict mirror of
                `decide()` refusing `FLAT`.
            portfolio: the portfolio to look up the existing position
                in. Read-only here, exactly like `decide()` -- this
                method never mutates `portfolio`.
            quantity: the quantity to close, if a caller wants to state
                it explicitly. Defaults to `None`, meaning "close the
                full existing position" -- the only close operation
                `PaperBroker`/`Portfolio` actually support this sprint
                (`DECISIONS.md`, ADR-0022). Passing any value other than
                the existing position's own full quantity is rejected
                with `RejectionReason.UNSUPPORTED_POSITION_OPERATION`
                -- partial reduction is never fabricated as supported.

        Returns:
            A `RiskDecision` with `is_close=True`: approved for the
            full existing quantity when a position exists,
            `RejectionReason.NO_POSITION_TO_CLOSE` when `portfolio` has
            no open position in `signal.symbol` (never treated as a
            risk-limit rejection -- there is no risk question to ask),
            or `RejectionReason.UNSUPPORTED_POSITION_OPERATION` for an
            unsupported partial-quantity request.

        Raises:
            ValueError: `signal.direction` is not `FLAT` -- this method
                only ever sizes/permits a close, the strict mirror of
                `decide()` raising on `FLAT`.
        """
        if signal.direction is not SignalDirection.FLAT:
            raise ValueError(
                "decide_close() only accepts a FLAT (exit/close) signal -- "
                "use decide() to size a new LONG/SHORT position"
            )

        symbol = signal.symbol
        existing = portfolio.positions.get(symbol)

        def close_decision(
            *,
            approved: bool,
            rejection_reason: RejectionReason | None,
            close_quantity: int,
            resulting_exposure: float,
            explanation: str,
        ) -> RiskDecision:
            return RiskDecision(
                approved=approved,
                symbol=symbol,
                direction=SignalDirection.FLAT,
                # A close decision echoes the position's own recorded
                # entry/stop (traceability), not a new risk boundary --
                # falls back to 0.0 when there is no position at all to
                # echo (the NO_POSITION_TO_CLOSE case).
                entry_price=existing.entry_price if existing else 0.0,
                stop_price=(
                    (existing.stop_price if existing.stop_price is not None else existing.entry_price)
                    if existing
                    else 0.0
                ),
                equity_used=portfolio.equity,
                risk_pct=0.0,
                risk_amount=0.0,
                requested_quantity=close_quantity,
                risk_quantity=close_quantity,
                capital_quantity=None,
                allocation_quantity=None,
                portfolio_exposure_quantity=None,
                symbol_exposure_quantity=None,
                final_approved_quantity=close_quantity if approved else 0,
                limiting_constraint=None,
                rejection_reason=rejection_reason,
                resulting_exposure=resulting_exposure,
                explanation=explanation,
                is_close=True,
                signal_id=signal.id,
                capital_model=None,
            )

        if existing is None:
            return close_decision(
                approved=False,
                rejection_reason=RejectionReason.NO_POSITION_TO_CLOSE,
                close_quantity=0,
                resulting_exposure=portfolio.total_exposure,
                explanation=f"rejected: no open position in {symbol!r} to close",
            )

        full_quantity = abs(int(existing.quantity))
        requested = full_quantity if quantity is None else abs(int(quantity))
        if requested != full_quantity:
            # PaperBroker/Portfolio support only a whole-position close
            # this sprint -- a request for any other quantity is not a
            # smaller version of the same operation, it is an operation
            # this platform does not implement yet (module docstring).
            return close_decision(
                approved=False,
                rejection_reason=RejectionReason.UNSUPPORTED_POSITION_OPERATION,
                close_quantity=requested,
                resulting_exposure=portfolio.total_exposure,
                explanation=(
                    f"rejected: partial close of {symbol!r} ({requested} of "
                    f"{full_quantity}) is not supported -- only a full close is"
                ),
            )

        return close_decision(
            approved=True,
            rejection_reason=None,
            close_quantity=full_quantity,
            # Closing releases this position's exposure entirely -- it
            # can only ever reduce total_exposure, never increase it,
            # regardless of any configured exposure limit.
            resulting_exposure=portfolio.total_exposure - existing.exposure,
            explanation=f"approved: close full {full_quantity} unit(s) of {symbol}",
        )
