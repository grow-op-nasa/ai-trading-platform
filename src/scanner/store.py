"""Durable scanner persistence (single-host SQLite). Watchlists, scans and per-symbol results.

The file is created only by a WRITE (create/update a watchlist, start a scan); reads of a missing file return empty results.
Every result is committed individually, so an interrupted scan keeps what it finished. Additive schema (no migration of
other stores). Scan history is append-only: results are never rewritten with current data.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.scanner.models import ResultStatus, RiskState, ScanRecord, ScanResult, ScanStatus, Watchlist

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path("data/scanner/scanner.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scanner_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchlists (
    watchlist_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    symbols_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    scan_id TEXT PRIMARY KEY,
    request_token TEXT UNIQUE,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    heartbeat_at TEXT,
    owner_pid INTEGER,
    config_hash TEXT NOT NULL,
    watchlist_id TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_results (
    scan_id TEXT NOT NULL REFERENCES scans(scan_id),
    symbol TEXT NOT NULL,
    position INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (scan_id, symbol)
);
CREATE INDEX IF NOT EXISTS ix_scans_started ON scans(started_at);
"""


class ScannerStoreError(RuntimeError):
    pass


class DuplicateWatchlistName(ScannerStoreError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ScannerStore:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    def exists(self) -> bool:
        return self.path.is_file()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                conn.execute("INSERT OR IGNORE INTO scanner_meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:       # a file that is not (yet) a scanner database
                if "no such table" in str(exc):
                    return []
                raise ScannerStoreError(str(exc)) from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------ watchlists
    @staticmethod
    def _wl(row: sqlite3.Row) -> Watchlist:
        try:
            symbols = tuple(json.loads(row["symbols_json"]))
            if not all(isinstance(s, str) and s for s in symbols):
                raise ValueError("symbols must be non-empty strings")
        except (ValueError, TypeError) as exc:
            raise ScannerStoreError(f"watchlist {row['watchlist_id']} has malformed stored symbols: {exc}") from exc
        return Watchlist(row["watchlist_id"], row["name"], symbols, row["created_at"], row["updated_at"])

    def list_watchlists(self) -> list[Watchlist]:
        return [self._wl(r) for r in self._read("SELECT * FROM watchlists ORDER BY name COLLATE NOCASE")]

    def get_watchlist(self, watchlist_id: str) -> Watchlist | None:
        rows = self._read("SELECT * FROM watchlists WHERE watchlist_id=?", (watchlist_id,))
        return self._wl(rows[0]) if rows else None

    def create_watchlist(self, name: str, symbols: tuple[str, ...]) -> Watchlist:
        wid, now = uuid.uuid4().hex, _now()
        try:
            with self._write() as c:
                c.execute("INSERT INTO watchlists VALUES (?,?,?,?,?)", (wid, name, json.dumps(list(symbols)), now, now))
        except sqlite3.IntegrityError as exc:
            raise DuplicateWatchlistName(f"a watchlist named {name!r} already exists") from exc
        return Watchlist(wid, name, tuple(symbols), now, now)

    def update_watchlist(self, watchlist_id: str, *, name: str | None = None, symbols: tuple[str, ...] | None = None) -> Watchlist:
        cur = self.get_watchlist(watchlist_id)
        if cur is None:
            raise ScannerStoreError("that watchlist does not exist")
        new_name, new_syms, now = name if name is not None else cur.name, tuple(symbols) if symbols is not None else cur.symbols, _now()
        try:
            with self._write() as c:
                c.execute("UPDATE watchlists SET name=?, symbols_json=?, updated_at=? WHERE watchlist_id=?",
                          (new_name, json.dumps(list(new_syms)), now, watchlist_id))
        except sqlite3.IntegrityError as exc:
            raise DuplicateWatchlistName(f"a watchlist named {new_name!r} already exists") from exc
        return Watchlist(watchlist_id, new_name, new_syms, cur.created_at, now)

    def delete_watchlist(self, watchlist_id: str) -> bool:
        with self._write() as c:
            return c.execute("DELETE FROM watchlists WHERE watchlist_id=?", (watchlist_id,)).rowcount > 0

    def seed_starter(self, name: str, symbols: tuple[str, ...]) -> Watchlist | None:
        """Create the starter list ONLY if the store has never held a watchlist (meta flag) and none exists now."""
        with self._write() as c:
            seeded = c.execute("SELECT 1 FROM scanner_meta WHERE key='starter_seeded'").fetchone()
            have = c.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0]
            if seeded or have:
                return None
            wid, now = uuid.uuid4().hex, _now()
            c.execute("INSERT INTO watchlists VALUES (?,?,?,?,?)", (wid, name, json.dumps(list(symbols)), now, now))
            c.execute("INSERT INTO scanner_meta(key,value) VALUES('starter_seeded',?)", (now,))
            return Watchlist(wid, name, tuple(symbols), now, now)

    # ------------------------------------------------------------------ scans
    @staticmethod
    def _scan_json(rec: ScanRecord) -> str:
        d = {k: (list(v) if isinstance(v, tuple) else v) for k, v in rec.__dict__.items()}
        d["status"] = rec.status.value
        return json.dumps(d, default=str, sort_keys=True)

    @staticmethod
    def _scan(row: sqlite3.Row) -> ScanRecord:
        d = json.loads(row["record_json"])
        d["status"] = ScanStatus(row["status"])               # the column is authoritative for status
        d["symbols"] = tuple(d["symbols"])
        d["finished_at"], d["heartbeat_at"], d["owner_pid"] = row["finished_at"], row["heartbeat_at"], row["owner_pid"]
        return ScanRecord(**d)

    def create_scan(self, rec: ScanRecord) -> tuple[ScanRecord, bool]:
        """(record, created). A repeated request token returns the EXISTING scan (idempotent submission)."""
        try:
            with self._write() as c:
                c.execute("INSERT INTO scans(scan_id,request_token,status,started_at,finished_at,heartbeat_at,owner_pid,config_hash,"
                          "watchlist_id,record_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (rec.scan_id, rec.request_token, rec.status.value, rec.started_at, rec.finished_at, rec.heartbeat_at,
                           rec.owner_pid, rec.config_hash, rec.watchlist_id, self._scan_json(rec)))
            return rec, True
        except sqlite3.IntegrityError:
            if rec.request_token:
                rows = self._read("SELECT * FROM scans WHERE request_token=?", (rec.request_token,))
                if rows:
                    return self._scan(rows[0]), False
            raise

    def get_scan(self, scan_id: str) -> ScanRecord | None:
        rows = self._read("SELECT * FROM scans WHERE scan_id=?", (scan_id,))
        return self._scan(rows[0]) if rows else None

    def list_scans(self, limit: int = 50) -> list[ScanRecord]:
        return [self._scan(r) for r in self._read("SELECT * FROM scans ORDER BY started_at DESC, scan_id LIMIT ?", (limit,))]

    def running(self) -> list[ScanRecord]:
        return [self._scan(r) for r in self._read("SELECT * FROM scans WHERE status=?", (ScanStatus.RUNNING.value,))]

    def heartbeat(self, scan_id: str) -> None:
        with self._write() as c:
            c.execute("UPDATE scans SET heartbeat_at=? WHERE scan_id=? AND status=?", (_now(), scan_id, ScanStatus.RUNNING.value))

    def finish_scan(self, scan_id: str, status: ScanStatus, counts: dict[str, int], error: str | None = None) -> ScanRecord:
        rec = self.get_scan(scan_id)
        if rec is None:
            raise ScannerStoreError("unknown scan")
        rec.status, rec.counts, rec.error, rec.finished_at = status, counts, error, _now()
        with self._write() as c:
            c.execute("UPDATE scans SET status=?, finished_at=?, record_json=? WHERE scan_id=?",
                      (status.value, rec.finished_at, self._scan_json(rec), scan_id))
        return rec

    def mark_interrupted(self, scan_id: str, counts: dict[str, int]) -> None:
        rec = self.get_scan(scan_id)
        if rec is not None and rec.status is ScanStatus.RUNNING:
            self.finish_scan(scan_id, ScanStatus.INTERRUPTED, counts, "the process that was running this scan ended before it finished")

    # ------------------------------------------------------------------ results
    def add_result(self, r: ScanResult) -> None:
        d = {**r.__dict__, "status": r.status.value, "risk_state": r.risk_state.value}
        with self._write() as c:
            c.execute("INSERT OR REPLACE INTO scan_results(scan_id,symbol,position,status,result_json) VALUES (?,?,?,?,?)",
                      (r.scan_id, r.symbol, r.position, r.status.value, json.dumps(d, default=str, sort_keys=True)))

    def results(self, scan_id: str) -> list[ScanResult]:
        out = []
        for row in self._read("SELECT * FROM scan_results WHERE scan_id=? ORDER BY position, symbol", (scan_id,)):
            d = json.loads(row["result_json"])
            d["status"], d["risk_state"] = ResultStatus(d["status"]), RiskState(d["risk_state"])
            out.append(ScanResult(**d))
        return out
