"""Sprint 22: scanner snapshots and offline replay (ADR-0059). Network-free, deterministic."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date


from src.appsvc.paths import AppPaths
from src.scanner.models import ResultStatus
from src.scanner.replay import (CORRUPT, DIFFERENT, MISMATCH, MISSING, NOT_CAPTURED, NOT_REPLAYABLE, REPRODUCED, VERIFIED,
                                ReplayService)
from src.snapshots import SnapshotError, SnapshotStore
from src.utils.hashing import dataframe_fingerprint
from tests.test_scanner import (STRAT, ScanProvider, ScriptedScan, _reset, _risk_world, by_symbol, make,  # noqa: F401
                                scan_of)


def _tamper(tmp_path, scan_id, symbol, fn):
    """Edit one stored result's JSON in place (simulating a changed/legacy record)."""
    conn = sqlite3.connect(AppPaths.under(tmp_path).scanner_db)
    row = conn.execute("SELECT result_json FROM scan_results WHERE scan_id=? AND symbol=?", (scan_id, symbol)).fetchone()
    d = json.loads(row[0])
    fn(d)
    conn.execute("UPDATE scan_results SET result_json=? WHERE scan_id=? AND symbol=?", (json.dumps(d), scan_id, symbol))
    conn.commit()
    conn.close()


def _db_hash(tmp_path) -> str:
    return hashlib.sha256(AppPaths.under(tmp_path).scanner_db.read_bytes()).hexdigest()


def _dir_state(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file())


# ------------------------------------------------------------------ capture
def test_every_symbol_with_data_references_a_verified_snapshot(tmp_path):
    svc, prov = make(tmp_path)
    view, _ = scan_of(svc, "QQQ SPY")
    store = SnapshotStore(AppPaths.under(tmp_path).snapshots_dir)
    for r in view.results:
        snap = r.data["snapshot"]
        assert snap["status"] == "CAPTURED" and snap["format_version"] == 1
        loaded = store.load(snap["snapshot_id"])                       # integrity-verified read
        assert dataframe_fingerprint(loaded.candles) == r.data["content_hash"]
        assert loaded.meta["symbol"] == r.symbol and loaded.meta["provider"] == r.data["provider"]
        assert r.data["as_of"] and r.data["environment"]["pandas"]
        assert r.data["evaluated_window"]["count"] == r.data["complete_bars"]


def test_history_assessment_is_persisted_per_result(tmp_path):
    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    h = view.results[0].data["trace"]["history"]
    assert h["status"] == "DECLARED" and h["satisfied"] is True and h["required"] == 60 and h["available"] >= 60
    assert view.scan.history_requirement["min_bars"] == 60 and view.scan.history_status == "DECLARED"


def test_a_snapshot_that_cannot_be_stored_makes_the_symbol_an_honest_error(tmp_path, monkeypatch):
    def boom(self, candles, meta):
        raise SnapshotError("disk full")
    monkeypatch.setattr(SnapshotStore, "put", boom)
    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ")
    r = view.results[0]
    assert r.status is ResultStatus.ERROR and r.reason_code == "SNAPSHOT_FAILED" and r.decision is None


