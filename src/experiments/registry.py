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

Deliberately decoupled from `src/backtesting`: `log_experiment()` itself
knows nothing about `BacktestResult` or `Trade` -- it stores whatever
dicts it's given for `metrics_before`/`metrics_after`. The richer,
additive persistence methods below (`save_trades`, `save_equity_curve`,
`save_portfolio`, `save_research_report`) do know about those shapes,
the same way `save_signals`/`save_spec` already know about `Signal`/
`ExperimentSpec` -- each is its own small, optional table, never a
required part of logging an experiment.

**Scope grows deliberately, one artifact at a time.** Signals are
stored as first-class rows (`DECISIONS.md`, ADR-0016); as of Sprint 6,
one `ExperimentSpec` per experiment is too (`save_spec()`/`get_spec()`,
ADR-0035). As of Sprint 9 (`DECISIONS.md`, ADR-0042), so are a
backtest's `Trade` list, its equity curve, the `Portfolio` its signals
resolved to after paper execution, and its `ResearchReport` --
completing enough of the long-term chain (strategy/version ->
parameters -> dataset/version -> signals -> trades -> metrics ->
attribution -> report) that the Analytics & Dashboard layer can inspect
a *past* experiment in full, not only one just run in the same process.
Each is its own table, written once per experiment by
`scripts/run_experiment.py` immediately after `log_experiment()`
returns an id -- an experiment logged before Sprint 9 simply has no
rows in these tables, and every accessor below returns an explicit
"nothing here" value (`None`, `[]`, or an empty `pd.Series`) rather
than fabricating one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pandas as pd

from src.backtesting.models import Trade
from src.data.base import Interval
from src.experiments.models import Experiment
from src.experiments.spec import ExperimentSpec
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionLifecycle, PositionSide
from src.research.models import Finding, ResearchFindings, ResearchReport
from src.signals.models import Signal, SignalDirection

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

# Signals are stored as first-class rows here, not in a separate
# repository (DECISIONS.md, ADR-0016) -- one experiment's signals are
# looked up by `experiment_id`, one signal by its own `id` (a UUID,
# assigned client-side by `Signal` itself, not by this table).
_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    experiment_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
)
"""
# `symbol` added in DECISIONS.md ADR-0033, alongside `Signal` itself
# gaining the field. `CREATE TABLE IF NOT EXISTS` means an
# `experiments.db` file created before ADR-0033 keeps its old schema
# (no `symbol` column) rather than being migrated automatically -- this
# codebase has no schema-migration tooling yet (a known, accepted gap;
# see `DECISIONS.md`, ADR-0004's package-layout migration for the same
# "tracked, not yet built" posture). A fresh database picks up the new
# column; an existing one needs a manual `ALTER TABLE` or to be recreated.

# One `ExperimentSpec` per experiment -- unlike signals, there's exactly
# one specification per experiment, so `experiment_id` is the primary
# key rather than a foreign key column (`DECISIONS.md`, ADR-0035).
# `strategy_params`/`risk_config`/`backtest_config` are JSON text
# columns, the same convention `changed`/`metrics_before`/
# `metrics_after` already use on `experiments` above.
_SPECS_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_specs (
    experiment_id INTEGER PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    strategy_params TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    dataset_start TEXT NOT NULL,
    dataset_end TEXT NOT NULL,
    dataset_source TEXT NOT NULL,
    dataset_fingerprint TEXT NOT NULL,
    risk_config TEXT NOT NULL,
    backtest_config TEXT NOT NULL
)
"""

# Sprint 9 (DECISIONS.md, ADR-0042): one experiment's Trade list. Many
# rows per experiment (like signals), no natural unique key of its own
# -- `save_trades()` deletes and reinserts rather than upserting, so
# calling it again for the same experiment_id replaces, not duplicates.
_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT NOT NULL,
    direction INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    entry_signal_id TEXT NOT NULL,
    exit_signal_id TEXT
)
"""

# One equity curve per experiment -- stored as a single JSON column
# (a list of [iso_timestamp, value] pairs) rather than one row per
# point, the same one-row-per-experiment convention `experiment_specs`
# already uses. `pd.Series.to_json()`/`pd.read_json()` are deliberately
# not used here: this keeps the on-disk shape a plain, dependency-free
# JSON list any future consumer can read without pandas at all.
_EQUITY_CURVES_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_equity_curves (
    experiment_id INTEGER PRIMARY KEY,
    curve_json TEXT NOT NULL
)
"""

