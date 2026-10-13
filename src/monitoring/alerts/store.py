"""Durable alert outbox -- src/monitoring/alerts/store.py (ADR-0052).

Lives in the SAME SQLite file as the findings (`monitoring.db`), so the alert events for a
run's finding transitions are written in the SAME transaction as the transitions themselves
(`MonitoringStore.apply`). Delivery state is separate rows (`alert_deliveries`,
`alert_attempts`): a failed or slow transport never touches findings or runs.

Guarantees (see DECISIONS.md, ADR-0052):
* a logical event exists at most once per `idempotency_key` (UNIQUE constraint);
* a delivery is claimed with a single conditional UPDATE, so two dispatchers cannot both send it
  (lease-based; an expired lease after a crash is re-claimable, which can duplicate an external
  send at the crash boundary -- at-least-once, never claimed as exactly-once);
* events for one fingerprint are delivered in creation order (a later event is held back while an
  earlier one is still pending/retrying/in progress); terminal FAILED does not block.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from src.monitoring.alerts.models import (
    AlertAttempt,
    AlertDelivery,
    AlertEvent,
    AlertEventType,
    DeliveryStatus,
)
from src.monitoring.models import FindingSeverity, FindingStatus

ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    previous_severity TEXT,
    finding_status TEXT NOT NULL,
    title TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_events_finding ON alert_events(finding_id);
CREATE INDEX IF NOT EXISTS idx_alert_events_fp ON alert_events(fingerprint, seq);
CREATE TABLE IF NOT EXISTS alert_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    external_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(event_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_status ON alert_deliveries(channel, status);
CREATE TABLE IF NOT EXISTS alert_attempts (
    attempt_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL,
    error TEXT,
    external_id TEXT,
    next_retry_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_attempts_delivery ON alert_attempts(delivery_id, attempt_no);
CREATE TABLE IF NOT EXISTS alert_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width => lexicographic order == chronological order
_ACTIVE = "('PENDING','IN_PROGRESS','RETRY_PENDING')"


def ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("alert timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime(_FMT)


def parse_ts(text: str | None) -> datetime | None:
    return datetime.strptime(text, _FMT).replace(tzinfo=timezone.utc) if text else None


def new_id() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# Writes used INSIDE MonitoringStore.apply's transaction
# ---------------------------------------------------------------------------
def insert_events(conn: sqlite3.Connection, events: Iterable[AlertEvent], channels: Iterable[str],
                  now: datetime) -> int:
    """Insert events (idempotent on `idempotency_key`) and one PENDING delivery per channel for each
    newly inserted event. Caller owns the transaction. Returns the number of new events."""
    created = 0
    channels = tuple(channels)
    for e in events:
        cur = conn.execute(
            "INSERT OR IGNORE INTO alert_events (event_id, idempotency_key, event_type, finding_id, "
            "fingerprint, rule_id, severity, previous_severity, finding_status, title, run_id, sequence, "
            "occurred_at, created_at, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e.event_id, e.idempotency_key, e.event_type.value, e.finding_id, e.fingerprint, e.rule_id,
             e.severity.value, e.previous_severity.value if e.previous_severity else None,
             e.finding_status.value, e.title, e.run_id, e.sequence, ts(e.occurred_at), ts(e.created_at),
             e.payload_json()),
        )
        if cur.rowcount != 1:
            continue
        created += 1
        for ch in channels:
            conn.execute(
                "INSERT OR IGNORE INTO alert_deliveries (delivery_id, event_id, channel, status, attempt_count, "
                "created_at, updated_at) VALUES (?,?,?,?,0,?,?)",
                (new_id(), e.event_id, ch, DeliveryStatus.PENDING.value, ts(now), ts(now)),
            )
    return created


def mark_initialized(conn: sqlite3.Connection, now: datetime, policy: str) -> None:
    conn.execute("INSERT OR IGNORE INTO alert_meta (key, value) VALUES ('initialized_at', ?)", (ts(now),))
    conn.execute("INSERT OR IGNORE INTO alert_meta (key, value) VALUES ('initial_policy', ?)", (policy,))


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------
def _event(row: tuple) -> AlertEvent:
    (event_id, key, etype, fid, fp, rule, sev, prev, fstatus, title, run_id, seq, occ, created, payload) = row
    p = json.loads(payload)
    return AlertEvent(
        event_id=event_id, idempotency_key=key, event_type=AlertEventType(etype), finding_id=fid,
        fingerprint=fp, rule_id=rule, severity=FindingSeverity(sev),
        previous_severity=FindingSeverity(prev) if prev else None, finding_status=FindingStatus(fstatus),
        title=title, run_id=run_id, sequence=seq, occurred_at=parse_ts(occ), created_at=parse_ts(created),
        snapshot=p.get("snapshot", {}), reason=p.get("reason", "transition"),
    )


_EVENT_COLS = ("event_id, idempotency_key, event_type, finding_id, fingerprint, rule_id, severity, "
               "previous_severity, finding_status, title, run_id, sequence, occurred_at, created_at, payload")
_DELIVERY_COLS = ("delivery_id, event_id, channel, status, attempt_count, next_attempt_at, lease_expires_at, "
                  "last_error, external_id, created_at, updated_at, delivered_at")
_ATTEMPT_COLS = ("attempt_id, delivery_id, event_id, channel, attempt_no, started_at, finished_at, outcome, "
                 "error, external_id, next_retry_at")


def _delivery(r: tuple) -> AlertDelivery:
    return AlertDelivery(
        delivery_id=r[0], event_id=r[1], channel=r[2], status=DeliveryStatus(r[3]), attempt_count=r[4],
        next_attempt_at=parse_ts(r[5]), lease_expires_at=parse_ts(r[6]), last_error=r[7], external_id=r[8],
        created_at=parse_ts(r[9]), updated_at=parse_ts(r[10]), delivered_at=parse_ts(r[11]),
    )


def _attempt(r: tuple) -> AlertAttempt:
    return AlertAttempt(
        attempt_id=r[0], delivery_id=r[1], event_id=r[2], channel=r[3], attempt_no=r[4],
        started_at=parse_ts(r[5]), finished_at=parse_ts(r[6]), outcome=r[7], error=r[8], external_id=r[9],
        next_retry_at=parse_ts(r[10]),
    )


class AlertStore:
    """Read/claim/complete operations over the outbox. Bound to a `MonitoringStore`'s connection."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    # -- plumbing
    def _run(self, fn):
        conn = self._owner._conn()
        try:
            return fn(conn)
        finally:
            self._owner._close(conn)

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self._run(lambda c: c.execute(sql, params).fetchall())

    # -- meta
    def is_initialized(self) -> bool:
        return bool(self._rows("SELECT 1 FROM alert_meta WHERE key='initialized_at'"))

    def initialized_info(self) -> dict[str, str]:
        return {k: v for k, v in self._rows("SELECT key, value FROM alert_meta")}

    # -- reads
    def get_event(self, event_id: str) -> AlertEvent | None:
        rows = self._rows(f"SELECT {_EVENT_COLS} FROM alert_events WHERE event_id=?", (event_id,))
        return _event(rows[0]) if rows else None

    def get_event_by_key(self, key: str) -> AlertEvent | None:
        rows = self._rows(f"SELECT {_EVENT_COLS} FROM alert_events WHERE idempotency_key=?", (key,))
        return _event(rows[0]) if rows else None

    def list_events(self, *, finding_id: str | None = None, run_id: str | None = None, limit: int = 50) -> list[AlertEvent]:
        sql, params = f"SELECT {_EVENT_COLS} FROM alert_events", []
        clauses = []
        if finding_id:
            clauses.append("finding_id=?"); params.append(finding_id)
        if run_id:
            clauses.append("run_id=?"); params.append(run_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        return [_event(r) for r in self._rows(sql, tuple(params))]

    def events_for_run(self, run_id: str) -> list[AlertEvent]:
        rows = self._rows(f"SELECT {_EVENT_COLS} FROM alert_events WHERE run_id=? ORDER BY seq", (run_id,))
        return [_event(r) for r in rows]

    def list_deliveries(self, *, status: DeliveryStatus | None = None, event_id: str | None = None,
                        finding_id: str | None = None, limit: int = 100) -> list[AlertDelivery]:
        sql = f"SELECT {', '.join('d.' + c.strip() for c in _DELIVERY_COLS.split(','))} FROM alert_deliveries d " \
              "JOIN alert_events e ON e.event_id = d.event_id"
        clauses, params = [], []
        if status:
            clauses.append("d.status=?"); params.append(status.value)
        if event_id:
            clauses.append("d.event_id=?"); params.append(event_id)
        if finding_id:
            clauses.append("e.finding_id=?"); params.append(finding_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.seq DESC, d.channel LIMIT ?"
        params.append(limit)
        return [_delivery(r) for r in self._rows(sql, tuple(params))]

    def get_delivery(self, delivery_id: str) -> AlertDelivery | None:
        rows = self._rows(f"SELECT {_DELIVERY_COLS} FROM alert_deliveries WHERE delivery_id=?", (delivery_id,))
        return _delivery(rows[0]) if rows else None

    def attempts_for(self, delivery_id: str) -> list[AlertAttempt]:
        rows = self._rows(f"SELECT {_ATTEMPT_COLS} FROM alert_attempts WHERE delivery_id=? ORDER BY attempt_no",
                          (delivery_id,))
        return [_attempt(r) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in DeliveryStatus}
        for status, n in self._rows("SELECT status, COUNT(*) FROM alert_deliveries GROUP BY status"):
            out[status] = n
        out["events"] = self._rows("SELECT COUNT(*) FROM alert_events")[0][0]
        return out

    # -- claim / complete (the dispatcher's only write paths)
    def claim_next(self, channel: str, now: datetime, lease_seconds: float,
                   *, event_id: str | None = None) -> tuple[AlertDelivery, AlertEvent, AlertAttempt] | None:
        def op(conn: sqlite3.Connection):
            extra = " AND d.event_id=?" if event_id else ""
            params: list[Any] = [channel, ts(now), ts(now)]
            if event_id:
                params.append(event_id)
            cands = conn.execute(
                "SELECT d.delivery_id FROM alert_deliveries d JOIN alert_events e ON e.event_id=d.event_id "
                "WHERE d.channel=? AND (d.status='PENDING' OR (d.status='RETRY_PENDING' AND d.next_attempt_at<=?) "
                "OR (d.status='IN_PROGRESS' AND d.lease_expires_at<=?))" + extra +
                " AND NOT EXISTS (SELECT 1 FROM alert_deliveries d2 JOIN alert_events e2 ON e2.event_id=d2.event_id "
                f"WHERE d2.channel=d.channel AND e2.fingerprint=e.fingerprint AND e2.seq<e.seq AND d2.status IN {_ACTIVE}) "
                "ORDER BY e.seq LIMIT 25", tuple(params)).fetchall()
            lease = now + timedelta(seconds=lease_seconds)
            for (did,) in cands:
                with conn:
                    cur = conn.execute(
                        "UPDATE alert_deliveries SET status='IN_PROGRESS', attempt_count=attempt_count+1, "
                        "lease_expires_at=?, updated_at=? WHERE delivery_id=? AND "
                        "(status='PENDING' OR (status='RETRY_PENDING' AND next_attempt_at<=?) "
                        "OR (status='IN_PROGRESS' AND lease_expires_at<=?))",
                        (ts(lease), ts(now), did, ts(now), ts(now)))
                    if cur.rowcount != 1:
                        continue  # another dispatcher won the claim
                    conn.execute("UPDATE alert_attempts SET outcome='ABANDONED', finished_at=? "
                                 "WHERE delivery_id=? AND outcome='IN_PROGRESS'", (ts(now), did))
                    n = conn.execute("SELECT COUNT(*) FROM alert_attempts WHERE delivery_id=?", (did,)).fetchone()[0]
                    aid = new_id()
                    conn.execute(
                        "INSERT INTO alert_attempts (attempt_id, delivery_id, event_id, channel, attempt_no, "
                        "started_at, outcome) SELECT ?, delivery_id, event_id, channel, ?, ?, 'IN_PROGRESS' "
                        "FROM alert_deliveries WHERE delivery_id=?", (aid, n + 1, ts(now), did))
                d = _delivery(conn.execute(f"SELECT {_DELIVERY_COLS} FROM alert_deliveries WHERE delivery_id=?",
                                           (did,)).fetchone())
                e = _event(conn.execute(f"SELECT {_EVENT_COLS} FROM alert_events WHERE event_id=?",
                                        (d.event_id,)).fetchone())
                a = _attempt(conn.execute(f"SELECT {_ATTEMPT_COLS} FROM alert_attempts WHERE attempt_id=?",
                                          (aid,)).fetchone())
                return d, e, a
            return None

        return self._run(op)

    def complete_delivered(self, delivery_id: str, attempt_id: str, now: datetime, external_id: str | None) -> None:
        def op(conn):
            with conn:
                conn.execute("UPDATE alert_deliveries SET status='DELIVERED', delivered_at=?, updated_at=?, "
                             "external_id=?, lease_expires_at=NULL, next_attempt_at=NULL, last_error=NULL "
                             "WHERE delivery_id=?", (ts(now), ts(now), external_id, delivery_id))
                conn.execute("UPDATE alert_attempts SET outcome='DELIVERED', finished_at=?, external_id=? "
                             "WHERE attempt_id=?", (ts(now), external_id, attempt_id))
        self._run(op)

    def complete_failed(self, delivery_id: str, attempt_id: str, now: datetime, error: str, *,
                        retry_at: datetime | None, permanent: bool) -> DeliveryStatus:
        """Record a failed attempt. `retry_at` set => RETRY_PENDING; otherwise terminal FAILED."""
        status = DeliveryStatus.RETRY_PENDING if retry_at is not None else DeliveryStatus.FAILED

        def op(conn):
            with conn:
                conn.execute("UPDATE alert_deliveries SET status=?, updated_at=?, last_error=?, "
                             "lease_expires_at=NULL, next_attempt_at=? WHERE delivery_id=?",
                             (status.value, ts(now), error, ts(retry_at), delivery_id))
                conn.execute("UPDATE alert_attempts SET outcome=?, finished_at=?, error=?, next_retry_at=? "
                             "WHERE attempt_id=?",
                             ("PERMANENT_FAILURE" if permanent else "RETRYABLE_FAILURE", ts(now), error,
                              ts(retry_at), attempt_id))
        self._run(op)
        return status

    def requeue_failed(self, now: datetime, *, delivery_id: str | None = None, event_id: str | None = None) -> int:
        """Operator action: FAILED -> RETRY_PENDING (fresh retry cycle). Same event, same delivery row."""
        def op(conn):
            sql = "UPDATE alert_deliveries SET status='RETRY_PENDING', attempt_count=0, next_attempt_at=?, " \
                  "updated_at=? WHERE status='FAILED'"
            params: list[Any] = [ts(now), ts(now)]
            if delivery_id:
                sql += " AND delivery_id=?"; params.append(delivery_id)
            if event_id:
                sql += " AND event_id=?"; params.append(event_id)
            with conn:
                return conn.execute(sql, tuple(params)).rowcount
        return self._run(op)
