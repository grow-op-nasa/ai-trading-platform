"""Deterministic monitoring scheduler (Sprint 16, ADR-0051).

Network-free, API-key-free, and free of real sleeping: time comes from an injected
`FakeClock`, waiting from an injected wait function. The scheduler runs the real
`MonitoringService` in the end-to-end tests; the semantic tests use a tiny fake
service so cadence/overlap/misfire rules are exact.
"""

from __future__ import annotations

import ast
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.cli.monitor import run_monitor
from src.cli.monitor_scheduler import (
    EXIT_ATTEMPT_ERRORS,
    EXIT_CONFIG,
    EXIT_LOCKED,
    EXIT_OK,
    run_monitor_scheduler,
)
from src.data.fault_injection import Fault, FaultInjectingProvider, FaultKind
from src.monitoring import FindingStatus, MonitoringStatus, MonitoringStore
from src.monitoring.scheduler import (
    MAX_CONSECUTIVE_RECORD_FAILURES,
    MonitoringScheduler,
    ScheduleAttempt,
    ScheduleStore,
    SchedulerConfig,
    SchedulerConfigError,
    SchedulerLock,
    SchedulerLockError,
    install_signal_handlers,
)
from tests.test_monitoring_failure_states import World, make, synth, T

REPO = Path(__file__).resolve().parent.parent
START = datetime(2026, 10, 7, 21, 30, tzinfo=timezone.utc)


# ------------------------------------------------------------------ test doubles
class FakeClock:
    def __init__(self, start=START):
        self.t = start

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeWait:
    """wait(seconds): advances the fake clock; optionally jumps extra (simulated suspend) or requests a stop."""

    def __init__(self, clock, *, jump_once=0.0, stop_on_call=None, stop_flag=None):
        self.clock, self.jump_once, self.stop_on_call = clock, jump_once, stop_on_call
        self.calls: list[float] = []
        self.stop_flag = stop_flag  # callable to request a stop on the scheduler

    def __call__(self, seconds):
        self.calls.append(seconds)
        self.clock.advance(seconds + self.jump_once)
        self.jump_once = 0.0
        if self.stop_on_call == len(self.calls):
            return True
        return False


class FakeService:
    """Stands in for MonitoringService; `duration` seconds of fake time per run."""

    def __init__(self, clock, duration=0.0, behaviours=None):
        self.clock, self.duration = clock, duration
        self.behaviours = list(behaviours or [])
        self.started_at: list[datetime] = []
        self.n = 0

    def run_once(self):
        self.started_at.append(self.clock.now())
        self.n += 1
        self.clock.advance(self.duration)
        b = self.behaviours[self.n - 1] if self.n - 1 < len(self.behaviours) else "ok"
        if isinstance(b, Exception):
            raise b
        persisted = b != "unpersisted"
        return SimpleNamespace(
            run_id=f"run-{self.n}", status=MonitoringStatus.HEALTHY, persisted=persisted, errors=[],
            persistence_error=None if persisted else "OSError: disk full",
        )


def build(config_kwargs=None, *, duration=0.0, behaviours=None, wait_kwargs=None, store=None):
    clock = FakeClock()
    cfg = SchedulerConfig(**{"schedule_id": "s1", "interval_seconds": 10, **(config_kwargs or {})})
    store = store or ScheduleStore(":memory:")
    svc = FakeService(clock, duration, behaviours)
    wait = FakeWait(clock, **(wait_kwargs or {}))
    lines: list[str] = []
    sched = MonitoringScheduler(svc, store, cfg, clock=clock.now, wait=wait, emit=lines.append)
    return sched, svc, store, clock, wait, lines


def rows(store):
    return list(reversed(store.list_attempts(limit=500)))


# ------------------------------------------------------------------ configuration
@pytest.mark.parametrize("bad", [0, -5, 0.5, float("nan"), float("inf"), 700_000, True, "10", None])
def test_invalid_intervals_are_rejected(bad):
    with pytest.raises(SchedulerConfigError):
        SchedulerConfig(schedule_id="s", interval_seconds=bad)


@pytest.mark.parametrize("bad", ["", " ", "a b", "../x", "-lead", "x" * 65, None, 5])
def test_invalid_schedule_ids_are_rejected(bad):
    with pytest.raises(SchedulerConfigError):
        SchedulerConfig(schedule_id=bad, interval_seconds=10)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_invalid_max_runs_are_rejected(bad):
    with pytest.raises(SchedulerConfigError):
        SchedulerConfig(schedule_id="s", interval_seconds=10, max_runs=bad)


