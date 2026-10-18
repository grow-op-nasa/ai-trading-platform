"""Durable paper-runtime persistence (Sprint 18, ADR-0053): a single-host SQLite file.

Tables: `paper_accounts` (the explicit opening baseline of the paper broker), `paper_cycles` (one row per decision
cycle, UNIQUE idempotency key), `paper_intents` (durable execution intent, UNIQUE per cycle / client order id /
(experiment, symbol, decision bar)), `paper_broker_fills` (the paper broker's own ledger: UNIQUE client order id --
this is what a lookup by client order id queries), `paper_reconciliations` (append-only history).

Only `create()` (used by the explicit `paper init` command) creates the file; `open_existing()` fails clearly if it
is missing, so ordinary runtime start-up can never fabricate an account or database.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.paper.models import (
    CycleRecord,
    CycleStatus,
    FillRecord,
    IntentRecord,
    IntentState,
    ReconRecord,
    ProposalRecord,
    ProposalStatus,
    ReconStatus,
    UNRESOLVED_INTENT,
)

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path("data/paper/paper_runtime.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_accounts (
    experiment_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    baseline_cash REAL NOT NULL,
    baseline_positions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_cycles (
    cycle_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    experiment_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    decision_bar TEXT,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_intents (
    intent_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL UNIQUE,
    experiment_id INTEGER NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    decision_bar TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    UNIQUE (experiment_id, symbol, decision_bar)
);
CREATE TABLE IF NOT EXISTS paper_broker_fills (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    experiment_id INTEGER NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_proposals (
    proposal_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    experiment_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_reconciliations (
    recon_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    cycle_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
"""


class PaperStoreError(RuntimeError):
    pass


class PaperStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def create(cls, path: Path | str) -> "PaperStore":
        """Create (or open) the database. Only the explicit `paper init` flow may call this."""
        store = cls(path)
        store._path.parent.mkdir(parents=True, exist_ok=True)
        with store._tx() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO paper_meta (key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
        return store

    @classmethod
    def open_existing(cls, path: Path | str) -> "PaperStore":
        p = Path(path)
        if not p.is_file():
            raise PaperStoreError(
                f"paper runtime database not found at {p}; it is never created implicitly "
                "(run `python -m src.cli paper init` first)")
        store = cls(p)
        version = store.schema_version()
        if version != SCHEMA_VERSION:
            raise PaperStoreError(f"unsupported paper database schema version {version!r} (expected {SCHEMA_VERSION})")
        return store

    @property
    def path(self) -> Path:
        return self._path

    def schema_version(self) -> int | None:
        try:
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute("SELECT value FROM paper_meta WHERE key='schema_version'").fetchone()
            return int(row[0]) if row else None
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------ accounts
    def init_account(self, experiment_id: int, label: str, baseline_cash: float,
                     positions: list[dict[str, Any]], created_at: str) -> None:
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM paper_accounts WHERE experiment_id=?", (experiment_id,)).fetchone():
                raise PaperStoreError(f"paper account for experiment {experiment_id} already exists")
            conn.execute("INSERT INTO paper_accounts VALUES (?,?,?,?,?)",
                         (experiment_id, label, baseline_cash, json.dumps(positions), created_at))

    def get_account(self, experiment_id: int) -> dict[str, Any] | None:
        rows = self._read("SELECT * FROM paper_accounts WHERE experiment_id=?", (experiment_id,))
        if not rows:
            return None
        r = rows[0]
        return {"experiment_id": r["experiment_id"], "label": r["label"], "baseline_cash": r["baseline_cash"],
                "positions": json.loads(r["baseline_positions_json"]), "created_at": r["created_at"]}

    # ------------------------------------------------------------------ cycles
    def insert_cycle(self, rec: CycleRecord) -> bool:
        """False when a cycle with the same idempotency key already exists (UNIQUE constraint)."""
        try:
            with self._tx() as conn:
                conn.execute("INSERT INTO paper_cycles VALUES (?,?,?,?,?,?,?,?)",
                             (rec.cycle_id, rec.idempotency_key, rec.experiment_id, rec.status.value, rec.started_at,
                              rec.finished_at, rec.decision_bar, json.dumps(rec.to_dict())))
            return True
        except sqlite3.IntegrityError:
            return False

    def save_cycle(self, rec: CycleRecord, conn: sqlite3.Connection | None = None) -> None:
        sql = ("UPDATE paper_cycles SET status=?, finished_at=?, decision_bar=?, record_json=? WHERE cycle_id=?")
        args = (rec.status.value, rec.finished_at, rec.decision_bar, json.dumps(rec.to_dict()), rec.cycle_id)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self._tx() as c:
                c.execute(sql, args)

    def get_cycle(self, cycle_id: str) -> CycleRecord | None:
        rows = self._read("SELECT record_json FROM paper_cycles WHERE cycle_id=?", (cycle_id,))
        return CycleRecord.from_dict(json.loads(rows[0][0])) if rows else None

    def get_cycle_by_key(self, key: str) -> CycleRecord | None:
        rows = self._read("SELECT record_json FROM paper_cycles WHERE idempotency_key=?", (key,))
        return CycleRecord.from_dict(json.loads(rows[0][0])) if rows else None

    def list_cycles(self, experiment_id: int | None = None, limit: int = 20,
                    statuses: tuple[CycleStatus, ...] | None = None) -> list[CycleRecord]:
        sql, params = "SELECT record_json FROM paper_cycles WHERE 1=1", []
        if experiment_id is not None:
            sql += " AND experiment_id=?"; params.append(experiment_id)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"; params.extend(s.value for s in statuses)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"; params.append(limit)
        return [CycleRecord.from_dict(json.loads(r[0])) for r in self._read(sql, tuple(params))]

    # ------------------------------------------------------------------ intents
    def record_intent(self, intent: IntentRecord, cycle: CycleRecord) -> bool:
        """Persist the intent AND the cycle's AWAITING state in one transaction. False if an intent already exists
        for the same cycle, client order id or decision bar (UNIQUE constraints)."""
        try:
            with self._tx() as conn:
                conn.execute("INSERT INTO paper_intents VALUES (?,?,?,?,?,?,?,?,?)",
                             (intent.intent_id, intent.cycle_id, intent.experiment_id, intent.client_order_id,
                              intent.symbol, intent.decision_bar, intent.state.value, intent.created_at,
                              json.dumps(intent.to_dict())))
                self.save_cycle(cycle, conn)
            return True
        except sqlite3.IntegrityError:
            return False

    def _intent_from(self, row: sqlite3.Row) -> IntentRecord:
        rec = IntentRecord.from_dict(json.loads(row["record_json"]))
        rec.state = IntentState(row["state"])
        return rec

    def get_intent(self, intent_id: str) -> IntentRecord | None:
        rows = self._read("SELECT * FROM paper_intents WHERE intent_id=?", (intent_id,))
        return self._intent_from(rows[0]) if rows else None

    def intent_for_bar(self, experiment_id: int, symbol: str, decision_bar: str) -> IntentRecord | None:
        rows = self._read("SELECT * FROM paper_intents WHERE experiment_id=? AND symbol=? AND decision_bar=?",
                          (experiment_id, symbol, decision_bar))
        return self._intent_from(rows[0]) if rows else None

    def intent_by_client_order_id(self, client_order_id: str) -> IntentRecord | None:
        rows = self._read("SELECT * FROM paper_intents WHERE client_order_id=?", (client_order_id,))
        return self._intent_from(rows[0]) if rows else None

    def list_intents(self, experiment_id: int, unresolved_only: bool = False, limit: int = 50) -> list[IntentRecord]:
        sql, params = "SELECT * FROM paper_intents WHERE experiment_id=?", [experiment_id]
        if unresolved_only:
            sql += f" AND state IN ({','.join('?' * len(UNRESOLVED_INTENT))})"
            params.extend(s.value for s in UNRESOLVED_INTENT)
        sql += " ORDER BY created_at ASC, rowid ASC LIMIT ?"; params.append(limit)
        return [self._intent_from(r) for r in self._read(sql, tuple(params))]

    def set_intent_state(self, intent_id: str, state: IntentState, reason: str | None = None) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT record_json FROM paper_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row is None:
                raise PaperStoreError(f"unknown intent {intent_id}")
            rec = json.loads(row["record_json"])
            rec["state"], rec["reason"] = state.value, reason if reason is not None else rec.get("reason")
            conn.execute("UPDATE paper_intents SET state=?, record_json=? WHERE intent_id=?",
                         (state.value, json.dumps(rec), intent_id))

    # ------------------------------------------------------------------ broker ledger
    def record_broker_fill(self, intent_id: str, experiment_id: int, fill: FillRecord) -> FillRecord:
        """Ledger insert + intent state change in ONE transaction. Idempotent on client order id: a second call
        returns the already-recorded fill unchanged (a fill is never recorded, hence never applied, twice)."""
        with self._tx() as conn:
            existing = conn.execute("SELECT seq, record_json FROM paper_broker_fills WHERE client_order_id=?",
                                    (fill.client_order_id,)).fetchone()
            if existing is not None:
                rec = FillRecord.from_dict(json.loads(existing["record_json"]))
                rec.seq = existing["seq"]
                return rec
            cur = conn.execute("INSERT INTO paper_broker_fills (client_order_id, experiment_id, record_json) "
                               "VALUES (?,?,?)", (fill.client_order_id, experiment_id, json.dumps(fill.to_dict())))
            fill.seq = cur.lastrowid
            row = conn.execute("SELECT record_json, state FROM paper_intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row is not None and row["state"] == IntentState.INTENT_RECORDED.value:
                rec = json.loads(row["record_json"]); rec["state"] = IntentState.FILLED_AT_BROKER.value
                conn.execute("UPDATE paper_intents SET state=?, record_json=? WHERE intent_id=?",
                             (IntentState.FILLED_AT_BROKER.value, json.dumps(rec), intent_id))
            return fill

    def get_fill(self, client_order_id: str) -> FillRecord | None:
        rows = self._read("SELECT seq, record_json FROM paper_broker_fills WHERE client_order_id=?", (client_order_id,))
        if not rows:
            return None
        rec = FillRecord.from_dict(json.loads(rows[0]["record_json"])); rec.seq = rows[0]["seq"]
        return rec

    def list_fills(self, experiment_id: int) -> list[FillRecord]:
        out = []
        for r in self._read("SELECT seq, record_json FROM paper_broker_fills WHERE experiment_id=? ORDER BY seq ASC",
                            (experiment_id,)):
            rec = FillRecord.from_dict(json.loads(r["record_json"])); rec.seq = r["seq"]; out.append(rec)
        return out

    # ------------------------------------------------------------------ proposals (Sprint 20)
    # The table is additive to schema v1. Databases created before Sprint 20 get it on the first WRITE (an explicit run);
    # every read tolerates its absence, so viewing never mutates the file.
    def _ensure_proposals(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS paper_proposals (proposal_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, "
                     "experiment_id INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, record_json TEXT NOT NULL)")

    def _read_proposals(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        try:
            return self._read(sql, params)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise

    def _proposal_from(self, row: sqlite3.Row) -> ProposalRecord:
        rec = ProposalRecord.from_dict(json.loads(row["record_json"]))
        rec.status = ProposalStatus(row["status"])
        return rec

    def add_proposal_and_cycle(self, proposal: ProposalRecord, cycle: CycleRecord) -> bool:
        """The proposal row and the cycle's AWAITING_APPROVAL state in ONE transaction. False if the proposal id exists."""
        try:
            with self._tx() as conn:
                self._ensure_proposals(conn)
                conn.execute("INSERT INTO paper_proposals VALUES (?,?,?,?,?,?)",
                             (proposal.proposal_id, proposal.cycle_id, proposal.experiment_id, proposal.status.value,
                              proposal.created_at, json.dumps(proposal.to_dict())))
                self.save_cycle(cycle, conn)
            return True
        except sqlite3.IntegrityError:
            return False

    def get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        rows = self._read_proposals("SELECT * FROM paper_proposals WHERE proposal_id=?", (proposal_id,))
        return self._proposal_from(rows[0]) if rows else None

    def list_proposals(self, experiment_id: int, statuses: tuple[ProposalStatus, ...] | None = None,
                       limit: int = 50) -> list[ProposalRecord]:
        sql, params = "SELECT * FROM paper_proposals WHERE experiment_id=?", [experiment_id]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"; params.extend(s.value for s in statuses)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"; params.append(limit)
        return [self._proposal_from(r) for r in self._read_proposals(sql, tuple(params))]

    def set_proposal_status(self, proposal_id: str, new: ProposalStatus, *, expect: tuple[ProposalStatus, ...],
                           decided_at: str, reason: str | None = None) -> ProposalRecord | None:
        """Compare-and-set: only moves a proposal currently in one of `expect`. Returns the updated record, or None when the
        proposal is missing or no longer in an expected state (so two concurrent decisions cannot both win)."""
        with self._tx() as conn:
            self._ensure_proposals(conn)
            row = conn.execute("SELECT * FROM paper_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if row is None or ProposalStatus(row["status"]) not in expect:
                return None
            rec = json.loads(row["record_json"])
            rec["status"], rec["decided_at"] = new.value, decided_at
            if reason is not None:
                rec["decision_reason"] = reason[:400]
            conn.execute("UPDATE paper_proposals SET status=?, record_json=? WHERE proposal_id=?",
                         (new.value, json.dumps(rec), proposal_id))
            return ProposalRecord.from_dict(rec)

    # ------------------------------------------------------------------ reconciliations
    def add_reconciliation(self, rec: ReconRecord) -> ReconRecord:
        with self._tx() as conn:
            cur = conn.execute("INSERT INTO paper_reconciliations (experiment_id, cycle_id, status, created_at, "
                               "record_json) VALUES (?,?,?,?,?)",
                               (rec.experiment_id, rec.cycle_id, rec.status.value, rec.created_at,
                                json.dumps(rec.to_dict())))
            rec.recon_id = cur.lastrowid
        return rec

    def _recon_from(self, row: sqlite3.Row) -> ReconRecord:
        d = json.loads(row["record_json"]); d["status"] = ReconStatus(d["status"]); d["recon_id"] = row["recon_id"]
        return ReconRecord(**d)

    def latest_reconciliation(self, experiment_id: int) -> ReconRecord | None:
        rows = self._read("SELECT * FROM paper_reconciliations WHERE experiment_id=? ORDER BY recon_id DESC LIMIT 1",
                          (experiment_id,))
        return self._recon_from(rows[0]) if rows else None

    def list_reconciliations(self, experiment_id: int, limit: int = 20,
                             only_problems: bool = False) -> list[ReconRecord]:
        sql, params = "SELECT * FROM paper_reconciliations WHERE experiment_id=?", [experiment_id]
        if only_problems:
            sql += " AND status != 'MATCH'"
        sql += " ORDER BY recon_id DESC LIMIT ?"; params.append(limit)
        return [self._recon_from(r) for r in self._read(sql, tuple(params))]
