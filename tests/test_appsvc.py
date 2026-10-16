"""Application services behind the local UI (Sprint 19, ADR-0054): network-free, deterministic, Streamlit-free.

The paper-cycle tests drive the REAL Sprint 18 runtime through `PaperService` against the synthetic fixture world of
`tests/test_paper_runtime.py` (scripted registered strategy, synthetic candles, real risk/PaperBroker/registry). Any
fill seen here is a SIMULATED fixture fill, never a real-market observation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.appsvc import monitoring as mon
from src.appsvc import portfolio as pf
from src.appsvc import research, strategies
from src.appsvc.paper import OUTCOME_MEANING, PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.system import DISABLED, FAIL, OK, UNKNOWN, WARN, collect_status
from src.data.service import MarketDataService
from src.monitoring import MonitoringService, MonitoringStore, PlatformState, StaticStateSource
from src.monitoring.alerts.config import AlertConfigError
from src.monitoring.alerts.models import AlertEventSettings, DeliveryStatus, TransportResult
from src.paper.config import PaperConfigError
from src.paper.models import CycleStatus
from tests.test_alerts import GOOD_ENV, SevCheck, Clock, FakeTransport, T0, _Ticking  # noqa: F401
from tests.test_paper_runtime import (  # noqa: F401  (autouse fixture re-exported so it applies here)
    AFTER_CLOSE, STRAT, WED, World, _reset_script, fingerprint)


def snapshot_tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def service_for(world: World, **kw) -> PaperService:
    md = MarketDataService(provider=world.provider, cache_dir=world.tmp / "cache", use_cache=False)
    return PaperService(
        AppPaths.under(world.tmp), market_data=md, monitor_factory_builder=lambda cfg: world.monitor,
        runtime_kwargs={"clock": lambda: world.now[0], "faults": world.faults,
                        "allowed_strategy_prefixes": ("src.strategies", "tests.test_paper_runtime")}, **kw)


# ------------------------------------------------------------------ paths and system status
def test_status_on_an_empty_directory_creates_nothing_and_reports_unknown_honestly(tmp_path):
    paths = AppPaths.under(tmp_path / "nothing")
    items = {i.name: i for i in collect_status(paths, env={}, root=tmp_path)}
    assert not (tmp_path / "nothing").exists()
    assert items["Experiment registry"].state == WARN and items["Paper database"].state == WARN
    assert items["Market-data source"].state == UNKNOWN          # health is not asserted without a request
    assert items["Alert delivery"].state == DISABLED and items["AI provider"].state == DISABLED
    assert items["Monitoring scheduler"].state == UNKNOWN        # nothing running is not "healthy"


def test_status_never_contains_a_secret_even_for_an_invalid_alert_configuration(tmp_path):
    secret_key, secret_pw = "sk-ant-" + "SECRET123", "hunter" + "2pass"
    env = {"ANTHROPIC_API_KEY": secret_key, "ATP_ALERTS_ENABLED": "true", "ATP_ALERT_SMTP_PASSWORD": secret_pw,
           "ATP_ALERT_SMTP_HOST": "smtp.example.com"}
    items = collect_status(AppPaths.under(tmp_path), env=env, root=tmp_path)
    text = json.dumps([i.__dict__ for i in items])
    assert secret_key not in text and secret_pw not in text
    by = {i.name: i for i in items}
    assert by["AI provider"].state == OK and by["Alert delivery"].state == FAIL


def test_valid_alert_configuration_is_not_reported_as_working_delivery(tmp_path):
    item = {i.name: i for i in collect_status(AppPaths.under(tmp_path), env=GOOD_ENV, root=tmp_path)}["Alert delivery"]
    assert item.state == WARN and "does not prove" in item.detail


def test_scheduler_lock_probe_detects_a_held_lock_without_writing(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    from src.appsvc.system import scheduler_locks
    from src.monitoring.scheduler import SchedulerLock
    d = tmp_path / "monitoring"
    lock = SchedulerLock(d, "exp-1")
    lock.acquire()
    try:
        before = (d / "monitoring_scheduler.exp-1.lock").read_text()
        assert scheduler_locks(d) == [("exp-1", True, before.strip())]
        assert (d / "monitoring_scheduler.exp-1.lock").read_text() == before
    finally:
        lock.release()
    assert scheduler_locks(d) == [("exp-1", False, "")]


# ------------------------------------------------------------------ strategies and eligibility
def test_registered_strategies_come_from_the_production_registry_with_identity():
    infos = {s.name: s for s in strategies.registered_strategies(AppPaths.under("/nonexistent-root"))}
    assert "ema_cross" in infos and infos["ema_cross"].version_hash and len(infos["ema_cross"].version_hash) == 64
    assert infos["ema_cross"].origin == "built-in" and infos["ema_cross"].class_path.startswith("src.strategies")
    assert "not_a_strategy" not in strategies.eligible_names()


def test_development_candidates_are_context_only_and_never_paper_eligible(tmp_path):
    from src.ai.agents.strategy_dev.models import CandidateStrategyStatus
    assert strategies.candidates(AppPaths.under(tmp_path)) == []
    for status, meaning in strategies._CANDIDATE_MEANING.items():
        assert status in {s.value for s in CandidateStrategyStatus}
    approved = strategies.CandidateInfo("c1", "x", "APPROVED", strategies._CANDIDATE_MEANING["APPROVED"])
    assert approved.paper_eligible is False and "NOT registered" in approved.meaning


# ------------------------------------------------------------------ profiles / configuration
def test_build_config_accepts_only_registered_strategies_and_paper_mode(tmp_path):
    svc = PaperService(AppPaths.under(tmp_path))
    cfg = svc.build_config(experiment_id=3, strategy_name="ema_cross", symbol=" qqq ")
    assert cfg.execution_mode == "paper" and cfg.symbol == "QQQ"
    with pytest.raises(PaperConfigError, match="not a registered production strategy"):
        svc.build_config(experiment_id=3, strategy_name="candidate_from_model", symbol="QQQ")
    with pytest.raises(PaperConfigError, match="interval"):
        svc.build_config(experiment_id=3, strategy_name="ema_cross", symbol="QQQ", form={**__import__("src.appsvc.paper", fromlist=["x"]).DEFAULT_FORM, "interval": "1m"})


def test_save_profile_is_explicit_never_overwrites_and_rejects_unsafe_names(tmp_path):
    svc = PaperService(AppPaths.under(tmp_path))
    cfg = svc.build_config(experiment_id=1, strategy_name="ema_cross", symbol="QQQ")
    assert svc.list_profiles() == [] and not svc.paths.profiles_dir.exists()
    path = svc.save_profile("qqq-ema", cfg)
    assert path.is_file() and json.loads(path.read_text())["execution_mode"] == "paper"
    with pytest.raises(PaperConfigError, match="already exists"):
        svc.save_profile("qqq-ema", cfg)
    for bad in ("../escape", "a/b", "", ".hidden", "x" * 80):
        with pytest.raises(PaperConfigError):
            svc.save_profile(bad, cfg)
    assert [p.name for p in svc.list_profiles()] == ["qqq-ema"] and svc.list_profiles()[0].config == cfg
    (svc.paths.profiles_dir / "broken.json").write_text('{"execution_mode": "live"}')
    broken = {p.name: p for p in svc.list_profiles()}["broken"]
    assert broken.config is None and broken.error


def test_a_saved_profile_cannot_select_live_execution(tmp_path):
    svc = PaperService(AppPaths.under(tmp_path))
    svc.paths.profiles_dir.mkdir(parents=True)
    (svc.paths.profiles_dir / "live.json").write_text(json.dumps({"execution_mode": "live", "experiment_id": 1}))
    info = svc.list_profiles()[0]
    assert info.config is None and info.error


# ------------------------------------------------------------------ paper operations through the real runtime
def test_preflight_is_read_only_and_ready_for_a_valid_fixture(tmp_path):
    w = World(tmp_path)
    before = (fingerprint(w.reg_path), fingerprint(w.store_path))
    rep = service_for(w).preflight(w.config(), check_data=True)
    assert rep.ready, [c.to_dict() for c in rep.checks if c.status.value != "PASS"]
    assert (fingerprint(w.reg_path), fingerprint(w.store_path)) == before and w.store().list_cycles(w.exp) == []


def test_no_signal_is_a_valid_outcome_with_no_order(tmp_path):
    w = World(tmp_path)
    out = service_for(w).run_cycle(w.config(), request_token="t1")
    assert out.ran and out.record.status is CycleStatus.NO_ACTION and out.error is None and out.refused is None
    assert out.record.client_order_id is None and w.store().list_fills(w.exp) == []
    assert "valid outcome" in OUTCOME_MEANING["NO_ACTION"]


def test_a_fixture_fill_is_recorded_traced_and_applied_to_the_stored_portfolio(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = service_for(w)
    out = svc.run_cycle(w.config(), request_token="t1")
    assert out.record.status is CycleStatus.ORDER_FILLED
    info = svc.inspect(w.exp)
    assert len(info["fills"]) == 1 and len(info["intents"]) == 1 and info["unresolved"] == []
    assert info["latest_reconciliation"].status.value == "MATCH"
    tr = svc.trace(out.record.cycle_id)
    assert tr["intent"].client_order_id == tr["fill"].client_order_id == out.record.client_order_id
    assert tr["reconciliation"].status.value == "MATCH"
    assert any("monitoring database" in s for s in tr["limitations"])      # run id recorded, no monitoring DB here
    stored = pf.stored_state(AppPaths.under(tmp_path), w.exp)
    assert len(stored.positions) == 1 and stored.cash < 10_000.0 and stored.positions[0].symbol == "QQQ"


def test_one_submission_runs_once_and_a_rerun_with_the_same_token_does_not_call_the_runtime(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = service_for(w)
    first = svc.run_cycle(w.config(), request_token="tok")
    again = svc.run_cycle(w.config(), request_token="tok")
    assert again.reused_token and again.record.cycle_id == first.record.cycle_id
    assert len(w.store().list_cycles(w.exp)) == 1 and len(w.monitor_calls) == 1


def test_a_new_submission_for_the_same_bar_is_a_backend_replay_never_a_second_order(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = service_for(w)
    svc.run_cycle(w.config(), request_token="a")
    second = svc.run_cycle(w.config(), request_token="b")
    assert second.record.replayed is True and second.record.status is CycleStatus.ORDER_FILLED
    assert len(w.store().list_fills(w.exp)) == 1 and len(w.store().list_intents(w.exp)) == 1


def test_failed_preflight_refuses_to_run_and_records_nothing(tmp_path):
    w = World(tmp_path)
    w.cfg_dict["experiment_id"] = 999
    before = fingerprint(w.store_path)
    out = service_for(w).run_cycle(w.config())
    assert out.refused and not out.ran and out.refused_checks and fingerprint(w.store_path) == before


def test_missing_paper_database_is_refused_and_never_created(tmp_path):
    w = World(tmp_path, initialize=False)
    out = service_for(w).run_cycle(w.config())
    assert out.refused and not out.ran and not w.store_path.exists()
    assert service_for(w).inspect(w.exp)["available"] is False


def test_an_unexpected_service_exception_is_never_reported_as_success(tmp_path):
    w = World(tmp_path)
    svc = service_for(w)
    svc.runtime = lambda cfg, monitoring=True: (_ for _ in ()).throw(RuntimeError("boom"))
    out = svc.run_cycle(w.config(), request_token="x")
    assert out.error and "RuntimeError" in out.error and not out.ran and out.refused is None


def test_initialise_requires_an_explicit_label_and_is_explicit(tmp_path):
    w = World(tmp_path, initialize=False)
    svc = service_for(w)
    with pytest.raises(Exception):
        svc.initialize_account(w.config(), "  ")
    info = svc.initialize_account(w.config(), "unit test")
    assert info["experiment_id"] == w.exp and w.store_path.exists()


def test_the_ui_service_cannot_reach_a_live_broker_or_an_llm():
    import ast
    for p in Path("src/appsvc").glob("*.py"):
        tree = ast.parse(p.read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        bad = {m for m in mods if m.split(".")[0] in {"anthropic", "streamlit", "subprocess"}
               or m.startswith(("src.broker", "src.ai.agents.research", "src.ai.agents.monitoring", "src.promotion"))}
        assert not bad, (p.name, bad)


# ------------------------------------------------------------------ portfolio
def test_valuation_with_an_unpriced_open_position_leaves_equity_undefined_not_partial_or_zero(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    service_for(w).run_cycle(w.config())
    paths = AppPaths.under(tmp_path)
    snap, ts = pf.value_now(paths, w.exp, price_lookup=lambda s: None)
    assert snap.equity.value is None and snap.equity.status.value == "undefined" and ts
    snap2, _ = pf.value_now(paths, w.exp, price_lookup=lambda s: 100.0)
    assert snap2.equity.value is not None and snap2.positions[0].market_price == 100.0
    assert pf.value_now(AppPaths.under(tmp_path / "x"), 1) == (None, "")
    assert pf.stored_state(AppPaths.under(tmp_path / "x"), 1) is None


# ------------------------------------------------------------------ research
def test_experiment_catalogue_reads_the_registry_and_reports_missing_pieces(tmp_path):
    w = World(tmp_path)
    rows = research.experiments(AppPaths.under(tmp_path))
    assert [r.experiment_id for r in rows] == [w.exp] and rows[0].has_spec and rows[0].has_portfolio
    assert rows[0].has_equity_curve is False and rows[0].decision == "KEEP"
    assert research.experiments(AppPaths.under(tmp_path / "none")) == []


# ------------------------------------------------------------------ monitoring and alerts
def _monitoring_world(tmp_path, mode="critical"):
    paths = AppPaths.under(tmp_path)
    paths.monitoring_db.parent.mkdir(parents=True)
    store = MonitoringStore(paths.monitoring_db)
    clock = Clock()
    svc = MonitoringService(StaticStateSource(PlatformState(as_of=T0, source="experiment_registry", experiment_id=7)),
                            store, checks=[SevCheck(mode)], clock=_Ticking(clock),
                            alerts=AlertEventSettings(enabled=True, channels=("email",)))
    svc.run_once()
    return paths, store


def test_monitoring_views_read_persisted_findings_and_keep_delivery_separate_from_severity(tmp_path):
    paths, store = _monitoring_world(tmp_path)
    assert mon.available(paths) and len(mon.runs(paths)) == 1
    crit = mon.open_findings(paths)["CRITICAL"]
    assert len(crit) == 1 and crit[0].title == "Scripted problem"
    counts = mon.alert_counts(paths)
    assert counts["PENDING"] == 1 and counts["DELIVERED"] == 0       # queued, NOT delivered
    assert len(mon.deliveries(paths, "PENDING")) == 1 and mon.deliveries(paths, "FAILED") == []
    assert mon.is_synthetic(mon.runs(paths)[0]) is False             # never guessed


def test_viewing_monitoring_state_sends_nothing_and_runs_nothing(tmp_path):
    paths, store = _monitoring_world(tmp_path)
    before = fingerprint(paths.monitoring_db)
    mon.runs(paths); mon.findings(paths); mon.alert_events(paths); mon.deliveries(paths); mon.alert_counts(paths)
    mon.scheduler_attempts(paths); mon.open_findings(paths)
    assert fingerprint(paths.monitoring_db) == before


def test_explicit_dispatch_uses_the_existing_dispatcher_and_delivers_exactly_once(tmp_path):
    paths, store = _monitoring_world(tmp_path)
    fake = FakeTransport()
    env = dict(GOOD_ENV)
    s1 = mon.dispatch_pending(paths, env=env, transport_factory=lambda cfg: fake, clock=Clock())
    assert s1.delivered == 1 and len(fake.sent) == 1
    s2 = mon.dispatch_pending(paths, env=env, transport_factory=lambda cfg: fake, clock=Clock())
    assert s2.attempted == 0 and len(fake.sent) == 1
    assert mon.alert_counts(paths)["DELIVERED"] == 1


def test_dispatch_requires_enabled_valid_alerting_and_an_existing_database(tmp_path):
    paths, store = _monitoring_world(tmp_path)
    with pytest.raises(AlertConfigError, match="not enabled"):
        mon.dispatch_pending(paths, env={})
    with pytest.raises(AlertConfigError, match="no monitoring database"):
        mon.dispatch_pending(AppPaths.under(tmp_path / "none"), env=GOOD_ENV)
    assert mon.alert_counts(paths)["PENDING"] == 1


def test_retry_requeues_failed_deliveries_without_new_events_or_runs(tmp_path):
    paths, store = _monitoring_world(tmp_path)
    fake = FakeTransport([TransportResult.permanent("550 no")])
    mon.dispatch_pending(paths, env=dict(GOOD_ENV), transport_factory=lambda cfg: fake, clock=Clock())
    assert mon.alert_counts(paths)["FAILED"] == 1
    events_before, runs_before = len(mon.alert_events(paths)), len(mon.runs(paths))
    assert mon.retry_failed(paths, clock=Clock()) == 1
    assert len(mon.alert_events(paths)) == events_before and len(mon.runs(paths)) == runs_before
    assert mon.alert_counts(paths)["FAILED"] == 0
    assert mon.retry_failed(AppPaths.under(tmp_path / "none")) == 0


def test_an_unevaluable_check_is_not_shown_as_a_healthy_open_finding_set(tmp_path):
    paths, store = _monitoring_world(tmp_path, mode="unknown")
    run = mon.runs(paths)[0]
    assert run.status.value != "HEALTHY"
    assert any(c.status.value == "NOT_EVALUABLE" for c in run.checks)
    assert mon.open_findings(paths) == {"CRITICAL": [], "WARNING": []}
