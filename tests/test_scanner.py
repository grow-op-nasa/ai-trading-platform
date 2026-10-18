"""Opportunity Scanner (Sprint 21, ADR-0058): watchlists, scan engine, persistence, concurrency and no-side-effect proofs.

Network-free: a thread-safe synthetic provider behind the real MarketDataService, a registered scripted test strategy, and
the real shared evaluation code. Nothing contacts a network, broker, LLM or SMTP server.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.appsvc.paths import AppPaths
from src.appsvc.scanner import MAX_WATCHLIST_SYMBOLS, ScannerService, ScanSetupError, WatchlistError, explain_result, parse_symbols
from src.data.base import DataProvider, Interval
from src.data.exceptions import DataProviderError, NoDataError
from src.data.fault_injection import SyntheticCandleProvider
from src.data.service import MarketDataService
from src.scanner.engine import ScanEngine, scanner_min_bars
from src.scanner.models import ResultStatus, RiskState, ScanStatus
from src.signals.models import Signal, SignalDirection
from src.strategies.registry import register_strategy

REPO = Path(__file__).resolve().parent.parent
WED = date(2026, 10, 7)
AFTER_CLOSE = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc)
STRAT = "scanner_scripted_test"


@register_strategy(STRAT)
class ScriptedScan:
    """Emits LONG on the WED bar for symbols in SIGNAL_ON, raises for symbols in RAISE_ON."""
    SIGNAL_ON: set = set()
    RAISE_ON: set = set()

    def __init__(self, symbol: str, **params) -> None:
        self.symbol = symbol

    @property
    def name(self) -> str:
        return STRAT

    def prepare(self, data):
        if self.symbol in ScriptedScan.RAISE_ON:
            raise RuntimeError("scripted failure token=abc123")
        return data.copy()

    def generate_signals(self, data):
        if self.symbol not in ScriptedScan.SIGNAL_ON:
            return []
        return [Signal(timestamp=pd.Timestamp(ts), symbol=self.symbol, direction=SignalDirection.LONG, confidence=0.9)
                for ts in data.index if pd.Timestamp(ts).date() == WED]


@pytest.fixture(autouse=True)
def _reset():
    ScriptedScan.SIGNAL_ON, ScriptedScan.RAISE_ON = set(), set()
    yield


class ScanProvider(DataProvider):
    """Thread-safe synthetic provider with per-symbol behaviour and concurrency accounting."""

    def __init__(self, last_session: date = WED, delay: float = 0.0) -> None:
        self.base = SyntheticCandleProvider(last_session)
        self.fail: dict[str, Exception] = {}
        self.short: set[str] = set()
        self.delay, self.calls, self.active, self.peak = delay, [], 0, 0
        self._lock = threading.Lock()

    def fetch_candles(self, symbol, start, end, interval):
        with self._lock:
            self.calls.append(symbol)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if symbol in self.fail:
                raise self.fail[symbol]
            df = self.base.fetch_candles(symbol, start, end, interval)
            return df.iloc[-20:] if symbol in self.short else df
        finally:
            with self._lock:
                self.active -= 1


def make(tmp_path, provider=None, **kw):
    provider = provider or ScanProvider()
    md = MarketDataService(provider=provider, cache_dir=tmp_path / "cache", use_cache=False)
    svc = ScannerService(AppPaths.under(tmp_path), market_data=md, clock=lambda: AFTER_CLOSE,
                         allowed_strategy_prefixes=("src.strategies", __name__), **kw)
    return svc, provider


def scan_of(svc, symbols, **kw):
    wl = svc.create_watchlist("t", symbols)
    rec, created = svc.scan(wl.watchlist_id, STRAT, {}, **kw)
    return svc.view(rec.scan_id), created


def by_symbol(view):
    return {r.symbol: r for r in view.results}


# ===================================================================== watchlists
def test_the_scanner_store_is_not_created_by_reading(tmp_path):
    svc, _ = make(tmp_path)
    assert svc.watchlists() == [] and svc.history() == []
    assert not AppPaths.under(tmp_path).scanner_db.exists()


def test_symbols_are_normalised_deduplicated_and_ordered(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("  My   list ", " aapl, msft  AAPL\nbrk-b ")
    assert wl.symbols == ("AAPL", "MSFT", "BRK-B") and wl.name == "My list"


def test_invalid_symbols_reject_the_whole_edit_and_name_the_problem(tmp_path):
    svc, _ = make(tmp_path)
    with pytest.raises(WatchlistError, match="not accepted"):
        svc.create_watchlist("x", "AAPL, bad$sym")
    assert svc.watchlists() == []
    syms, problems = parse_symbols("AAPL bad$sym")
    assert syms == ["AAPL"] and len(problems) == 1


def test_the_size_limit_is_enforced_and_never_silently_truncates(tmp_path):
    svc, _ = make(tmp_path)
    too_many = [f"S{i}" for i in range(MAX_WATCHLIST_SYMBOLS + 1)]
    with pytest.raises(WatchlistError, match="at most"):
        svc.create_watchlist("big", too_many)
    ok = svc.create_watchlist("ok", too_many[:MAX_WATCHLIST_SYMBOLS])
    assert len(ok.symbols) == MAX_WATCHLIST_SYMBOLS
    with pytest.raises(WatchlistError):
        svc.update_watchlist(ok.watchlist_id, symbols=too_many)
    assert len(svc.watchlist(ok.watchlist_id).symbols) == MAX_WATCHLIST_SYMBOLS


def test_empty_names_and_empty_lists_are_refused(tmp_path):
    svc, _ = make(tmp_path)
    with pytest.raises(WatchlistError):
        svc.create_watchlist("  ", "AAPL")
    with pytest.raises(WatchlistError):
        svc.create_watchlist("n", "  ")


def test_rename_edit_delete_and_duplicate_names(tmp_path):
    svc, _ = make(tmp_path)
    a, b = svc.create_watchlist("Alpha", "AAPL"), svc.create_watchlist("Beta", "MSFT")
    with pytest.raises(WatchlistError, match="already exists"):
        svc.create_watchlist("alpha", "NVDA")
    with pytest.raises(WatchlistError, match="already exists"):
        svc.update_watchlist(b.watchlist_id, name="ALPHA")
    r = svc.update_watchlist(a.watchlist_id, name="Alpha 2", symbols="aapl tsla")
    assert (r.name, r.symbols) == ("Alpha 2", ("AAPL", "TSLA"))
    assert svc.delete_watchlist(a.watchlist_id) and svc.watchlist(a.watchlist_id) is None
    assert [w.name for w in svc.watchlists()] == ["Beta"]


def test_watchlists_persist_across_service_instances(tmp_path):
    svc, _ = make(tmp_path)
    svc.create_watchlist("Keep", "QQQ SPY")
    again, _ = make(tmp_path)
    assert [(w.name, w.symbols) for w in again.watchlists()] == [("Keep", ("QQQ", "SPY"))]


def test_starter_watchlist_seeds_once_and_never_returns_after_deletion(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.ensure_starter()
    assert wl.name == "US Liquid Starter"
    assert wl.symbols == ("QQQ", "SPY", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD")
    assert svc.ensure_starter() is None
    svc.delete_watchlist(wl.watchlist_id)
    assert svc.ensure_starter() is None and svc.watchlists() == []


def test_starter_does_not_seed_over_existing_user_watchlists(tmp_path):
    svc, _ = make(tmp_path)
    svc.create_watchlist("Mine", "AAPL")
    assert svc.ensure_starter() is None
    assert [w.name for w in svc.watchlists()] == ["Mine"]


# ===================================================================== statuses
def test_each_status_is_reported_with_precedence_and_missing_data_is_never_no_signal(tmp_path):
    prov = ScanProvider()
    prov.fail.update({"DOWN": DataProviderError("timeout"), "EMPTY": NoDataError("nothing")})
    prov.short.add("SHORT")
    svc, _ = make(tmp_path, prov)
    ScriptedScan.SIGNAL_ON = {"BUY"}
    ScriptedScan.RAISE_ON = {"BOOM"}
    view, _ = scan_of(svc, "BUY QUIET DOWN EMPTY SHORT BOOM")
    r = by_symbol(view)
    assert r["BUY"].status is ResultStatus.SIGNAL_FOUND and r["BUY"].decision["direction"] == "LONG"
    assert r["QUIET"].status is ResultStatus.NO_SIGNAL
    assert r["DOWN"].status is ResultStatus.DATA_UNAVAILABLE and r["EMPTY"].status is ResultStatus.DATA_UNAVAILABLE
    assert r["SHORT"].status is ResultStatus.NOT_EVALUABLE and r["SHORT"].reason_code == "INSUFFICIENT_HISTORY"
    assert r["BOOM"].status is ResultStatus.ERROR
    assert "abc123" not in (r["BOOM"].error or "")                       # credential-like text redacted
    assert view.status is ScanStatus.PARTIAL
    assert view.scan.counts["SIGNAL_FOUND"] == 1 and view.scan.counts["DATA_UNAVAILABLE"] == 2 and view.scan.counts["TOTAL"] == 6


def test_results_are_ordered_by_watchlist_position_not_completion_order(tmp_path):
    svc, _ = make(tmp_path, ScanProvider(delay=0.01))
    syms = ["ZZZ", "AAA", "MMM", "BBB", "YYY", "CCC"]
    view, _ = scan_of(svc, syms)
    assert [r.symbol for r in view.results] == syms


def test_all_symbols_unavailable_makes_the_scan_failed_and_all_good_completed(tmp_path):
    prov = ScanProvider()
    prov.fail.update({"A": DataProviderError("x"), "B": DataProviderError("x")})
    svc, _ = make(tmp_path, prov)
    assert scan_of(svc, "A B")[0].status is ScanStatus.FAILED
    prov2 = ScanProvider()
    svc2, _ = make(tmp_path / "ok", prov2)
    assert scan_of(svc2, "A B")[0].status is ScanStatus.COMPLETED


def test_stale_data_is_not_evaluable_not_no_signal(tmp_path):
    svc, _ = make(tmp_path, ScanProvider(last_session=date(2026, 10, 2)))
    r = scan_of(svc, "QQQ")[0].results[0]
    assert r.status is ResultStatus.NOT_EVALUABLE and r.reason_code == "DATA_STALE"


def test_unknown_strategy_is_refused_before_anything_is_recorded(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("t", "QQQ")
    with pytest.raises(ScanSetupError):
        svc.scan(wl.watchlist_id, "no_such_strategy", {})
    assert svc.history() == []


def test_only_production_registry_strategies_resolve(tmp_path):
    svc, _ = make(tmp_path)
    svc._prefixes = ("src.strategies",)                                  # the test module is NOT an approved module
    wl = svc.create_watchlist("t", "QQQ")
    with pytest.raises(ScanSetupError, match="approved"):
        svc.scan(wl.watchlist_id, STRAT, {})


def test_the_scanner_uses_daily_bars_only(tmp_path):
    svc, prov = make(tmp_path)
    scan_of(svc, "QQQ")
    assert prov.calls == ["QQQ"]
    eng = ScanEngine(svc.store, svc._md(), clock=lambda: AFTER_CLOSE)
    wl = svc.create_watchlist("w", "QQQ")
    with pytest.raises(ScanSetupError, match="daily"):
        eng.start(wl, "ema_cross", {}, interval="1h")


def test_min_bars_policy_is_documented_and_scales_with_parameters():
    assert scanner_min_bars({}) == 60 and scanner_min_bars({"slow": 50}) == 150 and scanner_min_bars({"x": 1.5, "b": True}) == 60


# ===================================================================== concurrency
def test_concurrency_is_bounded_and_failures_are_isolated(tmp_path):
    prov = ScanProvider(delay=0.03)
    prov.fail["S3"] = RuntimeError("provider bug")
    svc, _ = make(tmp_path, prov, concurrency=3)
    view, _ = scan_of(svc, [f"S{i}" for i in range(12)])
    assert 1 < prov.peak <= 3
    assert len(view.results) == 12
    assert by_symbol(view)["S3"].status is ResultStatus.ERROR
    assert sum(1 for r in view.results if r.status is ResultStatus.NO_SIGNAL) == 11


def test_requested_concurrency_is_clamped_to_the_documented_ceiling(tmp_path):
    svc, _ = make(tmp_path, concurrency=500)
    assert svc._engine()._conc == 8


def test_progress_is_reported_once_per_symbol_and_a_broken_callback_cannot_break_the_scan(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("t", "A B C")
    seen = []

    def cb(done, total, sym):
        seen.append((done, total))
        raise RuntimeError("ui exploded")

    rec, _ = svc.scan(wl.watchlist_id, STRAT, {}, progress=cb)
    assert sorted(seen) == [(1, 3), (2, 3), (3, 3)] and rec.status is ScanStatus.COMPLETED


# ===================================================================== provenance, persistence, snapshots
def test_scan_records_immutable_provenance(tmp_path):
    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ SPY")
    s = view.scan
    assert s.symbols == ("QQQ", "SPY") and s.strategy_name == STRAT and s.strategy_hash and s.config_hash
    assert s.interval == "1d" and s.risk_mode == "NOT_ASSESSED" and s.watchlist_name == "t" and s.finished_at
    r = by_symbol(view)["QQQ"]
    assert r.data["provider"] and r.data["content_hash"] and r.data["bars"] > 60 and r.decision_bar.startswith("2026-10-07")


def test_history_survives_restart_and_stays_a_faithful_snapshot_after_watchlist_edits(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("snap", "QQQ SPY")
    rec, _ = svc.scan(wl.watchlist_id, STRAT, {})
    svc.update_watchlist(wl.watchlist_id, name="renamed", symbols="IWM")
    svc.delete_watchlist(wl.watchlist_id)
    again, _ = make(tmp_path)
    v = again.view(rec.scan_id)
    assert v.scan.symbols == ("QQQ", "SPY") and v.scan.watchlist_name == "snap"
    assert [r.symbol for r in v.results] == ["QQQ", "SPY"] and v.status is ScanStatus.COMPLETED
    assert [h.scan.scan_id for h in again.history()] == [rec.scan_id]


def test_changing_params_changes_the_config_hash(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("t", "QQQ")
    a, _ = svc.scan(wl.watchlist_id, STRAT, {})
    b, _ = svc.scan(wl.watchlist_id, STRAT, {"x": 5})
    assert a.config_hash != b.config_hash and a.scan_id != b.scan_id


# ===================================================================== duplicate guard + recovery
def test_duplicate_submission_is_guarded_by_token_and_by_identical_running_scan(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("t", "QQQ")
    rec, created = svc.start_scan(wl.watchlist_id, STRAT, {}, request_token="tok")
    assert created
    same_input, created2 = svc.start_scan(wl.watchlist_id, STRAT, {}, request_token="other")
    assert not created2 and same_input.scan_id == rec.scan_id            # identical scan still RUNNING in this process
    tokened, created3 = svc.start_scan(wl.watchlist_id, STRAT, {"y": 1}, request_token="tok")
    assert not created3 and tokened.scan_id == rec.scan_id               # token reuse returns the original
    svc.run_scan(rec)
    again, created4 = svc.start_scan(wl.watchlist_id, STRAT, {}, request_token="new")
    assert created4 and again.scan_id != rec.scan_id                     # once finished, a fresh scan is allowed
    svc.run_scan(again)
    assert len(svc.history()) == 2


def test_a_scan_whose_process_died_is_reported_interrupted_and_keeps_real_results(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("t", "QQQ SPY")
    rec, _ = svc.start_scan(wl.watchlist_id, STRAT, {})
    from src.scanner import engine as eng_mod
    eng_mod._ACTIVE.discard(rec.scan_id)                                 # simulate the owning process vanishing
    svc.store.add_result(svc._engine()._result(rec, "QQQ", 0, ResultStatus.NO_SIGNAL, "NO_SIGNAL", "ok"))
    assert svc.view(rec.scan_id).status is ScanStatus.INTERRUPTED        # read-only inference
    assert svc.store.get_scan(rec.scan_id).status is ScanStatus.RUNNING  # nothing was written by viewing
    svc.recover_interrupted() if hasattr(svc, "recover_interrupted") else svc._engine().recover_interrupted()
    stored = svc.store.get_scan(rec.scan_id)
    assert stored.status is ScanStatus.INTERRUPTED and stored.counts["NO_SIGNAL"] == 1 and stored.counts["TOTAL"] == 1
    assert [r.symbol for r in svc.store.results(rec.scan_id)] == ["QQQ"]


# ===================================================================== risk
def test_without_an_account_risk_is_explicitly_not_assessed(tmp_path):
    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    r = by_symbol(scan_of(svc, "QQQ")[0])["QQQ"]
    assert r.status is ResultStatus.SIGNAL_FOUND and r.risk_state is RiskState.NOT_ASSESSED
    assert any("NOT assessed" in line for line in explain_result(r))


def test_assess_risk_requires_an_account(tmp_path):
    svc, _ = make(tmp_path)
    wl = svc.create_watchlist("t", "QQQ")
    with pytest.raises(ScanSetupError, match="account"):
        svc.scan(wl.watchlist_id, STRAT, {}, assess_risk=True)


def _risk_world(tmp_path, cash=10_000.0):
    from src.experiments.registry import ExperimentRegistry
    from src.experiments.spec import ExperimentSpec
    from src.portfolio.models import Portfolio
    from src.strategies.identity import strategy_version
    from src.strategies.registry import get_strategy_class
    paths = AppPaths.under(tmp_path)
    reg = ExperimentRegistry(paths.registry)
    exp = reg.log_experiment(strategy_name=STRAT, changed={}, metrics_before={}, metrics_after={}, decision="KEEP", notes="SYNTHETIC")
    reg.save_spec(exp, ExperimentSpec(strategy_name=STRAT, strategy_version=strategy_version(get_strategy_class(STRAT)),
                                      strategy_params={}, symbol="QQQ", interval=Interval.DAY_1,
                                      dataset_start=pd.Timestamp("2026-01-01", tz="UTC"), dataset_end=pd.Timestamp("2026-09-30", tz="UTC"),
                                      dataset_source="synthetic-test", dataset_fingerprint="synthetic-test",
                                      risk_config={"max_portfolio_exposure_pct": 0.5}))
    reg.save_portfolio(exp, Portfolio.reconstruct(cash=cash, positions=[]))
    return exp


def test_risk_is_assessed_read_only_against_the_saved_portfolio(tmp_path, monkeypatch):
    exp = _risk_world(tmp_path)
    svc, _ = make(tmp_path)
    monkeypatch.setattr("src.appsvc.strategies.eligible_names", lambda paths=None: [STRAT])
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: [STRAT])
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    before = AppPaths.under(tmp_path).registry.read_bytes()
    view, _ = scan_of(svc, "QQQ SPY", account=exp, assess_risk=True)
    r = by_symbol(view)
    assert view.scan.risk_mode == "ASSESSED" and view.scan.account == exp
    assert r["QQQ"].status in (ResultStatus.SIGNAL_FOUND, ResultStatus.RISK_BLOCKED)
    assert r["QQQ"].risk_state in (RiskState.APPROVED, RiskState.REJECTED)
    assert r["SPY"].status is ResultStatus.NO_SIGNAL
    assert AppPaths.under(tmp_path).registry.read_bytes() == before      # the saved portfolio was not written


def test_a_risk_setup_failure_falls_back_to_not_assessed_with_a_visible_note(tmp_path):
    svc, _ = make(tmp_path)                                              # account 99 does not exist
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ", account=99, assess_risk=True)
    assert by_symbol(view)["QQQ"].risk_state is RiskState.NOT_ASSESSED
    assert "Risk could not be set up" in (view.scan.error or "")


# ===================================================================== handoff + safety
def test_review_handoff_is_descriptive_only_and_limited_to_signals(tmp_path):
    svc, _ = make(tmp_path)
    ScriptedScan.SIGNAL_ON = {"QQQ"}
    view, _ = scan_of(svc, "QQQ SPY")
    r = by_symbol(view)
    h = svc.review_handoff(view.scan, r["QQQ"])
    assert h["symbol"] == "QQQ" and h["strategy"] == STRAT and "approval" in h["note"]
    with pytest.raises(ScanSetupError):
        svc.review_handoff(view.scan, r["SPY"])


def test_scanning_creates_no_paper_records_and_never_submits_an_order(tmp_path, monkeypatch):
    exp = _risk_world(tmp_path)
    paths = AppPaths.under(tmp_path)
    from src.execution.engine import PaperBroker
    calls = []
    for name in ("submit", "execute", "place_order", "submit_order"):
        if hasattr(PaperBroker, name):
            monkeypatch.setattr(PaperBroker, name, lambda *a, **k: calls.append(1))
    svc, _ = make(tmp_path)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: [STRAT])
    ScriptedScan.SIGNAL_ON = {"QQQ", "SPY"}
    reg_before = paths.registry.read_bytes()
    scan_of(svc, "QQQ SPY IWM", account=exp, assess_risk=True)
    assert calls == [] and paths.registry.read_bytes() == reg_before
    assert not paths.paper_db.exists()                                   # no paper cycle/intent/proposal/fill store was even created


def test_scanner_tables_are_the_only_things_written(tmp_path):
    svc, _ = make(tmp_path)
    scan_of(svc, "QQQ")
    conn = sqlite3.connect(AppPaths.under(tmp_path).scanner_db)
    tables = {t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"scanner_meta", "watchlists", "scans", "scan_results"}


def _imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_scanner_packages_import_no_broker_execution_or_proposal_code():
    forbidden = ("src.broker", "src.execution", "src.reconciliation", "src.ai", "streamlit", "subprocess", "src.dashboard")
    for f in [*(REPO / "src" / "scanner").glob("*.py"), REPO / "src" / "appsvc" / "scanner.py"]:
        bad = [m for m in _imports(f) if m.startswith(forbidden)]
        assert not bad, (f.name, bad)
    for f in (REPO / "src" / "scanner").glob("*.py"):
        paper = [m for m in _imports(f) if m.startswith("src.paper")]
        assert set(paper) <= {"src.paper.evaluation"}, (f.name, paper)


def test_scanner_code_never_calls_mutating_paper_apis():
    text = "".join(p.read_text() for p in [*(REPO / "src" / "scanner").glob("*.py"), REPO / "src" / "appsvc" / "scanner.py"])
    for needle in ("run_cycle(", "run_with_preflight(", "approve_proposal(", "submit_order(", ".submit(", "create_paper_account(",
                   "initialize_account(", "save_portfolio(", "log_experiment("):
        assert needle not in text.replace("pool.submit", ""), needle