def test_valid_configuration_boundaries():
    SchedulerConfig(schedule_id="exp-3.a_b", interval_seconds=1)
    SchedulerConfig(schedule_id="s", interval_seconds=604_800, max_runs=1)


# ------------------------------------------------------------------ cadence & first-run policy
def test_first_run_is_immediate_by_default_and_ticks_follow_the_interval():
    sched, svc, store, clock, wait, _ = build({"max_runs": 3})
    s = sched.run_forever()
    assert svc.started_at == [START, START + timedelta(seconds=10), START + timedelta(seconds=20)]
    assert wait.calls == [10.0, 10.0]                      # no wait before the first run
    assert [r.scheduled_for for r in rows(store)] == svc.started_at
    assert s.runs == 3 and s.completed == 3 and s.stopped_reason == "max_runs"


def test_wait_first_policy_delays_the_first_run_by_one_interval():
    sched, svc, store, clock, wait, _ = build({"max_runs": 2, "run_immediately": False})
    sched.run_forever()
    assert wait.calls[0] == 10.0 and svc.started_at[0] == START + timedelta(seconds=10)


def test_attempt_rows_carry_utc_times_duration_and_the_linked_run():
    sched, svc, store, clock, wait, _ = build({"max_runs": 1}, duration=2.5)
    sched.run_forever()
    (r,) = rows(store)
    assert r.schedule_id == "s1" and r.outcome == "COMPLETED"
    assert r.monitoring_run_id == "run-1" and r.monitoring_status == "HEALTHY"
    assert r.started_at.tzinfo is not None and r.finished_at.tzinfo is not None and r.scheduled_for.tzinfo is not None
    assert r.duration_seconds == 2.5 and r.error is None


def test_naive_timestamps_are_rejected_when_recording():
    with pytest.raises(ValueError):
        ScheduleAttempt("a", "s", datetime(2026, 1, 1), START, START, "COMPLETED")


# ------------------------------------------------------------------ overlap, misses, restarts
def test_a_run_longer_than_the_interval_never_overlaps_and_skipped_ticks_are_recorded():
    sched, svc, store, clock, wait, _ = build({"max_runs": 2}, duration=25)
    sched.run_forever()
    # run 1 at t0 (ends t25): ticks at t10 and t20 elapsed while running -> dropped, not queued
    assert svc.started_at == [START, START + timedelta(seconds=30)]
    skips = [r for r in rows(store) if r.outcome == "SKIPPED"]
    assert skips[0].skip_reason == "OVERLAP" and skips[0].skipped_count == 2
    assert skips[0].scheduled_for == START + timedelta(seconds=10)
    assert all(r.skip_reason == "OVERLAP" for r in skips)       # also recorded for the 2nd run's overlap
    assert len([r for r in rows(store) if r.outcome == "COMPLETED"]) == 2


def test_waking_late_drops_missed_intervals_and_runs_once_without_a_burst():
    sched, svc, store, clock, wait, _ = build({"max_runs": 2}, wait_kwargs={"jump_once": 95})
    sched.run_forever()                                   # run at t0; then the wait "sleeps" 95s too long
    assert len(svc.started_at) == 2                       # NOT 10 catch-up runs
    assert svc.started_at[1] == START + timedelta(seconds=105)
    skip = [r for r in rows(store) if r.skip_reason == "MISSED"]
    assert len(skip) == 1 and skip[0].skipped_count == 9
    assert skip[0].scheduled_for == START + timedelta(seconds=10)


def test_restart_does_not_replay_history_it_simply_starts_a_new_schedule():
    store = ScheduleStore(":memory:")
    a, svc_a, *_ = build({"max_runs": 1}, store=store)
    a.run_forever()
    b, svc_b, *_ = build({"max_runs": 1}, store=store)
    b.run_forever()
    assert len(svc_a.started_at) == 1 and len(svc_b.started_at) == 1
    assert len([r for r in rows(store) if r.outcome == "COMPLETED"]) == 2


# ------------------------------------------------------------------ failures keep the schedule alive
def test_a_failing_run_is_recorded_and_the_schedule_continues():
    sched, svc, store, clock, wait, lines = build(
        {"max_runs": 2}, behaviours=[RuntimeError("boom\nsecret-ish\n" + "x" * 500), "ok"])
    s = sched.run_forever()
    first, second = rows(store)
    assert first.outcome == "ERROR" and first.monitoring_run_id is None
    assert first.error.startswith("RuntimeError: boom") and "\n" not in first.error and len(first.error) < 260
    assert second.outcome == "COMPLETED"
    assert s.errors == 1 and s.completed == 1 and s.infrastructure_errors == 1
    assert any("SCHEDULER-ERROR" in l for l in lines)


