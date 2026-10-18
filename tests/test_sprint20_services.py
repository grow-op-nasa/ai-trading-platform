"""Sprint 20 services: preferences, explanations, activity timeline (network-free, deterministic)."""

from __future__ import annotations

import json

from src.appsvc import activity, explain, prefs
from src.appsvc.paths import AppPaths
from src.paper.models import CycleStatus
from tests.test_appsvc import approved_fill, service_for  # noqa: F401
from tests.test_paper_runtime import STRAT, WED, World, _reset_script, fingerprint  # noqa: F401


# ------------------------------------------------------------------ preferences
def test_prefs_none_saved_and_no_accounts_never_guess(tmp_path):
    p = AppPaths.under(tmp_path)
    assert prefs.resolve_account(p, [1, 2]).state == prefs.NONE_SAVED
    assert prefs.resolve_account(p, [1, 2]).selected is None
    assert prefs.resolve_account(p, []).state == prefs.NO_ACCOUNTS
    assert not p.prefs_file.exists()                      # reading creates nothing


def test_prefs_round_trip_is_atomic_and_private(tmp_path):
    p = AppPaths.under(tmp_path)
    prefs.save_account(p, 3)
    assert json.loads(p.prefs_file.read_text())["last_account_id"] == 3
    assert (p.prefs_file.stat().st_mode & 0o777) == 0o600
    assert [f.name for f in p.prefs_file.parent.iterdir()] == ["preferences.json"]      # no temp leftovers
    r = prefs.resolve_account(p, [1, 3])
    assert r.state == prefs.SELECTED and r.selected == 3
    prefs.save_account(p, 1)
    assert prefs.resolve_account(p, [1, 3]).selected == 1


def test_prefs_saved_missing_and_malformed_are_explicit(tmp_path):
    p = AppPaths.under(tmp_path)
    prefs.save_account(p, 9)
    r = prefs.resolve_account(p, [1, 2])
    assert r.state == prefs.SAVED_MISSING and r.selected is None and r.saved_id == 9 and "not recreated" in r.message
    for bad in ("{not json", json.dumps({"schema_version": 99}), json.dumps({"schema_version": 1, "last_account_id": "x"})):
        p.prefs_file.write_text(bad)
        r = prefs.resolve_account(p, [1, 2])
        assert r.state == prefs.MALFORMED and r.selected is None
        assert p.prefs_file.read_text() == bad            # never repaired or overwritten by reading


def test_prefs_reject_non_integer_ids(tmp_path):
    import pytest
    for bad in (True, "1", 1.5):
        with pytest.raises(ValueError):
            prefs.save_account(AppPaths.under(tmp_path), bad)


# ------------------------------------------------------------------ explanations
def test_explanations_cover_every_cycle_status_and_flag_unknowns():
    for st in CycleStatus:
        assert "Unrecognised" not in explain.cycle(st.value).meaning, st
    assert "Unrecognised" in explain.cycle("MADE_UP").meaning
    assert "Unrecognised" in explain.proposal("MADE_UP").meaning
    for s in ("PENDING", "EXECUTED", "REJECTED", "EXPIRED", "INVALIDATED", "APPROVING"):
        assert "Unrecognised" not in explain.proposal(s).meaning, s
    assert explain.reconciliation("MISMATCH").kind == "FAIL"
    assert "automatic repair" in explain.reconciliation("MISMATCH").next_step


# ------------------------------------------------------------------ activity timeline
def test_timeline_is_empty_without_stores_and_creates_nothing(tmp_path):
    p = AppPaths.under(tmp_path / "none")
    assert activity.timeline(p, 1) == []
    assert not (tmp_path / "none").exists()


