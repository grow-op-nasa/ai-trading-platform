"""Deterministic monitoring scheduler CLI -- src/cli/monitor_scheduler.py (ADR-0051).

    python -m src.cli monitor-scheduler --experiment-id 3 --interval-seconds 900
    python -m src.cli monitor-scheduler --status [--schedule-id exp-3]

Runs the SAME deterministic `MonitoringService` as `monitor --once` on a fixed
interval. It starts only when you run this command; it never calls an LLM,
a broker, or any promotion/recovery code, and needs no API key. Stop it with
Ctrl+C or SIGTERM: an in-flight run finishes, then it exits.

Scope is explicit: `--experiment-id` is required, the registry must already
exist, and the experiment must already have a saved paper portfolio -- the
scheduler never creates a registry or invents a portfolio.

Exit codes
    0  clean shutdown, every attempt recorded without infrastructure errors
    1  clean shutdown, but one or more attempts failed in the scheduler/infrastructure
    2  invalid configuration, missing registry/experiment, or storage became unavailable
    3  another scheduler with the same schedule id is already running

Monitoring outcomes (CRITICAL, UNKNOWN, ...) are findings stored with each run;
they never change the scheduler's exit code. With --dispatch-alerts the scheduler also delivers pending
alert events after each run (Sprint 17, ADR-0052); delivery trouble is logged and never changes either
the monitoring result or the exit code.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, Sequence

from src.monitoring.scheduler import (
    MonitoringScheduler,
    ScheduleStore,
    SchedulerConfig,
    SchedulerConfigError,
    SchedulerLock,
    SchedulerLockError,
    install_signal_handlers,
    utc_now,
)
from src.monitoring.store import DEFAULT_DB_PATH

EXIT_OK, EXIT_ATTEMPT_ERRORS, EXIT_CONFIG, EXIT_LOCKED = 0, 1, 2, 3
DISPATCH_BUDGET_SECONDS = 20.0  # max time one tick may spend delivering alerts (each send is also timeout-bounded)


def _build_dispatcher(db_path: Path):
    """Alert dispatcher bound to the same monitoring database; raises if alerting is not usable."""
    from src.monitoring.alerts.config import AlertConfig, AlertConfigError
    from src.monitoring.alerts.dispatcher import AlertDispatcher
    from src.monitoring.alerts.transports import build_transport
    from src.monitoring.store import MonitoringStore

    cfg = AlertConfig.from_env()
    if not cfg.enabled:
        raise AlertConfigError("alerting is not enabled (set ATP_ALERTS_ENABLED=true and the ATP_ALERT_* settings)")
    cfg.validate()
    return AlertDispatcher(MonitoringStore(db_path).alerts, build_transport(cfg), cfg)


def _default_service(experiment_id: int, db_path: str | None, registry_path: str | None):
    """Validate the explicit scope, then build the standard monitoring service."""
    from src.experiments.registry import DEFAULT_DB_PATH as REGISTRY_DEFAULT
    from src.experiments.registry import ExperimentRegistry

    reg_path = Path(registry_path) if registry_path else Path(REGISTRY_DEFAULT)
    if not reg_path.exists():
        raise SchedulerConfigError(
            f"experiment registry not found at {reg_path}. The scheduler never creates one; "
            "run an experiment first or pass --registry."
        )
    if ExperimentRegistry(reg_path).get_portfolio(experiment_id) is None:
        raise SchedulerConfigError(
            f"experiment {experiment_id} has no saved paper portfolio in {reg_path}; "
            "refusing to schedule monitoring against an empty scope."
        )
    from src.cli.monitor import _default_service as build

    return build(experiment_id, db_path, str(reg_path))


def _print_status(db_path: Path, schedule_id: str | None, limit: int, emit: Callable[[str], None]) -> int:
    if not db_path.exists():
        emit(f"no monitoring database at {db_path}: nothing has been scheduled here.")
        return EXIT_OK
    attempts = ScheduleStore(db_path).list_attempts(schedule_id, limit)
    if not attempts:
        emit("no scheduler attempts recorded" + (f" for schedule {schedule_id}" if schedule_id else "") + ".")
        return EXIT_OK
    emit(f"last {len(attempts)} scheduler attempt(s) (newest first):")
    for a in attempts:
        detail = (
            f"run={a.monitoring_run_id} monitoring_status={a.monitoring_status}" if a.outcome == "COMPLETED"
            else f"{a.skip_reason} x{a.skipped_count}" if a.outcome == "SKIPPED"
            else f"error={a.error}"
        )
        emit(f"  {a.started_at.isoformat()} {a.schedule_id} {a.outcome} {detail} ({a.duration_seconds:.2f}s)")
    return EXIT_OK


def run_monitor_scheduler(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[int, str | None, str | None], object] | None = None,
    clock=None,
    wait=None,
    emit: Callable[[str], None] | None = None,
    install_signals: bool = True,
) -> int:
    emit = emit or (lambda line: print(line, flush=True))
    p = argparse.ArgumentParser(prog="python -m src.cli monitor-scheduler")
    p.add_argument("--experiment-id", type=int, help="saved paper portfolio to monitor (required to run)")
    p.add_argument("--interval-seconds", type=float, help="seconds between monitoring runs (1 to 604800)")
    p.add_argument("--schedule-id", default=None, help="schedule identifier (default exp-<experiment-id>)")
    p.add_argument("--registry", default=None, help="experiment registry path (default data/experiments.db)")
    p.add_argument("--db", default=None, help="monitoring database path (default data/monitoring/monitoring.db)")
    p.add_argument("--no-immediate", action="store_true", help="first run after one interval instead of now")
    p.add_argument("--max-runs", type=int, default=None, help="stop after this many runs (default: until stopped)")
    p.add_argument("--log-file", default=None, help="also append status lines to this size-rotated log file")
    p.add_argument("--dispatch-alerts", action="store_true",
                   help="after each monitoring run, deliver pending alert events (requires ATP_ALERTS_ENABLED=true "
                        "and valid ATP_ALERT_* settings; bounded per tick; never calls an LLM)")
    p.add_argument("--status", action="store_true", help="show recent scheduler attempts and exit (read-only)")
    p.add_argument("--limit", type=int, default=20, help="rows shown by --status")
    args = p.parse_args(list(argv) if argv is not None else None)

    db_path = Path(args.db) if args.db else Path(DEFAULT_DB_PATH)
    if args.status:
        return _print_status(db_path, args.schedule_id, args.limit, emit)

    if args.experiment_id is None or args.interval_seconds is None:
        emit("monitor-scheduler: --experiment-id and --interval-seconds are required "
             "(no default scope or interval is assumed). Use --status to inspect past attempts.")
        return EXIT_CONFIG
    try:
        cfg = SchedulerConfig(
            schedule_id=args.schedule_id or f"exp-{args.experiment_id}",
            interval_seconds=args.interval_seconds,
            run_immediately=not args.no_immediate,
            max_runs=args.max_runs,
        )
        service = (service_factory or _default_service)(args.experiment_id, args.db, args.registry)
    except (SchedulerConfigError, ValueError) as exc:
        emit(f"monitor-scheduler: invalid configuration: {exc}")
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001
        emit(f"monitor-scheduler: could not start ({type(exc).__name__}: {str(exc)[:200]})")
        return EXIT_CONFIG

    dispatcher = None
    if args.dispatch_alerts:
        try:
            dispatcher = _build_dispatcher(db_path)
        except Exception as exc:  # noqa: BLE001 -- AlertConfigError and storage errors alike
            emit(f"monitor-scheduler: --dispatch-alerts is not usable: {exc}")
            return EXIT_CONFIG

    lock = SchedulerLock(db_path.parent, cfg.schedule_id)
    try:
        lock.acquire()
    except SchedulerLockError as exc:
        emit(f"monitor-scheduler: {exc}")
        return EXIT_LOCKED

    sink = emit
    if args.log_file:
        from loguru import logger

        logger.add(args.log_file, rotation="5 MB", retention=3, level="INFO")
        sink = lambda line: (emit(line), logger.info(line))  # noqa: E731
    try:
        store = ScheduleStore(db_path)
        scheduler = MonitoringScheduler(service, store, cfg, clock=clock or utc_now, wait=wait, emit=sink)
        if dispatcher is not None:
            scheduler._after_run = lambda run: dispatcher.dispatch(  # noqa: E731
                budget_seconds=DISPATCH_BUDGET_SECONDS, should_stop=lambda: scheduler.stop_requested
            ).one_line()
        sink(f"monitor-scheduler: schedule={cfg.schedule_id} experiment={args.experiment_id} "
             f"interval={cfg.interval_seconds:g}s immediate={cfg.run_immediately} db={db_path}")
        sink("deterministic monitoring only: no LLM, broker, promotion or recovery action is ever called. "
             "Stop with Ctrl+C or SIGTERM.")
        if install_signals:
            with install_signal_handlers(scheduler):
                summary = scheduler.run_forever()
        else:
            summary = scheduler.run_forever()
    except Exception as exc:  # noqa: BLE001
        emit(f"monitor-scheduler: storage unavailable ({type(exc).__name__}: {str(exc)[:200]})")
        return EXIT_CONFIG
    finally:
        lock.release()
    if summary.stopped_reason == "persistence_unavailable":
        emit("monitor-scheduler: attempt records could not be written; stopped rather than run unrecorded.")
        return EXIT_CONFIG
    return EXIT_ATTEMPT_ERRORS if summary.infrastructure_errors else EXIT_OK