def test_an_unpersisted_monitoring_run_is_an_error_never_a_success():
    sched, svc, store, *_ = build({"max_runs": 1}, behaviours=["unpersisted"])
    s = sched.run_forever()
    (r,) = rows(store)
    assert r.outcome == "ERROR" and r.monitoring_run_id == "run-1" and "not persisted" in r.error
    assert s.completed == 0 and s.errors == 1


def test_attempt_recording_failures_abort_rather_than_run_unrecorded():
    class BrokenStore(ScheduleStore):
        def record(self, a):
            raise sqlite3.OperationalError("database is locked")

    sched, svc, _, clock, wait, lines = build(store=BrokenStore(":memory:"))
    s = sched.run_forever()
    assert s.stopped_reason == "persistence_unavailable"
    assert s.record_failures == MAX_CONSECUTIVE_RECORD_FAILURES and svc.n == MAX_CONSECUTIVE_RECORD_FAILURES
    assert any("could not record attempt" in l for l in lines)


# ------------------------------------------------------------------ shutdown
def test_stop_during_a_wait_exits_cleanly_without_another_run():
    sched, svc, store, clock, wait, lines = build({}, wait_kwargs={"stop_on_call": 2})
    s = sched.run_forever()
    assert svc.n == 2 and s.stopped_reason == "stop_requested"
    assert lines[-1].startswith("scheduler s1: stopped (stop_requested)")


def test_stop_requested_during_a_run_lets_the_run_finish_then_exits():
    sched, svc, store, clock, wait, _ = build({})
    original = svc.run_once

    def run_and_stop():
        r = original()
        sched.request_stop()
        return r

    svc.run_once = run_and_stop
    s = sched.run_forever()
    assert svc.n == 1 and s.completed == 1 and s.stopped_reason == "stop_requested"
    assert rows(store)[0].outcome == "COMPLETED"       # the in-flight run was recorded, not abandoned


def test_a_tick_that_comes_due_at_shutdown_is_recorded_as_skipped():
    clock = FakeClock()
    cfg = SchedulerConfig(schedule_id="s1", interval_seconds=10)
    store = ScheduleStore(":memory:")
    svc = FakeService(clock)
    holder = {}

    def wait(seconds):
        clock.advance(seconds)
        holder["s"].request_stop()     # stop arrives exactly as the tick fires
        return False

    sched = MonitoringScheduler(svc, store, cfg, clock=clock.now, wait=wait)
    holder["s"] = sched
    sched.run_forever()
    assert svc.n == 1                                   # only the immediate first run
    skip = [r for r in rows(store) if r.outcome == "SKIPPED"]
    assert len(skip) == 1 and skip[0].skip_reason == "SHUTDOWN"


def test_signal_handlers_request_a_stop_and_are_restored():
    sched, *_ = build({})
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    with install_signal_handlers(sched):
        signal.raise_signal(signal.SIGTERM)
        assert sched.stop_requested
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


# ------------------------------------------------------------------ duplicate-scheduler protection
def test_a_second_scheduler_with_the_same_schedule_id_is_refused_and_lock_is_reusable(tmp_path):
    a, b, other = SchedulerLock(tmp_path, "s1"), SchedulerLock(tmp_path, "s1"), SchedulerLock(tmp_path, "s2")
    a.acquire()
    with pytest.raises(SchedulerLockError, match="already holds"):
        b.acquire()
    other.acquire()                                    # different schedule ids are independent
    other.release()
    a.release()
    b.acquire()                                        # released -> available again
    b.release()


# ------------------------------------------------------------------ real service, real lifecycle
def test_scheduled_runs_use_the_real_service_and_findings_follow_the_normal_lifecycle(tmp_path):
    prov = FaultInjectingProvider(synth(T(2026, 10, 7)), [Fault(FaultKind.TIMEOUT)])
    clock = FakeClock()
    world = World(clock.t)
    svc, _ = make(prov, world, store=MonitoringStore(tmp_path / "m.db"))
    svc._clock = clock.now
    sstore = ScheduleStore(tmp_path / "m.db")
    cfg = SchedulerConfig(schedule_id="exp-1", interval_seconds=60, max_runs=2)
    sched = MonitoringScheduler(svc, sstore, cfg, clock=clock.now, wait=FakeWait(clock))
    sched.run_forever()

    atts = [a for a in reversed(sstore.list_attempts(limit=10)) if a.outcome == "COMPLETED"]
    assert len(atts) == 2 and all(a.monitoring_status == "CRITICAL" for a in atts)
    for a in atts:                                     # every attempt links to a persisted monitoring run
        assert svc.store.get_run(a.monitoring_run_id) is not None
        assert sstore.attempts_for_run(a.monitoring_run_id)[0].attempt_id == a.attempt_id
    f = [x for x in svc.store.list_findings(limit=50) if x.rule_id == "market_data.retrieval_failed"]
    assert len(f) == 1 and f[0].occurrence_count == 2 and f[0].status == FindingStatus.OPEN
    assert len(prov.calls) == 2                        # one real-path fetch per scheduled run


