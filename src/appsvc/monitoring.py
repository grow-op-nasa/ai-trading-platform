"""Monitoring and alert views and explicit operator actions (Sprint 19). Read-only unless a method says otherwise."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from src.appsvc.paths import AppPaths
from src.monitoring.alerts.config import AlertConfigError  # noqa: F401  (re-exported for the UI)


def _store(paths: AppPaths):
    if not paths.monitoring_exists():
        return None
    from src.monitoring.store import MonitoringStore
    return MonitoringStore(paths.monitoring_db)


def available(paths: AppPaths) -> bool:
    return paths.monitoring_exists()


def runs(paths: AppPaths, limit: int = 20) -> list:
    s = _store(paths)
    return s.list_runs(limit) if s else []


def get_run(paths: AppPaths, run_id: str):
    s = _store(paths)
    return s.get_run(run_id) if s else None


def findings(paths: AppPaths, *, status: str | None = None, severity: str | None = None, limit: int = 100) -> list:
    s = _store(paths)
    return s.list_findings(status=status, severity=severity, limit=limit) if s else []


def open_findings(paths: AppPaths) -> dict[str, list]:
    s = _store(paths)
    if not s:
        return {"CRITICAL": [], "WARNING": []}
    allf = s.list_findings(status="OPEN", limit=500)
    return {"CRITICAL": [f for f in allf if f.severity.value == "CRITICAL"],
            "WARNING": [f for f in allf if f.severity.value == "WARNING"]}


def alert_counts(paths: AppPaths) -> dict[str, int] | None:
    s = _store(paths)
    if not s:
        return None
    try:
        return dict(s.alerts.counts())
    except Exception:  # noqa: BLE001
        return None


def alert_events(paths: AppPaths, limit: int = 50) -> list:
    s = _store(paths)
    return s.alerts.list_events(limit=limit) if s else []


def deliveries(paths: AppPaths, status: str | None = None, limit: int = 100) -> list:
    s = _store(paths)
    if not s:
        return []
    from src.monitoring.alerts.models import DeliveryStatus
    return s.alerts.list_deliveries(status=DeliveryStatus(status) if status else None, limit=limit)


def attempts(paths: AppPaths, delivery_id: str) -> list:
    s = _store(paths)
    return s.alerts.attempts_for(delivery_id) if s else []


def scheduler_attempts(paths: AppPaths, limit: int = 20) -> list:
    if not paths.monitoring_exists():
        return []
    import sqlite3
    try:      # ScheduleStore creates its table on open; only open it when the table already exists (view stays read-only)
        conn = sqlite3.connect(f"file:{paths.monitoring_db}?mode=ro", uri=True)
        try:
            found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitoring_schedule_attempts'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    if not found:
        return []
    from src.monitoring.scheduler import ScheduleStore
    return ScheduleStore(paths.monitoring_db).list_attempts(None, limit)


def is_synthetic(run: Any) -> bool:
    """A run is labelled synthetic only if its own stored record says so (never guessed)."""
    blob = f"{getattr(run, 'source', '')} {getattr(run, 'config_snapshot', '')}".lower()
    return "synthetic" in blob or "injected" in blob or "fault" in blob


# ------------------------------------------------------------------ explicit operator actions (existing alert subsystem)
def retry_failed(paths: AppPaths, *, delivery_id: str | None = None, clock: Callable[[], datetime] | None = None) -> int:
    """Requeue FAILED deliveries (same event ids; no new events; no monitoring run; nothing is sent here)."""
    s = _store(paths)
    if not s:
        return 0
    return s.alerts.requeue_failed((clock or (lambda: datetime.now(timezone.utc)))(), delivery_id=delivery_id)


def dispatch_pending(paths: AppPaths, *, limit: int | None = None, env=None, transport_factory=None,
                     clock: Callable[[], datetime] | None = None):
    """Deliver eligible alerts once, via the existing dispatcher (retry/lease/ordering rules unchanged)."""
    from src.monitoring.alerts.config import AlertConfig
    from src.monitoring.alerts.dispatcher import AlertDispatcher
    from src.monitoring.alerts.transports import build_transport
    s = _store(paths)
    if not s:
        raise AlertConfigError("no monitoring database yet; there is nothing to dispatch")
    cfg = AlertConfig.from_env(env)
    if not cfg.enabled:
        raise AlertConfigError("alerting is not enabled (ATP_ALERTS_ENABLED=true required)")
    cfg.validate()
    transport = (transport_factory or build_transport)(cfg)
    kwargs = {"clock": clock} if clock else {}
    return AlertDispatcher(s.alerts, transport, cfg, **kwargs).dispatch(limit=limit)
