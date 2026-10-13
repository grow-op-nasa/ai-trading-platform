"""Deterministic monitoring scheduler -- src/monitoring/scheduler.py (ADR-0051).

Runs `MonitoringService.run_once()` periodically. It is a plain, single-process
loop with an INJECTABLE clock and wait function (so every semantic below is
tested without real sleeping). It never imports an LLM, broker, execution or
promotion module, never starts at import time, and is only ever started by an
explicit call (the `monitor-scheduler` CLI).

Execution semantics (all recorded as rows in `monitoring_schedule_attempts`):

* First run: immediately (`run_immediately=True`, default) or after one interval.
* Ticks fall on `first_tick + k * interval`; a run executes synchronously in the
  loop, so there is never more than one active execution per schedule.
* OVERLAP: ticks that elapse while a run is still executing are not run later;
  one SKIPPED/OVERLAP row records how many were dropped.
* MISSED: if the loop wakes more than one interval late (process suspended,
  clock jump), the whole intervals that were missed are dropped, recorded as one
  SKIPPED/MISSED row, and a single run happens now. There is no catch-up burst.
* SHUTDOWN: `request_stop()` (wired to SIGINT/SIGTERM by the CLI) lets an
  in-flight run finish, then exits. A tick that was due at that moment is
  recorded as SKIPPED/SHUTDOWN.
* An exception from the monitoring service is recorded as outcome ERROR and the
  schedule continues. A run that could not be persisted is ERROR, never success.
  If the attempt record itself cannot be written three times in a row the
  scheduler aborts rather than continue silently unrecorded.

Monitoring outcomes (HEALTHY/WARNING/CRITICAL/UNKNOWN) are findings data and are
stored with the run; they are NOT scheduler errors. Scheduler errors are
infrastructure failures (outcome ERROR).
"""

from __future__ import annotations

import math
import os
import re
import signal
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from src.monitoring.models import MonitoringRun

MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 7 * 24 * 3600.0
MAX_CONSECUTIVE_RECORD_FAILURES = 3
_SCHEDULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

OUTCOME_COMPLETED, OUTCOME_ERROR, OUTCOME_SKIPPED = "COMPLETED", "ERROR", "SKIPPED"
SKIP_OVERLAP, SKIP_MISSED, SKIP_SHUTDOWN = "OVERLAP", "MISSED", "SHUTDOWN"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SchedulerConfigError(ValueError):
    """Invalid scheduler configuration (reported by the CLI as exit code 2)."""


@dataclass(frozen=True)
class SchedulerConfig:
    """Explicit, validated scheduler settings (no hidden defaults for scope).

    Args:
        schedule_id: stable identifier for this schedule (`[A-Za-z0-9_.-]`, <= 64).
        interval_seconds: seconds between ticks; finite, within [1, 604800].
        run_immediately: first tick now (True) or one interval from now (False).
        max_runs: stop after this many executed runs (None = run until stopped).
    """

    schedule_id: str
    interval_seconds: float
    run_immediately: bool = True
    max_runs: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.schedule_id, str) or not _SCHEDULE_ID.match(self.schedule_id):
            raise SchedulerConfigError(
                "schedule_id must be 1-64 chars of letters, digits, '_', '.', '-' and start alphanumeric"
            )
        iv = self.interval_seconds
        if isinstance(iv, bool) or not isinstance(iv, (int, float)) or not math.isfinite(iv):
            raise SchedulerConfigError(f"interval_seconds must be a finite number, got {iv!r}")
        if iv < MIN_INTERVAL_SECONDS or iv > MAX_INTERVAL_SECONDS:
            raise SchedulerConfigError(
                f"interval_seconds must be between {MIN_INTERVAL_SECONDS:g} and {MAX_INTERVAL_SECONDS:g}, got {iv}"
            )
        if self.max_runs is not None and (isinstance(self.max_runs, bool) or not isinstance(self.max_runs, int)
                                          or self.max_runs < 1):
            raise SchedulerConfigError(f"max_runs must be a positive integer or None, got {self.max_runs!r}")


