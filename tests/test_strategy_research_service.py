"""Research service (src/strategy_research/service.py, Sprint 23, ADR-0061). Deterministic and network-free."""

from __future__ import annotations

import sqlite3
import types
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.appsvc.paths import AppPaths
from src.appsvc.strategy_research import StrategyResearchService
from src.calendar.nyse import NYSECalendar
from src.data.canonical import canonicalize_candles
from src.data.exceptions import DataValidationError, NoDataError
from src.snapshots import SnapshotStore
from src.strategies.catalogue import Directionality, StrategyFamily, StrategyMetadata
from src.strategies.ema_cross import EMACrossStrategy
from src.strategies.registry import _unregister_strategy, register_strategy
from src.strategy_research import (
    HoldoutAlreadyEvaluated,
    ResearchAssumptions,
    ResearchRequest,
    ResearchService,
    ResearchStore,
    ValidationProtocol,
    research_state,
)
from src.strategy_research.scorecard import Scorecard
from src.strategy_research.service import assess_out_of_sample
from src.strategy_research.universe import MAX_BATCH_SYMBOLS, NO_ACTIVITY, UNAVAILABLE
from src.utils.hashing import dataframe_fingerprint

START, END = date(2019, 1, 2), date(2022, 6, 30)


def sessions(n: int) -> pd.DatetimeIndex:
    """n real NYSE session dates from 2019-01-02 (holidays removed, as real provider data has none)."""
    cal = NYSECalendar()
    days = [d for d in pd.bdate_range("2019-01-02", periods=n + 80) if cal.is_session(d.date())][:n]
    return pd.DatetimeIndex(days, tz="UTC")


def declining(n: int = 900) -> pd.DataFrame:
    """A steady decline with no noise: a long-only trend rule never has its fast EMA above its slow one."""
    idx = sessions(n)
    c = 100 * 0.999 ** np.arange(n)
    return canonicalize_candles(pd.DataFrame({"open": c * 1.0005, "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 1e6}, index=idx))


def synth(n: int = 900, seed: int = 3, drift: float = 0.0004) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = sessions(n)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.012, n)))
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * 1.004
    low = np.minimum(o, c) * 0.996
    return canonicalize_candles(pd.DataFrame({"open": o, "high": h, "low": low, "close": c, "volume": 1e6}, index=idx))


