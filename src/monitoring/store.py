"""Monitoring persistence -- src/monitoring/store.py (ADR-0050).

A small SQLite store (stdlib `sqlite3`, the same convention as
`ExperimentRegistry`) at `data/monitoring/monitoring.db` -- already
Git-ignored by the repository's wholesale `data/*` rule. Holds runs and
findings; nothing about trading state.

`apply()` writes a run and all its finding changes in ONE transaction,
so a failure never leaves a half-updated lifecycle.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.monitoring.alerts.models import AlertEvent
from src.monitoring.alerts.store import ALERT_SCHEMA, AlertStore, insert_events, mark_initialized
from src.monitoring.models import (
    CheckStatus,
    CheckSummary,
    FindingSeverity,
    FindingStatus,
    MonitoringEvidence,
    MonitoringRun,
    MonitoringStatus,
    MonitorFinding,
)

DEFAULT_DB_PATH = Path("data/monitoring/monitoring.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitoring_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitoring_findings (
    finding_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_fp ON monitoring_findings(fingerprint, status);
"""


def _finding_to_json(f: MonitorFinding) -> str:
    return json.dumps(
        {
            "finding_id": f.finding_id, "fingerprint": f.fingerprint, "rule_id": f.rule_id,
            "category": f.category, "severity": f.severity.value, "status": f.status.value,
            "title": f.title, "description": f.description, "observed_value": f.observed_value,
            "expected_value": f.expected_value, "entities": f.entities,
            "first_seen": f.first_seen.isoformat(), "last_seen": f.last_seen.isoformat(),
            "occurrence_count": f.occurrence_count, "rule_version": f.rule_version,
            "last_run_id": f.last_run_id,
            "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
            "evidence": [
                {"reference": e.reference, "kind": e.kind, "summary": e.summary, "data": e.data}
                for e in f.evidence
            ],
        },
        default=str,
    )


def _finding_from_json(text: str) -> MonitorFinding:
    raw = json.loads(text)
    return MonitorFinding(
        finding_id=raw["finding_id"], fingerprint=raw["fingerprint"], rule_id=raw["rule_id"],
        category=raw["category"], severity=FindingSeverity(raw["severity"]),
        status=FindingStatus(raw["status"]), title=raw["title"], description=raw["description"],
        observed_value=raw["observed_value"], expected_value=raw["expected_value"],
        entities=raw["entities"], first_seen=datetime.fromisoformat(raw["first_seen"]),
        last_seen=datetime.fromisoformat(raw["last_seen"]), occurrence_count=raw["occurrence_count"],
        evidence=[MonitoringEvidence(**e) for e in raw["evidence"]], rule_version=raw["rule_version"],
        last_run_id=raw["last_run_id"],
        resolved_at=datetime.fromisoformat(raw["resolved_at"]) if raw["resolved_at"] else None,
    )


def _run_to_json(run: MonitoringRun) -> str:
    return json.dumps(
        {
            "run_id": run.run_id, "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat(), "status": run.status.value,
            "checks": [
                {"check_id": c.check_id, "status": c.status.value, "reason": c.reason,
                 "summary": c.summary, "finding_ids": list(c.finding_ids), "notes": list(c.notes)}
                for c in run.checks
            ],
            "config_version": run.config_version, "config_snapshot": run.config_snapshot,
            "source": run.source, "finding_ids": run.finding_ids,
            "resolved_finding_ids": run.resolved_finding_ids, "errors": run.errors,
        },
        default=str,
    )


def _run_from_json(text: str) -> MonitoringRun:
    raw = json.loads(text)
    return MonitoringRun(
        run_id=raw["run_id"], started_at=datetime.fromisoformat(raw["started_at"]),
        finished_at=datetime.fromisoformat(raw["finished_at"]), status=MonitoringStatus(raw["status"]),
        checks=[
            CheckSummary(c["check_id"], CheckStatus(c["status"]), c["reason"], c["summary"],
                         tuple(c["finding_ids"]), tuple(c["notes"]))
            for c in raw["checks"]
        ],
        config_version=raw["config_version"], config_snapshot=raw["config_snapshot"],
        source=raw["source"], finding_ids=raw["finding_ids"],
        resolved_finding_ids=raw["resolved_finding_ids"], errors=raw["errors"], persisted=True,
    )


class MonitoringStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self._memory = str(db_path) == ":memory:"
        self._path = Path(str(db_path))
        self._shared: sqlite3.Connection | None = None
        if not self._memory:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            c.executescript(ALERT_SCHEMA)
        self._alerts = AlertStore(self)

    @property
    def alerts(self) -> AlertStore:
        """The durable alert outbox sharing this store's database (ADR-0052)."""
        return self._alerts

    def _close(self, conn: sqlite3.Connection) -> None:
        if not self._memory:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        if self._memory:
            if self._shared is None:
                self._shared = sqlite3.connect(":memory:")
            return self._shared
        return sqlite3.connect(self._path)

    # -- writes (one transaction) -------------------------------------
    def apply(
        self,
        run: MonitoringRun,
        findings: list[MonitorFinding],
        *,
        alert_events: list[AlertEvent] | None = None,
        alert_channels: tuple[str, ...] = (),
        alert_initialize_policy: str | None = None,
    ) -> None:
        """Persist the run, its finding transitions AND the alert events (+ their pending deliveries) for
        those transitions in ONE transaction: either all are stored or none are (ADR-0052)."""
        conn = self._conn()
        try:
            with conn:  # commit on success, rollback on any exception
                for f in findings:
                    conn.execute(
                        "INSERT OR REPLACE INTO monitoring_findings VALUES (?,?,?,?,?,?,?)",
                        (f.finding_id, f.fingerprint, f.status.value, f.severity.value, f.category,
                         f.last_seen.isoformat(), _finding_to_json(f)),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO monitoring_runs VALUES (?,?,?)",
                    (run.run_id, run.started_at.isoformat(), _run_to_json(run)),
                )
                if alert_events:
                    insert_events(conn, alert_events, alert_channels, run.finished_at)
                if alert_initialize_policy is not None:
                    mark_initialized(conn, run.finished_at, alert_initialize_policy)
        finally:
            if not self._memory:
                conn.close()

    # -- reads ---------------------------------------------------------
    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._conn()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            if not self._memory:
                conn.close()

    def open_by_fingerprint(self) -> dict[str, MonitorFinding]:
        rows = self._rows("SELECT payload FROM monitoring_findings WHERE status = 'OPEN'")
        return {f.fingerprint: f for f in (_finding_from_json(r[0]) for r in rows)}

    def get_finding(self, finding_id: str) -> MonitorFinding | None:
        rows = self._rows("SELECT payload FROM monitoring_findings WHERE finding_id = ?", (finding_id,))
        return _finding_from_json(rows[0][0]) if rows else None

    def list_findings(
        self,
        *,
        severity: str | None = None,
        category: str | None = None,
        status: str | None = None,
        entity: str | None = None,
        limit: int = 50,
    ) -> list[MonitorFinding]:
        clauses, params = [], []
        for col, val in (("severity", severity), ("category", category), ("status", status)):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val.upper() if col != "category" else val)
        sql = "SELECT payload FROM monitoring_findings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_seen DESC"
        out = [_finding_from_json(r[0]) for r in self._rows(sql, tuple(params))]
        if entity:
            out = [f for f in out if entity in {str(v) for v in f.entities.values()}]
        return out[:limit]

    def get_run(self, run_id: str) -> MonitoringRun | None:
        rows = self._rows("SELECT payload FROM monitoring_runs WHERE run_id = ?", (run_id,))
        return _run_from_json(rows[0][0]) if rows else None

    def list_runs(self, limit: int = 20) -> list[MonitoringRun]:
        rows = self._rows(
            "SELECT payload FROM monitoring_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [_run_from_json(r[0]) for r in rows]