# ---------------------------------------------------------------------------
# Attempt records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleAttempt:
    attempt_id: str
    schedule_id: str
    scheduled_for: datetime
    started_at: datetime
    finished_at: datetime
    outcome: str                      # COMPLETED | ERROR | SKIPPED
    monitoring_run_id: str | None = None
    monitoring_status: str | None = None
    skip_reason: str | None = None    # OVERLAP | MISSED | SHUTDOWN
    skipped_count: int = 0
    error: str | None = None
    pid: int = field(default_factory=os.getpid)

    def __post_init__(self) -> None:
        for name in ("scheduled_for", "started_at", "finished_at"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware (UTC)")

    @property
    def duration_seconds(self) -> float:
        return max((self.finished_at - self.started_at).total_seconds(), 0.0)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitoring_schedule_attempts (
    attempt_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    outcome TEXT NOT NULL,
    monitoring_run_id TEXT,
    monitoring_status TEXT,
    skip_reason TEXT,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS idx_attempts_sched ON monitoring_schedule_attempts(schedule_id, started_at);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON monitoring_schedule_attempts(monitoring_run_id);
"""


class ScheduleStore:
    """Attempt log in the same SQLite file as `MonitoringStore` (its own table)."""

    def __init__(self, db_path: Path | str) -> None:
        self._memory = str(db_path) == ":memory:"
        self._path = Path(str(db_path))
        self._shared: sqlite3.Connection | None = None
        if not self._memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        try:
            with conn:
                conn.executescript(_SCHEMA)
        finally:
            self._release(conn)

    def _conn(self) -> sqlite3.Connection:
        if self._memory:
            if self._shared is None:
                self._shared = sqlite3.connect(":memory:")
            return self._shared
        return sqlite3.connect(self._path)

    def _release(self, conn: sqlite3.Connection) -> None:
        if not self._memory:
            conn.close()

    def record(self, a: ScheduleAttempt) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO monitoring_schedule_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a.attempt_id, a.schedule_id, a.scheduled_for.isoformat(), a.started_at.isoformat(),
                     a.finished_at.isoformat(), a.duration_seconds, a.outcome, a.monitoring_run_id,
                     a.monitoring_status, a.skip_reason, a.skipped_count, a.error, a.pid),
                )
        finally:
            self._release(conn)

    def _query(self, sql: str, params: tuple = ()) -> list[ScheduleAttempt]:
        conn = self._conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            self._release(conn)
        return [
            ScheduleAttempt(
                attempt_id=r[0], schedule_id=r[1], scheduled_for=datetime.fromisoformat(r[2]),
                started_at=datetime.fromisoformat(r[3]), finished_at=datetime.fromisoformat(r[4]),
                outcome=r[6], monitoring_run_id=r[7], monitoring_status=r[8], skip_reason=r[9],
                skipped_count=r[10], error=r[11], pid=r[12],
            )
            for r in rows
        ]

    _COLS = ("attempt_id, schedule_id, scheduled_for, started_at, finished_at, duration_seconds, outcome, "
             "monitoring_run_id, monitoring_status, skip_reason, skipped_count, error, pid")

    def list_attempts(self, schedule_id: str | None = None, limit: int = 20) -> list[ScheduleAttempt]:
        if schedule_id:
            return self._query(
                f"SELECT {self._COLS} FROM monitoring_schedule_attempts WHERE schedule_id=? "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?", (schedule_id, limit))
        return self._query(
            f"SELECT {self._COLS} FROM monitoring_schedule_attempts ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (limit,))

    def attempts_for_run(self, monitoring_run_id: str) -> list[ScheduleAttempt]:
        return self._query(
            f"SELECT {self._COLS} FROM monitoring_schedule_attempts WHERE monitoring_run_id=?", (monitoring_run_id,))


# ---------------------------------------------------------------------------
# Duplicate-scheduler protection (single host, POSIX advisory lock)
# ---------------------------------------------------------------------------


class SchedulerLockError(RuntimeError):
    """Another scheduler with the same schedule id holds the lock."""


class SchedulerLock:
    """`fcntl.flock` on `<dir>/monitoring_scheduler.<schedule_id>.lock`.

    The kernel releases the lock when the process exits or crashes, so there are
    no stale locks to clean up. Scope: one host (a local file); it is deliberately
    not a distributed lock. Unsupported without `fcntl` (Windows) -> clear error.
    """

    def __init__(self, directory: Path | str, schedule_id: str) -> None:
        self._path = Path(directory) / f"monitoring_scheduler.{schedule_id}.lock"
        self._fh = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - non-POSIX
            raise SchedulerLockError("duplicate-scheduler protection needs POSIX fcntl; unsupported here") from exc
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            fh.close()
            raise SchedulerLockError(
                f"another monitoring scheduler already holds {self._path} (holder: {holder})"
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} since={utc_now().isoformat()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


@dataclass
class SchedulerSummary:
    runs: int = 0
    completed: int = 0
    errors: int = 0
    skipped_ticks: int = 0
    record_failures: int = 0
    stopped_reason: str = ""

    @property
    def infrastructure_errors(self) -> int:
        return self.errors + self.record_failures


class MonitoringScheduler:
    """Args:
        service: anything with `run_once() -> MonitoringRun` (a `MonitoringService`).
        store: `ScheduleStore` receiving one row per attempt/skip.
        config: validated `SchedulerConfig`.
        clock: UTC-aware time source (inject a fake in tests).
        wait: `wait(seconds) -> bool`, True if a stop was requested while waiting.
            Default blocks on the stop event, so `request_stop()` interrupts it.
        emit: one-line status output (default: no output).
    """

    def __init__(
        self,
        service,
        store: ScheduleStore,
        config: SchedulerConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
        wait: Callable[[float], bool] | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self._service = service
        self._store = store
        self._cfg = config
        self._clock = clock
        self._stop = threading.Event()
        self._wait = wait or (lambda seconds: self._stop.wait(timeout=seconds))
        self._emit = emit or (lambda line: None)
        self._consecutive_record_failures = 0
        self.summary = SchedulerSummary()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    # -- main loop -------------------------------------------------------------
    def run_forever(self) -> SchedulerSummary:
        interval = timedelta(seconds=self._cfg.interval_seconds)
        start = self._clock()
        next_due = start if self._cfg.run_immediately else start + interval
        self._emit(f"scheduler {self._cfg.schedule_id}: started, interval={self._cfg.interval_seconds:g}s, "
                   f"first tick {next_due.isoformat()}")
        while True:
            if self._stop.is_set():
                self.summary.stopped_reason = self.summary.stopped_reason or "stop_requested"
                break
            delay = (next_due - self._clock()).total_seconds()
            if delay > 0 and self._wait(delay):
                self.summary.stopped_reason = self.summary.stopped_reason or "stop_requested"
                break
            now = self._clock()
            late = now - next_due
            if late >= interval:  # woke far too late: drop whole missed intervals, no catch-up burst
                missed = int(late // interval)
                self._skip(next_due, now, SKIP_MISSED, missed)
                next_due = next_due + missed * interval
            if self._stop.is_set():
                self._skip(next_due, self._clock(), SKIP_SHUTDOWN, 1)
                self.summary.stopped_reason = self.summary.stopped_reason or "stop_requested"
                break
            scheduled = next_due
            self._attempt(scheduled)
            self.summary.runs += 1
            if self.summary.stopped_reason == "persistence_unavailable":
                break
            finished = self._clock()
            ticks_passed = int((finished - scheduled) // interval)  # ticks that elapsed during the run
            if ticks_passed > 0:
                self._skip(scheduled + interval, finished, SKIP_OVERLAP, ticks_passed)
            next_due = scheduled + (ticks_passed + 1) * interval
            if self._cfg.max_runs is not None and self.summary.runs >= self._cfg.max_runs:
                self.summary.stopped_reason = "max_runs"
                break
        self._emit(f"scheduler {self._cfg.schedule_id}: stopped ({self.summary.stopped_reason}); "
                   f"runs={self.summary.runs} completed={self.summary.completed} "
                   f"errors={self.summary.errors} skipped_ticks={self.summary.skipped_ticks}")
        return self.summary

    # -- one execution ----------------------------------------------------------
    def _attempt(self, scheduled: datetime) -> None:
        started = self._clock()
        run: MonitoringRun | None = None
        error: str | None = None
        try:
            run = self._service.run_once()
        except Exception as exc:  # noqa: BLE001 -- the schedule must survive a failed run
            error = _safe_error(exc)
        outcome, status, run_id = OUTCOME_COMPLETED, None, None
        if run is not None:
            run_id, status = run.run_id, run.status.value
            if not run.persisted:
                outcome = OUTCOME_ERROR
                error = f"monitoring run was not persisted: {run.persistence_error or 'unknown persistence failure'}"
        else:
            outcome = OUTCOME_ERROR
        finished = self._clock()
        attempt = ScheduleAttempt(
            attempt_id=uuid4().hex, schedule_id=self._cfg.schedule_id, scheduled_for=scheduled,
            started_at=started, finished_at=finished, outcome=outcome, monitoring_run_id=run_id,
            monitoring_status=status, error=error,
        )
        if outcome == OUTCOME_COMPLETED:
            self.summary.completed += 1
            extra = f" run={run_id} monitoring_status={status}" + (f" run_errors={len(run.errors)}" if run.errors else "")
            self._emit(f"{finished.isoformat()} attempt COMPLETED{extra} ({attempt.duration_seconds:.2f}s)")
        else:
            self.summary.errors += 1
            self._emit(f"{finished.isoformat()} attempt SCHEDULER-ERROR {error} ({attempt.duration_seconds:.2f}s)")
        self._record(attempt)

    def _skip(self, scheduled: datetime, now: datetime, reason: str, count: int) -> None:
        self.summary.skipped_ticks += count
        self._emit(f"{now.isoformat()} skipped {count} tick(s): {reason}")
        self._record(ScheduleAttempt(
            attempt_id=uuid4().hex, schedule_id=self._cfg.schedule_id, scheduled_for=scheduled,
            started_at=now, finished_at=now, outcome=OUTCOME_SKIPPED, skip_reason=reason, skipped_count=count,
        ))

    def _record(self, attempt: ScheduleAttempt) -> None:
        try:
            self._store.record(attempt)
            self._consecutive_record_failures = 0
        except Exception as exc:  # noqa: BLE001
            self._consecutive_record_failures += 1
            self.summary.record_failures += 1
            self._emit(f"SCHEDULER-ERROR could not record attempt {attempt.attempt_id}: {_safe_error(exc)}")
            if self._consecutive_record_failures >= MAX_CONSECUTIVE_RECORD_FAILURES:
                self.summary.stopped_reason = "persistence_unavailable"
                self._stop.set()


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"


@contextmanager
def install_signal_handlers(scheduler: MonitoringScheduler) -> Iterator[None]:
    """Route SIGINT/SIGTERM to a graceful stop; restore the previous handlers on exit.
    Only valid on the main thread (Python restriction)."""
    previous = {}

    def handler(signum, frame):  # noqa: ARG001
        scheduler.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
