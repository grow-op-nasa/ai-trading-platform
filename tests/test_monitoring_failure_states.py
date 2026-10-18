"""Failure-state behaviour of the deterministic monitor (Sprint 16, ADR-0051).

Every scenario runs the PRODUCTION path:

    FaultInjectingProvider -> MarketDataService (canonicalise, validate, session filter)
    -> monitoring observation -> checks -> finding lifecycle -> MonitoringStore -> CLI/report

Only the data provider is synthetic (`src.data.fault_injection`). Nothing is written
to the findings store by hand. These tests prove the platform handles each CLASS of
failure correctly; they do not claim any third-party provider actually failed.

Semantics under test (see DECISIONS.md ADR-0051):
  * unhealthy      = the monitor has evidence of a real condition (e.g. data required but unavailable)
  * not evaluable  = the monitor cannot reach a reliable conclusion; never healthy, never proof of recovery
  * a finding resolves ONLY when its own rule was genuinely re-evaluated and found clear
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from src.cli.monitor import EXIT_ERROR, EXIT_ATTENTION, EXIT_UNKNOWN, format_report, run_monitor
from src.data.base import Interval
from src.data.fault_injection import Fault, FaultInjectingProvider, FaultKind, SyntheticCandleProvider
from src.data.service import MarketDataService
from src.monitoring import (
    CheckStatus,
    FindingSeverity,
    FindingStatus,
    MonitoringService,
    MonitoringStatus,
    MonitoringStore,
    PlatformState,
)
from src.monitoring.checks import default_checks
from src.monitoring.state import MarketDataTarget
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionSide
from src.risk.models import RiskLimits

WED_EVENING = datetime(2026, 10, 7, 21, 30, tzinfo=timezone.utc)   # session closed 20:00Z, grace passed
SAT = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
T = lambda *a: date(*a)  # noqa: E731


# ------------------------------------------------------------------ builders
def _position(qty=10, price=100.0):
    return Position(symbol="QQQ", side=PositionSide.LONG, quantity=qty, entry_price=price,
                    entry_timestamp=pd.Timestamp("2026-10-01", tz="UTC"), entry_signal_id=uuid4())


class World:
    """Mutable inputs for successive monitoring runs (each run builds a fresh PlatformState)."""

    def __init__(self, now=WED_EVENING):
        self.now = now
        self.qty = 10            # 10 * 100 = 1000 of 10000 -> exposure healthy
        self.curve: pd.Series | None = None
        self.fail_state_source = False

    def state(self, now):
        if self.fail_state_source:
            raise RuntimeError("registry unreadable")
        s = PlatformState(
            as_of=now, source="test", experiment_id=1,
            portfolio=Portfolio.reconstruct(cash=10000.0 - self.qty * 100, positions=[_position(self.qty)]),
            risk_limits=RiskLimits(max_portfolio_exposure_pct=0.5),
            equity_curve=self.curve,
            market_data_targets=[MarketDataTarget("QQQ", Interval.DAY_1)],
        )
        return s


class _Source:
    def __init__(self, world): self._w = world
    def load(self, now): return self._w.state(now)


def make(provider, world=None, *, checks=None, store=None):
    world = world or World()
    md = MarketDataService(provider=provider, cache_dir=Path(tempfile.mkdtemp()), use_cache=False)
    svc = MonitoringService(_Source(world), store or MonitoringStore(":memory:"), market_data=md,
                            checks=checks, clock=lambda: world.now)
    return svc, world


def synth(last_session):
    return SyntheticCandleProvider(last_session=last_session)


def chk(run, check_id):
    return next(c for c in run.checks if c.check_id == check_id)


def findings(svc):
    return {f.rule_id: f for f in svc.store.list_findings(limit=200)}


# ------------------------------------------------------------------ fault-injection seam
def test_fault_script_repeats_last_entry_and_records_calls():
    p = FaultInjectingProvider(synth(T(2026, 10, 7)), [Fault(FaultKind.TIMEOUT), None])
    from src.data.exceptions import DataProviderError
    with pytest.raises(DataProviderError, match=r"\[fault-injection\]"):
        p.fetch_candles("QQQ", T(2026, 9, 7), T(2026, 10, 7), Interval.DAY_1)
    assert len(p.fetch_candles("QQQ", T(2026, 9, 7), T(2026, 10, 7), Interval.DAY_1)) > 0
    assert len(p.fetch_candles("QQQ", T(2026, 9, 7), T(2026, 10, 7), Interval.DAY_1)) > 0  # last entry repeats
    assert len(p.calls) == 3 and p.calls[0][0] == "QQQ"


def test_script_must_not_be_empty():
    with pytest.raises(ValueError):
        FaultInjectingProvider(synth(T(2026, 10, 7)), [])


def test_synthetic_provider_serves_real_sessions_only():
    df = synth(T(2026, 10, 9)).fetch_candles("QQQ", T(2026, 10, 5), T(2026, 10, 12), Interval.DAY_1)
    assert [d.isoformat() for d in df.index.date] == ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]


# ------------------------------------------------------------------ healthy
def test_healthy_data_is_healthy_with_evidence_but_run_is_not_called_healthy():
    svc, _ = make(synth(T(2026, 10, 7)))
    run = svc.run_once()
    assert chk(run, "market_data_freshness").status == CheckStatus.HEALTHY
    assert chk(run, "market_data_integrity").status == CheckStatus.HEALTHY
    assert chk(run, "portfolio_valuation").status == CheckStatus.HEALTHY
    assert chk(run, "market_data_freshness").summary == "1 target(s) evaluated"
    assert findings(svc) == {}
    # other checks (drawdown, strategy compat, ...) have no evidence -> the RUN cannot be HEALTHY
    assert run.status == MonitoringStatus.UNKNOWN
    assert chk(run, "portfolio_drawdown").status == CheckStatus.NOT_EVALUABLE
    assert chk(run, "portfolio_drawdown").reason == "no equity history available"


# ------------------------------------------------------------------ unhealthy: provider failures through the production path
@pytest.mark.parametrize("kind,code,provider_raised", [
    (FaultKind.TIMEOUT, "DataProviderError", True),
    (FaultKind.CONNECTION, "DataProviderError", True),
    (FaultKind.EMPTY, "NoDataError", True),
    (FaultKind.MALFORMED_COLUMNS, "StructuralValidationError", False),
    (FaultKind.IMPOSSIBLE_VALUES, "FinancialSanityError", False),
    (FaultKind.UNEXPECTED, "UNEXPECTED:RuntimeError", True),
])
def test_provider_failure_is_unhealthy_with_evidence_and_freshness_not_evaluable(kind, code, provider_raised):
    prov = FaultInjectingProvider(synth(T(2026, 10, 7)), [Fault(kind)])
    svc, _ = make(prov)
    run = svc.run_once()
    assert run.persisted and run.status == MonitoringStatus.CRITICAL
    # the integrity check ESTABLISHES a real condition: required data unavailable
    integ = chk(run, "market_data_integrity")
    assert integ.status == CheckStatus.CRITICAL
    f = findings(svc)["market_data.retrieval_failed"]
    assert f.severity == FindingSeverity.CRITICAL and f.status == FindingStatus.OPEN
    assert f.observed_value["error_code"] == code
    ev = f.evidence[0]
    assert ev.kind == "provider_outcome" and ev.data["error_code"] == code and ev.data["message"]
    if provider_raised:
        assert "[fault-injection]" in ev.data["message"]  # evidence is unmistakably synthetic
    # freshness cannot judge staleness without data -> NOT evaluable, never healthy
    fresh = chk(run, "market_data_freshness")
    assert fresh.status == CheckStatus.NOT_EVALUABLE and "no target could be evaluated" in fresh.reason
    assert "market_data.stale" not in findings(svc)
    assert len(prov.calls) == 1  # exactly one production-path fetch


def test_provider_unexpected_exception_does_not_crash_the_run():
    svc, _ = make(FaultInjectingProvider(synth(T(2026, 10, 7)), [Fault(FaultKind.UNEXPECTED)]))
    run = svc.run_once()
    assert run.persisted and run.errors == []  # observed and classified, not an infrastructure error


# ------------------------------------------------------------------ session-aware freshness
def test_stale_daily_data_warns_then_goes_critical():
    svc, w = make(synth(T(2026, 10, 5)))            # Wed 7th evening: Oct 6 and 7 missing -> 2 sessions
    svc.run_once()
    f = findings(svc)["market_data.stale"]
    assert f.severity == FindingSeverity.WARNING and f.evidence[0].data["missing_sessions"] == 2
    svc2, _ = make(synth(T(2026, 10, 2)))           # 3 sessions missing -> CRITICAL
    svc2.run_once()
    assert findings(svc2)["market_data.stale"].severity == FindingSeverity.CRITICAL


@pytest.mark.parametrize("now,last,label", [
    (SAT, T(2026, 10, 9), "weekend: Friday bar is current"),
    (datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc), T(2026, 10, 6), "inside the post-close grace window"),
    (datetime(2026, 11, 26, 21, 30, tzinfo=timezone.utc), T(2026, 11, 25), "Thanksgiving: market legitimately closed"),
])
def test_legitimate_market_closure_is_not_a_failure(now, last, label):
    svc, _ = make(synth(last), World(now))
    run = svc.run_once()
    assert chk(run, "market_data_freshness").status == CheckStatus.HEALTHY, label
    assert findings(svc) == {}, label


def test_same_weekend_with_a_genuinely_missing_friday_bar_is_flagged():
    svc, _ = make(synth(T(2026, 10, 8)), World(SAT))   # Thursday bar on Saturday: Friday missing
    svc.run_once()
    assert findings(svc)["market_data.stale"].evidence[0].data["missing_sessions"] == 1


# ------------------------------------------------------------------ lifecycle: unevaluable is not recovery
def test_failure_does_not_resolve_a_stale_finding_and_valid_data_does():
    prov = FaultInjectingProvider(synth(T(2026, 10, 5)), [None])
    svc, w = make(prov)
    svc.run_once()
    stale = findings(svc)["market_data.stale"]
    assert stale.status == FindingStatus.OPEN and stale.occurrence_count == 1
    first_seen_last = stale.last_seen

    w.now += timedelta(hours=1)                      # provider now FAILS
    prov.set_script([Fault(FaultKind.TIMEOUT)])
    svc.run_once()
    after_fail = findings(svc)
    s2 = after_fail["market_data.stale"]
    assert s2.status == FindingStatus.OPEN           # absence of evidence is not recovery
    assert s2.occurrence_count == 1 and s2.last_seen == first_seen_last and s2.resolved_at is None
    assert after_fail["market_data.retrieval_failed"].status == FindingStatus.OPEN

    w.now += timedelta(hours=1)                      # provider recovers with CURRENT data
    prov.set_script([None])
    prov._base.last_session = T(2026, 10, 7)
    svc.run_once()
    final = findings(svc)
    assert final["market_data.stale"].status == FindingStatus.RESOLVED
    assert final["market_data.stale"].resolved_at == w.now
    assert final["market_data.retrieval_failed"].status == FindingStatus.RESOLVED


def test_retrieval_failure_does_not_resolve_coverage_gap_findings():
    """A failed fetch cannot show that a gap or an identity problem cleared."""
    prov = FaultInjectingProvider(synth(T(2026, 10, 7)), [Fault(FaultKind.MISSING_BARS)])
    svc, w = make(prov)
    svc.run_once()
    assert findings(svc)["market_data.coverage_gaps"].status == FindingStatus.OPEN

    w.now += timedelta(hours=1)
    prov.set_script([Fault(FaultKind.TIMEOUT)])
    svc.run_once()
    assert findings(svc)["market_data.coverage_gaps"].status == FindingStatus.OPEN   # NOT resolved by a failure

    w.now += timedelta(hours=1)
    prov.set_script([None])                                                          # clean, complete data
    svc.run_once()
    assert findings(svc)["market_data.coverage_gaps"].status == FindingStatus.RESOLVED


def test_price_unavailable_does_not_resolve_a_stale_valuation_input_finding():
    prov = FaultInjectingProvider(synth(T(2026, 10, 5)), [None])
    svc, w = make(prov)
    svc.run_once()
    assert findings(svc)["valuation.stale_input"].status == FindingStatus.OPEN
    w.now += timedelta(hours=1)
    prov.set_script([Fault(FaultKind.TIMEOUT)])
    svc.run_once()
    assert findings(svc)["valuation.stale_input"].status == FindingStatus.OPEN


def test_repeated_failure_deduplicates_into_one_finding():
    prov = FaultInjectingProvider(synth(T(2026, 10, 7)), [Fault(FaultKind.TIMEOUT)])
    svc, w = make(prov)
    svc.run_once()
    w.now += timedelta(hours=1)
    svc.run_once()
    rows = [f for f in svc.store.list_findings(limit=100) if f.rule_id == "market_data.retrieval_failed"]
    assert len(rows) == 1 and rows[0].occurrence_count == 2
    assert rows[0].first_seen < rows[0].last_seen and rows[0].last_seen.tzinfo is not None


def _curve(n, drop):
    idx = pd.date_range("2026-08-17", periods=n, freq="D", tz="UTC")
    vals = [10000.0] * (n - 1) + [10000.0 * (1 - drop)]
    return pd.Series(vals, index=idx)


def test_unevaluable_drawdown_does_not_resolve_an_open_drawdown_finding():
    svc, w = make(synth(T(2026, 10, 7)))
    w.curve = _curve(30, 0.15)
    svc.run_once()
    dd = findings(svc)["drawdown.portfolio"]
    assert dd.status == FindingStatus.OPEN

    w.now += timedelta(hours=1)
    w.curve = _curve(10, 0.15)                      # too little history -> NOT evaluable
    run = svc.run_once()
    c = chk(run, "portfolio_drawdown")
    assert c.status == CheckStatus.NOT_EVALUABLE and "need 20" in c.reason
    assert findings(svc)["drawdown.portfolio"].status == FindingStatus.OPEN

    w.now += timedelta(hours=1)
    w.curve = _curve(30, 0.0)                       # genuinely recovered, enough evidence
    svc.run_once()
    assert findings(svc)["drawdown.portfolio"].status == FindingStatus.RESOLVED


def test_exposure_finding_survives_when_the_portfolio_disappears():
    svc, w = make(synth(T(2026, 10, 7)))
    w.qty = 80                                      # 80% vs 50% ceiling
    svc.run_once()
    assert findings(svc)["risk.portfolio_exposure"].severity == FindingSeverity.CRITICAL
    orig = w.state
    w.state = lambda now: PlatformState(as_of=now, source="test")   # nothing to evaluate
    run = svc.run_once()
    assert chk(run, "risk_exposure").status == CheckStatus.NOT_EVALUABLE
    assert findings(svc)["risk.portfolio_exposure"].status == FindingStatus.OPEN
    w.state = orig
    w.qty = 10
    svc.run_once()
    assert findings(svc)["risk.portfolio_exposure"].status == FindingStatus.RESOLVED


# ------------------------------------------------------------------ failure to execute a check / the run
def test_state_source_failure_makes_the_run_unknown_with_errors_and_keeps_findings():
    svc, w = make(synth(T(2026, 10, 7)))
    w.qty = 80
    svc.run_once()
    w.fail_state_source = True
    run = svc.run_once()
    assert run.status == MonitoringStatus.UNKNOWN and run.persisted
    assert any("state source failed" in e for e in run.errors)
    assert all(c.status == CheckStatus.NOT_EVALUABLE for c in run.checks)
    assert findings(svc)["risk.portfolio_exposure"].status == FindingStatus.OPEN


def test_a_raising_check_is_not_evaluable_not_healthy_and_does_not_resolve():
    class Boom:
        check_id = "risk_exposure"

        def run(self, state, config, ctx):
            raise RuntimeError("bug in check")

    checks = [c for c in default_checks() if c.check_id != "risk_exposure"] + [Boom()]
    svc, w = make(synth(T(2026, 10, 7)))
    w.qty = 80
    svc.run_once()
    svc._checks = checks
    run = svc.run_once()
    c = chk(run, "risk_exposure")
    assert c.status == CheckStatus.NOT_EVALUABLE and c.reason == "check raised RuntimeError"
    assert any("risk_exposure: RuntimeError" in e for e in run.errors)
    assert findings(svc)["risk.portfolio_exposure"].status == FindingStatus.OPEN
    assert run.status in (MonitoringStatus.UNKNOWN, MonitoringStatus.CRITICAL)  # never HEALTHY


def test_persistence_failure_is_reported_never_claimed_as_persisted():
    class BrokenStore(MonitoringStore):
        def apply(self, run, findings):
            raise OSError("disk full")

    svc, _ = make(synth(T(2026, 10, 7)), store=BrokenStore(":memory:"))
    run = svc.run_once()
    assert run.persisted is False and "OSError" in run.persistence_error
    assert any("persistence failed" in e for e in run.errors)


# ------------------------------------------------------------------ report / CLI consistency
def test_cli_json_text_and_store_agree_for_a_failure_run(capsys):
    prov = FaultInjectingProvider(synth(T(2026, 10, 7)), [Fault(FaultKind.TIMEOUT)])
    svc, _ = make(prov)
    rc = run_monitor(["--once", "--json"], service_factory=lambda e, d: svc)
    payload = json.loads(capsys.readouterr().out)
    stored = svc.store.list_findings(limit=50)
    assert rc == EXIT_ATTENTION and payload["status"] == "CRITICAL" and payload["persisted"] is True
    assert {f["finding_id"] for f in payload["findings"]} == {f.finding_id for f in stored}
    statuses = {c["check_id"]: c["status"] for c in payload["checks"]}
    assert statuses["market_data_integrity"] == "CRITICAL" and statuses["market_data_freshness"] == "NOT_EVALUABLE"
    text = format_report(svc.report(svc.store.get_run(payload["run_id"])))
    assert "[CRITICAL] market_data_integrity" in text and "[NOT_EVALUABLE] market_data_freshness" in text
    assert "Market data unavailable for QQQ" in text


def test_cli_exit_code_is_an_error_when_the_monitor_itself_failed_and_nothing_is_critical(capsys):
    svc, w = make(synth(T(2026, 10, 7)))
    w.fail_state_source = True                       # infrastructure failure, not a data judgement
    rc = run_monitor(["--once"], service_factory=lambda e, d: svc)
    assert rc == EXIT_ERROR
    assert "state source failed" in capsys.readouterr().out


def test_cli_plain_unknown_without_errors_keeps_exit_code_3(capsys):
    svc, _ = make(synth(T(2026, 10, 7)))
    rc = run_monitor(["--once"], service_factory=lambda e, d: svc)   # drawdown etc. simply lack evidence
    assert rc == EXIT_UNKNOWN


def test_non_dataframe_provider_response_becomes_a_structural_failure_not_a_crash():
    class Garbage:
        def fetch_candles(self, symbol, start, end, interval):
            return [{"close": 1}]          # not a DataFrame

    svc, _ = make(Garbage())
    run = svc.run_once()
    assert run.persisted
    assert chk(run, "market_data_integrity").status == CheckStatus.CRITICAL
    assert chk(run, "market_data_freshness").status == CheckStatus.NOT_EVALUABLE
    f = findings(svc)["market_data.retrieval_failed"]
    assert "StructuralValidationError" in str(f.evidence)