# The `Portfolio` (`src.portfolio.models.Portfolio`) a backtest's
# signals resolved to after being paper-executed through Risk ->
# Execution (`scripts/run_experiment.py`) -- cash plus every open and
# closed `Position`, serialized field-for-field. This is a snapshot as
# of the end of that experiment's run, not a live position: Sprint 9's
# Paper Portfolio view marks it to *current* market price on read
# (`src.analytics.valuation`), it does not re-simulate anything.
_PORTFOLIOS_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_portfolios (
    experiment_id INTEGER PRIMARY KEY,
    cash REAL NOT NULL,
    open_positions_json TEXT NOT NULL,
    closed_positions_json TEXT NOT NULL
)
"""

# One `ResearchReport` per experiment -- the narrative `ResearchReporter`
# already rendered at run time (Claude or the deterministic fallback),
# stored so the dashboard can display it later without ever calling an
# LLM itself (Sprint 9 spec, section 28: analytics/dashboard must stay
# independent of the LLM). `findings_json` is `ResearchFindings.findings`
# as a list of `{"label": ..., "value": ...}` dicts.
_RESEARCH_REPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_research_reports (
    experiment_id INTEGER PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    recommendation TEXT,
    narrative TEXT NOT NULL,
    rendered_by TEXT NOT NULL,
    renderer_error TEXT
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
            conn.execute(_SIGNALS_SCHEMA)
            conn.execute(_SPECS_SCHEMA)
            conn.execute(_TRADES_SCHEMA)
            conn.execute(_EQUITY_CURVES_SCHEMA)
            conn.execute(_PORTFOLIOS_SCHEMA)
            conn.execute(_RESEARCH_REPORTS_SCHEMA)

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

    def save_signals(self, experiment_id: int, signals: list[Signal]) -> None:
        """Persist `signals` as belonging to `experiment_id`.

        Deliberately separate from `log_experiment()` rather than a new
        parameter on it -- keeps `log_experiment()`'s existing signature
        (and every test against it) untouched. Call this after
        `log_experiment()` returns the id it should attach to.
        """
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO signals "
                "(id, experiment_id, timestamp, symbol, direction, confidence, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(signal.id),
                        experiment_id,
                        signal.timestamp.isoformat(),
                        signal.symbol,
                        signal.direction.value,
                        signal.confidence,
                        json.dumps(signal.metadata),
                    )
                    for signal in signals
                ],
            )

    def get_signals(self, experiment_id: int) -> list[Signal]:
        """All signals saved under `experiment_id`, ordered by timestamp."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE experiment_id = ? ORDER BY timestamp",
                (experiment_id,),
            ).fetchall()
        return [_row_to_signal(r) for r in rows]

    def get_signal(self, signal_id: UUID) -> Signal | None:
        """Fetch one signal by id, or None if it doesn't exist."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM signals WHERE id = ?", (str(signal_id),)
            ).fetchone()
        return _row_to_signal(row) if row is not None else None

    def save_spec(self, experiment_id: int, spec: ExperimentSpec) -> None:
        """Persist `spec` as `experiment_id`'s specification.

        Deliberately separate from `log_experiment()` rather than a new
        parameter on it (`DECISIONS.md`, ADR-0035) -- the same reasoning
        `save_signals()` is separate (ADR-0016): `log_experiment()`'s
        signature, and every existing test against it, stays untouched.
        `INSERT OR REPLACE` -- calling this again for the same
        `experiment_id` overwrites, it doesn't duplicate.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiment_specs "
                "(experiment_id, strategy_name, strategy_version, strategy_params, "
                "symbol, interval, dataset_start, dataset_end, dataset_source, "
                "dataset_fingerprint, risk_config, backtest_config) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    spec.strategy_name,
                    spec.strategy_version,
                    json.dumps(spec.strategy_params),
                    spec.symbol,
                    spec.interval.value,
                    spec.dataset_start.isoformat(),
                    spec.dataset_end.isoformat(),
                    spec.dataset_source,
                    spec.dataset_fingerprint,
                    json.dumps(spec.risk_config),
                    json.dumps(spec.backtest_config),
                ),
            )

    def get_spec(self, experiment_id: int) -> ExperimentSpec | None:
        """Fetch `experiment_id`'s specification, or None if none was saved."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_specs WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return _row_to_spec(row) if row is not None else None

    def save_trades(self, experiment_id: int, trades: list[Trade]) -> None:
        """Persist `trades` as `experiment_id`'s Trade list (Sprint 9,
        `DECISIONS.md` ADR-0042).

        Deletes any trades already saved for `experiment_id` first, so
        calling this again (e.g. a re-run) replaces rather than
        duplicates -- `Trade` has no id of its own to upsert against,
        unlike `Signal`/`ExperimentSpec`.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM experiment_trades WHERE experiment_id = ?", (experiment_id,)
            )
            conn.executemany(
                "INSERT INTO experiment_trades "
                "(experiment_id, entry_time, exit_time, direction, entry_price, "
                "exit_price, entry_signal_id, exit_signal_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        experiment_id,
                        trade.entry_time.isoformat(),
                        trade.exit_time.isoformat(),
                        trade.direction,
                        trade.entry_price,
                        trade.exit_price,
                        str(trade.entry_signal_id),
                        str(trade.exit_signal_id) if trade.exit_signal_id is not None else None,
                    )
                    for trade in trades
                ],
            )

    def get_trades(self, experiment_id: int) -> list[Trade]:
        """All trades saved under `experiment_id`, ordered by entry
        time -- `[]` if none were ever saved (including every
        experiment logged before Sprint 9)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM experiment_trades WHERE experiment_id = ? ORDER BY entry_time",
                (experiment_id,),
            ).fetchall()
        return [_row_to_trade(r) for r in rows]

    def save_equity_curve(self, experiment_id: int, equity_curve: pd.Series) -> None:
        """Persist `equity_curve` (a `BacktestResult.equity_curve`,
        timezone-aware `DatetimeIndex`) as `experiment_id`'s equity
        curve (Sprint 9, `DECISIONS.md` ADR-0042).

        `INSERT OR REPLACE` -- calling this again for the same
        `experiment_id` overwrites, matching `save_spec()`'s convention.
        """
        curve_json = json.dumps(
            [[timestamp.isoformat(), float(value)] for timestamp, value in equity_curve.items()]
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiment_equity_curves (experiment_id, curve_json) "
                "VALUES (?, ?)",
                (experiment_id, curve_json),
            )

    def get_equity_curve(self, experiment_id: int) -> pd.Series:
        """`experiment_id`'s equity curve, indexed by a timezone-aware
        UTC `DatetimeIndex` named `"timestamp"` -- an empty `pd.Series`
        (never `None`) if none was ever saved, so callers can treat
        "no equity curve" and "empty equity curve" identically (Sprint 9
        spec, section 9: no equity observations means undefined
        analytics, not a special-cased `None`)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT curve_json FROM experiment_equity_curves WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            return pd.Series(dtype=float, name="equity")
        points = json.loads(row["curve_json"])
        if not points:
            return pd.Series(dtype=float, name="equity")
        index = pd.DatetimeIndex([pd.Timestamp(p[0]) for p in points], name="timestamp")
        return pd.Series([p[1] for p in points], index=index, name="equity")

    def save_portfolio(self, experiment_id: int, portfolio: Portfolio) -> None:
        """Persist `portfolio`'s current state (cash, open positions,
        closed positions) as `experiment_id`'s resulting paper
        portfolio (Sprint 9, `DECISIONS.md` ADR-0042) -- a snapshot as
        of whenever this is called, not a live position.

        `INSERT OR REPLACE` -- matches `save_spec()`'s convention.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiment_portfolios "
                "(experiment_id, cash, open_positions_json, closed_positions_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    experiment_id,
                    portfolio.cash,
                    json.dumps([_position_to_dict(p) for p in portfolio.positions.values()]),
                    json.dumps([_position_to_dict(p) for p in portfolio.closed_positions]),
                ),
            )

    def get_portfolio(self, experiment_id: int) -> Portfolio | None:
        """Reconstruct `experiment_id`'s saved `Portfolio`, or `None` if
        none was ever saved (including every experiment logged before
        Sprint 9, or one whose signals were never paper-executed).

        Reconstructed via `Portfolio.reconstruct()`, not by replaying
        fills through `open_position()`/`close_position()` -- those
        simulate a *new* fill (re-deriving `cash`, rejecting a symbol
        that's already open or closed), which is not what loading an
        already-correct saved snapshot should do.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_portfolios WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            return None
        return Portfolio.reconstruct(
            cash=row["cash"],
            positions=[_dict_to_position(d) for d in json.loads(row["open_positions_json"])],
            closed_positions=[
                _dict_to_position(d) for d in json.loads(row["closed_positions_json"])
            ],
        )

    def save_research_report(self, experiment_id: int, report: ResearchReport) -> None:
        """Persist `report` as `experiment_id`'s research report
        (Sprint 9, `DECISIONS.md` ADR-0042) -- whatever
        `ResearchReporter` rendered at run time (Claude or the
        deterministic fallback), stored exactly as produced so the
        dashboard can display it later without ever invoking an LLM
        itself.

        `INSERT OR REPLACE` -- matches `save_spec()`'s convention.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiment_research_reports "
                "(experiment_id, strategy_name, findings_json, recommendation, "
                "narrative, rendered_by, renderer_error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    report.findings.strategy_name,
                    json.dumps([{"label": f.label, "value": f.value} for f in report.findings.findings]),
                    report.findings.recommendation,
                    report.narrative,
                    report.rendered_by,
                    report.renderer_error,
                ),
            )

    def get_research_report(self, experiment_id: int) -> ResearchReport | None:
        """`experiment_id`'s saved research report, or `None` if none
        was ever saved (including every experiment logged before
        Sprint 9)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM experiment_research_reports WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return _row_to_research_report(row) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _row_to_signal(row: sqlite3.Row) -> Signal:
    return Signal(
        id=UUID(row["id"]),
        timestamp=pd.Timestamp(row["timestamp"]),
        symbol=row["symbol"],
        direction=SignalDirection(row["direction"]),
        confidence=row["confidence"],
        metadata=json.loads(row["metadata"]),
    )


def _row_to_spec(row: sqlite3.Row) -> ExperimentSpec:
    return ExperimentSpec(
        strategy_name=row["strategy_name"],
        strategy_version=row["strategy_version"],
        strategy_params=json.loads(row["strategy_params"]),
        symbol=row["symbol"],
        interval=Interval(row["interval"]),
        dataset_start=pd.Timestamp(row["dataset_start"]),
        dataset_end=pd.Timestamp(row["dataset_end"]),
        dataset_source=row["dataset_source"],
        dataset_fingerprint=row["dataset_fingerprint"],
        risk_config=json.loads(row["risk_config"]),
        backtest_config=json.loads(row["backtest_config"]),
    )


def _row_to_trade(row: sqlite3.Row) -> Trade:
    return Trade(
        entry_time=pd.Timestamp(row["entry_time"]),
        exit_time=pd.Timestamp(row["exit_time"]),
        direction=row["direction"],
        entry_price=row["entry_price"],
        exit_price=row["exit_price"],
        entry_signal_id=UUID(row["entry_signal_id"]),
        exit_signal_id=UUID(row["exit_signal_id"]) if row["exit_signal_id"] else None,
    )


def _position_to_dict(position: Position) -> dict:
    return {
        "symbol": position.symbol,
        "side": position.side.value,
        "quantity": position.quantity,
        "entry_price": position.entry_price,
        "entry_timestamp": position.entry_timestamp.isoformat(),
        "entry_signal_id": str(position.entry_signal_id),
        "stop_price": position.stop_price,
        "current_price": position.current_price,
        "lifecycle": position.lifecycle.value,
        "realized_pnl": position.realized_pnl,
    }


def _dict_to_position(data: dict) -> Position:
    return Position(
        symbol=data["symbol"],
        side=PositionSide(data["side"]),
        quantity=data["quantity"],
        entry_price=data["entry_price"],
        entry_timestamp=pd.Timestamp(data["entry_timestamp"]),
        entry_signal_id=UUID(data["entry_signal_id"]),
        stop_price=data["stop_price"],
        current_price=data["current_price"],
        lifecycle=PositionLifecycle(data["lifecycle"]),
        realized_pnl=data["realized_pnl"],
    )


def _row_to_research_report(row: sqlite3.Row) -> ResearchReport:
    findings = [Finding(**f) for f in json.loads(row["findings_json"])]
    return ResearchReport(
        findings=ResearchFindings(
            strategy_name=row["strategy_name"],
            findings=findings,
            recommendation=row["recommendation"],
        ),
        narrative=row["narrative"],
        rendered_by=row["rendered_by"],
        renderer_error=row["renderer_error"],
    )


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
