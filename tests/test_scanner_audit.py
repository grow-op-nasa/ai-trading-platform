"""Scanner data-to-decision audit (Sprint 21 follow-up). Network-free and deterministic.

The REAL registered `ema_cross` strategy is run on a deterministic oscillating price path. Nothing is tuned to make a signal
appear: the path simply crosses repeatedly, and for every decision day the scanner must agree with a DIRECT, independent
evaluation of the strategy on the same completed bars.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from src.appsvc.paths import AppPaths
from src.appsvc.scanner import ScannerService, explain_result, trace_of
from src.calendar.nyse import NYSECalendar
from src.data.fault_injection import SyntheticCandleProvider
from src.data.service import MarketDataService
from src.paper import bars
from src.paper.evaluation import evaluate_at_bar, prepare_bars, resolve_strategy
from src.scanner.models import ResultStatus, RiskState
from tests.test_scanner import _risk_world, make, scan_of as _fixture_scan  # noqa: F401

import math


def scan_of(svc, symbols, **kw):
    """Scan with the REAL registered ema_cross (default parameters unless given)."""
    wl = svc.create_watchlist('t', symbols)
    rec, _ = svc.scan(wl.watchlist_id, 'ema_cross', kw.pop('params', {}), **kw)
    return svc.view(rec.scan_id), True


class OscillatingProvider(SyntheticCandleProvider):
    """Closes are a pure function of the calendar DATE (period ~60 days), so a bar's value never depends on the window."""

    def fetch_candles(self, symbol, start, end, interval):
        df = super().fetch_candles(symbol, start, end, interval)
        close = pd.Series([100.0 + 10.0 * math.sin(2 * math.pi * pd.Timestamp(i).toordinal() / 60.0) for i in df.index], index=df.index)
        out = df.copy()
        out["open"], out["close"], out["high"], out["low"] = close, close, close + 1.0, close - 1.0
        return out


def _days(n=60):
    cal = NYSECalendar()
    sessions = cal.sessions_between(pd.Timestamp("2026-06-01").date(), pd.Timestamp("2026-10-07").date())
    return sessions[-n:]


def _service(tmp_path, day):
    md = MarketDataService(provider=OscillatingProvider(day), cache_dir=tmp_path / "cache", use_cache=False)
    clock = lambda: datetime(day.year, day.month, day.day, 22, 0, tzinfo=timezone.utc)   # noqa: E731  after the close
    return ScannerService(AppPaths.under(tmp_path), market_data=md, clock=clock), md, clock


def _direct(md, day, clock, symbol="QQQ"):
    """Independent evaluation: fetch, keep complete bars, run the strategy class itself, read the signal ON the last bar."""
    from datetime import timedelta
    ds = md.get_dataset(symbol, start=day - timedelta(days=400), end=day, use_cache=False)
    prep = prepare_bars(ds.candles, NYSECalendar(), clock())
    assert prep.kind == "OK"
    inst, _ = resolve_strategy("ema_cross", symbol, {})
    sigs = inst.generate_signals(inst.prepare(prep.complete.copy()))
    last = [s for s in sigs if pd.Timestamp(s.timestamp) == prep.decision_bar]
    return prep, ds, sigs, last


def test_scanner_matches_a_direct_strategy_evaluation_on_every_decision_day_and_real_rules_do_fire(tmp_path):
    seen = {"LONG": 0, "FLAT": 0, "none": 0}
    for i, day in enumerate(_days()):
        svc, md, clock = _service(tmp_path / f"d{i}", day)
        _, _, _, last = _direct(md, day, clock)
        view, _ = scan_of(svc, "QQQ")
        r = view.results[0]
        if not last:
            seen["none"] += 1
            assert r.status is ResultStatus.NO_SIGNAL and r.decision is None and r.reason_code == "NO_SIGNAL"
        else:
            assert len(last) == 1
            seen[last[0].direction.value] += 1
            # no account selected: a strategy signal is reported as SIGNAL_FOUND with risk NOT assessed
            assert r.status is ResultStatus.SIGNAL_FOUND and r.decision["direction"] == last[0].direction.value
            assert r.decision["timestamp"] == pd.Timestamp(last[0].timestamp).isoformat() and r.risk_state is RiskState.NOT_ASSESSED
    assert seen["LONG"] >= 1 and seen["FLAT"] >= 1 and seen["none"] >= 1, seen      # fixture really exercised both real rules