class SyntheticSource:
    """A MarketDataService stand-in returning a fixed, already-canonical frame."""

    def __init__(self, frame: pd.DataFrame, gaps: int = 0, error: Exception | None = None) -> None:
        self.frame, self.gaps, self.error, self.calls = frame, gaps, error, 0

    def get_dataset(self, symbol, start=None, end=None, interval=None, use_cache=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(
            candles=self.frame, provider="synthetic", content_hash=dataframe_fingerprint(self.frame),
            validation_report=types.SimpleNamespace(gaps=types.SimpleNamespace(count=self.gaps), is_clean=self.gaps == 0, suspicious=()),
            interval=interval, session_policy="regular", session_timezone="America/New_York", requested_start=start,
            requested_end=end, dataset_start=self.frame.index.min(), dataset_end=self.frame.index.max())


def service(tmp_path, source=None, clock=None) -> ResearchService:
    return ResearchService(source or SyntheticSource(synth()), ResearchStore(tmp_path / "r.db"), SnapshotStore(tmp_path / "snap"),
                           clock=clock)


def request(name: str = "ema_cross", **kw) -> ResearchRequest:
    return ResearchRequest(name, kw.pop("symbol", "SPY"), START, END, **kw)


def completed(tmp_path, name="ema_cross", **kw):
    svc = service(tmp_path)
    out = svc.run(request(name, **kw))
    assert out.status == "COMPLETED", out.reason
    return svc, out


# ----------------------------------------------------------------------------------------------- provenance
def test_completed_run_records_full_provenance(tmp_path):
    svc, out = completed(tmp_path, configs=({}, {"fast": 8, "slow": 21}))
    rec = out.record
    prov = rec["provenance"]
    assert prov["provider"] == "synthetic" and prov["symbol"] == "SPY" and prov["interval"] == "1d"
    assert prov["requested_start"] == "2019-01-02" and prov["requested_end"] == "2022-06-30"
    assert prov["valid_bars"] == 900 and prov["first_bar"].startswith("2019-01-02") and prov["dropped_incomplete"] == []
    assert prov["dataset_hash"] == dataframe_fingerprint(synth()) and prov["snapshot_id"]
    SnapshotStore(tmp_path / "snap").verify(prov["snapshot_id"])
    assert rec["strategy"]["id"] == "ema_cross" and len(rec["strategy"]["version"]) == 64
    assert rec["strategy"]["family"] == "TREND_FOLLOWING"
    chosen = EMACrossStrategy(symbol="SPY", **rec["selected"]["params"]).history_requirement().to_dict()
    assert rec["history"]["requirement"] == chosen and rec["history"]["max_requirement_bars"] == 61   # 12/26 needs 61 bars
    assert rec["assumptions"]["fill_timing"] == "NEXT_BAR_OPEN" and rec["assumptions"]["slippage_bps"] == 2.0
    assert [a["params"] for a in rec["attempts"]] == [{}, {"fast": 8, "slow": 21}]
    assert all(len(a["params_hash"]) == 16 for a in rec["attempts"])
    assert rec["selection"]["configurations_attempted"] == 2 and rec["holdout"]["status"] == "LOCKED"
    assert rec["limitations"] and out.run_id == svc._store.get_run(out.run_id)["run_id"]


def test_protocol_boundaries_are_ordered_and_exclude_warmup_bars(tmp_path):
    _, out = completed(tmp_path)
    b = out.record["boundaries"]
    assert b["development"]["last_bar"] < b["validation"]["first_bar"] <= b["validation"]["last_bar"] < b["holdout"]["first_bar"]
    first_valid = out.record["history"]["first_valid_bar"]
    assert b["development"]["first_bar"] == first_valid
    assert first_valid == synth().index[60].isoformat()               # 61 bars required -> position 60
    assert b["development"]["bars"] + b["validation"]["bars"] + b["holdout"]["bars"] == 900 - 60


def test_same_inputs_give_identical_scorecards_and_share_one_snapshot(tmp_path):
    svc = service(tmp_path)
    a, b = svc.run(request()), svc.run(request())
    assert a.run_id != b.run_id
    assert a.record["attempts"] == b.record["attempts"]
    assert a.record["provenance"]["snapshot_id"] == b.record["provenance"]["snapshot_id"]
    assert len(SnapshotStore(tmp_path / "snap").list_ids()) == 1


def test_forming_bars_are_excluded_and_recorded(tmp_path):
    def clock():
        return datetime(2022, 3, 31, 12, 0, tzinfo=timezone.utc)      # a fixed clock inside the data range

    out = service(tmp_path, clock=clock).run(request())
    assert out.status == "COMPLETED"
    prov = out.record["provenance"]
    assert prov["dropped_incomplete"] and prov["valid_bars"] == 900 - len(prov["dropped_incomplete"])
    assert prov["last_bar"] < "2022-04-01" and any("incomplete" in x for x in prov["limitations"])


# ------------------------------------------------------------------------- selection discipline / holdout
SEEN: list[pd.Timestamp] = []


@pytest.fixture
def probe_strategy():
    SEEN.clear()
    meta = StrategyMetadata(family=StrategyFamily.TREND_FOLLOWING, hypothesis="h", entry="e", exit="x",
                            directionality=Directionality.LONG_FLAT, supported_intervals=("1d",), regime_assumptions="r",
                            known_weaknesses=("w",))

    @register_strategy("zz_probe_ema")
    class ProbeEMA(EMACrossStrategy):
        METADATA = meta

        def prepare(self, data):
            SEEN.append(data.index.max())
            return super().prepare(data)

    yield "zz_probe_ema"
    _unregister_strategy("zz_probe_ema")


def test_selection_never_receives_a_single_holdout_bar(tmp_path, probe_strategy):
    svc = service(tmp_path)
    out = svc.run(request(probe_strategy, configs=({}, {"fast": 5, "slow": 13})))
    assert out.status == "COMPLETED"
    validation_end = pd.Timestamp(out.record["boundaries"]["validation"]["last_bar"])
    holdout_start = pd.Timestamp(out.record["boundaries"]["holdout"]["first_bar"])
    assert SEEN and max(SEEN) == validation_end and max(SEEN) < holdout_start
    SEEN.clear()
    svc.evaluate_holdout(out.run_id)
    assert max(SEEN) == synth().index[-1]                               # only now are the later bars ever used


def test_holdout_is_evaluated_exactly_once_and_on_the_selected_configuration(tmp_path):
    svc, out = completed(tmp_path, configs=({}, {"fast": 5, "slow": 13}))
    payload = svc.evaluate_holdout(out.run_id)
    chosen = out.record["selected"]["params"]
    assert payload["configuration"] == chosen and payload["params_hash"] == out.record["selected"]["params_hash"]
    assert payload["scorecard"]["label"] == "holdout"
    assert payload["scorecard"]["first_bar"] == out.record["boundaries"]["holdout"]["first_bar"]
    assert payload["scorecard"]["last_bar"] == out.record["boundaries"]["holdout"]["last_bar"]
    with pytest.raises(HoldoutAlreadyEvaluated):
        svc.evaluate_holdout(out.run_id)
    assert svc._store.get_holdout(out.run_id)["payload"]["scorecard"] == payload["scorecard"]


def test_holdout_cannot_change_the_recorded_selection(tmp_path):
    svc, out = completed(tmp_path, configs=({}, {"fast": 5, "slow": 13}))
    before = svc._store.get_run(out.run_id)["payload"]
    svc.evaluate_holdout(out.run_id)
    assert svc._store.get_run(out.run_id)["payload"] == before


def test_a_run_that_could_not_be_evaluated_has_no_holdout(tmp_path):
    svc = service(tmp_path)
    out = svc.run(request(interval="1h"))
    with pytest.raises(ValueError):
        svc.evaluate_holdout(out.run_id)


def test_holdout_is_refused_if_the_strategy_source_has_changed(tmp_path, monkeypatch):
    svc, out = completed(tmp_path)
    monkeypatch.setattr("src.strategy_research.service.strategy_version", lambda cls: "0" * 64)
    with pytest.raises(ValueError, match="changed"):
        svc.evaluate_holdout(out.run_id)
    assert svc._store.get_holdout(out.run_id) is None                   # a refusal does not consume the one-time holdout


def test_selection_picks_by_validation_return_only_and_records_every_attempt(tmp_path):
    svc, out = completed(tmp_path, configs=({}, {"fast": 5, "slow": 13}, {"fast": 8, "slow": 21}))
    cards = [Scorecard.from_dict(a["scorecards"]["validation"]) for a in out.record["attempts"]]
    values = [c.net_return.value for c in cards]
    assert out.record["selection"]["selected_index"] == values.index(max(values))
    assert out.record["selection"]["configurations_attempted"] == 3 and "validation" in out.record["selection"]["rule"]


# ------------------------------------------------------------------------------- immutability
def test_research_records_are_immutable(tmp_path):
    svc, out = completed(tmp_path)
    svc.evaluate_holdout(out.run_id)
    with svc._store._connect() as con:
        for sql in ("UPDATE research_runs SET status='X'", "DELETE FROM research_runs",
                    "UPDATE research_holdouts SET payload='{}'", "DELETE FROM research_holdouts"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                con.execute(sql)


def test_changed_data_creates_a_new_run_and_leaves_the_old_one_untouched(tmp_path):
    store = ResearchStore(tmp_path / "r.db")
    snaps = SnapshotStore(tmp_path / "snap")
    first = ResearchService(SyntheticSource(synth(seed=3)), store, snaps).run(request())
    snapshot_before = store.get_run(first.run_id)
    second = ResearchService(SyntheticSource(synth(seed=4)), store, snaps).run(request())
    assert second.run_id != first.run_id
    assert store.get_run(first.run_id) == snapshot_before
    assert first.record["provenance"]["dataset_hash"] != second.record["provenance"]["dataset_hash"]
    assert len(snaps.list_ids()) == 2 and len(store.list_runs()) == 2


# --------------------------------------------------------------------------------- not evaluable
@pytest.mark.parametrize("kw,code", [
    ({"interval": "1h"}, "UNSUPPORTED_TIMEFRAME"),
    ({"strategy_name": "no_such_strategy"}, "UNKNOWN_STRATEGY"),
    ({"configs": ({"fast": 30, "slow": 10},)}, "INVALID_PARAMETERS"),
    ({"configs": ({"bogus_param": 1},)}, "INVALID_PARAMETERS"),
    ({"configs": tuple({"fast": 5 + i, "slow": 20} for i in range(6))}, "TOO_MANY_CONFIGS"),
    ({"configs": ()}, "TOO_MANY_CONFIGS"),
])
def test_requests_that_cannot_be_evaluated_say_why_and_are_recorded(tmp_path, kw, code):
    svc = service(tmp_path)
    kw = dict(kw)
    name = kw.pop("strategy_name", "ema_cross")
    out = svc.run(request(name, **kw))
    assert out.status == "NOT_EVALUABLE" and out.reason_code == code and out.reason
    stored = svc._store.get_run(out.run_id)
    assert stored["status"] == "NOT_EVALUABLE" and stored["payload"]["reason_code"] == code
    assert "attempts" not in stored["payload"]                            # an explanation, never a scorecard


def test_insufficient_history_is_not_a_no_signal_result(tmp_path):
    svc = service(tmp_path, SyntheticSource(synth(100)))
    out = svc.run(request())
    assert out.reason_code == "INSUFFICIENT_HISTORY" and "61-bar history requirement" in out.reason


def test_provider_failures_and_invalid_data_are_reported_not_hidden(tmp_path):
    out = service(tmp_path, SyntheticSource(synth(), error=NoDataError("SPY: nothing"))).run(request())
    assert out.reason_code == "PROVIDER_UNAVAILABLE"
    out = service(tmp_path, SyntheticSource(synth(), error=DataValidationError("bad high/low"))).run(request())
    assert out.reason_code == "DATA_INVALID" and "bad high/low" in out.reason


def test_material_data_gaps_make_the_run_not_evaluable_while_minor_ones_are_disclosed(tmp_path):
    big = service(tmp_path, SyntheticSource(synth(), gaps=50)).run(request())
    assert big.reason_code == "DATA_GAPS" and big.record.get("attempts") is None
    small = service(tmp_path, SyntheticSource(synth(), gaps=3)).run(request())
    assert small.status == "COMPLETED" and any("3 expected session" in x for x in small.record["provenance"]["limitations"])


def test_a_strategy_that_cannot_be_built_from_parameters_is_not_researchable(tmp_path):
    meta = StrategyMetadata(family=StrategyFamily.MODEL_DRIVEN, hypothesis="h", entry="e", exit="x",
                            directionality=Directionality.LONG_SHORT, supported_intervals=("1d",), regime_assumptions="r",
                            known_weaknesses=("w",), researchable=False, researchable_note="needs a model_id")

    @register_strategy("zz_model_probe")
    class Needs(EMACrossStrategy):
        METADATA = meta

    try:
        out = service(tmp_path).run(request("zz_model_probe"))
        assert out.reason_code == "NOT_RESEARCHABLE" and "model_id" in out.reason
    finally:
        _unregister_strategy("zz_model_probe")


def test_a_strategy_without_a_history_declaration_is_refused(tmp_path):
    meta = StrategyMetadata(family=StrategyFamily.MOMENTUM, hypothesis="h", entry="e", exit="x",
                            directionality=Directionality.LONG_FLAT, supported_intervals=("1d",), regime_assumptions="r",
                            known_weaknesses=("w",))

    @register_strategy("zz_undeclared_probe")
    class Undeclared(EMACrossStrategy):
        METADATA = meta
        history_requirement = None

    try:
        assert service(tmp_path).run(request("zz_undeclared_probe")).reason_code == "HISTORY_REQUIREMENT_UNKNOWN"
    finally:
        _unregister_strategy("zz_undeclared_probe")


# ---------------------------------------------------------------------- backtest correctness / lookahead
def test_fills_happen_at_the_next_bars_open_with_adverse_slippage(tmp_path):
    svc = service(tmp_path)
    candles = synth()
    a = ResearchAssumptions()
    rs = svc._backtests(EMACrossStrategy, "SPY", {}, candles, a, candles.index[60])
    assert rs.net.trades
    for t in rs.net.trades:
        pos = candles.index.get_loc(t.entry_time)
        assert t.entry_fill_price == pytest.approx(candles["open"].iloc[pos + 1] * (1 + a.slippage_bps / 10_000))
        assert t.entry_fill_price != pytest.approx(candles["close"].iloc[pos] * (1 + a.slippage_bps / 10_000))
        if t.exit_signal_id is not None:
            xpos = candles.index.get_loc(t.exit_time)
            assert t.exit_fill_price == pytest.approx(candles["open"].iloc[xpos + 1] * (1 - a.slippage_bps / 10_000))
        assert t.entry_fee > 0


def test_gross_run_has_no_costs_and_stressed_run_has_more_than_net(tmp_path):
    svc = service(tmp_path)
    candles = synth()
    rs = svc._backtests(EMACrossStrategy, "SPY", {}, candles, ResearchAssumptions(), candles.index[60])
    assert all(t.entry_fee == 0 and t.exit_fee == 0 and t.slippage_cost == 0 for t in rs.gross.trades)
    g, n, s = (r.equity_curve.iloc[-1] for r in (rs.gross, rs.net, rs.stress))
    assert g > n > s


def test_changing_bars_after_a_time_never_changes_anything_decided_at_or_before_it(tmp_path):
    svc = service(tmp_path)
    candles = synth()
    cut = 600
    altered = candles.copy()
    rng = np.random.default_rng(99)
    for col in ("open", "high", "low", "close"):
        altered.iloc[cut + 1:, altered.columns.get_loc(col)] *= rng.uniform(0.5, 1.5, len(altered) - cut - 1)
    a = svc._backtests(EMACrossStrategy, "SPY", {}, candles, ResearchAssumptions(), candles.index[60])
    b = svc._backtests(EMACrossStrategy, "SPY", {}, altered, ResearchAssumptions(), candles.index[60])
    t0 = candles.index[cut]
    same = [(t.entry_time, t.entry_fill_price, t.exit_time) for t in a.net.trades if t.exit_time <= t0 and t.exit_signal_id is not None]
    other = [(t.entry_time, t.entry_fill_price, t.exit_time) for t in b.net.trades if t.exit_time <= t0 and t.exit_signal_id is not None]
    assert same and same == other
    pd.testing.assert_series_equal(a.net.equity_curve.iloc[: cut - 1], b.net.equity_curve.iloc[: cut - 1])


def test_no_position_is_carried_in_from_before_the_history_requirement_is_met(tmp_path):
    svc = service(tmp_path)
    candles = synth()
    first_valid = candles.index[60]
    rs = svc._backtests(EMACrossStrategy, "SPY", {}, candles, ResearchAssumptions(), first_valid)
    assert all(t.entry_time >= first_valid for t in rs.net.trades)
    assert all(s.timestamp >= first_valid for s in rs.net.signals)


def test_every_strategy_receives_identical_assumptions(tmp_path):
    svc = service(tmp_path)
    recs = [svc.run(request(n)).record for n in ("ema_cross", "rsi_mean_reversion", "donchian_breakout", "ema_cross_vol_filter")]
    assert all(r["assumptions"] == recs[0]["assumptions"] and r["cost_note"] == recs[0]["cost_note"] for r in recs)
    assert all(r["boundaries"]["holdout"]["last_bar"] == recs[0]["boundaries"]["holdout"]["last_bar"] for r in recs[:1])


def test_cost_assumptions_are_stated_as_simulated_not_real_fees():
    text = ResearchAssumptions().describe_costs()
    assert "not broker fees" in text and "next bar's open" in text


# ------------------------------------------------------------------------------------------- walk-forward
def test_walk_forward_uses_unseen_test_windows_and_records_how_it_chose(tmp_path):
    svc = service(tmp_path)
    out = svc.walk_forward(request("donchian_breakout", configs=({}, {"entry_lookback": 40, "exit_lookback": 20})), 200, 100)
    assert out.status == "COMPLETED"
    rec = out.record
    assert rec["kind"] == "WALK_FORWARD" and rec["aggregate"]["folds"] == len(rec["folds"]) >= 3
    for a, b in zip(rec["folds"], rec["folds"][1:]):
        assert a["test_last_bar"] < b["test_first_bar"]
    for f in rec["folds"]:
        assert f["train_last_bar"] < f["test_first_bar"]
        assert f["selected_params"] in rec["request"]["configs"]
    chained = 1.0
    for f in rec["folds"]:
        chained *= 1 + f["test_scorecard"]["net_return"]["value"]
    assert rec["aggregate"]["out_of_sample_net_return_chained"] == pytest.approx(chained - 1)
    assert "nothing is optimised" in rec["method"] and rec["aggregate"]["configurations_attempted"] == 2


def test_walk_forward_too_short_is_not_evaluable(tmp_path):
    out = service(tmp_path).walk_forward(request(), 800, 100)
    assert out.status == "NOT_EVALUABLE" and out.reason_code == "INVALID_PROTOCOL"


# --------------------------------------------------------------------------------------------- replay
def test_reproduce_replays_each_layer_from_the_stored_snapshot(tmp_path):
    svc, out = completed(tmp_path)
    r = svc.reproduce(out.run_id)
    assert (r["data"], r["strategy"], r["backtest"]) == ("VERIFIED", "SAME_SOURCE", "REPRODUCED")


def test_reproduce_reports_a_changed_strategy_and_never_refetches(tmp_path, monkeypatch):
    source = SyntheticSource(synth())
    svc = service(tmp_path, source)
    out = svc.run(request())
    calls = source.calls
    monkeypatch.setattr("src.strategy_research.service.strategy_version", lambda cls: "1" * 64)
    r = svc.reproduce(out.run_id)
    assert r["strategy"] == "STRATEGY_CHANGED" and r["backtest"] == "NOT_REPLAYABLE"
    assert source.calls == calls                                        # replay never asks the provider for data


def test_reproduce_without_the_snapshot_is_not_replayable_rather_than_using_todays_data(tmp_path):
    source = SyntheticSource(synth())
    svc = service(tmp_path, source)
    out = svc.run(request())
    calls = source.calls
    SnapshotStore(tmp_path / "snap").path_for(out.record["provenance"]["snapshot_id"]).unlink()
    r = svc.reproduce(out.run_id)
    assert r["data"] == "NOT_REPLAYABLE" and r["backtest"] == "NOT_REPLAYABLE" and source.calls == calls


# ----------------------------------------------------------------------------------- research state
def card_with(tmp_path, **over):
    svc, out = completed(tmp_path)
    c = out.record["attempts"][0]["scorecards"]["validation"]
    for k, v in over.items():
        c[k] = v
    return svc, out, c


def ok(v):
    return {"value": v, "status": "ok", "reason": None}


def fake_run(card_validation, *, status="COMPLETED"):
    return {"kind": "SINGLE", "payload": {"status": status, "engine": {"version": 2}, "selection": {"selected_index": 0},
                                          "attempts": [{"scorecards": {"validation": card_validation}}]}}


def test_research_state_ladder(tmp_path):
    _, out, base = card_with(tmp_path)
    assert research_state(None, None)[0] == "EXPERIMENTAL"
    assert research_state(fake_run(base, status="NOT_EVALUABLE"), None)[0] == "EXPERIMENTAL"
    assert research_state(fake_run({**base, "net_return": ok(0.05), "closed_trades": 5, "open_trades_at_end": 0}), None)[0] == "RESEARCH_CANDIDATE"
    assert research_state(fake_run({**base, "net_return": ok(-0.01), "closed_trades": 5}), None)[0] == "REJECTED"
    assert research_state(fake_run({**base, "net_return": ok(0.0), "closed_trades": 0, "open_trades_at_end": 0}), None)[0] == "VALIDATION_PENDING"
    run = fake_run({**base, "net_return": ok(0.05), "closed_trades": 5})
    good = {**base, "closed_trades": 40, "open_trades_at_end": 0, "net_return": ok(0.08), "net_return_at_stress_costs": ok(0.03),
            "expectancy": ok(12.0)}
    assert research_state(run, {"payload": {"scorecard": good}})[0] == "ELIGIBLE_FOR_PAPER_EVALUATION"
    thin = {**good, "closed_trades": 6}
    assert research_state(run, {"payload": {"scorecard": thin}})[0] == "OUT_OF_SAMPLE_EVALUATED"
    fragile = {**good, "net_return_at_stress_costs": ok(-0.01)}
    assert research_state(run, {"payload": {"scorecard": fragile}})[0] == "OUT_OF_SAMPLE_EVALUATED"
    lost = {**good, "net_return": ok(-0.02)}
    assert research_state(run, {"payload": {"scorecard": lost}})[0] == "REJECTED"
    silent = {**good, "closed_trades": 0, "open_trades_at_end": 0}
    assert research_state(run, {"payload": {"scorecard": silent}})[0] == "OUT_OF_SAMPLE_EVALUATED"


def test_no_state_ever_claims_promotion(tmp_path):
    _, out, base = card_with(tmp_path)
    states = {research_state(fake_run({**base, "net_return": ok(v), "closed_trades": 5}), None)[1] for v in (0.1, -0.1)}
    assert not any("promot" in s.lower() and "not" not in s.lower() for s in states)
    assert "nothing is promoted" in research_state(fake_run({**base, "net_return": ok(0.05), "closed_trades": 5}), {"payload": {"scorecard": {
        **base, "closed_trades": 40, "open_trades_at_end": 0, "net_return": ok(0.08), "net_return_at_stress_costs": ok(0.03),
        "expectancy": ok(12.0)}}})[1]


def test_out_of_sample_degradation_is_reported_honestly(tmp_path):
    _, out, base = card_with(tmp_path)

    def sc(**kw):
        return Scorecard.from_dict({**base, **kw})

    dev = sc(net_return=ok(0.2), expectancy=ok(50.0))
    val = sc(net_return=ok(0.1), expectancy=ok(40.0), max_drawdown=ok(-0.05))
    bad = sc(net_return=ok(-0.05), expectancy=ok(-10.0), closed_trades=40, sample="ADEQUATE", max_drawdown=ok(-0.2))
    notes = assess_out_of_sample(dev, val, bad)
    assert any("did not hold up" in n for n in notes) and any("Expectancy turned" in n for n in notes)
    assert any("more than twice" in n for n in notes)
    good = sc(net_return=ok(0.1), expectancy=ok(40.0), closed_trades=40, sample="ADEQUATE", max_drawdown=ok(-0.05))
    assert assess_out_of_sample(dev, val, good) == ["No out-of-sample degradation flagged by these checks (this is not a statement of profitability)."]
    thin = sc(net_return=ok(0.1), closed_trades=3, sample="VERY_SMALL")
    assert any("insufficient for a reliable conclusion" in n for n in assess_out_of_sample(dev, val, thin))


def test_a_strategy_that_never_trades_is_pending_not_rejected(tmp_path):
    frame = declining()                                               # the long-only trend rule never enters
    svc = service(tmp_path, SyntheticSource(frame))
    out = svc.run(request())
    cards = out.record["attempts"][0]["scorecards"]
    assert cards["validation"]["closed_trades"] == 0 and cards["validation"]["sample"] == "NONE"
    assert research_state(svc._store.get_run(out.run_id), None)[0] == "VALIDATION_PENDING"


# ------------------------------------------------------------------------- app service / safety / universe
def test_research_creates_no_orders_intents_proposals_or_paper_records(tmp_path):
    paths = AppPaths.under(tmp_path)
    rs = StrategyResearchService(paths, market_data=SyntheticSource(synth()))
    out = rs.run(rs.default_request("ema_cross", "SPY", START, END))
    rs.evaluate_holdout(out.run_id)
    assert paths.research_db.exists()
    assert not paths.paper_db.exists() and not paths.scanner_db.exists() and not paths.monitoring_db.exists()
    assert not paths.registry.exists() and not paths.candidates_dir.exists()


def test_saved_watchlists_are_never_modified_by_research(tmp_path):
    from src.appsvc.scanner import ScannerService
    paths = AppPaths.under(tmp_path)
    sc = ScannerService(paths)
    made = sc.create_watchlist("mine", ["SPY", "QQQ", "AAPL"])
    before = [(w.watchlist_id, w.name, tuple(w.symbols)) for w in sc.watchlists()]
    rs = StrategyResearchService(paths, market_data=SyntheticSource(synth()))
    assert rs.watchlists() == {"mine": ("SPY", "QQQ", "AAPL")}
    rs.run_batch("ema_cross", list(rs.watchlists()["mine"]), START, END)
    assert [(w.watchlist_id, w.name, tuple(w.symbols)) for w in sc.watchlists()] == before and made.name == "mine"


def test_batch_is_bounded_independent_and_reports_coverage(tmp_path):
    paths = AppPaths.under(tmp_path)

    class Mixed(SyntheticSource):
        def get_dataset(self, symbol, **kw):
            if symbol == "BAD":
                raise NoDataError("no data for BAD")
            if symbol == "SHORT":
                return SyntheticSource(synth(100)).get_dataset(symbol, **kw)
            if symbol == "DOWN":
                return SyntheticSource(declining()).get_dataset(symbol, **kw)
            return super().get_dataset(symbol, **kw)

    rs = StrategyResearchService(paths, market_data=Mixed(synth()))
    outcomes, rows = rs.run_batch("ema_cross", ["SPY", "BAD", "SHORT", "DOWN", "spy"], START, END)
    assert list(outcomes) == ["SPY", "BAD", "SHORT", "DOWN"]                    # normalised and de-duplicated
    by = {r.symbol: r.status for r in rows}
    assert by["BAD"] == UNAVAILABLE and by["SHORT"] == "EXCLUDED_INSUFFICIENT_HISTORY" and by["DOWN"] == NO_ACTIVITY
    assert by["SPY"] == "EVALUATED_WITH_TRADES"
    assert rs.coverage_summary(rows)[NO_ACTIVITY] == 1
    with pytest.raises(ValueError):
        rs.run_batch("ema_cross", [f"S{i}" for i in range(MAX_BATCH_SYMBOLS + 1)], START, END)


def test_each_instrument_is_its_own_run_never_a_combined_portfolio(tmp_path):
    rs = StrategyResearchService(AppPaths.under(tmp_path), market_data=SyntheticSource(synth()))
    outcomes, _ = rs.run_batch("ema_cross", ["SPY", "QQQ"], START, END)
    assert len({o.run_id for o in outcomes.values()}) == 2
    assert {o.record["request"]["symbol"] for o in outcomes.values()} == {"SPY", "QQQ"}
    assert all(len(o.record["request"]["configs"]) == 1 for o in outcomes.values())


def test_comparison_has_no_composite_score_and_flags_different_instruments_and_periods(tmp_path):
    rs = StrategyResearchService(AppPaths.under(tmp_path), market_data=SyntheticSource(synth()))
    a = rs.run(rs.default_request("ema_cross", "SPY", START, END))
    b = rs.run(rs.default_request("donchian_breakout", "QQQ", START, END))
    rows, warnings = rs.compare([a.run_id, b.run_id])
    assert [r.run_id for r in rows] == [a.run_id, b.run_id]                      # order as given, never re-ranked
    assert any("different instruments" in w for w in warnings)
    fields = set(rows[0].__dataclass_fields__)
    assert not fields & {"score", "rank", "composite", "overall"}
    assert rows[0].family == "TREND_FOLLOWING" and rows[1].family == "BREAKOUT"
    assert rows[0].holdout_net_return is None and rows[0].state == "RESEARCH_CANDIDATE"


def test_curated_universe_is_separate_from_watchlists_and_states_its_bias():
    from src.strategy_research.universe import INITIAL_COMPARISON, RESEARCH_UNIVERSE, all_universe_symbols, describe_universe
    assert set(INITIAL_COMPARISON) <= set(all_universe_symbols())
    assert "SPY" in RESEARCH_UNIVERSE["Major index ETFs"] and "XLK" in RESEARCH_UNIVERSE["Sector ETFs"]
    d = describe_universe()
    assert "not a watchlist" in d["name"] and "not an unbiased" in d["note"]


def test_protocol_can_be_set_explicitly_and_is_persisted(tmp_path):
    protocol = ValidationProtocol(400, 200, 240)
    out = service(tmp_path).run(request(protocol=protocol))
    assert out.status == "COMPLETED" and out.record["request"]["protocol"] == protocol.to_dict()
    wrong = service(tmp_path).run(request(protocol=ValidationProtocol(100, 100, 100)))
    assert wrong.reason_code == "INVALID_PROTOCOL"


def test_warmup_guard_starts_the_strategy_flat_at_the_first_evaluable_bar():
    """A spurious early entry from immature indicators must not leave the strategy 'already long' (ADR-0061 correction)."""
    import numpy as np
    import pandas as pd

    from src.strategies.rsi_mean_reversion import RSIMeanReversionStrategy
    from src.strategy_research.service import _WarmupGuard

    n = 260
    idx = pd.bdate_range("2020-01-01", periods=n, tz="UTC")
    # Falls on the first bars (RSI reads 0 almost immediately), rises, then a genuine oversold dip after warm-up.
    close = np.r_[np.linspace(100, 96, 4), np.linspace(96, 140, 120), np.linspace(140, 100, 136)]
    frame = pd.DataFrame({"open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": 1e6}, index=idx)
    inner = RSIMeanReversionStrategy(symbol="X")
    prepared = inner.prepare(frame)
    unguarded = inner.generate_signals(prepared)
    first_valid = idx[80]
    guarded = _WarmupGuard(RSIMeanReversionStrategy(symbol="X"), first_valid)
    signals = guarded.generate_signals(guarded.prepare(frame))

    assert unguarded and unguarded[0].timestamp < first_valid          # the immature-indicator entry exists...
    assert all(s.timestamp >= first_valid for s in signals)            # ...and is never seen by the guarded run
    direct = RSIMeanReversionStrategy(symbol="X").generate_signals(prepared.loc[prepared.index >= first_valid])
    assert [(s.timestamp, s.direction) for s in signals] == [(s.timestamp, s.direction) for s in direct]
    assert signals and signals[0].direction.value == "LONG"            # the first signal is a genuine, post-warm-up entry


def test_a_run_from_an_earlier_engine_version_is_never_replayed_or_holdout_evaluated(tmp_path, monkeypatch):
    """Mixing methodologies inside one run would make its holdout result meaningless (ADR-0061 correction)."""
    monkeypatch.setattr("src.strategy_research.service.ENGINE_VERSION", 1)
    svc, out = completed(tmp_path)
    assert out.record["engine"] == {"version": 1}
    monkeypatch.setattr("src.strategy_research.service.ENGINE_VERSION", 2)

    replay = svc.reproduce(out.run_id)
    assert replay["data"] == "VERIFIED" and replay["backtest"] == "ENGINE_CHANGED"
    assert any("engine" in d for d in replay["detail"])
    with pytest.raises(ValueError, match="earlier research engine"):
        svc.evaluate_holdout(out.run_id)
    assert svc._store.get_holdout(out.run_id) is None                   # the refusal does not consume the holdout

    from src.strategy_research.service import research_state
    state, why = research_state(svc._store.get_run(out.run_id), None)
    assert state == "EXPERIMENTAL" and "earlier research engine" in why


def test_new_runs_record_the_current_engine_version(tmp_path):
    svc, out = completed(tmp_path)
    assert out.record["engine"] == {"version": 2}
    assert svc.reproduce(out.run_id)["backtest"] == "REPRODUCED"