def test_two_schedules_on_one_database_keep_separate_histories(tmp_path):
    db = tmp_path / "m.db"
    for sid in ("a", "b"):
        clock = FakeClock()
        svc, _ = make(FaultInjectingProvider(synth(T(2026, 10, 7)), [None]), World(clock.t),
                      store=MonitoringStore(db))
        svc._clock = clock.now
        MonitoringScheduler(svc, ScheduleStore(db), SchedulerConfig(sid, 30, max_runs=2), clock=clock.now,
                            wait=FakeWait(clock)).run_forever()
    st = ScheduleStore(db)
    assert len(st.list_attempts("a")) == 2 and len(st.list_attempts("b")) == 2


# ------------------------------------------------------------------ CLI
def svc_factory(tmp_path, *, duration=0.0, behaviours=None, clock=None):
    clock = clock or FakeClock()
    svc = FakeService(clock, duration, behaviours)
    return svc, (lambda e, d, r: svc), clock


def cli(args, tmp_path, **kw):
    out: list[str] = []
    rc = run_monitor_scheduler(["--db", str(tmp_path / "data" / "monitoring.db"), *args], emit=out.append,
                               install_signals=False, **kw)
    return rc, out


def test_cli_requires_explicit_scope_and_interval(tmp_path):
    rc, out = cli([], tmp_path)
    assert rc == EXIT_CONFIG and "required" in out[0]
    rc, out = cli(["--experiment-id", "1"], tmp_path)
    assert rc == EXIT_CONFIG
    assert not (tmp_path / "data").exists()            # nothing was created


@pytest.mark.parametrize("interval", ["0", "-3", "nan", "inf", "0.2"])
def test_cli_rejects_invalid_intervals(tmp_path, interval):
    svc, factory, clock = svc_factory(tmp_path)
    rc, out = cli(["--experiment-id", "1", "--interval-seconds", interval], tmp_path, service_factory=factory)
    assert rc == EXIT_CONFIG and "invalid configuration" in out[0] and svc.n == 0


def test_cli_missing_registry_fails_clearly_and_creates_nothing(tmp_path):
    missing = tmp_path / "nope" / "experiments.db"
    rc, out = cli(["--experiment-id", "1", "--interval-seconds", "60", "--registry", str(missing)], tmp_path)
    assert rc == EXIT_CONFIG and "registry not found" in out[0] and "never creates" in out[0]
    assert not missing.exists() and not missing.parent.exists()


def test_cli_experiment_without_a_portfolio_is_refused(tmp_path):
    from src.experiments.registry import ExperimentRegistry

    reg = tmp_path / "e.db"
    ExperimentRegistry(db_path=reg).log_experiment(strategy_name="x", changed={}, metrics_before={},
                                                   metrics_after={}, decision="KEEP")
    rc, out = cli(["--experiment-id", "1", "--interval-seconds", "60", "--registry", str(reg)], tmp_path)
    assert rc == EXIT_CONFIG and "no saved paper portfolio" in out[0]


def test_cli_bounded_run_reports_distinct_monitoring_and_scheduler_outcomes(tmp_path):
    clock = FakeClock()
    svc, factory, _ = svc_factory(tmp_path, behaviours=["ok", RuntimeError("db gone"), "ok"], clock=clock)
    rc, out = cli(["--experiment-id", "1", "--interval-seconds", "30", "--max-runs", "3"], tmp_path,
                  service_factory=factory, clock=clock.now, wait=FakeWait(clock))
    text = "\n".join(out)
    assert rc == EXIT_ATTEMPT_ERRORS                     # one infrastructure failure
    assert text.count("attempt COMPLETED") == 2 and text.count("SCHEDULER-ERROR") == 1
    assert "monitoring_status=HEALTHY" in text and "deterministic monitoring only" in text
    assert "schedule=exp-1" in text