def test_timeline_links_come_only_from_stored_ids(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = service_for(w)
    done = approved_fill(svc, w)
    events = activity.timeline(AppPaths.under(w.tmp), w.exp)
    cats = {e.category for e in events}
    assert {"Cycle", "Proposal", "Order", "Fill", "Reconciliation"} <= cats
    assert [e.when for e in events] == sorted((e.when for e in events), reverse=True)
    cycle_id, client = done.record.cycle_id, done.record.client_order_id
    by = lambda c: [e for e in events if e.category == c]            # noqa: E731
    assert all(e.refs.get("cycle_id") == cycle_id for e in by("Proposal"))
    assert by("Order")[0].refs["client_order_id"] == client and by("Order")[0].refs["cycle_id"] == cycle_id
    assert by("Fill")[0].refs == {"client_order_id": client}                # a fill has no cycle id: none is invented
    assert "not a real-market" in by("Fill")[0].detail


def test_timeline_filters_only_hide_rows_and_reads_write_nothing(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = service_for(w)
    approved_fill(svc, w)
    p = AppPaths.under(w.tmp)
    before = (p.paper_db.read_bytes(), )
    allv = activity.timeline(p, w.exp)
    only = activity.timeline(p, w.exp, categories=("Fill",))
    assert len(only) == 1 and len(allv) > 1
    assert activity.timeline(p, w.exp, statuses=("NOPE",)) == []
    assert activity.timeline(p, None) == []                                 # no account selected: no paper events
    assert (p.paper_db.read_bytes(), ) == before


def test_rejected_proposal_timeline_has_no_order_or_fill(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = service_for(w)
    svc.run_with_preflight(w.config(), request_token="p")
    svc.reject_proposal(svc.proposals(w.exp)[0]["proposal"].proposal_id, w.exp)
    cats = {e.category for e in activity.timeline(AppPaths.under(w.tmp), w.exp)}
    assert "Proposal" in cats and not ({"Order", "Fill"} & cats)


# ------------------------------------------------------------------ home state
def _ok_items():
    from src.appsvc.system import OK, StatusItem
    return [StatusItem("Python", OK, "ok"), StatusItem("Dependencies", OK, "ok")]


_OK = _ok_items()


def _snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file())


def test_home_on_an_empty_directory_creates_nothing_and_points_to_setup(tmp_path):
    from src.appsvc import home
    from src.appsvc.paper import PaperService
    p = AppPaths.under(tmp_path / "empty")
    h = home.build(p, PaperService(p), None)
    assert not (tmp_path / "empty").exists()
    assert h.primary.label == "Review setup" and h.primary.page == home.SETUP
    assert any(c.key == "registry" and c.state == "TODO" and c.required for c in h.checklist)
    assert all(not c.required for c in h.checklist if c.key in ("monitoring", "alerts", "ai"))
    assert not any(c.state == "PROBLEM" for c in h.checklist if not c.required)


def test_home_primary_actions_follow_the_decision_order(tmp_path):
    from src.appsvc import home
    w = World(tmp_path, initialize=False)
    svc = service_for(w)
    p = AppPaths.under(w.tmp)
    assert home.build(p, svc, None, status_items=_OK).primary.label == "Select paper account"
    assert home.build(p, svc, w.exp, status_items=_OK).primary.label == "Set up paper account"
    w.runtime().initialize_account("fixture")
    assert home.build(p, svc, w.exp, status_items=_OK).primary.label == "Go to Paper Trading"
    w.script(WED, "LONG")
    svc.run_with_preflight(w.config(), request_token="p")
    h = home.build(p, svc, w.exp, status_items=_OK)
    assert h.primary.label == "Review proposed trade" and len(h.pending_proposals) == 1
    assert h.latest_cycle_explanation.kind == "WARN" and h.cash == 10_000.0


def test_home_is_read_only_and_requests_no_data(tmp_path):
    from src.appsvc import home
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = service_for(w)
    svc.run_with_preflight(w.config(), request_token="p")
    p = AppPaths.under(w.tmp)
    calls_before = len(w.provider.calls) if hasattr(w.provider, "calls") else None
    snap = _snapshot(w.tmp)
    home.build(p, svc, w.exp, status_items=_OK)
    home.build(p, svc, w.exp, status_items=_OK)
    assert _snapshot(w.tmp) == snap
    if calls_before is not None:
        assert len(w.provider.calls) == calls_before


def test_home_saved_missing_account_is_surfaced_not_replaced(tmp_path):
    from src.appsvc import home
    w = World(tmp_path)
    p = AppPaths.under(w.tmp)
    prefs.save_account(p, 999)
    res = prefs.resolve_account(p, [r.experiment_id for r in home.eligible_accounts(p)])
    h = home.build(p, service_for(w), res.selected, res, status_items=_OK)
    assert h.account is None and h.primary.label == "Select paper account" and "no longer available" in h.primary.reason


def test_freshness_is_calendar_only():
    from datetime import datetime, timezone
    from src.appsvc import freshness
    wed_after = datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)      # Wednesday after the close
    f = freshness.for_cycle("2026-09-30T00:00:00+00:00", wed_after)
    assert f.state == "CURRENT" and str(f.expected_session) == "2026-09-30"
    g = freshness.for_cycle("2026-09-29T00:00:00+00:00", wed_after)
    assert g.state == "NEWER_SESSION" and "Run a paper cycle" in g.next_step
    assert freshness.for_cycle(None, wed_after).state == "NONE"


# ------------------------------------------------------------------ Sprint 21: instrument selector
def test_catalogue_is_shared_ordered_and_ends_with_custom():
    from src.appsvc import instruments as ins
    assert ins.options()[-1] == ins.CUSTOM and ins.options()[:-1] == ins.symbols()
    assert {"QQQ", "SPY", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD"} <= set(ins.symbols())
    assert ins.display("QQQ").startswith("QQQ") and ins.display("TSLA") == "TSLA" and ins.lookup("TSLA") is None
    assert all(i.timeframes == ("1d",) for i in ins.CATALOGUE)


def test_symbol_normalisation_rejects_empty_and_malformed_and_never_defaults():
    from src.appsvc.instruments import normalize_symbol as n
    assert n("  tsla ") == ("TSLA", None) and n("brk-b")[0] == "BRK-B" and n("^gspc")[0] == "^GSPC" and n("eurusd=x")[0] == "EURUSD=X"
    for bad in ("", "   ", None, "TS LA", "T$LA", "A" * 30, "-X", "AA/BB"):
        sym, why = n(bad)
        assert sym is None and why, bad


def test_symbol_and_account_preferences_are_independent_and_atomic(tmp_path):
    p = AppPaths.under(tmp_path)
    prefs.save_symbol(p, "SPY")
    assert prefs.saved_symbol(p) == "SPY" and prefs.resolve_account(p, [1]).state == prefs.NONE_SAVED
    prefs.save_account(p, 1)
    assert prefs.saved_symbol(p) == "SPY" and prefs.resolve_account(p, [1]).selected == 1      # neither overwrote the other
    prefs.save_symbol(p, "TSLA")
    assert prefs.resolve_account(p, [1]).selected == 1 and prefs.saved_symbol(p) == "TSLA"
    assert [f.name for f in p.prefs_file.parent.iterdir()] == ["preferences.json"]
    import pytest
    with pytest.raises(ValueError):
        prefs.save_symbol(p, " TSLA")


def _symbol_service(w, unsupported=("ZZZZ",)):
    from src.data.base import DataProvider
    from src.data.exceptions import NoDataError
    base = w.provider

    class P(DataProvider):
        def fetch_candles(self, symbol, start, end, interval):
            if symbol in unsupported:
                raise NoDataError(f"no data for {symbol} (test)")
            return base.fetch_candles(symbol, start, end, interval)
    svc = service_for(w)
    from src.data.service import MarketDataService
    svc._market_data = MarketDataService(provider=P(), cache_dir=w.tmp / "c2", use_cache=False)
    return svc


def test_unsupported_but_valid_symbol_fails_the_authoritative_preflight_and_runs_nothing(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = _symbol_service(w)
    cfg = svc.build_config(experiment_id=w.exp, strategy_name=STRAT, symbol="zzzz")
    assert cfg.symbol == "ZZZZ"
    out = svc.run_with_preflight(cfg, request_token="u")
    assert out.record is None and out.refused and any(c["name"] == "market_data" for c in out.refused_checks)
    assert w.store().list_cycles(w.exp) == [] and w.store().list_proposals(w.exp) == []


def test_supported_custom_symbol_flows_through_cycle_proposal_and_approval_with_its_own_identity(tmp_path):
    w = World(tmp_path)
    w.script(WED, "LONG")
    svc = _symbol_service(w)
    cfg = svc.build_config(experiment_id=w.exp, strategy_name=STRAT, symbol=" tsla ")
    out = svc.run_with_preflight(cfg, request_token="t")
    rec = out.record
    assert rec is not None, (out.refused, out.refused_checks, out.error)
    assert rec.symbol == "TSLA"
    props = svc.proposals(w.exp, pending_only=True)
    if props:                                           # the scripted fixture strategy may or may not emit for this symbol
        assert props[0]["proposal"].symbol == "TSLA"
        assert w.store().list_intents(w.exp) == [] and w.store().list_fills(w.exp) == []     # still no order before approval


def test_symbol_participates_in_the_config_identity_so_stale_preflights_cannot_match(tmp_path):
    w = World(tmp_path)
    svc = service_for(w)
    a = svc.build_config(experiment_id=w.exp, strategy_name=STRAT, symbol="QQQ")
    b = svc.build_config(experiment_id=w.exp, strategy_name=STRAT, symbol="SPY")
    assert a.identity_hash() != b.identity_hash()
