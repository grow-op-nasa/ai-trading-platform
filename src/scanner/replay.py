"""Offline replay of stored scan decisions (Sprint 22, ADR-0059).

Replay answers one question per symbol -- "if I give the SAME stored data to the strategy again, do I get the SAME
decision?" -- in three separately reported layers, because they fail for different reasons:

  DATA      the stored snapshot is intact, is the dataset the scan recorded (content hash), and re-deriving the complete bars
            with the calendar at the recorded time gives exactly the bars the strategy saw (evaluated hash).
  STRATEGY  the registered strategy, run on those bars, reproduces the recorded outcome and signal. If the strategy's source
            hash differs from the one recorded, the run is labelled as the CURRENT implementation, never as the original.
  RISK      the read-only risk classification (approved / rejected / ...) is re-derived from the captured private risk
            context (limits, stop policy, saved portfolio, decision price) -- not from today's portfolio.

Nothing here contacts a provider, writes to the scanner store, creates intents/cycles/proposals/fills, or touches the
portfolio, registry or promotion state. A symbol whose inputs were not preserved (legacy scans, failed snapshots, corrupt
objects) is reported NOT_REPLAYABLE with the reason; it is never reported as reproduced.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


from src.paper.evaluation import (StrategyResolutionError, assess_risk, evaluate_at_bar, prepare_bars, resolve_strategy,
                                  safe_error_text, signal_to_record)
from src.scanner.models import ResultStatus, ScanRecord, ScanResult
from src.scanner.replay_context import rebuild_risk_inputs
from src.snapshots.meta import environment_fingerprint
from src.scanner.store import ScannerStore
from src.snapshots import SnapshotCorruptError, SnapshotError, SnapshotFormatError, SnapshotMissingError, SnapshotStore
from src.strategies.history import assess_history, requirement_of
from src.utils.hashing import dataframe_fingerprint

# Layer statuses
VERIFIED = "VERIFIED"                 # data
REPRODUCED = "REPRODUCED"             # strategy / risk
DIFFERENT = "DIFFERENT"               # strategy / risk (ran, answer differs from the record)
MISMATCH = "MISMATCH"                 # data (intact snapshot, but it does not reproduce the recorded bars)
NOT_CAPTURED = "NOT_CAPTURED"         # legacy result / no snapshot was recorded
MISSING = "SNAPSHOT_MISSING"
CORRUPT = "SNAPSHOT_CORRUPT"
UNSUPPORTED = "SNAPSHOT_UNSUPPORTED"
NOT_REPLAYABLE = "NOT_REPLAYABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNAVAILABLE = "UNAVAILABLE"           # strategy cannot be built now
CHANGED = "STRATEGY_CHANGED"          # strategy source differs from the recorded hash (run is informational only)
NO_CONTEXT = "NO_RISK_CONTEXT"        # risk was assessed originally but its inputs were not preserved


@dataclass
class LayerResult:
    status: str
    detail: str = ""
    stored: dict[str, Any] = field(default_factory=dict)
    replayed: dict[str, Any] = field(default_factory=dict)


@dataclass
class SymbolReplay:
    symbol: str
    overall: str                       # REPRODUCED | DIFFERENT | NOT_REPLAYABLE
    reason: str
    data: LayerResult
    strategy: LayerResult
    risk: LayerResult
    notes: list[str] = field(default_factory=list)


@dataclass
class ScanReplay:
    scan_id: str
    results: list[SymbolReplay]
    legacy: bool
    notes: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {"REPRODUCED": 0, "DIFFERENT": 0, "NOT_REPLAYABLE": 0}
        for r in self.results:
            out[r.overall] += 1
        return out


class ReplayService:
    """Read-only. Holds a scanner store for READS and a snapshot store for READS; owns no market-data service."""

    def __init__(self, store: ScannerStore, snapshots: SnapshotStore, *, calendar: Any = None,
                 allowed_strategy_prefixes: tuple[str, ...] = ("src.strategies",)) -> None:
        if calendar is None:
            from src.calendar.nyse import NYSECalendar
            calendar = NYSECalendar()
        self._store, self._snaps, self._cal, self._prefixes = store, snapshots, calendar, allowed_strategy_prefixes

    # ------------------------------------------------------------------ public
    def replay_scan(self, scan_id: str, *, progress: Callable[[int, int, str], None] | None = None) -> ScanReplay:
        rec = self._store.get_scan(scan_id)
        if rec is None:
            raise LookupError(f"no scan {scan_id!r}")
        results = self._store.results(scan_id)
        out = []
        for i, r in enumerate(results, 1):
            out.append(self.replay_result(rec, r))
            if progress:
                progress(i, len(results), r.symbol)
        legacy = rec.history_status == "LEGACY_HEURISTIC"
        notes = []
        if legacy:
            notes.append("This scan was recorded before history requirements and snapshots existed; its inputs were not preserved.")
        return ScanReplay(scan_id, out, legacy, notes)

    def replay_result(self, scan: ScanRecord, r: ScanResult) -> SymbolReplay:
        data_layer, state = self._replay_data(scan, r)
        strat_layer, replayed_signal = self._replay_strategy(scan, r, data_layer, state)
        risk_layer = self._replay_risk(scan, r, data_layer, strat_layer, state, replayed_signal)
        notes: list[str] = []
        env = (r.data or {}).get("environment")
        now_env = environment_fingerprint()
        if env and env != now_env:
            notes.append(f"Software environment differs from the recording ({env} then, {now_env} now); numerical results can differ.")
        overall, reason = self._overall(data_layer, strat_layer, risk_layer)
        return SymbolReplay(r.symbol, overall, reason, data_layer, strat_layer, risk_layer, notes)

    # ------------------------------------------------------------------ data layer
    def _replay_data(self, scan: ScanRecord, r: ScanResult) -> tuple[LayerResult, dict[str, Any]]:
        d = r.data or {}
        snap = d.get("snapshot")
        stored = {"snapshot": (snap or {}).get("snapshot_id"), "content_hash": d.get("content_hash"),
                  "evaluated_hash": d.get("evaluated_hash"), "reason_code": r.reason_code}
        if not snap or snap.get("status") != "CAPTURED" or not snap.get("snapshot_id"):
            why = ("Recorded before snapshots existed (legacy scan): its input data was not preserved."
                   if not snap else f"No snapshot was captured ({snap.get('detail') or snap.get('status')}).")
            if r.status in (ResultStatus.DATA_UNAVAILABLE,) or (r.reason_code in ("DATA_INVALID", "SNAPSHOT_FAILED")):
                why = "No dataset was obtained for this symbol at scan time, so there is nothing to replay."
            return LayerResult(NOT_CAPTURED, why, stored), {}
        try:
            snapshot = self._snaps.load(snap["snapshot_id"])
        except SnapshotMissingError:
            return LayerResult(MISSING, "The stored snapshot file is not present.", stored), {}
        except SnapshotFormatError as exc:
            return LayerResult(UNSUPPORTED, str(exc), stored), {}
        except SnapshotCorruptError as exc:
            return LayerResult(CORRUPT, str(exc), stored), {}
        except SnapshotError as exc:
            return LayerResult(CORRUPT, safe_error_text(exc), stored), {}
        replayed = {"snapshot": snapshot.snapshot_id, "content_hash": dataframe_fingerprint(snapshot.candles)}
        if d.get("content_hash") and replayed["content_hash"] != d["content_hash"]:
            return LayerResult(MISMATCH, "The snapshot's dataset is not the dataset the scan recorded.", stored, replayed), {}
        try:
            as_of = datetime.fromisoformat(d["as_of"])
        except (KeyError, ValueError, TypeError):
            return LayerResult(NOT_CAPTURED, "The evaluation time was not recorded, so the complete bars cannot be re-derived.",
                               stored, replayed), {}
        prep = prepare_bars(snapshot.candles, self._cal, as_of)
        replayed["prepare_kind"] = prep.kind
        stored_kind = "OK" if d.get("evaluated_hash") else r.reason_code
        if prep.kind != "OK":
            ok = prep.kind == stored_kind
            return LayerResult(VERIFIED if ok else MISMATCH,
                               "Bars re-derived with the calendar at the recorded time; outcome %s." % prep.kind
                               if ok else f"Re-deriving the bars gave {prep.kind}, but the scan recorded {stored_kind}.",
                               stored, replayed), {}
        replayed["evaluated_hash"] = dataframe_fingerprint(prep.complete)
        if d.get("evaluated_hash") != replayed["evaluated_hash"]:
            return LayerResult(MISMATCH, "The bars re-derived from the snapshot are not the bars the strategy saw.", stored, replayed), {}
        return (LayerResult(VERIFIED, "Snapshot intact; recorded dataset and evaluated bars reproduced exactly.", stored, replayed),
                {"prep": prep})

    # ------------------------------------------------------------------ strategy layer
    def _replay_strategy(self, scan: ScanRecord, r: ScanResult, data: LayerResult, state: dict[str, Any]):
        if data.status != VERIFIED:
            return LayerResult(NOT_REPLAYABLE, f"Data layer is {data.status}; the strategy cannot be replayed on unverified data."), None
        prep = state.get("prep")
        if prep is None:
            return LayerResult(NOT_APPLICABLE, "The scan stopped before the strategy ran (data was not evaluable); the data layer "
                                               "reproduced that."), None
        try:
            instance, current_hash = resolve_strategy(scan.strategy_name, r.symbol, scan.strategy_params, self._prefixes)
        except StrategyResolutionError as exc:
            return LayerResult(UNAVAILABLE, f"The strategy cannot be built now: {safe_error_text(exc)}"), None
        stored = {"status": r.status.value, "reason_code": r.reason_code, "decision": r.decision, "strategy_hash": scan.strategy_hash}
        replayed: dict[str, Any] = {"strategy_hash": current_hash}
        hist = assess_history(requirement_of(instance), int(len(prep.complete)))
        replayed["history_satisfied"] = hist.satisfied
        label = "" if current_hash == scan.strategy_hash else \
            " NOTE: the strategy's source differs from the recorded hash, so this is the CURRENT implementation, not the original."
        if r.reason_code in ("INSUFFICIENT_HISTORY", "HISTORY_REQUIREMENT_UNKNOWN"):
            ok = not hist.satisfied
            return self._strategy_verdict(ok, current_hash != scan.strategy_hash, stored, replayed,
                                          "History requirement re-assessed on the stored bars." + label), None
        if hist.satisfied is False:
            return self._strategy_verdict(False, current_hash != scan.strategy_hash, stored, replayed,
                                          "The strategy now declares more history than these bars hold." + label), None
        ev = evaluate_at_bar(instance, prep.complete, prep.decision_bar, r.symbol)
        replayed["outcome"] = ev.outcome
        replayed["decision"] = signal_to_record(ev.signal) if ev.signal is not None else None
        expected = {ResultStatus.SIGNAL_FOUND: "SIGNAL", ResultStatus.RISK_BLOCKED: "SIGNAL", ResultStatus.NO_SIGNAL: "NO_SIGNAL",
                    ResultStatus.ERROR: "STRATEGY_ERROR"}.get(r.status)
        if r.reason_code == "ALREADY_FLAT":
            expected = "SIGNAL"
        if r.status is ResultStatus.NOT_EVALUABLE and r.reason_code in ("SYMBOL_MISMATCH", "AMBIGUOUS"):
            expected = r.reason_code
        if expected is None:
            return LayerResult(NOT_REPLAYABLE, f"Result status {r.status.value} has no strategy outcome to compare.", stored, replayed), None
        ok = ev.outcome == expected
        if ok and expected == "SIGNAL":
            a, b = r.decision or {}, replayed["decision"] or {}
            ok = all(a.get(k) == b.get(k) for k in ("timestamp", "symbol", "direction", "confidence"))
        return self._strategy_verdict(ok, current_hash != scan.strategy_hash, stored, replayed,
                                      f"Strategy re-run on the stored bars: {ev.outcome}.{label}"), ev.signal

    @staticmethod
    def _strategy_verdict(ok: bool, changed: bool, stored: dict, replayed: dict, detail: str) -> LayerResult:
        if changed:
            return LayerResult(CHANGED, detail + (" The result matches the record." if ok else " The result differs from the record."),
                               stored, replayed)
        return LayerResult(REPRODUCED if ok else DIFFERENT, detail, stored, replayed)

    # ------------------------------------------------------------------ risk layer
    def _replay_risk(self, scan: ScanRecord, r: ScanResult, data: LayerResult, strat: LayerResult, state: dict[str, Any], signal):
        stored = {"risk_state": r.risk_state.value, "reason_code": r.reason_code}
        trace = ((r.data or {}).get("trace") or {})
        ctx = trace.get("risk_context")
        if "risk" not in trace and ctx is None:
            return LayerResult(NOT_APPLICABLE, "Risk was not assessed in this scan (no signal, or risk assessment was off).", stored)
        if data.status != VERIFIED or signal is None:
            return LayerResult(NOT_REPLAYABLE, "The signal could not be reproduced, so its risk classification cannot be replayed.", stored)
        if ctx is None or not ctx.get("assessed") or ctx.get("unavailable"):
            return LayerResult(NO_CONTEXT, "Risk was assessed but its private inputs were not preserved.", stored)
        try:
            engine, policy, portfolio, price = rebuild_risk_inputs(ctx)
        except Exception as exc:  # noqa: BLE001
            return LayerResult(NO_CONTEXT, f"The stored risk inputs cannot be rebuilt: {safe_error_text(exc)}", stored)
        prep = state["prep"]
        re = assess_risk(engine, policy, signal, prep.complete, prep.decision_bar, copy.deepcopy(portfolio), price)
        replayed = {"kind": re.kind, "code": re.code}
        stored_risk = trace.get("risk") or {}
        stored.update({"kind": stored_risk.get("kind"), "code": stored_risk.get("code")})
        ok = (stored_risk.get("kind") == re.kind) and ((stored_risk.get("code") or None) == (re.code or None))
        return LayerResult(REPRODUCED if ok else DIFFERENT,
                           "Risk classification re-derived from the captured limits, stop policy and saved portfolio (not today's).",
                           stored, replayed)

    # ------------------------------------------------------------------ verdict
    @staticmethod
    def _overall(data: LayerResult, strat: LayerResult, risk: LayerResult) -> tuple[str, str]:
        if data.status != VERIFIED:
            if data.status == MISMATCH:
                return "DIFFERENT", data.detail
            return "NOT_REPLAYABLE", data.detail
        if strat.status in (NOT_REPLAYABLE, UNAVAILABLE):
            return "NOT_REPLAYABLE", strat.detail
        if strat.status == DIFFERENT or risk.status == DIFFERENT:
            return "DIFFERENT", (strat.detail if strat.status == DIFFERENT else risk.detail)
        if strat.status == CHANGED:
            return "NOT_REPLAYABLE", "The strategy implementation changed since the scan, so the original decision cannot be reproduced exactly."
        if risk.status in (NO_CONTEXT, NOT_REPLAYABLE):
            return "NOT_REPLAYABLE", risk.detail
        return "REPRODUCED", "Data, strategy and risk classification reproduced from the stored snapshot."
