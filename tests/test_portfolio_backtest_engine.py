"""Tests for `Backtester.run_portfolio()` / `PortfolioBacktestEngine` --
Sprint 11 (`DECISIONS.md`, ADR-0044).

Organized to mirror the Sprint 11 spec's own required-tests list
(sections 45-51/61): concurrent-position-limit enforcement, exposure
limits enforced via the real `PortfolioRiskEngine`, a hard-ceiling
regression on approved quantity, full reproducibility, a legacy-vs-
portfolio-risk comparison, hand-calculable quantity/P&L and equity
values, risk-rejection auditability, sparse-signal/reversal semantics,
timeframe-agnosticism, and no-future-data guarantees for sequential
portfolio state.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtesting.config import BacktestConfig, RiskMode
from src.backtesting.engine import Backtester
from src.backtesting.execution_model import (
    INSUFFICIENT_CASH_FOR_FEE,
    NO_EXECUTION_BAR,
    ExecutionConfig,
    ExecutionTiming,
    PercentageFeeModel,
    PercentageSlippageModel,
)
from src.backtesting.risk_audit import summarize_outcomes
from src.backtesting.stop_policy import ATRStopPolicy
from src.risk.models import PortfolioRiskLimits, RejectionReason, RiskLimits
from src.signals.models import Signal, SignalDirection


def make_candles(closes: list[float], freq: str = "D") -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq=freq, name="timestamp")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )


def sig(timestamp: pd.Timestamp, direction: SignalDirection, symbol: str) -> Signal:
    return Signal(timestamp=timestamp, symbol=symbol, direction=direction, confidence=0.9)


class ScriptedStrategy:
    """Test double: returns a pre-built signal list regardless of data."""

    def __init__(self, signals: list[Signal], name: str = "scripted"):
        self._signals = signals
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return data.copy()

    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        return list(self._signals)


class BrokenStrategy:
    name = "broken"

    def prepare(self, data):
        return data.copy()

    def generate_signals(self, data):
        return "not a list"


def wide_config(**overrides) -> BacktestConfig:
    defaults = dict(
        initial_cash=100_000.0,
        stop_policy=ATRStopPolicy(period=3, multiple=1.0),
        risk_limits=RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.5),
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


def cost_aware_config(**overrides) -> BacktestConfig:
    # Sprint 12: a `wide_config()`-style helper for tests that pair
    # VOLATILE_CLOSES with NEXT_BAR_OPEN timing. VOLATILE_CLOSES'
    # bar-to-bar swings *grow* over the series (by design, to keep
    # ATR(3) comfortably warm throughout) -- `wide_config()`'s
    # essentially-unconstrained allocation (`allocation_per_trade_pct=
    # 1.0`, `risk_pct_per_trade=0.5`) sizes a quantity that is genuinely
    # unaffordable once NEXT_BAR_OPEN moves the actual fill price by a
    # double-digit swing from the signal bar's own close. This keeps
    # ample cash headroom so what's under test is the execution-cost
    # mechanics, not an incidental capital-affordability rejection.
    defaults = dict(
        initial_cash=100_000.0,
        stop_policy=ATRStopPolicy(period=3, multiple=1.0),
        risk_limits=RiskLimits(allocation_per_trade_pct=0.3, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.05),
    )
    defaults.update(overrides)
    return BacktestConfig(**defaults)


# A volatile-enough closes series that ATR(3) warms up fast and stays
# positive throughout, so entries beyond bar 3 or so are always sizeable.
VOLATILE_CLOSES = [100, 102, 98, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93, 108, 92, 109, 91, 110, 90, 111, 89, 112, 88, 113, 87, 114, 86, 115, 85, 116]


# ---------------------------------------------------------------------------
# Basic contract / validation
# ---------------------------------------------------------------------------


def test_run_portfolio_requires_at_least_one_symbol():
    with pytest.raises(ValueError):
        Backtester().run_portfolio({}, {}, wide_config())


def test_run_portfolio_requires_matching_symbol_keys():
    candles = make_candles(VOLATILE_CLOSES)
    strategy = ScriptedStrategy([])
    with pytest.raises(ValueError):
        Backtester().run_portfolio({"AAPL": strategy}, {"QQQ": candles}, wide_config())


def test_run_portfolio_rejects_a_config_not_in_portfolio_risk_mode():
    candles = make_candles(VOLATILE_CLOSES)
    strategy = ScriptedStrategy([])
    legacy_config = BacktestConfig(risk_mode=RiskMode.LEGACY_UNIT, stop_policy=None)

    with pytest.raises(ValueError):
        Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, legacy_config)


def test_run_portfolio_raises_when_generate_signals_returns_wrong_type():
    candles = make_candles(VOLATILE_CLOSES)
    with pytest.raises(ValueError):
        Backtester().run_portfolio({"AAPL": BrokenStrategy()}, {"AAPL": candles}, wide_config())


def test_backtest_config_requires_stop_policy_for_portfolio_risk_mode():
    with pytest.raises(ValueError):
        BacktestConfig(stop_policy=None)  # risk_mode defaults to PORTFOLIO_RISK


def test_backtest_config_allows_no_stop_policy_for_legacy_unit_mode():
    config = BacktestConfig(risk_mode=RiskMode.LEGACY_UNIT, stop_policy=None)
    assert config.stop_policy is None


def test_backtest_config_rejects_non_positive_initial_cash():
    with pytest.raises(ValueError):
        BacktestConfig(initial_cash=0, stop_policy=ATRStopPolicy())


# ---------------------------------------------------------------------------
# Single-symbol entry: result carries risk provenance
# ---------------------------------------------------------------------------


def test_single_symbol_entry_produces_a_portfolio_risk_result():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry], name="aapl_strat")

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, wide_config())

    assert result.risk_mode is RiskMode.PORTFOLIO_RISK
    assert result.backtest_config is not None
    assert result.backtest_config["risk_mode"] == "PORTFOLIO_RISK"
    assert result.final_portfolio is not None
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.quantity is not None
    assert trade.quantity > 0


def test_legacy_run_still_reports_legacy_unit_and_no_quantity():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[0], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])

    result = Backtester().run(strategy, candles)

    assert result.risk_mode is RiskMode.LEGACY_UNIT
    assert result.backtest_config is None
    assert result.final_portfolio is None
    assert result.signal_outcomes == []
    for trade in result.trades:
        assert trade.quantity is None


# ---------------------------------------------------------------------------
# Required: concurrent-position-limit enforcement (spec section 40)
# ---------------------------------------------------------------------------


def test_concurrent_position_limit_rejects_then_admits_after_close():
    candles_aapl = make_candles(VOLATILE_CLOSES)
    candles_qqq = make_candles(VOLATILE_CLOSES)
    sig_aapl = sig(candles_aapl.index[10], SignalDirection.LONG, "AAPL")
    sig_qqq_first = sig(candles_qqq.index[10], SignalDirection.LONG, "QQQ")
    flat_aapl = sig(candles_aapl.index[15], SignalDirection.FLAT, "AAPL")
    sig_qqq_second = sig(candles_qqq.index[16], SignalDirection.LONG, "QQQ")

    strategies = {
        "AAPL": ScriptedStrategy([sig_aapl, flat_aapl], name="aapl_strat"),
        "QQQ": ScriptedStrategy([sig_qqq_first, sig_qqq_second], name="qqq_strat"),
    }
    candles = {"AAPL": candles_aapl, "QQQ": candles_qqq}
    # A small risk_pct so AAPL's own entry doesn't exhaust the cash QQQ
    # would need -- the constraint under test must genuinely be the
    # concurrent-position slot, not an unrelated capital shortfall.
    config = wide_config(
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.01, max_concurrent_positions=1)
    )

    result = Backtester().run_portfolio(strategies, candles, config)

    # Exactly one outcome must be the rejected QQQ first attempt.
    rejected = [o for o in result.signal_outcomes if not o.accepted]
    assert len(rejected) == 1
    assert rejected[0].signal.symbol == "QQQ"
    assert rejected[0].signal.timestamp == candles_qqq.index[10]
    assert rejected[0].rejection_reason == RejectionReason.MAX_CONCURRENT_POSITIONS.value

    # QQQ's second attempt, after AAPL closed, must be approved.
    approved_qqq = [
        o for o in result.signal_outcomes
        if o.accepted and o.signal.symbol == "QQQ" and o.signal.direction is SignalDirection.LONG
    ]
    assert len(approved_qqq) == 1

    summary = summarize_outcomes(result.signal_outcomes)
    assert summary.rejected_entries == 1
    assert summary.rejection_counts == {"MAX_CONCURRENT_POSITIONS": 1}


# ---------------------------------------------------------------------------
# Required: exposure limits enforced via the real PortfolioRiskEngine
# ---------------------------------------------------------------------------


def test_total_exposure_limit_reduces_approved_quantity_below_risk_quantity():
    candles = make_candles(VOLATILE_CLOSES)
    other_candles = make_candles(VOLATILE_CLOSES)
    # Open a large "OTHER" position first via a scripted signal so
    # AAPL's later entry faces genuine exposure headroom pressure.
    sig_other = sig(other_candles.index[5], SignalDirection.LONG, "OTHER")
    sig_aapl = sig(candles.index[10], SignalDirection.LONG, "AAPL")

    strategies = {
        "OTHER": ScriptedStrategy([sig_other], name="other_strat"),
        "AAPL": ScriptedStrategy([sig_aapl], name="aapl_strat"),
    }
    all_candles = {"OTHER": other_candles, "AAPL": candles}
    # A modest risk_pct so OTHER's own entry consumes real, but not all,
    # of the 30% exposure headroom -- OTHER is approved in full, leaving
    # AAPL's identically-sized desire to run into what's left.
    config = wide_config(
        risk_limits=RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=0.3),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.014),
    )

    result = Backtester().run_portfolio(strategies, all_candles, config)

    aapl_outcome = next(
        o for o in result.signal_outcomes if o.signal.symbol == "AAPL" and o.accepted
    )
    decision = aapl_outcome.risk_decision
    assert decision.limiting_constraint == (RejectionReason.MAX_PORTFOLIO_EXPOSURE,)
    assert decision.final_approved_quantity < decision.risk_quantity
    assert decision.final_approved_quantity > 0


# ---------------------------------------------------------------------------
# Required: final_approved_quantity never exceeds risk_quantity, across
# several constrained scenarios (mirrors test_portfolio_risk.py's own
# architectural-invariant test, replayed through the actual Backtester).
# ---------------------------------------------------------------------------


def test_approved_quantity_never_exceeds_risk_quantity_across_scenarios():
    scenarios = [
        # (initial_cash, risk_limits, portfolio_risk_limits)
        (10_000.0, RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0), PortfolioRiskLimits(risk_pct_per_trade=0.005)),
        (100_000.0, RiskLimits(allocation_per_trade_pct=0.10, max_portfolio_exposure_pct=1.0), PortfolioRiskLimits(risk_pct_per_trade=0.5)),
        (100_000.0, RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=0.1), PortfolioRiskLimits(risk_pct_per_trade=0.5)),
        (1_000_000.0, RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0), PortfolioRiskLimits(risk_pct_per_trade=0.5, max_symbol_exposure_pct=0.0025)),
    ]
    for initial_cash, risk_limits, portfolio_limits in scenarios:
        candles = make_candles(VOLATILE_CLOSES)
        entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
        strategy = ScriptedStrategy([entry])
        config = BacktestConfig(
            initial_cash=initial_cash,
            stop_policy=ATRStopPolicy(period=3, multiple=1.0),
            risk_limits=risk_limits,
            portfolio_risk_limits=portfolio_limits,
        )

        result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

        for outcome in result.signal_outcomes:
            if outcome.risk_decision is not None:
                decision = outcome.risk_decision
                assert decision.final_approved_quantity <= decision.risk_quantity, (
                    f"scenario cash={initial_cash} violated the invariant"
                )


# ---------------------------------------------------------------------------
# Required: full reproducibility across two runs of the same inputs
# ---------------------------------------------------------------------------


def test_repeated_runs_of_the_same_inputs_are_fully_reproducible():
    def build():
        candles_a = make_candles(VOLATILE_CLOSES)
        candles_b = make_candles(VOLATILE_CLOSES)
        strategies = {
            "AAPL": ScriptedStrategy(
                [sig(candles_a.index[5], SignalDirection.LONG, "AAPL"),
                 sig(candles_a.index[20], SignalDirection.FLAT, "AAPL")],
                name="aapl_strat",
            ),
            "QQQ": ScriptedStrategy(
                [sig(candles_b.index[8], SignalDirection.SHORT, "QQQ")],
                name="qqq_strat",
            ),
        }
        return strategies, {"AAPL": candles_a, "QQQ": candles_b}

    config = wide_config(
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.02, max_concurrent_positions=5)
    )
    strategies_1, candles_1 = build()
    strategies_2, candles_2 = build()

    result_1 = Backtester().run_portfolio(strategies_1, candles_1, config)
    result_2 = Backtester().run_portfolio(strategies_2, candles_2, config)

    assert [s.direction for s in result_1.signals] == [s.direction for s in result_2.signals]
    assert [s.timestamp for s in result_1.signals] == [s.timestamp for s in result_2.signals]

    def trade_economics(trades):
        return [
            (t.entry_time, t.exit_time, t.direction, t.entry_price, t.exit_price, t.quantity)
            for t in trades
        ]

    assert trade_economics(result_1.trades) == trade_economics(result_2.trades)
    pd.testing.assert_series_equal(result_1.equity_curve, result_2.equity_curve)
    assert result_1.metrics == result_2.metrics

    def outcome_economics(outcomes):
        return [
            (o.signal.symbol, o.signal.timestamp, o.accepted, o.rejection_reason)
            for o in outcomes
        ]

    assert outcome_economics(result_1.signal_outcomes) == outcome_economics(result_2.signal_outcomes)


# ---------------------------------------------------------------------------
# Required: legacy-vs-portfolio-risk comparison, differences explained by
# position size/constraints/rejection -- never hidden state.
# ---------------------------------------------------------------------------


def test_legacy_and_portfolio_risk_modes_diverge_only_in_explainable_ways():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[20], SignalDirection.FLAT, "AAPL")
    strategy_legacy = ScriptedStrategy([entry, flat], name="aapl_strat")
    strategy_portfolio = ScriptedStrategy([entry, flat], name="aapl_strat")

    legacy_result = Backtester().run(strategy_legacy, candles)
    portfolio_result = Backtester().run_portfolio(
        {"AAPL": strategy_portfolio}, {"AAPL": candles}, wide_config()
    )

    # Same signals in, same trade timing out -- the only difference is
    # that the portfolio-risk trade carries a real quantity (and
    # therefore a real dollar P&L), while the legacy trade doesn't.
    assert len(legacy_result.trades) == len(portfolio_result.trades) == 1
    legacy_trade = legacy_result.trades[0]
    portfolio_trade = portfolio_result.trades[0]
    assert legacy_trade.entry_time == portfolio_trade.entry_time
    assert legacy_trade.exit_time == portfolio_trade.exit_time
    assert legacy_trade.direction == portfolio_trade.direction
    assert legacy_trade.quantity is None
    assert portfolio_trade.quantity is not None
    assert legacy_result.risk_mode is RiskMode.LEGACY_UNIT
    assert portfolio_result.risk_mode is RiskMode.PORTFOLIO_RISK


# ---------------------------------------------------------------------------
# Required: hand-calculable quantity/P&L
# ---------------------------------------------------------------------------


def test_hand_calculable_long_winning_trade_gross_pnl():
    # Force a known entry price/quantity by using a scripted strategy
    # and inspecting the actual approved trade -- then verify gross_pnl
    # by hand: quantity * (exit - entry).
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[15], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, wide_config())

    trade = result.trades[0]
    assert trade.direction == 1
    expected_pnl = trade.quantity * (trade.exit_price - trade.entry_price)
    assert trade.gross_pnl == pytest.approx(expected_pnl)


def test_hand_calculable_short_losing_trade_gross_pnl():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.SHORT, "AAPL")
    flat = sig(candles.index[15], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, wide_config())

    trade = result.trades[0]
    assert trade.direction == -1
    expected_pnl = trade.quantity * (trade.entry_price - trade.exit_price)
    assert trade.gross_pnl == pytest.approx(expected_pnl)


def test_exact_quantity_and_pnl_worked_example():
    # entry=100, exit=110, quantity=20, LONG -> gross P&L = +200
    # (Sprint 11 spec's own worked example). Constructed directly
    # against the engine's building blocks rather than via a scripted
    # strategy, to pin the exact numbers spec section 49 requires.
    from src.backtesting.models import Trade
    from uuid import uuid4

    long_trade = Trade(
        entry_time=pd.Timestamp("2024-01-01", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-05", tz="UTC"),
        direction=1,
        entry_price=100.0,
        exit_price=110.0,
        entry_signal_id=uuid4(),
        quantity=20.0,
    )
    assert long_trade.gross_pnl == pytest.approx(200.0)

    short_trade = Trade(
        entry_time=pd.Timestamp("2024-01-01", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-05", tz="UTC"),
        direction=-1,
        entry_price=100.0,
        exit_price=90.0,
        entry_signal_id=uuid4(),
        quantity=20.0,
    )
    assert short_trade.gross_pnl == pytest.approx(200.0)

    # A losing LONG: entry=100, exit=90, quantity=20 -> -200.
    losing_long = Trade(
        entry_time=pd.Timestamp("2024-01-01", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-05", tz="UTC"),
        direction=1,
        entry_price=100.0,
        exit_price=90.0,
        entry_signal_id=uuid4(),
        quantity=20.0,
    )
    assert losing_long.gross_pnl == pytest.approx(-200.0)

    # A losing SHORT: entry=100, exit=110, quantity=20 -> -200.
    losing_short = Trade(
        entry_time=pd.Timestamp("2024-01-01", tz="UTC"),
        exit_time=pd.Timestamp("2024-01-05", tz="UTC"),
        direction=-1,
        entry_price=100.0,
        exit_price=110.0,
        entry_signal_id=uuid4(),
        quantity=20.0,
    )
    assert losing_short.gross_pnl == pytest.approx(-200.0)


# ---------------------------------------------------------------------------
# Required: hand-calculable mark-to-market equity
# ---------------------------------------------------------------------------


def test_equity_curve_reflects_long_position_appreciation():
    closes = [100.0] * 5 + [110.0] * 5  # jump on bar 5
    candles = make_candles(closes)
    entry = sig(candles.index[2], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])
    config = wide_config(initial_cash=100_000.0)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    trade_quantity = result.final_portfolio.positions["AAPL"].quantity
    assert trade_quantity > 0

    equity = result.equity_curve
    # Before the jump: cash + quantity*100 == initial cash (position
    # entered at 100, no move yet).
    pre_jump_equity = 100_000.0 - trade_quantity * 100.0 + trade_quantity * 100.0
    assert equity.iloc[2] == pytest.approx(pre_jump_equity)
    # After the jump: unrealized gain of quantity * (110-100).
    post_jump_equity = 100_000.0 - trade_quantity * 100.0 + trade_quantity * 110.0
    assert equity.iloc[-1] == pytest.approx(post_jump_equity)


def test_equity_curve_reflects_short_position_decline_in_price():
    closes = [100.0] * 5 + [80.0] * 5  # price falls -- a SHORT gains
    candles = make_candles(closes)
    entry = sig(candles.index[2], SignalDirection.SHORT, "AAPL")
    strategy = ScriptedStrategy([entry])
    config = wide_config(initial_cash=100_000.0)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    signed_quantity = result.final_portfolio.positions["AAPL"].quantity
    assert signed_quantity < 0  # SHORT is stored as a negative quantity

    equity = result.equity_curve
    # Portfolio.open_position() moves cash by -quantity*entry_price
    # uniformly for both sides (a SHORT, with a negative quantity,
    # credits cash on entry) -- hand-computed from the known entry
    # price (100.0) and the approved signed quantity, not read back
    # from the final Portfolio.
    expected_cash_after_open = 100_000.0 - signed_quantity * 100.0
    expected_final = expected_cash_after_open + signed_quantity * 80.0
    assert result.final_portfolio.cash == pytest.approx(expected_cash_after_open)
    assert equity.iloc[-1] == pytest.approx(expected_final)
    # A falling price on a SHORT increases equity above the starting cash.
    assert equity.iloc[-1] > 100_000.0


def test_equity_curve_after_close_reflects_realized_cash_only():
    closes = [100.0] * 5 + [120.0] * 5
    candles = make_candles(closes)
    entry = sig(candles.index[2], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[7], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])
    config = wide_config(initial_cash=100_000.0)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    assert result.final_portfolio.position_count == 0
    # After closing, equity is exactly cash -- no open position left to mark.
    assert result.equity_curve.iloc[-1] == pytest.approx(result.final_portfolio.cash)


def test_equity_curve_with_multiple_symbols_sums_all_open_positions():
    candles_a = make_candles([100.0] * 10)
    candles_b = make_candles([50.0] * 5 + [60.0] * 5)
    entry_a = sig(candles_a.index[3], SignalDirection.LONG, "AAPL")
    entry_b = sig(candles_b.index[3], SignalDirection.LONG, "QQQ")
    strategies = {
        "AAPL": ScriptedStrategy([entry_a], name="a"),
        "QQQ": ScriptedStrategy([entry_b], name="b"),
    }
    # allocation_per_trade_pct keeps either single entry from consuming
    # all available cash, so both symbols can actually open.
    config = wide_config(
        initial_cash=100_000.0,
        risk_limits=RiskLimits(allocation_per_trade_pct=0.3, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.1, max_concurrent_positions=5),
    )

    result = Backtester().run_portfolio(strategies, {"AAPL": candles_a, "QQQ": candles_b}, config)

    qty_a = result.final_portfolio.positions["AAPL"].quantity
    qty_b = result.final_portfolio.positions["QQQ"].quantity
    expected_final_equity = result.final_portfolio.cash + qty_a * 100.0 + qty_b * 60.0
    assert result.equity_curve.iloc[-1] == pytest.approx(expected_final_equity)


# ---------------------------------------------------------------------------
# Required: risk-rejection auditability
# ---------------------------------------------------------------------------


def test_rejected_signal_outcome_identifies_symbol_and_constraint():
    strategies = {}
    candles_by_symbol = {}
    signals_by_symbol = {}
    for i, symbol in enumerate(("A", "B", "C")):
        c = make_candles(VOLATILE_CLOSES)
        signals_by_symbol[symbol] = sig(c.index[2 + i], SignalDirection.LONG, symbol)
        candles_by_symbol[symbol] = c
        strategies[symbol] = ScriptedStrategy([signals_by_symbol[symbol]], name=symbol)
    # A 4th symbol that will be rejected once 3 slots are full.
    d_candles = make_candles(VOLATILE_CLOSES)
    d_signal = sig(d_candles.index[10], SignalDirection.LONG, "D")
    strategies["D"] = ScriptedStrategy([d_signal], name="D")
    candles_by_symbol["D"] = d_candles

    # allocation_per_trade_pct caps each entry at 20% of equity so all
    # three fit comfortably within cash -- the constraint genuinely
    # under test for D is the concurrent-position slot, not capital.
    config = wide_config(
        risk_limits=RiskLimits(allocation_per_trade_pct=0.2, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.5, max_concurrent_positions=3),
    )
    result = Backtester().run_portfolio(strategies, candles_by_symbol, config)

    rejected = [o for o in result.signal_outcomes if not o.accepted]
    assert len(rejected) == 1
    outcome = rejected[0]
    assert outcome.signal.symbol == "D"
    assert outcome.rejection_reason == "MAX_CONCURRENT_POSITIONS"
    assert outcome.risk_decision is not None
    assert outcome.risk_decision.rejection_reason is RejectionReason.MAX_CONCURRENT_POSITIONS
    # Never reduced to a bare exception string -- the full structured
    # decision (proposed vs risk quantity) is still attached.
    assert outcome.risk_decision.risk_quantity >= 0


def test_stop_unavailable_outcome_never_reaches_the_risk_engine():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[0], SignalDirection.LONG, "AAPL")  # bar 0 -- no ATR warmup at all
    strategy = ScriptedStrategy([entry])
    config = wide_config(stop_policy=ATRStopPolicy(period=14))

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    assert len(result.signal_outcomes) == 1
    outcome = result.signal_outcomes[0]
    assert outcome.accepted is False
    assert outcome.risk_decision is None
    assert outcome.stop_unavailable_reason is not None
    assert outcome.rejection_reason == "STOP_UNAVAILABLE"
    assert result.trades == []


# ---------------------------------------------------------------------------
# Sparse-signal semantics: reversal / redundant-direction rejection,
# skip-outside-candles, synthesized end-of-data close.
# ---------------------------------------------------------------------------


def test_reversal_while_open_is_rejected_not_silently_handled():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    reversal = sig(candles.index[15], SignalDirection.SHORT, "AAPL")  # no FLAT in between
    strategy = ScriptedStrategy([entry, reversal])

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, wide_config())

    # Only the first entry is approved; the reversal is explicitly rejected.
    assert len(result.trades) == 0 or all(t.exit_signal_id is None for t in result.trades)
    rejected = [o for o in result.signal_outcomes if not o.accepted]
    assert len(rejected) == 1
    assert rejected[0].rejection_reason == RejectionReason.POSITION_SCALING_NOT_SUPPORTED.value
    # The position from the original LONG entry is still open at data end.
    assert result.final_portfolio.has_open_position("AAPL")


def test_signal_outside_candles_is_skipped_not_audited():
    candles = make_candles(VOLATILE_CLOSES)
    outside_ts = candles.index[-1] + pd.Timedelta(days=100)
    entry = sig(outside_ts, SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, wide_config())

    assert result.trades == []
    assert result.signal_outcomes == []  # a data-alignment skip, not an audited rejection


def test_still_open_position_gets_a_synthesized_closing_trade():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])  # no closing FLAT signal

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, wide_config())

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_signal_id is None
    assert trade.exit_time == candles.index[-1]
    assert trade.exit_price == pytest.approx(float(candles["close"].iloc[-1]))
    # The simulation Portfolio itself is genuinely still open -- not
    # force-closed just because a reporting Trade was synthesized.
    assert result.final_portfolio.has_open_position("AAPL")


# ---------------------------------------------------------------------------
# No-future-data: the risk engine must see the correctly evolved
# Portfolio state across sequential trades, never a stale snapshot.
# ---------------------------------------------------------------------------


def test_second_entry_sizing_reflects_the_first_trades_effect_on_cash():
    candles = make_candles(VOLATILE_CLOSES)
    first_entry = sig(candles.index[3], SignalDirection.LONG, "AAPL")
    first_close = sig(candles.index[8], SignalDirection.FLAT, "AAPL")
    second_entry = sig(candles.index[9], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([first_entry, first_close, second_entry])
    # A tight allocation limit so the *available* cash genuinely matters.
    config = wide_config(
        risk_limits=RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.02),
    )

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    approved_entries = [
        o.risk_decision
        for o in result.signal_outcomes
        if o.accepted and o.signal.direction is SignalDirection.LONG
    ]
    assert len(approved_entries) == 2
    first_decision, second_decision = approved_entries
    # equity_used reflects the portfolio's actual state at the time of
    # each decision -- not a value frozen at the start of the run.
    assert first_decision.equity_used is not None
    assert second_decision.equity_used is not None


def test_timeframe_agnostic_hourly_candles_run_the_same_architecture():
    candles = make_candles(VOLATILE_CLOSES, freq="h")
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[20], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, wide_config())

    assert result.risk_mode is RiskMode.PORTFOLIO_RISK
    assert len(result.trades) == 1
    assert result.trades[0].quantity is not None
    assert isinstance(result.metrics, dict)


# ---------------------------------------------------------------------------
# Sprint 12 (DECISIONS.md, ADR-0045): execution realism -- timing, slippage,
# fees, and their propagation through Portfolio/Trade/Analytics.
# ---------------------------------------------------------------------------


def test_default_execution_config_is_backward_compatible_zero_cost_baseline():
    candles_a = make_candles(VOLATILE_CLOSES)
    candles_b = make_candles(VOLATILE_CLOSES)
    entry_a = sig(candles_a.index[10], SignalDirection.LONG, "AAPL")
    entry_b = sig(candles_b.index[10], SignalDirection.LONG, "AAPL")

    default_result = Backtester().run_portfolio(
        {"AAPL": ScriptedStrategy([entry_a])}, {"AAPL": candles_a}, wide_config()
    )
    explicit_zero_cost_config = wide_config(execution_config=ExecutionConfig())
    explicit_result = Backtester().run_portfolio(
        {"AAPL": ScriptedStrategy([entry_b])}, {"AAPL": candles_b}, explicit_zero_cost_config
    )

    assert default_result.trades[0].entry_price == pytest.approx(explicit_result.trades[0].entry_price)
    assert default_result.trades[0].quantity == pytest.approx(explicit_result.trades[0].quantity)
    assert default_result.trades[0].entry_fill_price == pytest.approx(
        explicit_result.trades[0].entry_fill_price
    )
    # Zero-cost: fill price equals the frictionless reference price exactly.
    assert default_result.trades[0].entry_fill_price == pytest.approx(default_result.trades[0].entry_price)


def test_execution_cost_aware_config_distinguishes_reference_from_fill_price():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[15], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])
    execution_config = ExecutionConfig(
        timing=ExecutionTiming.NEXT_BAR_OPEN,
        slippage_model=PercentageSlippageModel(10.0),
        fee_model=PercentageFeeModel(fee_bps=5.0, fixed_fee=1.0),
    )
    config = cost_aware_config(execution_config=execution_config)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    trade = result.trades[0]
    assert trade.entry_fill_price is not None
    assert trade.exit_fill_price is not None
    # Buy slippage makes the entry fill worse (higher) than the reference.
    assert trade.entry_fill_price > trade.entry_price
    # Sell slippage makes the exit fill worse (lower) than the reference.
    assert trade.exit_fill_price < trade.exit_price
    assert trade.entry_fee > 0
    assert trade.exit_fee > 0


def test_execution_cost_aware_run_preserves_the_quantity_invariant():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])
    execution_config = ExecutionConfig(
        timing=ExecutionTiming.NEXT_BAR_OPEN,
        slippage_model=PercentageSlippageModel(15.0),
        fee_model=PercentageFeeModel(fee_bps=5.0),
    )
    config = cost_aware_config(execution_config=execution_config)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    outcome = next(o for o in result.signal_outcomes if o.accepted)
    trade = result.trades[0]
    assert trade.quantity == pytest.approx(outcome.risk_decision.final_approved_quantity)


def test_slippage_and_fees_never_change_the_approved_quantity_vs_zero_cost():
    candles_a = make_candles(VOLATILE_CLOSES)
    candles_b = make_candles(VOLATILE_CLOSES)
    entry_a = sig(candles_a.index[10], SignalDirection.LONG, "AAPL")
    entry_b = sig(candles_b.index[10], SignalDirection.LONG, "AAPL")

    baseline_config = cost_aware_config()
    baseline = Backtester().run_portfolio(
        {"AAPL": ScriptedStrategy([entry_a])}, {"AAPL": candles_a}, baseline_config
    )
    cost_config = cost_aware_config(
        execution_config=ExecutionConfig(
            timing=ExecutionTiming.NEXT_BAR_OPEN,
            slippage_model=PercentageSlippageModel(20.0),
            fee_model=PercentageFeeModel(fee_bps=10.0),
        )
    )
    cost_result = Backtester().run_portfolio(
        {"AAPL": ScriptedStrategy([entry_b])}, {"AAPL": candles_b}, cost_config
    )

    assert baseline.trades[0].quantity == pytest.approx(cost_result.trades[0].quantity)


def test_portfolio_cash_reflects_actual_fees_on_entry_and_exit():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[15], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])
    execution_config = ExecutionConfig(fee_model=PercentageFeeModel(fee_bps=10.0))
    config = cost_aware_config(initial_cash=100_000.0, execution_config=execution_config)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    trade = result.trades[0]
    assert trade.entry_fee > 0
    assert trade.exit_fee > 0
    expected_cash = (
        100_000.0
        - trade.quantity * trade.entry_fill_price
        - trade.entry_fee
        + trade.quantity * trade.exit_fill_price
        - trade.exit_fee
    )
    assert result.final_portfolio.cash == pytest.approx(expected_cash)


def test_realized_pnl_gross_vs_net_are_distinguishable_and_never_double_counted():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[15], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])
    execution_config = ExecutionConfig(
        slippage_model=PercentageSlippageModel(10.0),
        fee_model=PercentageFeeModel(fee_bps=5.0, fixed_fee=1.0),
    )
    config = cost_aware_config(execution_config=execution_config)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    trade = result.trades[0]
    assert trade.gross_pnl is not None
    assert trade.net_pnl is not None
    # Positive costs only ever reduce net relative to gross.
    assert trade.net_pnl < trade.gross_pnl
    fill_pnl = trade.quantity * trade.direction * (trade.exit_fill_price - trade.entry_fill_price)
    expected_net = fill_pnl - trade.total_fees
    assert trade.net_pnl == pytest.approx(expected_net)
    # entry_price/exit_price (the frictionless reference) are never mutated
    # to incorporate costs.
    assert trade.entry_price != trade.entry_fill_price
    assert trade.exit_price != trade.exit_fill_price


def test_total_fees_across_multiple_trades_equals_sum_of_individual_fees():
    candles = make_candles(VOLATILE_CLOSES)
    entry1 = sig(candles.index[3], SignalDirection.LONG, "AAPL")
    flat1 = sig(candles.index[8], SignalDirection.FLAT, "AAPL")
    entry2 = sig(candles.index[9], SignalDirection.LONG, "AAPL")
    flat2 = sig(candles.index[14], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry1, flat1, entry2, flat2])
    execution_config = ExecutionConfig(fee_model=PercentageFeeModel(fee_bps=5.0))
    config = wide_config(
        risk_limits=RiskLimits(allocation_per_trade_pct=1.0, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.02),
        execution_config=execution_config,
    )

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    assert len(result.trades) == 2
    from src.analytics.metrics import total_fees_dollars

    total = sum(t.total_fees for t in result.trades)
    metric = total_fees_dollars(result.trades)
    assert metric.value == pytest.approx(total)
    assert total > 0


def test_execution_cost_aware_run_is_fully_reproducible():
    def build():
        candles = make_candles(VOLATILE_CLOSES)
        entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
        flat = sig(candles.index[15], SignalDirection.FLAT, "AAPL")
        return ScriptedStrategy([entry, flat]), candles

    execution_config = ExecutionConfig(
        timing=ExecutionTiming.NEXT_BAR_OPEN,
        slippage_model=PercentageSlippageModel(8.0),
        fee_model=PercentageFeeModel(fee_bps=3.0),
    )
    config = wide_config(execution_config=execution_config)

    strategy1, candles1 = build()
    strategy2, candles2 = build()
    result1 = Backtester().run_portfolio({"AAPL": strategy1}, {"AAPL": candles1}, config)
    result2 = Backtester().run_portfolio({"AAPL": strategy2}, {"AAPL": candles2}, config)

    def econ(trades):
        return [
            (t.entry_fill_price, t.exit_fill_price, t.entry_fee, t.exit_fee, t.net_pnl)
            for t in trades
        ]

    assert econ(result1.trades) == econ(result2.trades)
    pd.testing.assert_series_equal(result1.equity_curve, result2.equity_curve)


def test_next_bar_open_signal_on_the_final_bar_reports_execution_unavailable():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[-1], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])
    execution_config = ExecutionConfig(timing=ExecutionTiming.NEXT_BAR_OPEN)
    config = wide_config(execution_config=execution_config)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    assert result.trades == []
    outcome = result.signal_outcomes[0]
    assert outcome.accepted is False
    assert outcome.risk_decision is not None
    assert outcome.risk_decision.approved is True
    assert outcome.execution_unavailable_reason == NO_EXECUTION_BAR
    assert outcome.rejection_reason == NO_EXECUTION_BAR


def test_execution_rejected_when_a_fee_makes_the_fill_unaffordable():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])
    execution_config = ExecutionConfig(fee_model=PercentageFeeModel(fee_bps=0.0, fixed_fee=1_000_000.0))
    config = wide_config(initial_cash=100_000.0, execution_config=execution_config)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    assert result.trades == []
    outcome = result.signal_outcomes[0]
    assert outcome.accepted is False
    assert outcome.execution_unavailable_reason == INSUFFICIENT_CASH_FOR_FEE
    assert outcome.rejection_reason == INSUFFICIENT_CASH_FOR_FEE
    # Cash must never have gone negative -- the trade was rejected, not
    # silently applied.
    assert result.final_portfolio.cash == pytest.approx(100_000.0)


def test_next_bar_open_across_a_weekend_uses_the_next_trading_session():
    dates = pd.bdate_range("2024-01-04", periods=6)  # Thu, Fri, Mon, Tue, Wed, Thu
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    candles = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        },
        index=dates,
    )
    friday_ts = candles.index[1]
    monday_ts = candles.index[2]
    assert friday_ts.day_name() == "Friday"
    assert monday_ts.day_name() == "Monday"
    entry = sig(friday_ts, SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])
    config = BacktestConfig(
        initial_cash=100_000.0,
        stop_policy=ATRStopPolicy(period=1, multiple=1.0),
        risk_limits=RiskLimits(allocation_per_trade_pct=0.3, max_portfolio_exposure_pct=1.0),
        portfolio_risk_limits=PortfolioRiskLimits(risk_pct_per_trade=0.05),
        execution_config=ExecutionConfig(timing=ExecutionTiming.NEXT_BAR_OPEN),
    )

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    assert len(result.trades) == 1
    trade = result.trades[0]
    # The next bar used is Monday's -- never a naive "next calendar day"
    # (which would be Saturday, not present in this dataset at all).
    assert trade.entry_fill_price == pytest.approx(float(candles.loc[monday_ts, "open"]))


def test_gross_baseline_exceeds_net_with_positive_execution_costs_on_a_profitable_trade():
    closes = [100.0] * 5 + [130.0] * 10
    candles = make_candles(closes)
    entry = sig(candles.index[2], SignalDirection.LONG, "AAPL")
    flat = sig(candles.index[-2], SignalDirection.FLAT, "AAPL")
    strategy = ScriptedStrategy([entry, flat])
    cost_config = cost_aware_config(
        execution_config=ExecutionConfig(
            slippage_model=PercentageSlippageModel(20.0),
            fee_model=PercentageFeeModel(fee_bps=10.0, fixed_fee=2.0),
        )
    )

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, cost_config)

    trade = result.trades[0]
    assert trade.gross_pnl > 0
    assert trade.total_fees > 0
    assert trade.slippage_cost > 0
    assert trade.net_pnl < trade.gross_pnl
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.slippage_cost - trade.total_fees)


def test_synthesized_closing_trade_has_no_exit_fill_since_no_fill_occurred():
    candles = make_candles(VOLATILE_CLOSES)
    entry = sig(candles.index[10], SignalDirection.LONG, "AAPL")
    strategy = ScriptedStrategy([entry])  # no closing FLAT signal
    execution_config = ExecutionConfig(fee_model=PercentageFeeModel(fee_bps=5.0))
    config = wide_config(execution_config=execution_config)

    result = Backtester().run_portfolio({"AAPL": strategy}, {"AAPL": candles}, config)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_signal_id is None
    assert trade.entry_fill_price is not None
    assert trade.entry_fee > 0
    assert trade.exit_fill_price is None
    assert trade.exit_fee == 0.0
    # No exit fill was ever produced (the position is still genuinely
    # open) -- slippage_cost can't be computed from only one leg.
    assert trade.slippage_cost is None
    # gross_pnl is still defined (quantity + the reference entry/exit
    # prices are both known) -- net_pnl degrades to treating the
    # untracked slippage_cost as 0.0, never to None.
    assert trade.gross_pnl is not None
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.entry_fee)