def test_identical_data_is_stored_once(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("t", "QQQ")
    svc.scan(wl.watchlist_id, STRAT, {"history_bars": 61})
    svc.scan(wl.watchlist_id, STRAT, {"history_bars": 62})
    assert len(SnapshotStore(AppPaths.under(tmp_path).snapshots_dir).list_ids()) == 1


# ------------------------------------------------------------------ replay
def _replay(tmp_path, svc, scan_id):
    return svc.replay(scan_id)


def test_replay_reproduces_signal_and_no_signal_without_any_provider_call(tmp_path):
    svc, prov = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ SPY")
    calls_after_scan = list(prov.calls)
    prov.fail = {s: RuntimeError("provider must not be called") for s in ("QQQ", "SPY")}
    rep = svc.replay(view.scan.scan_id)
    res = {r.symbol: r for r in rep.results}
    assert prov.calls == calls_after_scan                                # replay made no provider request
    for sym in ("QQQ", "SPY"):
        assert res[sym].overall == REPRODUCED and res[sym].data.status == VERIFIED
    assert res["QQQ"].strategy.status == REPRODUCED and res["QQQ"].strategy.replayed["decision"]["direction"] == "LONG"
    assert rep.counts == {"REPRODUCED": 2, "DIFFERENT": 0, "NOT_REPLAYABLE": 0}


def test_replay_mutates_nothing(tmp_path):
    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ SPY")
    paths = AppPaths.under(tmp_path)
    before = (_db_hash(tmp_path), _dir_state(paths.snapshots_dir))
    svc.replay(view.scan.scan_id)
    svc.replay(view.scan.scan_id)
    assert (_db_hash(tmp_path), _dir_state(paths.snapshots_dir)) == before
    assert not paths.paper_db.exists() and not paths.registry.exists()


def test_replay_of_a_stored_decision_survives_a_new_process_view(tmp_path):
    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ")
    fresh, _ = make(tmp_path)                                            # new service objects over the same files
    assert fresh.replay(view.scan.scan_id).results[0].overall == REPRODUCED


def test_insufficient_history_is_reproduced_as_not_evaluable(tmp_path):
    prov = ScanProvider()
    prov.short.add("SHORT")
    svc, _ = make(tmp_path, prov)
    view, _ = scan_of(svc, "SHORT QQQ")
    r = by_symbol(view)["SHORT"]
    assert r.status is ResultStatus.NOT_EVALUABLE and r.reason_code == "INSUFFICIENT_HISTORY"
    res = {x.symbol: x for x in svc.replay(view.scan.scan_id).results}
    assert res["SHORT"].overall == REPRODUCED and res["SHORT"].strategy.status == REPRODUCED


def test_stale_data_is_reproduced_from_the_snapshot_and_calendar(tmp_path):
    svc, _ = make(tmp_path, ScanProvider(last_session=date(2026, 10, 1)))
    view, _ = scan_of(svc, "QQQ")
    assert view.results[0].reason_code == "DATA_STALE"
    r = svc.replay(view.scan.scan_id).results[0]
    assert r.data.status == VERIFIED and r.overall == REPRODUCED


def test_a_corrupt_snapshot_is_detected_and_never_trusted(tmp_path):
    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    store = SnapshotStore(AppPaths.under(tmp_path).snapshots_dir)
    p = store.path_for(view.results[0].data["snapshot"]["snapshot_id"])
    raw = bytearray(p.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    p.write_bytes(bytes(raw))
    r = svc.replay(view.scan.scan_id).results[0]
    assert r.data.status == CORRUPT and r.overall == NOT_REPLAYABLE and r.strategy.status == NOT_REPLAYABLE


def test_a_missing_snapshot_is_reported_not_replayable(tmp_path):
    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    SnapshotStore(AppPaths.under(tmp_path).snapshots_dir).path_for(view.results[0].data["snapshot"]["snapshot_id"]).unlink()
    r = svc.replay(view.scan.scan_id).results[0]
    assert r.data.status == MISSING and r.overall == NOT_REPLAYABLE


def test_a_recorded_hash_that_the_snapshot_does_not_reproduce_is_a_data_mismatch(tmp_path):
    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    _tamper(tmp_path, view.scan.scan_id, "QQQ", lambda d: d["data"].__setitem__("evaluated_hash", "0" * 16))
    r = svc.replay(view.scan.scan_id).results[0]
    assert r.data.status == MISMATCH and r.overall == DIFFERENT


def test_a_changed_strategy_output_is_reported_different_at_the_strategy_layer(tmp_path):
    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ")
    ScriptedScan.SIGNAL_ON = set()                                       # same source hash, different behaviour
    r = svc.replay(view.scan.scan_id).results[0]
    assert r.data.status == VERIFIED and r.strategy.status == DIFFERENT and r.overall == DIFFERENT


def test_a_changed_strategy_source_is_never_presented_as_the_original_decision(tmp_path):
    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    conn = sqlite3.connect(AppPaths.under(tmp_path).scanner_db)
    row = conn.execute("SELECT record_json FROM scans WHERE scan_id=?", (view.scan.scan_id,)).fetchone()
    d = json.loads(row[0])
    d["strategy_hash"] = "f" * 64
    conn.execute("UPDATE scans SET record_json=? WHERE scan_id=?", (json.dumps(d), view.scan.scan_id))
    conn.commit()
    conn.close()
    r = svc.replay(view.scan.scan_id).results[0]
    assert r.strategy.status == "STRATEGY_CHANGED" and r.overall == NOT_REPLAYABLE
    assert "CURRENT implementation" in r.strategy.detail


def test_legacy_results_are_labelled_not_replayable_never_reproduced(tmp_path):
    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    _tamper(tmp_path, view.scan.scan_id, "QQQ", lambda d: d["data"].pop("snapshot"))
    r = svc.replay(view.scan.scan_id).results[0]
    assert r.data.status == NOT_CAPTURED and r.overall == NOT_REPLAYABLE and "legacy" in r.data.detail.lower()


def test_scan_records_written_before_sprint_22_load_with_legacy_labels(tmp_path):
    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    conn = sqlite3.connect(AppPaths.under(tmp_path).scanner_db)
    d = json.loads(conn.execute("SELECT record_json FROM scans").fetchone()[0])
    d.pop("history_requirement"), d.pop("history_status")
    conn.execute("UPDATE scans SET record_json=?", (json.dumps(d),))
    conn.commit()
    conn.close()
    again = make(tmp_path)[0].view(view.scan.scan_id)
    assert again.scan.history_status == "LEGACY_HEURISTIC" and again.scan.history_requirement is None
    assert make(tmp_path)[0].replay(view.scan.scan_id).legacy is True


# ------------------------------------------------------------------ risk layer
def test_risk_classification_is_replayed_from_the_captured_context_not_todays_portfolio(tmp_path, monkeypatch):
    exp = _risk_world(tmp_path)
    svc, _ = make(tmp_path)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: [STRAT])
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ", account=exp, assess_risk=True)
    r = by_symbol(view)["QQQ"]
    ctx = r.data["trace"]["risk_context"]
    assert ctx["assessed"] and ctx["portfolio"]["cash"] == 10_000.0 and ctx["stop"]["type"] == "atr"
    # the saved portfolio is changed afterwards; replay must still use the captured one
    from src.experiments.registry import ExperimentRegistry
    from src.portfolio.models import Portfolio
    ExperimentRegistry(AppPaths.under(tmp_path).registry).save_portfolio(exp, Portfolio.reconstruct(cash=1.0, positions=[]))
    res = svc.replay(view.scan.scan_id).results[0]
    assert res.risk.status == REPRODUCED and res.overall == REPRODUCED
    assert res.risk.replayed["kind"] == r.data["trace"]["risk"]["kind"]


def test_a_different_stored_risk_classification_is_reported_different(tmp_path, monkeypatch):
    exp = _risk_world(tmp_path)
    svc, _ = make(tmp_path)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: [STRAT])
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ", account=exp, assess_risk=True)
    _tamper(tmp_path, view.scan.scan_id, "QQQ", lambda d: d["data"]["trace"]["risk"].__setitem__("kind", "REJECTED_X"))
    res = svc.replay(view.scan.scan_id).results[0]
    assert res.risk.status == DIFFERENT and res.overall == DIFFERENT


