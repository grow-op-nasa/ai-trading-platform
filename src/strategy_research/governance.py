"""Research specification hash and holdout-consumption governance (Sprint 23 closeout, ADR-0061 amendment).

Two separate guarantees, both independent of run ids:

1. ``canonical_spec`` / ``spec_hash`` -- everything that determines a research result, frozen as canonical JSON and
   hashed with SHA-256 *before* any holdout bar is looked at. The hash is stored in the run record and re-derived at
   holdout time; a mismatch (code, parameters, data, engine, assumptions or dependency drift) refuses the evaluation.

2. ``HoldoutLedger`` -- an insert-only event log of holdout *consumption*, keyed by (symbol, interval, holdout bar
   range) and NOT by run id, so creating a second run over the same instrument and the same locked bars cannot be
   used to look at the holdout again. Overlapping ranges count as the same holdout.

   States:  RESERVED  -> written before any holdout figure is computed.
            COMPLETED -> the figures were persisted.
            ABORTED   -> only when the figures were computed but could not be persisted, were discarded, and nothing was
                         returned or shown. A retry is then a safe re-computation of the identical, already-hashed spec.
   A reservation with no terminal event (crash, exception while computing) is CONSUMED: the process touched the locked
   bars and we cannot prove nothing leaked, so the conservative reading is that the holdout is spent.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCHMARK_METHODOLOGY = "benchmarks-v1: full buy-and-hold (close-to-close, fully invested); exposure-matched (entry<bar<=exit, cash 0%)"

RESERVED, COMPLETED, ABORTED = "RESERVED", "COMPLETED", "ABORTED"


class HoldoutConsumed(RuntimeError):
    """The locked holdout for this instrument/range has already been used (or is being used) by another evaluation."""


class SpecMismatch(RuntimeError):
    """The research specification re-derived now differs from the one frozen when the run was created."""


def _dependency_versions() -> dict[str, str]:
    import numpy
    import pandas
    return {"python": platform.python_version().rsplit(".", 1)[0], "pandas": pandas.__version__, "numpy": numpy.__version__}


def canonical_spec(*, strategy: dict[str, Any], params: dict[str, Any], request: dict[str, Any], assumptions: dict[str, Any],
                   engine_version: int, provenance: dict[str, Any], boundaries: dict[str, Any], holdout_hash: str,
                   selection: dict[str, Any], configs: list[Any], dependencies: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "strategy": {"id": strategy["id"], "source_hash": strategy["version"]},
        "selected_params": params,
        "candidate_configs": configs,
        "selection_protocol": {"rule": selection["rule"], "configurations_attempted": selection["configurations_attempted"]},
        "engine_version": engine_version,
        "dependencies": dependencies or _dependency_versions(),
        "assumptions": assumptions,                       # backtest / execution / cost configuration
        "instrument": {"symbol": request["symbol"], "interval": request["interval"]},
        "data": {"snapshot_id": provenance.get("snapshot_id"), "dataset_hash": provenance.get("dataset_hash"),
                 "holdout_slice_hash": holdout_hash},
        "windows": {k: {"first_bar": v["first_bar"], "last_bar": v["last_bar"], "bars": v["bars"]} for k, v in boundaries.items()},
        "protocol": request.get("protocol"),
        "walk_forward": request.get("walk_forward"),
        "benchmark_methodology": BENCHMARK_METHODOLOGY,
    }


def spec_hash(spec: dict[str, Any]) -> str:
    blob = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verify_spec(record: dict[str, Any]) -> None:
    """Raise ``SpecMismatch`` unless the stored spec still hashes to the stored hash and the environment is unchanged."""
    spec, stored = record.get("spec"), record.get("spec_hash")
    if not spec or not stored:
        raise SpecMismatch("this run has no frozen research specification, so its holdout cannot be evaluated")
    if spec_hash(spec) != stored:
        raise SpecMismatch("the stored research specification no longer matches its hash (the record was altered)")
    now = _dependency_versions()
    if spec["dependencies"] != now:
        raise SpecMismatch(f"dependency versions changed since the specification was frozen ({spec['dependencies']} -> {now}); "
                           "numerical results may differ, so the holdout is not evaluated. Create a new run instead")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_holdout_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    first_bar TEXT NOT NULL,
    last_bar TEXT NOT NULL,
    holdout_hash TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    detail TEXT
);
CREATE TRIGGER IF NOT EXISTS research_holdout_events_no_update BEFORE UPDATE ON research_holdout_events
BEGIN SELECT RAISE(ABORT, 'holdout events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_holdout_events_no_delete BEFORE DELETE ON research_holdout_events
BEGIN SELECT RAISE(ABORT, 'holdout events are immutable'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HoldoutLedger:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._path), isolation_level=None, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def consuming_events(self, symbol: str, interval: str, first_bar: str, last_bar: str) -> list[dict[str, Any]]:
        """Reservations on an overlapping range that are not ABORTED (i.e. RESERVED-without-terminal or COMPLETED)."""
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM research_holdout_events WHERE symbol=? AND interval=? ORDER BY seq", (symbol, interval))]
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            latest[r["reservation_id"]] = r
        # ISO-8601 UTC timestamps of equal format compare chronologically as strings.
        return [r for r in latest.values() if r["state"] != ABORTED and r["first_bar"] <= last_bar and first_bar <= r["last_bar"]]

    def reserve(self, *, run_id: str, symbol: str, interval: str, first_bar: str, last_bar: str, holdout_hash: str,
                spec_hash_: str) -> str:
        """Atomically refuse if the range is already consumed, else record RESERVED. Returns the reservation id."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM research_holdout_events WHERE symbol=? AND interval=? ORDER BY seq", (symbol, interval))]
            latest: dict[str, dict[str, Any]] = {}
            for r in rows:
                latest[r["reservation_id"]] = r
            clash = [r for r in latest.values()
                     if r["state"] != ABORTED and r["first_bar"] <= last_bar and first_bar <= r["last_bar"]]
            if clash:
                con.execute("ROLLBACK")
                c = clash[0]
                raise HoldoutConsumed(
                    f"the locked holdout of {symbol} {interval} ({c['first_bar']} .. {c['last_bar']}) was already used by run "
                    f"{c['run_id']} (state {c['state']}); a new run over the same bars cannot evaluate it again")
            rid = uuid.uuid4().hex
            con.execute(
                "INSERT INTO research_holdout_events(reservation_id,state,at,run_id,symbol,interval,first_bar,last_bar,"
                "holdout_hash,spec_hash,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, RESERVED, _now(), run_id, symbol, interval, first_bar, last_bar, holdout_hash, spec_hash_, None))
            con.execute("COMMIT")
            return rid
        except HoldoutConsumed:
            raise
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    def finish(self, reservation_id: str, state: str, detail: str | None = None) -> None:
        if state not in (COMPLETED, ABORTED):
            raise ValueError("a reservation can only be completed or aborted")
        with self._connect() as con:
            row = con.execute("SELECT * FROM research_holdout_events WHERE reservation_id=? ORDER BY seq DESC LIMIT 1",
                              (reservation_id,)).fetchone()
            if row is None or row["state"] != RESERVED:
                raise ValueError("only an open (RESERVED) reservation can be finished")
            con.execute(
                "INSERT INTO research_holdout_events(reservation_id,state,at,run_id,symbol,interval,first_bar,last_bar,"
                "holdout_hash,spec_hash,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (reservation_id, state, _now(), row["run_id"], row["symbol"], row["interval"], row["first_bar"],
                 row["last_bar"], row["holdout_hash"], row["spec_hash"], detail))
