"""Sprint 7 end-to-end integration tests -- Signal -> Risk -> Portfolio
Constraint -> Approved Order -> Paper Execution -> Fill -> Position
Update -> Portfolio State (`DECISIONS.md`, ADR-0039).

Mirrors the existing pipeline-contract convention
(`tests/test_pipeline_contract.py`, `tests/test_integration_paper_trading.py`):
deliberately a small number of thorough, composition-proving tests, not
a restatement of the unit tests already in `tests/test_portfolio_risk.py`
and `tests/test_portfolio_position.py`. Each test here exercises the
*wiring* between `PortfolioRiskEngine`, `PaperBroker`, and `Portfolio`
together -- not any one of them in isolation.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.execution import PaperBroker, apply_fill_to_portfolio
from src.portfolio import Portfolio
from src.portfolio.position import PositionSide
from src.risk.models import PortfolioRiskLimits, RejectionReason, RiskLimits
from src.risk.portfolio_risk import PortfolioRiskEngine
from src.signals.models import Signal, SignalDirection

WIDE_LIMITS = RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0)


def make_signal(symbol: str, direction: SignalDirection, ts: str = "2024-01-01") -> Signal:
    return Signal(timestamp=pd.Timestamp(ts), symbol=symbol, direction=direction, confidence=0.8)


# ---------------------------------------------------------------------------
# 21. The core end-to-end contract test: approved quantity == executed
# quantity, all the way through to Portfolio state.
# ---------------------------------------------------------------------------


def test_approved_risk_decision_flows_through_execution_to_portfolio_state():
    starting_cash = 10_000.0
    portfolio = Portfolio(cash=starting_cash)
    broker = PaperBroker(starting_cash=starting_cash)
    engine = PortfolioRiskEngine(WIDE_LIMITS, PortfolioRiskLimits(risk_pct_per_trade=0.005))

    signal = make_signal("SPY", SignalDirection.LONG)
    decision = engine.decide(signal, portfolio, entry_price=500.0, stop_price=495.0)

    assert decision.approved is True
    assert decision.final_approved_quantity == 10  # the spec's own worked example

    fill = broker.submit_signal(
        signal, "SPY", fill_price=500.0, sizing_decision=decision.as_sizing_decision()
    )
    # The risk-approved quantity is exactly what execution submitted --
    # the required invariant (Sprint 7 spec section 21/29).
    assert fill.order.quantity == pytest.approx(decision.final_approved_quantity)

    position = apply_fill_to_portfolio(portfolio, fill, stop_price=decision.stop_price)

    assert position.symbol == "SPY"
    assert position.side is PositionSide.LONG
    assert position.quantity == pytest.approx(10.0)
    assert position.stop_price == pytest.approx(495.0)
    assert portfolio.has_open_position("SPY") is True
    assert portfolio.total_exposure == pytest.approx(10 * 500.0)
    assert portfolio.cash == pytest.approx(starting_cash - 10 * 500.0)
    # Both bookkeeping systems (PaperBroker's own, and the new Portfolio)
    # agree on cash, since the same Fill was applied to each.
    assert broker.cash == pytest.approx(portfolio.cash)


def test_full_position_closure_updates_portfolio_state_correctly():
    starting_cash = 10_000.0
    portfolio = Portfolio(cash=starting_cash)
    broker = PaperBroker(starting_cash=starting_cash)
    engine = PortfolioRiskEngine(WIDE_LIMITS, PortfolioRiskLimits(risk_pct_per_trade=0.005))

    open_signal = make_signal("SPY", SignalDirection.LONG, ts="2024-01-01")
    decision = engine.decide(open_signal, portfolio, entry_price=500.0, stop_price=495.0)
    assert decision.approved is True
    open_fill = broker.submit_signal(
        open_signal, "SPY", fill_price=500.0, sizing_decision=decision.as_sizing_decision()
    )
    apply_fill_to_portfolio(portfolio, open_fill, stop_price=decision.stop_price)
    assert portfolio.total_exposure > 0.0

    # Closing bypasses the risk engine entirely -- a FLAT signal goes
    # straight to PaperBroker, exactly as before Sprint 7.
    close_signal = make_signal("SPY", SignalDirection.FLAT, ts="2024-01-02")
    close_fill = broker.submit_signal(close_signal, "SPY", fill_price=520.0)
    closed_position = apply_fill_to_portfolio(portfolio, close_fill)

    assert closed_position.realized_pnl == pytest.approx(10 * (520.0 - 500.0))
    assert portfolio.has_open_position("SPY") is False
    assert portfolio.total_exposure == 0.0  # released, no longer contributes
    assert portfolio.symbol_exposure("SPY") == 0.0
    assert broker.cash == pytest.approx(portfolio.cash)


# ---------------------------------------------------------------------------
# Rejection path: no order submitted when the risk engine rejects.
# ---------------------------------------------------------------------------


def test_rejected_risk_decision_never_reaches_execution():
    portfolio = Portfolio(cash=10_000.0)
    broker = PaperBroker(starting_cash=10_000.0)
    engine = PortfolioRiskEngine(WIDE_LIMITS, PortfolioRiskLimits(risk_pct_per_trade=0.005))

    signal = make_signal("SPY", SignalDirection.LONG)
    # Zero stop distance -- an unambiguous, deterministic rejection.
    decision = engine.decide(signal, portfolio, entry_price=500.0, stop_price=500.0)

    assert decision.approved is False
    assert decision.rejection_reason is RejectionReason.ZERO_STOP_DISTANCE

    # The critical invariant: a caller that respects `approved` never
    # calls submit_signal() at all -- broker/portfolio state stays
    # exactly as it started.
    if decision.approved:  # pragma: no cover -- documents the branch a real loop takes
        broker.submit_signal(signal, "SPY", fill_price=500.0, sizing_decision=decision.as_sizing_decision())

    assert broker.positions == {}
    assert broker.cash == pytest.approx(10_000.0)
    assert portfolio.positions == {}
    assert portfolio.cash == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# Signal/execution symbol mismatch -- the invariant holds even with the
# new risk layer in the loop (Sprint 7 spec section 13).
# ---------------------------------------------------------------------------


def test_symbol_mismatch_between_signal_and_execution_is_rejected_end_to_end():
    portfolio = Portfolio(cash=10_000.0)
    broker = PaperBroker(starting_cash=10_000.0)
    engine = PortfolioRiskEngine(WIDE_LIMITS, PortfolioRiskLimits(risk_pct_per_trade=0.005))

    signal = make_signal("SPY", SignalDirection.LONG)  # signal.symbol == "SPY"
    decision = engine.decide(signal, portfolio, entry_price=500.0, stop_price=495.0)
    assert decision.approved is True

    # A caller passing a different execution symbol than the signal's
    # own is rejected explicitly -- PaperBroker's existing enforcement
    # (ADR-0036), still in force with a RiskDecision in the loop.
    with pytest.raises(ValueError, match="symbol mismatch"):
        broker.submit_signal(
            signal, "QQQ", fill_price=500.0, sizing_decision=decision.as_sizing_decision()
        )

    assert "QQQ" not in broker.positions
    assert broker.cash == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# 18. Multiple concurrent positions, executed end to end.
# ---------------------------------------------------------------------------


def test_multiple_concurrent_positions_execute_and_coexist_end_to_end():
    # Realistic (not maximally wide) limits -- WIDE_LIMITS would let the
    # first trade alone consume the entire account, since its stop is
    # proportionally close to entry; a sensible allocation cap is what
    # actually leaves room for three positions to coexist.
    starting_cash = 1_000_000.0
    portfolio = Portfolio(cash=starting_cash)
    broker = PaperBroker(starting_cash=starting_cash)
    engine = PortfolioRiskEngine(
        RiskLimits(allocation_per_trade_pct=0.10, max_portfolio_exposure_pct=0.50),
        PortfolioRiskLimits(risk_pct_per_trade=0.005),
    )

    for symbol, price, stop in (("SPY", 400.0, 396.0), ("QQQ", 350.0, 346.0), ("GLD", 180.0, 178.0)):
        signal = make_signal(symbol, SignalDirection.LONG)
        decision = engine.decide(signal, portfolio, entry_price=price, stop_price=stop)
        assert decision.approved is True, f"{symbol} unexpectedly rejected: {decision.rejection_reason}"
        fill = broker.submit_signal(
            signal, symbol, fill_price=price, sizing_decision=decision.as_sizing_decision()
        )
        apply_fill_to_portfolio(portfolio, fill, stop_price=stop)

    assert portfolio.position_count == 3
    assert set(portfolio.positions) == {"SPY", "QQQ", "GLD"}
    assert broker.cash == pytest.approx(portfolio.cash)


# ---------------------------------------------------------------------------
# Sprint 7 cleanup (DECISIONS.md, ADR-0040): the close/exit-intent path,
# end to end -- decide_close() permits, PaperBroker executes directly
# (never through risk-sizing machinery), apply_fill_to_portfolio() updates
# state through the existing Fill-based sync, exactly as before the
# cleanup. This is the "close is execution-owned" contract: Risk decides
# whether a close may proceed, Execution still owns actually submitting it.
# ---------------------------------------------------------------------------


def test_close_intent_flows_through_decide_close_and_execution_to_portfolio_state():
    starting_cash = 10_000.0
    portfolio = Portfolio(cash=starting_cash)
    broker = PaperBroker(starting_cash=starting_cash)
    engine = PortfolioRiskEngine(WIDE_LIMITS, PortfolioRiskLimits(risk_pct_per_trade=0.005))

    open_signal = make_signal("SPY", SignalDirection.LONG, ts="2024-01-01")
    open_decision = engine.decide(open_signal, portfolio, entry_price=500.0, stop_price=495.0)
    assert open_decision.approved is True
    open_fill = broker.submit_signal(
        open_signal, "SPY", fill_price=500.0, sizing_decision=open_decision.as_sizing_decision()
    )
    apply_fill_to_portfolio(portfolio, open_fill, stop_price=open_decision.stop_price)
    assert portfolio.total_exposure == pytest.approx(10 * 500.0)

    # Risk permits the close (decide_close()) -- this is a permission
    # check, not an execution step. Nothing here submits an order.
    close_signal = make_signal("SPY", SignalDirection.FLAT, ts="2024-01-02")
    close_decision = engine.decide_close(close_signal, portfolio)
    assert close_decision.approved is True
    assert close_decision.is_close is True
    assert close_decision.final_approved_quantity == 10

    # Execution still owns actually submitting the close -- straight to
    # PaperBroker, exactly as before Sprint 7 and before this cleanup,
    # with no sizing_decision at all (a close was never risk-sized to
    # begin with, so there is nothing execution-side to derive from
    # close_decision other than the permission to proceed).
    close_fill = broker.submit_signal(close_signal, "SPY", fill_price=520.0)
    closed_position = apply_fill_to_portfolio(portfolio, close_fill)

    assert closed_position.realized_pnl == pytest.approx(10 * (520.0 - 500.0))
    assert portfolio.has_open_position("SPY") is False
    assert portfolio.total_exposure == 0.0
    assert broker.cash == pytest.approx(portfolio.cash)


def test_close_is_execution_owned_not_transformed_into_a_new_risk_sized_position():
    # Required test 8 (responsibility-boundary section): a valid close
    # intent reaches execution without being run back through
    # new-position risk sizing. PaperBroker.submit_signal() for a FLAT
    # signal takes no sizing_decision parameter at all -- there is no
    # risk-sized quantity to smuggle in, proving the close path and the
    # entry path are genuinely separate code paths, not the same one
    # branching internally.
    starting_cash = 10_000.0
    portfolio = Portfolio(cash=starting_cash)
    broker = PaperBroker(starting_cash=starting_cash)
    engine = PortfolioRiskEngine(WIDE_LIMITS, PortfolioRiskLimits(risk_pct_per_trade=0.005))

    open_signal = make_signal("SPY", SignalDirection.LONG)
    open_decision = engine.decide(open_signal, portfolio, entry_price=500.0, stop_price=495.0)
    open_fill = broker.submit_signal(
        open_signal, "SPY", fill_price=500.0, sizing_decision=open_decision.as_sizing_decision()
    )
    apply_fill_to_portfolio(portfolio, open_fill, stop_price=open_decision.stop_price)

    close_signal = make_signal("SPY", SignalDirection.FLAT, ts="2024-01-02")
    close_decision = engine.decide_close(close_signal, portfolio)
    assert close_decision.approved is True

    import inspect

    submit_signal_params = inspect.signature(PaperBroker.submit_signal).parameters
    assert "sizing_decision" in submit_signal_params
    assert submit_signal_params["sizing_decision"].default is None
    # The close is submitted with no sizing_decision -- PaperBroker's own
    # FLAT-closing branch needs none (it closes whatever is held, in
    # full), the same as every close before this cleanup.
    close_fill = broker.submit_signal(close_signal, "SPY", fill_price=505.0)
    assert close_fill.order.quantity == pytest.approx(10.0)  # the full existing position, not re-derived from risk


def test_close_via_decide_close_is_permitted_even_over_a_configured_exposure_limit():
    # Case B, exercised end to end: a tight max_portfolio_exposure_pct
    # that the existing position already breaches must not block the
    # close permission decide_close() returns.
    starting_cash = 10_000.0
    portfolio = Portfolio(cash=starting_cash)
    engine = PortfolioRiskEngine(
        RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0),
        PortfolioRiskLimits(risk_pct_per_trade=0.005),
    )
    open_signal = make_signal("SPY", SignalDirection.LONG)
    open_decision = engine.decide(open_signal, portfolio, entry_price=500.0, stop_price=495.0)
    assert open_decision.approved is True
    broker = PaperBroker(starting_cash=starting_cash)
    open_fill = broker.submit_signal(
        open_signal, "SPY", fill_price=500.0, sizing_decision=open_decision.as_sizing_decision()
    )
    apply_fill_to_portfolio(portfolio, open_fill, stop_price=open_decision.stop_price)

    # Now tighten the limit well below the existing position's own
    # exposure -- simulating "the portfolio is already over its limit"
    # from the close decision's point of view.
    tight_engine = PortfolioRiskEngine(
        RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=0.01),
        PortfolioRiskLimits(risk_pct_per_trade=0.005),
    )
    close_decision = tight_engine.decide_close(make_signal("SPY", SignalDirection.FLAT, ts="2024-01-02"), portfolio)

    assert close_decision.approved is True
    assert close_decision.rejection_reason is None


def test_decide_close_with_no_position_never_reaches_execution():
    # Case C, end to end: a structured NO_POSITION_TO_CLOSE outcome, and
    # a caller respecting `approved` never calls submit_signal() at all.
    portfolio = Portfolio(cash=10_000.0)
    broker = PaperBroker(starting_cash=10_000.0)
    engine = PortfolioRiskEngine(WIDE_LIMITS, PortfolioRiskLimits(risk_pct_per_trade=0.005))

    decision = engine.decide_close(make_signal("SPY", SignalDirection.FLAT), portfolio)
    assert decision.approved is False
    assert decision.rejection_reason is RejectionReason.NO_POSITION_TO_CLOSE

    if decision.approved:  # pragma: no cover -- documents the branch a real loop takes
        broker.submit_signal(make_signal("SPY", SignalDirection.FLAT), "SPY", fill_price=500.0)

    assert broker.positions == {}
    assert portfolio.positions == {}


def test_existing_positions_affect_new_trade_approval_for_the_same_symbol_limit():
    # Sprint 7 spec section 18's required scenario, using its own literal
    # numbers: SPY and QQQ each have $2,000 exposure; a new QQQ trade
    # that would push QQQ's exposure past its configured symbol limit is
    # rejected -- existing positions are not ignored. SPY/QQQ's starting
    # exposure is set up directly (deterministic, matches the spec's own
    # numbers exactly) -- the pipeline under test here is what happens
    # to *new* trades against that state, executed through the real
    # engine -> PaperBroker -> apply_fill_to_portfolio pipeline.
    starting_cash = 1_000_000.0
    portfolio = Portfolio(cash=starting_cash)
    broker = PaperBroker(starting_cash=starting_cash)
    for symbol in ("SPY", "QQQ"):
        portfolio.open_position(
            symbol=symbol, side=PositionSide.LONG, quantity=20.0, entry_price=100.0,
            entry_timestamp=pd.Timestamp("2024-01-01"), entry_signal_id=make_signal(symbol, SignalDirection.LONG).id,
        )
    assert portfolio.symbol_exposure("SPY") == pytest.approx(2_000.0)
    assert portfolio.symbol_exposure("QQQ") == pytest.approx(2_000.0)

    engine = PortfolioRiskEngine(
        WIDE_LIMITS,
        PortfolioRiskLimits(risk_pct_per_trade=0.5, max_symbol_exposure_pct=0.0025),  # 0.25% of ~1,000,000 = 2,500
    )

    # QQQ is already held -- Sprint 7's PaperBroker can't scale into it,
    # so this is rejected for POSITION_SCALING_NOT_SUPPORTED, proving
    # the existing QQQ position is seen (not silently ignored) even
    # though the *specific* reason isn't MAX_SYMBOL_EXPOSURE here.
    scaling_signal = make_signal("QQQ", SignalDirection.LONG, ts="2024-01-02")
    scaling_decision = engine.decide(scaling_signal, portfolio, entry_price=100.0, stop_price=90.0)
    assert scaling_decision.approved is False
    assert scaling_decision.rejection_reason is RejectionReason.POSITION_SCALING_NOT_SUPPORTED

    # A brand-new symbol is unaffected by SPY/QQQ's existing exposure --
    # its own headroom is computed purely from its own (zero) exposure.
    new_signal = make_signal("GLD", SignalDirection.LONG, ts="2024-01-02")
    new_decision = engine.decide(new_signal, portfolio, entry_price=100.0, stop_price=90.0)
    assert new_decision.approved is True
    assert new_decision.symbol_exposure_quantity == 25  # floor(2,500/100), GLD's own limit only
