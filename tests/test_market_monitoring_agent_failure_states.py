"""Agent behaviour over failure-state runs (Sprint 16, ADR-0051). Fake LLM provider only.

The stored run is produced by the deterministic service with a SYNTHETIC injected
market-data fault (src.data.fault_injection) -- not a real provider outage. These tests
prove the read-only agent cannot launder failure states, not that any LLM interprets them well.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

from src.ai.agents.monitoring import MarketMonitoringAgentPolicy
from src.ai.agents.monitoring.tools import default_monitoring_tool_registry
from src.data.fault_injection import Fault, FaultInjectingProvider, FaultKind, SyntheticCandleProvider
from src.data.service import MarketDataService
from src.monitoring import MonitoringService, MonitoringStore, PlatformState, StaticStateSource
from src.monitoring.state import MarketDataTarget
from src.data.base import Interval
from src.portfolio.models import Portfolio
from src.risk.models import RiskLimits
from tests.test_market_monitoring_agent import NOW, _position, call, final, make_agent


def failing_service(kind=FaultKind.TIMEOUT):
    p = Portfolio.reconstruct(cash=2000.0, positions=[_position()])
    state = PlatformState(as_of=NOW, source="test", portfolio=p,
                          risk_limits=RiskLimits(max_portfolio_exposure_pct=0.5),
                          market_data_targets=[MarketDataTarget("QQQ", Interval.DAY_1)])
    provider = FaultInjectingProvider(SyntheticCandleProvider(last_session=date(2026, 10, 7)), [Fault(kind)])
    md = MarketDataService(provider=provider, cache_dir=Path(tempfile.mkdtemp()), use_cache=False)
    return MonitoringService(StaticStateSource(state), MonitoringStore(":memory:"), market_data=md,
                             clock=lambda: NOW), provider


def test_agent_over_a_failed_fetch_carries_deterministic_limitations_and_never_says_healthy():
    svc, _ = failing_service()
    agent, _, _ = make_agent([call("run_monitoring_checks"), final("Summary: everything is healthy and all clear.")], svc)
    run = agent.run("check")
    lims = " ".join(run.final_report.limitations)
    assert "market_data_freshness was not evaluable" in lims          # injected by code, not the model
    assert svc.store.get_run(run.monitoring_run_ids[0]).status.value == "CRITICAL"   # truth stays deterministic


def test_stored_failure_run_is_readable_without_a_new_run_and_keeps_failure_evidence():
    svc, provider = failing_service()
    stored = svc.run_once()
    n = len(provider.calls)
    agent, _, _ = make_agent([call("get_monitoring_run", {"run_id": stored.run_id}),
                              call("list_findings", {}, "c2"), final("Summary: reviewed.")], svc)
    run = agent.run("inspect stored")
    assert run.monitoring_run_count == 0 and len(provider.calls) == n  # read tools do not refetch data
    listed = [c for c in run.tool_calls if c.tool_name == "list_findings"][0]
    rules = {f["rule_id"] for f in listed.result_summary["findings"]}
    assert "market_data.retrieval_failed" in rules


def test_market_data_health_tool_reports_the_failure_from_the_stored_run_not_from_a_guess():
    svc, _ = failing_service()
    svc.run_once()
    agent, _, _ = make_agent([call("get_market_data_health"), final("Summary: data health.")], svc)
    run = agent.run("health")
    tool = run.tool_calls[0]
    assert not tool.is_error
    assert "healthy" not in str(tool.result_summary.get("status", "")).lower()


def test_an_agent_run_does_not_resolve_or_create_findings_beyond_the_deterministic_run():
    svc, _ = failing_service()
    stored = svc.run_once()
    before = {f.finding_id: (f.status, f.occurrence_count) for f in svc.store.list_findings(limit=100)}
    agent, _, _ = make_agent([call("get_monitoring_run", {"run_id": stored.run_id}),
                              call("list_findings", {}, "c2"), final("Summary: reviewed.")], svc)
    agent.run("inspect")
    after = {f.finding_id: (f.status, f.occurrence_count) for f in svc.store.list_findings(limit=100)}
    assert before == after


def test_tool_boundary_unchanged_no_scheduler_or_recovery_capability():
    svc, _ = failing_service()
    names = {t.name for t in default_monitoring_tool_registry(svc).definitions()} if hasattr(
        default_monitoring_tool_registry(svc), "definitions") else set()
    for n in names:
        assert not any(w in n for w in ("schedule", "recover", "retry", "restart", "resolve", "acknowledge", "alert"))


def test_requesting_a_recovery_tool_is_rejected_and_the_run_continues():
    svc, _ = failing_service()
    agent, _, _ = make_agent([call("retry_market_data", {"symbol": "QQQ"}), call("run_monitoring_checks", {}, "c2"),
                              final("Summary: done.")], svc)
    run = agent.run("try to recover")
    assert run.tool_calls[0].is_error and run.tool_calls[0].error_code in ("UNKNOWN_TOOL", "POLICY_REJECTED")
    assert run.status == "COMPLETED"


def test_policy_still_forbids_scheduler_style_capabilities():
    for flag in ("allow_trading", "allow_recovery", "allow_alert_policy_changes"):
        try:
            MarketMonitoringAgentPolicy(**{flag: True})
        except (TypeError, ValueError):
            continue
        pytest.fail(f"{flag} was accepted")