def test_cli_clean_run_exits_zero_and_status_lists_the_attempts(tmp_path):
    clock = FakeClock()
    svc, factory, _ = svc_factory(tmp_path, clock=clock)
    rc, _ = cli(["--experiment-id", "1", "--interval-seconds", "30", "--max-runs", "2"], tmp_path,
                service_factory=factory, clock=clock.now, wait=FakeWait(clock))
    assert rc == EXIT_OK
    rc, out = cli(["--status", "--schedule-id", "exp-1"], tmp_path)
    assert rc == EXIT_OK and "last 2 scheduler attempt(s)" in out[0] and "run-2" in "\n".join(out)


def test_status_on_a_missing_database_reports_and_creates_nothing(tmp_path):
    rc, out = cli(["--status"], tmp_path)
    assert rc == EXIT_OK and "nothing has been scheduled" in out[0]
    assert not (tmp_path / "data").exists()


def test_cli_refuses_a_second_scheduler_for_the_same_schedule(tmp_path):
    held = SchedulerLock(tmp_path / "data", "exp-1")
    held.acquire()
    try:
        svc, factory, clock = svc_factory(tmp_path)
        rc, out = cli(["--experiment-id", "1", "--interval-seconds", "30", "--max-runs", "1"], tmp_path,
                      service_factory=factory)
        assert rc == EXIT_LOCKED and "already holds" in out[0] and svc.n == 0
    finally:
        held.release()


def test_cli_releases_its_lock_after_shutdown(tmp_path):
    clock = FakeClock()
    svc, factory, _ = svc_factory(tmp_path, clock=clock)
    cli(["--experiment-id", "1", "--interval-seconds", "30", "--max-runs", "1"], tmp_path,
        service_factory=factory, clock=clock.now, wait=FakeWait(clock))
    again = SchedulerLock(tmp_path / "data", "exp-1")
    again.acquire()
    again.release()


def test_cli_stops_with_error_when_attempts_cannot_be_recorded(tmp_path, monkeypatch):
    def boom(self, a):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ScheduleStore, "record", boom)
    clock = FakeClock()
    svc, factory, _ = svc_factory(tmp_path, clock=clock)
    rc, out = cli(["--experiment-id", "1", "--interval-seconds", "30"], tmp_path,
                  service_factory=factory, clock=clock.now, wait=FakeWait(clock))
    assert rc == EXIT_CONFIG and "could not be written" in out[-1]


# ------------------------------------------------------------------ no auto-start, no AI, one-off monitor unchanged
def test_monitor_once_does_not_touch_the_scheduler_table(tmp_path, capsys):
    db = tmp_path / "m.db"
    store = MonitoringStore(db)
    rc = run_monitor(["--once", "--json"], service_factory=lambda e, d: __import__(
        "src.monitoring", fromlist=["MonitoringService"]).MonitoringService(
        __import__("src.monitoring", fromlist=["StaticStateSource"]).StaticStateSource(
            __import__("src.monitoring", fromlist=["PlatformState"]).PlatformState(as_of=START, source="t")),
        store, clock=lambda: START))
    assert rc in (0, 1, 3)
    tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master")}
    assert "monitoring_schedule_attempts" not in tables


def test_importing_the_cli_never_starts_a_scheduler(monkeypatch):
    called = []
    monkeypatch.setattr(MonitoringScheduler, "run_forever", lambda self: called.append(1))
    before = threading.active_count()
    import importlib

    import src.cli.__main__ as m

    importlib.reload(m)
    assert "monitor-scheduler" in m.ARGV_COMMANDS and not called
    assert threading.active_count() == before


SCHEDULER_MODULES = [REPO / "src" / "monitoring" / "scheduler.py", REPO / "src" / "cli" / "monitor_scheduler.py"]
FORBIDDEN = ("anthropic", "src.ai", "src.broker", "src.execution", "src.promotion", "src.cli.strategy_promote",
             "src.strategies.promoted", "src.research")


def test_scheduler_modules_import_no_llm_broker_or_promotion_code():
    for path in SCHEDULER_MODULES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for m in mods:
                assert not m.startswith(FORBIDDEN), (path.name, m)


def test_running_the_scheduler_in_a_clean_process_never_loads_the_llm_or_agent_stack():
    code = (
        "import sys\n"
        "from src.cli.monitor_scheduler import run_monitor_scheduler\n"
        "from src.monitoring.scheduler import *\n"
        "bad=[m for m in sys.modules if m=='anthropic' or m.startswith('src.ai') or m.startswith('src.promotion')]\n"
        "assert not bad, bad\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