def _first_day_with(direction, tmp_path):
    for i, day in enumerate(_days()):
        svc, md, clock = _service(tmp_path / f"p{i}", day)
        _, _, _, last = _direct(md, day, clock)
        if last and last[0].direction.value == direction:
            return day
    raise AssertionError(f"fixture produced no {direction}")


def test_a_flat_signal_with_no_position_is_interpreted_as_already_flat_not_a_reviewable_signal(tmp_path, monkeypatch):
    day = _first_day_with("FLAT", tmp_path)
    exp = _risk_world(tmp_path / "w")
    svc, md, clock = _service(tmp_path / "w", day)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: ["ema_cross"])
    wl = svc.create_watchlist("t", "QQQ")
    rec, _ = svc.scan(wl.watchlist_id, "ema_cross", {}, account=exp, assess_risk=True)
    r = svc.view(rec.scan_id).results[0]
    assert r.status is ResultStatus.NO_SIGNAL and r.reason_code == "ALREADY_FLAT"
    assert r.decision["direction"] == "FLAT"                                # the strategy output is preserved, not discarded
    assert r.risk_state is RiskState.NOT_APPLICABLE                        # "nothing to assess" -- never a risk approval
    text = " ".join(explain_result(r))
    assert "DID emit FLAT" in text and "NOT a risk approval" in text
    with pytest.raises(Exception):
        svc.review_handoff(rec, r)                                          # not reviewable


def test_a_long_signal_with_an_account_goes_through_the_real_risk_check(tmp_path, monkeypatch):
    day = _first_day_with("LONG", tmp_path)
    exp = _risk_world(tmp_path / "w")
    svc, md, clock = _service(tmp_path / "w", day)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: ["ema_cross"])
    wl = svc.create_watchlist("t", "QQQ")
    rec, _ = svc.scan(wl.watchlist_id, "ema_cross", {}, account=exp, assess_risk=True)
    r = svc.view(rec.scan_id).results[0]
    assert r.decision["direction"] == "LONG" and r.risk_state in (RiskState.APPROVED, RiskState.REJECTED)
    assert r.status in (ResultStatus.SIGNAL_FOUND, ResultStatus.RISK_BLOCKED)
    assert trace_of(r)["risk"]["kind"] in ("APPROVED", "REJECTED", "STOP_UNAVAILABLE", "SHORT_NOT_MODELED", "ENGINE_ERROR")


def test_the_recorded_trace_matches_the_shared_evaluation_and_the_data_that_was_used(tmp_path):
    day = _first_day_with("LONG", tmp_path)
    svc, md, clock = _service(tmp_path / "t", day)
    view, _ = scan_of(svc, "QQQ")
    r = view.results[0]
    t = trace_of(r)
    prep, ds, sigs, last = _direct(md, day, clock)
    assert r.data["content_hash"] == ds.content_hash and r.data["provider"] == "OscillatingProvider"
    # bars: the last three COMPLETED bars with OHLCV, newest last, the decision bar equal to the final one
    lb = t["bars"]["last_bars"]
    assert len(lb) == 3 and lb[-1]["timestamp"] == pd.Timestamp(prep.decision_bar).isoformat() and t["bars"]["previous_bar"] == lb[-2]["timestamp"]
    assert lb[-1]["close"] == pytest.approx(float(prep.complete["close"].iloc[-1])) and set(lb[-1]) >= {"open", "high", "low", "close", "volume"}
    assert t["validation"]["outcome"].startswith("passed") and t["history"]["required"] == view.scan.min_bars
    assert t["history"]["available"] == len(prep.complete)
    # strategy evidence: the EMA values the strategy itself computed, for the last two bars, and the crossover condition they imply
    inst, _ = resolve_strategy("ema_cross", "QQQ", {})
    prepared = inst.prepare(prep.complete.copy())
    ind = t["strategy"]["indicators_last_two_bars"]
    (prev_ts, prev), (last_ts, cur) = list(ind.items())
    assert cur["ema_fast"] == pytest.approx(float(prepared["ema_fast"].iloc[-1])) and prev["ema_slow"] == pytest.approx(float(prepared["ema_slow"].iloc[-2]))
    assert cur["ema_fast"] > cur["ema_slow"]                                # a LONG at this bar means fast is above slow...
    assert t["strategy"]["signals_at_decision_bar"] == 1 and t["strategy"]["outcome"] == "SIGNAL"
    # the persisted normalised decision equals the shared evaluation module's own result
    ev = evaluate_at_bar(inst, prep.complete, prep.decision_bar, "QQQ")
    assert ev.outcome == "SIGNAL" and r.decision["direction"] == ev.signal.direction.value
    assert r.decision["direction"] == ev.signal.direction.value and r.decision["timestamp"] == pd.Timestamp(ev.signal.timestamp).isoformat()


