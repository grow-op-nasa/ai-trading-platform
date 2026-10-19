"""Tests for the deterministic monitoring layer (src/monitoring/, ADR-0050).

Network-free and deterministic: fixed clocks, `MarketDataObservation`
fixtures (plus a few through a real `MarketDataService` with a fake
provider), in-memory stores. No LLM is involved anywhere in this file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd
import pytest

from src.backtesting.models import Trade
from src.calendar.nyse import NYSECalendar
from src.data.base import DataProvider, Interval
from src.data.exceptions import NoDataError
from src.data.service import MarketDataService
from src.execution.models import Fill, Order, OrderSide
from src.experiments.spec import ExperimentSpec
from src.monitoring import (
    CheckStatus,
    FindingSeverity,
    FindingStatus,
    MonitoringConfig,
    MonitoringService,
    MonitoringStatus,
    MonitoringStore,
    PlatformState,
    StaticStateSource,
)
from src.monitoring.checks import (
    CheckContext,
    DrawdownCheck,
    ExecutionConsistencyCheck,
    MarketDataFreshnessCheck,
    MarketDataIntegrityCheck,
    PortfolioValuationCheck,
    RiskExposureCheck,
    StrategyModelCompatibilityCheck,
    evaluate_freshness,
)
from src.monitoring.models import (
    CheckResult,
    FindingCandidate,
    fingerprint,
)
from src.monitoring.state import (
    ExecutionRecords,
    MarketDataObservation,
    MarketDataTarget,
    ModelInfo,
    observe_market_data,
)
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionSide
from src.reconciliation.models import FillReconciliation
from src.risk.models import PortfolioRiskLimits, RiskLimits
from src.signals.models import Signal, SignalDirection
from src.strategies.identity import strategy_version
from src.strategies.registry import get_strategy_class
import src.strategies  # noqa: F401  (registers the built-in strategies)

CAL = NYSECalendar()
CFG = MonitoringConfig()


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def ctx(now):
    return CheckContext(now=now, calendar=CAL)


def obs(symbol="QQQ", interval=Interval.DAY_1, end=None, ok=True, rows=100, gaps=0, last_close=100.0,
        content_hash="abc", code=None):
    if not ok:
        return MarketDataObservation(symbol, interval, False, utc(2026, 10, 7), error_code=code or "NoDataError",
                                     error_message="no data")
    return MarketDataObservation(
        symbol, interval, True, utc(2026, 10, 7), rows=rows,
        dataset_start=pd.Timestamp(end) - pd.Timedelta(days=30) if end is not None else None,
        dataset_end=pd.Timestamp(end) if end is not None else None,
        content_hash=content_hash, gap_count=gaps, last_close=last_close,
    )


def state_with(*observations, **kw):
    s = PlatformState(as_of=utc(2026, 10, 7), source="test", **kw)
    for o in observations:
        t = MarketDataTarget(o.symbol, o.interval)
        s.market_data_targets.append(t)
        s.market_data[t.key] = o
    return s


# ---------------------------------------------------------------------------
# Models / configuration
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_and_ignores_volatile_values():
    a = FindingCandidate("r", "c", FindingSeverity.WARNING, "t", "d", observed_value=1, entities={"symbol": "QQQ"})
    b = FindingCandidate("r", "c", FindingSeverity.CRITICAL, "other", "other", observed_value=999, entities={"symbol": "QQQ"})
    c = FindingCandidate("r", "c", FindingSeverity.WARNING, "t", "d", entities={"symbol": "SPY"})
    assert a.fingerprint == b.fingerprint == fingerprint("r", {"symbol": "QQQ"})
    assert a.fingerprint != c.fingerprint


def test_config_defaults_are_valid_and_invalid_values_are_rejected():
    assert MonitoringConfig().to_dict()["drawdown_warning"] == 0.10
    bad = [
        dict(daily_stale_warning_sessions=0),
        dict(daily_stale_warning_sessions=5, daily_stale_critical_sessions=2),
        dict(intraday_stale_warning_bars=9, intraday_stale_critical_bars=3),
        dict(drawdown_warning=0.3, drawdown_critical=0.2),
        dict(drawdown_warning=0.0),
        dict(daily_close_grace_minutes=-1),
        dict(exposure_warning_ratio=1.5),
        dict(min_bars=0),
    ]
    for kw in bad:
        with pytest.raises(ValueError):
            MonitoringConfig(**kw)


def test_finding_requires_timezone_aware_timestamps():
    from src.monitoring.models import MonitorFinding

    with pytest.raises(ValueError):
        MonitorFinding("i", "f", "r", "c", FindingSeverity.WARNING, FindingStatus.OPEN, "t", "d", None, None, {},
                       datetime(2026, 1, 1), datetime(2026, 1, 1), 1, [], "v", "run")


# ---------------------------------------------------------------------------
# Market-data freshness (session-aware)
# ---------------------------------------------------------------------------

DAILY_NOW = utc(2026, 10, 7, 21, 30)  # Wed 17:30 ET, after close + 60 min grace


def fresh(o, now):
    return evaluate_freshness(o, CFG, ctx(now))


def test_daily_bar_for_the_current_session_is_fresh():
    assert fresh(obs(end=utc(2026, 10, 7)), DAILY_NOW).status == CheckStatus.HEALTHY


def test_daily_one_missing_session_warns_and_three_are_critical():
    assert fresh(obs(end=utc(2026, 10, 6)), DAILY_NOW).status == CheckStatus.WARNING
    r = fresh(obs(end=utc(2026, 10, 2)), DAILY_NOW)  # Fri; Mon/Tue/Wed missing
    assert r.status == CheckStatus.CRITICAL and r.data["missing_sessions"] == 3


def test_daily_bar_not_yet_expected_before_close_plus_grace():
    now = utc(2026, 10, 7, 20, 30)  # 16:30 ET, inside the 60-minute grace
    assert fresh(obs(end=utc(2026, 10, 6)), now).status == CheckStatus.HEALTHY


def test_overnight_does_not_raise_a_false_stale_alert():
    now = utc(2026, 10, 8, 12, 0)  # Thursday 08:00 ET, before the open
    assert fresh(obs(end=utc(2026, 10, 7)), now).status == CheckStatus.HEALTHY


def test_weekend_does_not_raise_a_false_stale_alert():
    now = utc(2026, 10, 10, 15, 0)  # Saturday
    assert fresh(obs(end=utc(2026, 10, 9)), now).status == CheckStatus.HEALTHY
    assert fresh(obs(end=utc(2026, 10, 8)), now).status == CheckStatus.WARNING  # a genuinely missed Friday


def test_exchange_holiday_does_not_raise_a_false_stale_alert():
    now = utc(2026, 11, 26, 18, 0)  # Thanksgiving Day: market closed
    assert fresh(obs(end=utc(2026, 11, 25)), now).status == CheckStatus.HEALTHY


def test_intraday_stale_while_open_and_fresh_when_current():
    now = utc(2026, 10, 7, 15, 0)  # 11:00 ET, regular session open
    five = Interval.MINUTE_5
    assert fresh(obs(interval=five, end=utc(2026, 10, 7, 14, 55)), now).status == CheckStatus.HEALTHY
    assert fresh(obs(interval=five, end=utc(2026, 10, 7, 14, 40)), now).status == CheckStatus.WARNING
    assert fresh(obs(interval=five, end=utc(2026, 10, 7, 14, 5)), now).status == CheckStatus.CRITICAL


def test_intraday_after_close_is_measured_from_the_close_not_from_now():
    now = utc(2026, 10, 7, 23, 0)  # three hours after the close
    five = Interval.MINUTE_5
    r = fresh(obs(interval=five, end=utc(2026, 10, 7, 19, 55)), now)
    assert r.status == CheckStatus.HEALTHY and r.data["market_open"] is False


def test_intraday_weekend_fresh_through_friday_close():
    now = utc(2026, 10, 10, 15, 0)
    assert fresh(obs(interval=Interval.MINUTE_5, end=utc(2026, 10, 9, 19, 55)), now).status == CheckStatus.HEALTHY
    assert fresh(obs(interval=Interval.MINUTE_5, end=utc(2026, 10, 9, 17, 0)), now).status == CheckStatus.CRITICAL


def test_missing_timestamp_unavailable_dataset_and_unsupported_timeframe_are_not_evaluable():
    assert fresh(obs(end=None), DAILY_NOW).status == CheckStatus.NOT_EVALUABLE
    assert fresh(obs(ok=False), DAILY_NOW).status == CheckStatus.NOT_EVALUABLE
    weekly = fresh(obs(interval=Interval.WEEK_1, end=utc(2026, 10, 2)), DAILY_NOW)
    assert weekly.status == CheckStatus.NOT_EVALUABLE and "not defined" in weekly.detail


def test_calendar_year_outside_verified_range_is_not_evaluable():
    assert fresh(obs(end=utc(2040, 1, 2)), utc(2040, 1, 3, 22, 0)).status == CheckStatus.NOT_EVALUABLE


def test_freshness_check_emits_a_finding_with_evidence_and_checks_scope():
    s = state_with(obs(end=utc(2026, 10, 6)))
    res = MarketDataFreshnessCheck().run(s, CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.WARNING
    (cand,) = res.candidates
    assert cand.rule_id == "market_data.stale" and cand.entities == {"symbol": "QQQ", "interval": "1d"}
    assert cand.evidence[0].reference == "market_data:QQQ:1d"
    assert cand.fingerprint in res.checked


def test_freshness_check_without_targets_or_observations_is_not_evaluable():
    assert MarketDataFreshnessCheck().run(state_with(), CFG, ctx(DAILY_NOW)).status == CheckStatus.NOT_EVALUABLE
    s = state_with(obs(ok=False))
    assert MarketDataFreshnessCheck().run(s, CFG, ctx(DAILY_NOW)).status == CheckStatus.NOT_EVALUABLE


# ---------------------------------------------------------------------------
# Market-data integrity
# ---------------------------------------------------------------------------


def test_integrity_retrieval_failure_is_critical_with_error_code():
    res = MarketDataIntegrityCheck().run(state_with(obs(ok=False, code="DataProviderError")), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.CRITICAL
    assert res.candidates[0].rule_id == "market_data.retrieval_failed"
    assert res.candidates[0].observed_value["error_code"] == "DataProviderError"


def test_integrity_insufficient_gaps_and_missing_identity_warn():
    o = obs(end=utc(2026, 10, 7), rows=5, gaps=2, content_hash="")
    res = MarketDataIntegrityCheck().run(state_with(o), CFG, ctx(DAILY_NOW))
    assert {c.rule_id for c in res.candidates} == {
        "market_data.insufficient_data", "market_data.coverage_gaps", "market_data.identity_unavailable"}
    assert res.status == CheckStatus.WARNING


def test_integrity_clean_dataset_is_healthy():
    res = MarketDataIntegrityCheck().run(state_with(obs(end=utc(2026, 10, 7))), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.HEALTHY and res.candidates == []


class _Provider(DataProvider):
    def __init__(self, frame=None, raises=None):
        self.frame, self.raises = frame, raises

    def fetch_candles(self, symbol, start, end, interval):
        if self.raises:
            raise self.raises
        return self.frame


def _frame(n=40, start="2026-08-03"):
    idx = pd.date_range(start, periods=n, freq="B", name="timestamp")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000}, index=idx)


def test_observe_market_data_goes_through_market_data_service(tmp_path):
    svc = MarketDataService(provider=_Provider(frame=_frame()), cache_dir=tmp_path, use_cache=False)
    o = observe_market_data(svc, MarketDataTarget("QQQ", Interval.DAY_1), utc(2026, 9, 25), 60)
    assert o.ok and o.rows == 40 and o.content_hash and o.last_close == 100.5


def test_observe_market_data_turns_provider_errors_into_structured_observations(tmp_path):
    svc = MarketDataService(provider=_Provider(raises=NoDataError("none")), cache_dir=tmp_path, use_cache=False)
    o = observe_market_data(svc, MarketDataTarget("QQQ", Interval.DAY_1), utc(2026, 9, 25), 60)
    assert not o.ok and o.error_code == "NoDataError"
    svc2 = MarketDataService(provider=_Provider(raises=RuntimeError("secret-token-xyz boom")), cache_dir=tmp_path, use_cache=False)
    o2 = observe_market_data(svc2, MarketDataTarget("QQQ", Interval.DAY_1), utc(2026, 9, 25), 60)
    assert not o2.ok and o2.error_code.startswith("UNEXPECTED:")


# ---------------------------------------------------------------------------
# Portfolio valuation (read-only)
# ---------------------------------------------------------------------------


def _position(symbol="QQQ", qty=10, price=100.0, side=PositionSide.LONG):
    return Position(symbol=symbol, side=side, quantity=qty if side == PositionSide.LONG else -qty,
                    entry_price=price, entry_timestamp=pd.Timestamp("2026-10-01", tz="UTC"), entry_signal_id=uuid4())


def _portfolio(*positions, cash=9000.0):
    return Portfolio.reconstruct(cash=cash, positions=list(positions))


def test_valuation_healthy_with_current_price_and_no_mutation():
    p = _portfolio(_position())
    before = (p.cash, p.positions["QQQ"].current_price, p.equity)
    s = state_with(obs(end=utc(2026, 10, 7)), portfolio=p)
    res = PortfolioValuationCheck().run(s, CFG, ctx(DAILY_NOW))
    PortfolioValuationCheck().run(s, CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.HEALTHY
    assert (p.cash, p.positions["QQQ"].current_price, p.equity) == before  # read-only


def test_valuation_missing_price_is_a_warning_and_never_zero():
    s = state_with(obs(ok=False), portfolio=_portfolio(_position()))
    res = PortfolioValuationCheck().run(s, CFG, ctx(DAILY_NOW))
    rules = {c.rule_id for c in res.candidates}
    assert "valuation.missing_price" in rules and "valuation.unavailable" in rules
    assert res.status == CheckStatus.CRITICAL  # nothing priced at all


def test_valuation_stale_input_is_flagged():
    s = state_with(obs(end=utc(2026, 10, 1)), portfolio=_portfolio(_position()))
    res = PortfolioValuationCheck().run(s, CFG, ctx(DAILY_NOW))
    assert "valuation.stale_input" in {c.rule_id for c in res.candidates}


def test_valuation_no_positions_healthy_and_no_portfolio_not_evaluable():
    assert PortfolioValuationCheck().run(state_with(portfolio=_portfolio()), CFG, ctx(DAILY_NOW)).status == CheckStatus.HEALTHY
    assert PortfolioValuationCheck().run(state_with(), CFG, ctx(DAILY_NOW)).status == CheckStatus.NOT_EVALUABLE


# ---------------------------------------------------------------------------
# Risk / exposure
# ---------------------------------------------------------------------------


def _exposure_state(qty, limit=0.5):
    # equity = cash + qty*100; exposure = qty*100
    p = _portfolio(_position(qty=qty), cash=10000.0 - qty * 100)
    return state_with(portfolio=p, risk_limits=RiskLimits(max_portfolio_exposure_pct=limit))


def test_exposure_below_at_and_above_the_ceiling():
    below = RiskExposureCheck().run(_exposure_state(10), CFG, ctx(DAILY_NOW))   # 10%/50%
    at = RiskExposureCheck().run(_exposure_state(50), CFG, ctx(DAILY_NOW))      # 50%/50%
    above = RiskExposureCheck().run(_exposure_state(80), CFG, ctx(DAILY_NOW))   # 80%/50%
    assert below.status == CheckStatus.HEALTHY
    assert at.status == CheckStatus.WARNING
    assert above.status == CheckStatus.CRITICAL


def test_exposure_without_limits_is_not_evaluable():
    s = state_with(portfolio=_portfolio(_position()))
    assert RiskExposureCheck().run(s, CFG, ctx(DAILY_NOW)).status == CheckStatus.NOT_EVALUABLE


def test_invalid_risk_configuration_is_a_critical_finding():
    s = state_with(portfolio=_portfolio(_position()), risk_config_error="ValueError: max_portfolio_exposure_pct")
    res = RiskExposureCheck().run(s, CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.CRITICAL and res.candidates[0].rule_id == "risk.invalid_config"


def test_symbol_and_concurrency_limits_use_portfolio_methods():
    p = _portfolio(_position("QQQ", 40), _position("SPY", 10), cash=5000.0)
    s = state_with(portfolio=p, portfolio_risk_limits=PortfolioRiskLimits(max_symbol_exposure_pct=0.2,
                                                                         max_concurrent_positions=2))
    res = RiskExposureCheck().run(s, CFG, ctx(DAILY_NOW))
    rules = {(c.rule_id, c.severity) for c in res.candidates}
    assert ("risk.symbol_exposure", FindingSeverity.CRITICAL) in rules   # QQQ way over
    assert ("risk.concurrent_positions", FindingSeverity.WARNING) in rules  # at the cap


def test_short_positions_carry_a_not_modeled_caveat_not_a_safety_claim():
    p = _portfolio(_position(side=PositionSide.SHORT), cash=10000.0)
    s = state_with(portfolio=p, risk_limits=RiskLimits())
    res = RiskExposureCheck().run(s, CFG, ctx(DAILY_NOW))
    assert any("NOT_MODELED" in n for n in res.notes)


def test_a_normal_risk_rejection_is_not_a_fault_but_a_contradiction_is():
    from src.risk.models import RejectionReason, RiskDecision

    def decision(approved, final, risk_q):
        return RiskDecision(
            approved=approved, symbol="QQQ", direction=SignalDirection.LONG, entry_price=100.0, stop_price=95.0,
            equity_used=10000.0, risk_pct=0.01, risk_amount=100.0, requested_quantity=20, risk_quantity=risk_q,
            capital_quantity=None, allocation_quantity=None, portfolio_exposure_quantity=None,
            symbol_exposure_quantity=None, final_approved_quantity=final, limiting_constraint=None,
            rejection_reason=None if approved else RejectionReason.NO_POSITION_TO_CLOSE,
            resulting_exposure=None, explanation="x")

    p = _portfolio(_position())
    ok = state_with(portfolio=p, risk_decisions=[decision(False, 0, 20)])
    bad = state_with(portfolio=p, risk_decisions=[decision(True, 30, 20)])
    assert RiskExposureCheck().run(ok, CFG, ctx(DAILY_NOW)).status == CheckStatus.HEALTHY
    res = RiskExposureCheck().run(bad, CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.CRITICAL and res.candidates[0].rule_id == "risk.contradictory_decision"


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


def _curve(values):
    return pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values), freq="D", tz="UTC"))


def test_drawdown_below_warning_and_critical_thresholds():
    up = [100.0 + i for i in range(30)]
    assert DrawdownCheck().run(state_with(equity_curve=_curve(up)), CFG, ctx(DAILY_NOW)).status == CheckStatus.HEALTHY
    warn = up + [up[-1] * 0.88]
    res = DrawdownCheck().run(state_with(equity_curve=_curve(warn)), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.WARNING
    crit = up + [up[-1] * 0.75]
    assert DrawdownCheck().run(state_with(equity_curve=_curve(crit)), CFG, ctx(DAILY_NOW)).status == CheckStatus.CRITICAL


def test_drawdown_recovers_when_the_curve_recovers():
    curve = [100.0] * 25 + [80.0] + [100.0] * 5
    res = DrawdownCheck().run(state_with(equity_curve=_curve(curve)), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.HEALTHY  # the *current* drawdown is zero


def test_drawdown_without_history_is_not_evaluable_never_fabricated():
    for curve in (None, _curve([]), _curve([100.0, 90.0, 80.0]), _curve([100.0] * 25 + [float("nan")])):
        res = DrawdownCheck().run(state_with(equity_curve=curve), CFG, ctx(DAILY_NOW))
        assert res.status == CheckStatus.NOT_EVALUABLE and not res.candidates


# ---------------------------------------------------------------------------
# Execution / fill consistency
# ---------------------------------------------------------------------------


def _order(sid=None, qty=10.0, ts="2026-10-05"):
    return Order(symbol="QQQ", side=OrderSide.BUY, quantity=qty, signal_id=sid or uuid4(), timestamp=pd.Timestamp(ts, tz="UTC"))


def test_valid_orders_and_fills_are_healthy():
    o = _order()
    rec = ExecutionRecords(orders=[o], fills=[Fill(order=o, fill_price=100.0, cash_delta=-1000.0,
                                                   fill_timestamp=pd.Timestamp("2026-10-06", tz="UTC"))],
                           approved_quantity_by_signal_id={str(o.signal_id): 10.0})
    assert ExecutionConsistencyCheck().run(state_with(execution=rec), CFG, ctx(DAILY_NOW)).status == CheckStatus.HEALTHY


def test_each_provable_execution_anomaly_is_detected():
    o = _order()
    other = _order()
    over = ExecutionRecords(orders=[o], fills=[Fill(o, 100.0, -1000.0)], approved_quantity_by_signal_id={str(o.signal_id): 5.0})
    unknown = ExecutionRecords(orders=[o], fills=[Fill(other, 100.0, -1000.0)])
    dup = ExecutionRecords(orders=[o], fills=[Fill(o, 100.0, -1000.0), Fill(o, 100.0, -1000.0)])
    early = ExecutionRecords(orders=[o], fills=[Fill(o, 100.0, -1000.0, fill_timestamp=pd.Timestamp("2026-10-01", tz="UTC"))])
    expect = {"execution.quantity_exceeds_approved": over, "execution.unknown_order": unknown,
              "execution.duplicate_fill": dup, "execution.invalid_chronology": early}
    for rule, rec in expect.items():
        res = ExecutionConsistencyCheck().run(state_with(execution=rec), CFG, ctx(DAILY_NOW))
        assert rule in {c.rule_id for c in res.candidates}, rule
        assert res.status == CheckStatus.CRITICAL


def test_trade_chronology_and_unknown_signal_are_detected_from_registry_data():
    sig = Signal(timestamp=pd.Timestamp("2026-10-01", tz="UTC"), symbol="QQQ", direction=SignalDirection.LONG, confidence=0.5)
    good = Trade(entry_time=pd.Timestamp("2026-10-01"), exit_time=pd.Timestamp("2026-10-03"), direction=1,
                 entry_price=100.0, exit_price=101.0, entry_signal_id=sig.id)
    backwards = Trade(entry_time=pd.Timestamp("2026-10-04"), exit_time=pd.Timestamp("2026-10-03"), direction=1,
                      entry_price=100.0, exit_price=101.0, entry_signal_id=uuid4())
    res = ExecutionConsistencyCheck().run(state_with(trades=[good, backwards], signals=[sig]), CFG, ctx(DAILY_NOW))
    assert {c.rule_id for c in res.candidates} == {"trade.invalid_chronology", "trade.unknown_entry_signal"}
    ok = ExecutionConsistencyCheck().run(state_with(trades=[good], signals=[sig]), CFG, ctx(DAILY_NOW))
    assert ok.status == CheckStatus.HEALTHY


def _recon(shortfall=0.0, slip=0.0):
    return FillReconciliation(symbol="QQQ", real_broker_order_id="b1", simulated_price=100.0, real_price=100.0,
                              price_slippage_per_share=0.0, price_slippage_pct=slip, simulated_quantity=10.0,
                              real_filled_quantity=10.0 - shortfall, quantity_shortfall=shortfall, cost_impact=0.0)


def test_reconciliation_discrepancies_come_from_the_reconciliation_service_output():
    quiet = ExecutionConsistencyCheck().run(state_with(reconciliations=[_recon()]), CFG, ctx(DAILY_NOW))
    loud = ExecutionConsistencyCheck().run(state_with(reconciliations=[_recon(shortfall=2.0)]), CFG, ctx(DAILY_NOW))
    assert quiet.status == CheckStatus.HEALTHY
    assert loud.status == CheckStatus.WARNING and loud.candidates[0].rule_id == "reconciliation.material_discrepancy"


def test_execution_check_without_any_records_is_not_evaluable():
    assert ExecutionConsistencyCheck().run(state_with(), CFG, ctx(DAILY_NOW)).status == CheckStatus.NOT_EVALUABLE


# ---------------------------------------------------------------------------
# Strategy / model compatibility
# ---------------------------------------------------------------------------


def _spec(name="ema_cross", version=None, params=None):
    cls = get_strategy_class(name)
    return ExperimentSpec(
        strategy_name=name, strategy_version=version or strategy_version(cls), strategy_params=params or {},
        symbol="QQQ", interval=Interval.DAY_1, dataset_start=pd.Timestamp("2026-01-01"),
        dataset_end=pd.Timestamp("2026-06-01"), dataset_source="test", dataset_fingerprint="fp", risk_config={})


def test_registered_strategy_with_matching_version_is_healthy():
    res = StrategyModelCompatibilityCheck().run(state_with(spec=_spec()), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.HEALTHY


def test_version_drift_warns_and_unregistered_strategy_is_critical():
    res = StrategyModelCompatibilityCheck().run(state_with(spec=_spec(version="old-version")), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.WARNING and res.candidates[0].rule_id == "strategy.version_drift"
    missing = _spec()
    object.__setattr__(missing, "strategy_name", "does_not_exist")
    res = StrategyModelCompatibilityCheck().run(state_with(spec=missing), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.CRITICAL and res.candidates[0].rule_id == "strategy.missing_implementation"


def test_model_unavailable_hash_mismatch_and_context_mismatch():
    spec = _spec(params={"model_id": "m1"})
    s_missing = state_with(spec=spec, models={"m1": None})
    assert StrategyModelCompatibilityCheck().run(s_missing, CFG, ctx(DAILY_NOW)).candidates[0].rule_id == "model.unavailable"
    s_hash = state_with(spec=spec, models={"m1": ModelInfo("m1", "aaa", "bbb", "QQQ", "1d")})
    assert "model.artifact_hash_mismatch" in {c.rule_id for c in StrategyModelCompatibilityCheck().run(s_hash, CFG, ctx(DAILY_NOW)).candidates}
    s_ctx = state_with(spec=spec, models={"m1": ModelInfo("m1", "aaa", "aaa", "SPY", "1d")})
    assert "model.context_mismatch" in {c.rule_id for c in StrategyModelCompatibilityCheck().run(s_ctx, CFG, ctx(DAILY_NOW)).candidates}
    s_ok = state_with(spec=spec, models={"m1": ModelInfo("m1", "aaa", "aaa", "QQQ", "1d")})
    assert StrategyModelCompatibilityCheck().run(s_ok, CFG, ctx(DAILY_NOW)).status == CheckStatus.HEALTHY


def test_no_spec_means_no_context_not_a_fabricated_alert():
    res = StrategyModelCompatibilityCheck().run(state_with(), CFG, ctx(DAILY_NOW))
    assert res.status == CheckStatus.NOT_EVALUABLE and not res.candidates


# ---------------------------------------------------------------------------
# Service: lifecycle, dedup, persistence
# ---------------------------------------------------------------------------


class ScriptedCheck:
    """A check whose output the test controls."""

    check_id = "scripted"

    def __init__(self):
        self.mode = "bad"  # "bad" | "good" | "unknown" | "raise"

    def run(self, state, config, ctx):
        if self.mode == "raise":
            raise RuntimeError("boom")
        if self.mode == "unknown":
            return CheckResult.not_evaluable(self.check_id, "no data this time")
        res = CheckResult(self.check_id, CheckStatus.HEALTHY)
        res.mark_checked("scripted.rule", symbol="QQQ")
        if self.mode == "bad":
            res.candidates.append(FindingCandidate("scripted.rule", "test", FindingSeverity.CRITICAL, "bad", "desc",
                                                   entities={"symbol": "QQQ"}))
        res.finalize_status()
        return res


class Clock:
    def __init__(self):
        self.t = utc(2026, 10, 7, 12, 0)

    def __call__(self):
        self.t += pd.Timedelta(minutes=5)
        return self.t.to_pydatetime() if hasattr(self.t, "to_pydatetime") else self.t


def _service(check=None, store=None, state=None):
    chk = check or ScriptedCheck()
    store = store or MonitoringStore(":memory:")
    svc = MonitoringService(StaticStateSource(state or PlatformState(as_of=utc(2026, 10, 7), source="t")),
                            store, checks=[chk], clock=Clock())
    return svc, chk, store


def test_new_finding_is_created_open_and_persisted():
    svc, _, store = _service()
    run = svc.run_once()
    assert run.persisted and run.status == MonitoringStatus.CRITICAL
    (f,) = store.list_findings()
    assert f.status == FindingStatus.OPEN and f.occurrence_count == 1 and f.finding_id in run.finding_ids


def test_repeated_finding_is_deduplicated_and_counters_advance():
    svc, _, store = _service()
    first = svc.run_once()
    second = svc.run_once()
    findings = store.list_findings()
    assert len(findings) == 1
    f = findings[0]
    assert f.occurrence_count == 2 and f.last_seen > f.first_seen
    assert first.finding_ids == second.finding_ids


def test_finding_resolves_only_after_a_valid_clean_observation():
    svc, chk, store = _service()
    svc.run_once()
    chk.mode = "good"
    run = svc.run_once()
    (f,) = store.list_findings()
    assert f.status == FindingStatus.RESOLVED and f.resolved_at is not None
    assert f.finding_id in run.resolved_finding_ids and run.status == MonitoringStatus.HEALTHY


def test_a_not_evaluable_run_does_not_resolve_an_open_critical_finding():
    svc, chk, store = _service()
    svc.run_once()
    chk.mode = "unknown"
    run = svc.run_once()
    (f,) = store.list_findings()
    assert f.status == FindingStatus.OPEN and f.severity == FindingSeverity.CRITICAL
    assert run.status == MonitoringStatus.UNKNOWN and run.resolved_finding_ids == []


def test_a_raising_check_degrades_to_not_evaluable_and_never_resolves():
    svc, chk, store = _service()
    svc.run_once()
    chk.mode = "raise"
    run = svc.run_once()
    assert run.checks[0].status == CheckStatus.NOT_EVALUABLE and any("boom" in e for e in run.errors)
    assert store.list_findings()[0].status == FindingStatus.OPEN


def test_reappearing_condition_after_resolution_is_a_new_open_finding():
    svc, chk, store = _service()
    svc.run_once()
    chk.mode = "good"
    svc.run_once()
    chk.mode = "bad"
    svc.run_once()
    statuses = sorted(f.status.value for f in store.list_findings())
    assert statuses == ["OPEN", "RESOLVED"]


def test_store_failure_is_reported_never_claimed_as_persisted():
    class Broken(MonitoringStore):
        def apply(self, run, findings):
            raise OSError("disk full")

    svc, _, _ = _service(store=Broken(":memory:"))
    run = svc.run_once()
    assert run.persisted is False and "disk full" in run.persistence_error
    assert any("persistence failed" in e for e in run.errors)


def test_state_source_failure_yields_an_unknown_run_not_a_crash():
    class Bad:
        def load(self, now):
            raise RuntimeError("registry offline")

    svc = MonitoringService(Bad(), MonitoringStore(":memory:"), checks=[ScriptedCheck()])
    run = svc.run_once()
    assert any("registry offline" in e for e in run.errors) and run.persisted


def test_full_default_checks_with_empty_state_are_all_not_evaluable():
    svc = MonitoringService(StaticStateSource(PlatformState(as_of=utc(2026, 10, 7), source="t", notes=["empty"])),
                            MonitoringStore(":memory:"))
    run = svc.run_once()
    assert run.status == MonitoringStatus.UNKNOWN
    assert run.checks_run == 7 and run.unknown_checks == 7 and run.checks_passed == 0


def test_service_gathers_market_data_through_the_market_data_service_only(tmp_path):
    frame = _frame(40, start="2026-08-03")
    market = MarketDataService(provider=_Provider(frame=frame), cache_dir=tmp_path, use_cache=False)
    state = PlatformState(as_of=utc(2026, 9, 25), source="t",
                          market_data_targets=[MarketDataTarget("QQQ", Interval.DAY_1)])
    svc = MonitoringService(StaticStateSource(state), MonitoringStore(":memory:"), market_data=market,
                            clock=lambda: utc(2026, 9, 25, 22, 0))
    run = svc.run_once()
    by_id = {c.check_id: c for c in run.checks}
    assert by_id["market_data_integrity"].status == CheckStatus.HEALTHY
    assert "QQQ:1d" in state.market_data


def test_without_a_market_data_service_market_checks_are_not_evaluable():
    state = PlatformState(as_of=utc(2026, 10, 7), source="t", market_data_targets=[MarketDataTarget("QQQ", Interval.DAY_1)])
    run = MonitoringService(StaticStateSource(state), MonitoringStore(":memory:")).run_once()
    by_id = {c.check_id: c for c in run.checks}
    assert by_id["market_data_freshness"].status == CheckStatus.NOT_EVALUABLE
    assert "MarketDataService" in by_id["market_data_freshness"].reason


def test_monitoring_never_mutates_the_portfolio_across_repeated_runs():
    p = _portfolio(_position())
    snap = (p.cash, dict(p.positions), p.equity, p.position_count)
    state = state_with(obs(end=utc(2026, 10, 7)), portfolio=p, risk_limits=RiskLimits(),
                       equity_curve=_curve([100.0 + i for i in range(30)]))
    svc = MonitoringService(StaticStateSource(state), MonitoringStore(":memory:"), clock=lambda: DAILY_NOW)
    for _ in range(3):
        svc.run_once()
    assert (p.cash, dict(p.positions), p.equity, p.position_count) == snap
    assert p.positions["QQQ"].current_price is None


def test_file_backed_store_round_trips_runs_and_findings(tmp_path):
    db = tmp_path / "mon" / "monitoring.db"
    svc, _, store = _service(store=MonitoringStore(db))
    run = svc.run_once()
    reopened = MonitoringStore(db)
    assert reopened.get_run(run.run_id).status == MonitoringStatus.CRITICAL
    assert reopened.list_findings(severity="critical")[0].rule_id == "scripted.rule"
    assert reopened.list_findings(entity="QQQ") and not reopened.list_findings(entity="NOPE")
    assert reopened.get_finding("missing") is None


def test_report_summarises_counts_and_findings():
    svc, _, _ = _service()
    run = svc.run_once()
    report = svc.report(run)
    assert report.counts["critical"] == 1 and report.findings[0]["rule_id"] == "scripted.rule"
    assert report.persisted is True