def test_risk_context_that_was_not_preserved_is_not_replayed(tmp_path, monkeypatch):
    exp = _risk_world(tmp_path)
    svc, _ = make(tmp_path)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: [STRAT])
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ", account=exp, assess_risk=True)
    _tamper(tmp_path, view.scan.scan_id, "QQQ", lambda d: d["data"]["trace"].pop("risk_context"))
    res = svc.replay(view.scan.scan_id).results[0]
    assert res.risk.status == "NO_RISK_CONTEXT" and res.overall == NOT_REPLAYABLE


# ------------------------------------------------------------------ CLI + safety
def test_replay_cli_reports_and_exit_codes(tmp_path, capsys):
    import importlib.util

    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ SPY")
    spec = importlib.util.spec_from_file_location("replay_scan_cli", "scripts/replay_scan.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    from src.appsvc.scanner import ScannerService
    cli.ScannerService = lambda paths: ScannerService(paths, allowed_strategy_prefixes=("src.strategies", "tests.test_scanner"))
    assert cli.main(["--scan-id", view.scan.scan_id, "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["REPRODUCED"] == 2 and {r["symbol"] for r in payload["results"]} == {"QQQ", "SPY"}
    ScriptedScan.SIGNAL_ON = set()
    assert cli.main(["--scan-id", view.scan.scan_id, "--root", str(tmp_path)]) == 1          # a differing decision is non-zero
    assert cli.main(["--scan-id", "nope", "--root", str(tmp_path)]) == 2


def test_replay_code_has_no_provider_broker_or_write_surface():
    import ast
    from pathlib import Path

    for rel in ("src/scanner/replay.py", "src/scanner/replay_context.py", "src/snapshots/store.py"):
        tree = ast.parse(Path(rel).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        banned = ("src.broker", "src.execution", "src.data.service", "src.data.providers", "src.ai", "src.dashboard",
                  "src.promotion", "src.alerts", "src.monitoring", "yfinance", "smtplib", "requests")
        assert not [m for m in mods if any(m == b or m.startswith(b + ".") for b in banned)], (rel, mods)
    src = Path("src/scanner/replay.py").read_text()
    for forbidden in ("add_result", "create_scan", "finish_scan", "save_portfolio", "get_dataset", "fetch_candles"):
        assert forbidden not in src


# ------------------------------------------------------------------ audit UI service helper
def test_reproducibility_summary_reports_stored_facts_and_labels_legacy_results(tmp_path):
    from src.appsvc.scanner import reproducibility_of

    svc, _ = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    r = view.results[0]
    rep = reproducibility_of(view.scan, r)
    assert rep["replayable"] and rep["history"]["satisfied"] and rep["snapshot"]["snapshot_id"] == r.data["snapshot"]["snapshot_id"]
    r.data.pop("snapshot")
    legacy = reproducibility_of(view.scan, r)
    assert not legacy["replayable"] and "Not replayable" in legacy["label"] and legacy["snapshot"] is None


def test_viewing_the_reproducibility_summary_runs_no_replay_and_touches_no_files(tmp_path):
    from src.appsvc.scanner import reproducibility_of

    svc, prov = make(tmp_path)
    view, _ = scan_of(svc, "QQQ")
    paths = AppPaths.under(tmp_path)
    before = (_db_hash(tmp_path), _dir_state(paths.snapshots_dir), list(prov.calls))
    reproducibility_of(view.scan, view.results[0])
    svc.view(view.scan.scan_id)
    assert (_db_hash(tmp_path), _dir_state(paths.snapshots_dir), list(prov.calls)) == before
