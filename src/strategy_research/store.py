"""Append-only persistence for research runs -- ``data/research/research.db`` (Sprint 23, ADR-0061).

Records are never updated or deleted: SQLite triggers refuse UPDATE and DELETE, so a result can only ever be
*added to*. A new evaluation (different data, parameters, code or assumptions) is a new run id. The holdout result of
a run lives in its own insert-only row whose primary key is the run id, so a run's holdout can be evaluated exactly once.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data/research/research.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_holdouts (
    run_id TEXT PRIMARY KEY REFERENCES research_runs(run_id),
    evaluated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_runs_no_update BEFORE UPDATE ON research_runs
BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_runs_no_delete BEFORE DELETE ON research_runs
BEGIN SELECT RAISE(ABORT, 'research runs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_holdouts_no_update BEFORE UPDATE ON research_holdouts
BEGIN SELECT RAISE(ABORT, 'research holdout results are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_holdouts_no_delete BEFORE DELETE ON research_holdouts
BEGIN SELECT RAISE(ABORT, 'research holdout results are immutable'); END;
"""


class HoldoutAlreadyEvaluated(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchStore:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._path))
        con.row_factory = sqlite3.Row
        return con

    def add_run(self, run_id: str, kind: str, strategy_name: str, strategy_version: str | None, symbol: str,
                interval: str, status: str, payload: dict[str, Any]) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO research_runs(run_id, kind, created_at, strategy_name, strategy_version, symbol, interval,"
                " status, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, kind, _now(), strategy_name, strategy_version, symbol, interval, status,
                 json.dumps(payload, sort_keys=True, default=str)))

    def add_holdout(self, run_id: str, payload: dict[str, Any]) -> None:
        try:
            with self._connect() as con:
                con.execute("INSERT INTO research_holdouts(run_id, evaluated_at, payload) VALUES (?,?,?)",
                            (run_id, _now(), json.dumps(payload, sort_keys=True, default=str)))
        except sqlite3.IntegrityError as exc:
            raise HoldoutAlreadyEvaluated(f"the holdout of run {run_id} has already been evaluated") from exc

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM research_runs" + (" WHERE kind=?" if kind else "") + " ORDER BY created_at DESC, run_id"
        with self._connect() as con:
            rows = con.execute(sql, (kind,) if kind else ()).fetchall()
        return [self._run_row(r) for r in rows]

    def get_holdout(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM research_holdouts WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


class ResearchReader:
    """Read-only view of a research store: it has no method that can add, change or remove anything.

    This is what the AI research tools receive, so they cannot alter stored results even by mistake.
    """

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self._store = ResearchStore(path)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._store.get_run(run_id)

    def list_runs(self, kind: str | None = None) -> list[dict[str, Any]]:
        return self._store.list_runs(kind)

    def get_holdout(self, run_id: str) -> dict[str, Any] | None:
        return self._store.get_holdout(run_id)