def test_equivalent_data_and_configuration_give_identical_decisions_and_hashes(tmp_path):
    day = _first_day_with("LONG", tmp_path)
    a_svc, *_ = _service(tmp_path / "a", day)
    b_svc, *_ = _service(tmp_path / "b", day)
    va, _ = scan_of(a_svc, "QQQ SPY")
    vb, _ = scan_of(b_svc, "QQQ SPY")
    key = lambda v: [(r.symbol, r.status, r.decision_bar, (r.decision or {}).get("direction"), r.data["content_hash"]) for r in v.results]  # noqa: E731
    assert key(va) == key(vb) and va.scan.config_hash == vb.scan.config_hash and va.scan.strategy_hash == vb.scan.strategy_hash


def test_each_result_stays_bound_to_its_own_symbol_scan_configuration_and_data(tmp_path):
    day = _first_day_with("LONG", tmp_path)
    svc, md, clock = _service(tmp_path / "x", day)
    wl = svc.create_watchlist("t", "QQQ SPY")
    a, _ = svc.scan(wl.watchlist_id, "ema_cross", {})
    b, _ = svc.scan(wl.watchlist_id, "ema_cross", {"fast": 5, "slow": 20})
    va, vb = svc.view(a.scan_id), svc.view(b.scan_id)
    assert a.strategy_params == {} and b.strategy_params == {"fast": 5, "slow": 20} and a.config_hash != b.config_hash
    assert {r.scan_id for r in va.results} == {a.scan_id} and {r.scan_id for r in vb.results} == {b.scan_id}
    assert [r.symbol for r in va.results] == ["QQQ", "SPY"] == [r.symbol for r in vb.results]


def test_a_strategy_exception_and_missing_data_are_never_reported_as_no_signal(tmp_path):
    from tests.test_scanner import STRAT, ScanProvider, ScriptedScan
    from src.data.exceptions import DataProviderError
    prov = ScanProvider()
    prov.fail["DOWN"] = DataProviderError("no network")
    svc, _ = make(tmp_path, prov)
    ScriptedScan.RAISE_ON = {"BOOM"}
    wl = svc.create_watchlist("t", "DOWN BOOM")
    try:
        rec, _ = svc.scan(wl.watchlist_id, STRAT, {})
    finally:
        ScriptedScan.RAISE_ON = set()
    view = svc.view(rec.scan_id)
    st = {r.symbol: r.status for r in view.results}
    assert st == {"DOWN": ResultStatus.DATA_UNAVAILABLE, "BOOM": ResultStatus.ERROR}
    assert all(r.status is not ResultStatus.NO_SIGNAL for r in view.results)


def test_a_plain_no_signal_is_explained_as_no_state_change_and_risk_is_not_a_pass(tmp_path):
    from tests.test_scanner import STRAT  # noqa: F401
    svc, _ = make(tmp_path)
    r = scan_of(svc, "QQQ")[0].results[0]
    text = " ".join(explain_result(r))
    assert r.status is ResultStatus.NO_SIGNAL and r.decision is None
    assert "state CHANGES" in text and "does not say the symbol is bullish" in text and "NOT a risk approval" in text


def test_the_audit_trace_is_observational_only_and_never_changes_paper_state(tmp_path):
    day = _first_day_with("LONG", tmp_path)
    svc, *_ = _service(tmp_path / "s", day)
    scan_of(svc, "QQQ")
    paths = AppPaths.under(tmp_path / "s")
    assert not paths.paper_db.exists() and not paths.registry.exists() and not paths.monitoring_db.exists()
