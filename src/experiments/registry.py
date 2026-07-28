"""The Experiment Registry.

Every backtest run that changes something and produces a result becomes
a permanent, queryable record -- not a printout that scrolls off the
terminal and is forgotten. Backed by SQLite (stdlib `sqlite3`, no new
dependency), since the value of this module compounds: after a year of
systematic use there could be hundreds of rows, and being able to query
them ("show me every experiment where I changed an RSI period and the
decision was KEEP") is the whole point.

    from src.experiments import ExperimentRegistry

    registry = ExperimentRegistry()
    experiment_id = registry.log_experiment(
        changed={"RSI.period": [14, 10]},
        metrics_before={"sharpe": 1.31, "win_rate": 0.56},
        metrics_after={"sharpe": 1.42, "win_rate": 0.59},
        decision="KEEP",
        strategy_name="sma_cross",
    )
    print(registry.get_experiment(experiment_id).summary())

Deliberately decoupled from `src/backtesting`: this module knows
nothing about `BacktestResult` or `Trade` -- it stores whatever dicts
it's given. The caller is responsible for turning two `BacktestResult`
objects into `metrics_before`/`metrics_after` dicts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.experiments.models import Experiment

DEFAULT_DB_PATH = Path("data/experiments.db")

VALID_DECISIONS = {"KEEP", "DISCARD", "INCONCLUSIVE"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    strategy_name TEXT,
    changed TEXT NOT NULL,
    metrics_before TEXT NOT NULL,
    metrics_after TEXT NOT NULL,
    decision TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
)
"""


class ExperimentRegistry:
    """SQLite-backed store of experiment records.

    Args:
        db_path: path to the SQLite database file. Created (along with
            its parent directory) on first use if it doesn't exist.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def log_experiment(
        self,
        changed: dict,
        metrics_before: dict,
        metrics_after: dict,
        decision: str,
        strategy_name: str | None = None,
        notes: str = "",
    ) -> int:
        """Record a new experiment. Returns its id.

        Raises:
            ValueError: `decision` isn't one of `VALID_DECISIONS`.
        """
        decision_upper = decision.upper()
        if decision_upper not in VALID_DECISIONS:
            raise ValueError(
                f"decision must be one of {sorted(VALID_DECISIONS)}, got {decision!r}"
            )

        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO experiments "
                "(created_at, strategy_name, changed, metrics_before, metrics_after, "
                "decision, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    strategy_name,
                    json.dumps(changed),
                    json.dumps(metrics_before),
                    json.dumps(metrics_after),
                    decision_upper,
                    notes,
                ),
            )
            return cursor.lastrowid

    def get_experiment(self, experiment_id: int) -> Experiment | None:
        """Fetch one experiment by id, or None if it doesn't exist."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        return _row_to_experiment(row) if row is not None else None

    def list_experiments(
        self, decision: str | None = None, strategy_name: str | None = None
    ) -> list[Experiment]:
        """List experiments, optionally filtered, ordered by id ascending."""
        clauses = []
        params: list = []
        if decision is not None:
            clauses.append("decision = ?")
            params.append(decision.upper())
        if strategy_name is not None:
            clauses.append("strategy_name = ?")
            params.append(strategy_name)

        query = "SELECT * FROM experiments"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_experiment(r) for r in rows]

    def count(self) -> int:
        """Total number of logged experiments."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM experiments").fetchone()
        return int(row["n"])

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _row_to_experiment(row: sqlite3.Row) -> Experiment:
    return Experiment(
        id=row["id"],
        created_at=row["created_at"],
        strategy_name=row["strategy_name"],
        changed=json.loads(row["changed"]),
        metrics_before=json.loads(row["metrics_before"]),
        metrics_after=json.loads(row["metrics_after"]),
        decision=row["decision"],
        notes=row["notes"],
    )
