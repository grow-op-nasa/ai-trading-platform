"""Content-hash reproducibility and scan-layer parity (Sprint 21 verification follow-up). Network-free, deterministic.

Q1  identical NORMALISED data must always give the same content hash, regardless of fetch time, object identity, dtype,
    column order, row order, timezone representation, index name/freq or provider metadata; any real value change must change it.
Q2  the audit's two layers differ ON PURPOSE: the strategy decision (FLAT) is SIGNAL_FOUND without an account and
    NO_SIGNAL/ALREADY_FLAT when the account holds no position; with a position the same FLAT is a reviewable close.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import numpy as np
import pandas as pd

from src.appsvc.paths import AppPaths
from src.calendar.nyse import NYSECalendar
from src.data.base import DataProvider
from src.data.canonical import canonicalize_candles
from src.data.fault_injection import SyntheticCandleProvider
from src.data.service import MarketDataService
from src.paper.evaluation import prepare_bars
from src.scanner.models import ResultStatus, RiskState
from src.utils.hashing import dataframe_fingerprint
from tests.test_scanner_audit import _first_day_with, _service, scan_of
from tests.test_scanner import _risk_world


def _frame(n=30, seed=1):
    idx = pd.date_range("2026-08-03", periods=n, freq="B", tz="UTC", name="timestamp")
    rng = np.random.default_rng(seed)
    close = 100 + rng.normal(0, 1, n).cumsum()
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": rng.integers(1_000, 9_000, n)}, index=idx)


# =============================================================== Q1
def test_the_same_data_built_in_different_ways_has_one_canonical_hash():
    base = _frame()
    want = dataframe_fingerprint(canonicalize_candles(base))
    variants = {
        "copy": base.copy(),
        "int volume vs float volume": base.astype({"volume": "float64"}),
        "shuffled rows": base.sample(frac=1.0, random_state=3),
        "reordered columns": base[["volume", "close", "low", "high", "open"]],
        "naive UTC index": base.tz_convert("UTC").tz_localize(None),
        "New York tz index": base.tz_convert("America/New_York"),
        "renamed index": base.rename_axis("ts"),
        "index with a freq": base.asfreq("B"),
        "extra non-OHLCV column": base.assign(adj_close=base["close"]),
    }
    for name, df in variants.items():
        assert dataframe_fingerprint(canonicalize_candles(df)) == want, name


def test_the_hash_depends_only_on_values_not_on_when_or_how_often_the_data_was_produced():
    class P(DataProvider):
        def __init__(self): self.calls = 0
        def fetch_candles(self, symbol, start, end, interval):
            self.calls += 1
            return SyntheticCandleProvider(date(2026, 10, 7)).fetch_candles(symbol, start, end, interval)

    p = P()
    md = MarketDataService(provider=p, use_cache=False)
    hashes = {md.get_dataset("QQQ", start=date(2026, 1, 1), end=date(2026, 10, 7), use_cache=False).content_hash for _ in range(4)}
    assert p.calls == 4 and len(hashes) == 1                                  # four real "fetches", one identity


def test_a_real_value_change_changes_the_hash_even_by_one_float_ulp_documented_sensitivity():
    base = canonicalize_candles(_frame())
    h = dataframe_fingerprint(base)
    bumped = base.copy()
    bumped.iloc[-1, bumped.columns.get_loc("close")] = np.nextafter(bumped.iloc[-1]["close"], np.inf)
    assert dataframe_fingerprint(bumped) != h                                  # bit-exact by design: a vendor revision is detectable
    assert dataframe_fingerprint(base.iloc[:-1]) != h                          # an extra/forming bar changes it
    moved = base.copy(); moved.index = moved.index + pd.Timedelta(hours=1)
    assert dataframe_fingerprint(moved) != h


def test_content_hash_covers_a_still_forming_bar_but_evaluated_hash_covers_only_complete_bars(tmp_path):
    """Why a re-fetch hash can differ while the strategy's input did not: a forming bar is in `content_hash`, not `evaluated_hash`."""
    cal = NYSECalendar()
    session = date(2026, 10, 7)
    during = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)                 # market still open that Wednesday
    base = SyntheticCandleProvider(session).fetch_candles("QQQ", session - timedelta(days=400), session, __import__("src.data.base", fromlist=["x"]).Interval.DAY_1)

    def with_forming(last_close):
        df = base.copy()
        df.iloc[-1, df.columns.get_loc("close")] = last_close
        return canonicalize_candles(df)

    a, b = with_forming(150.0), with_forming(151.25)                           # the forming bar ticked between two fetches
    assert dataframe_fingerprint(a) != dataframe_fingerprint(b)                # content hash moves ...
    pa, pb = prepare_bars(a, cal, during), prepare_bars(b, cal, during)
    assert pa.kind == pb.kind and len(pa.dropped or []) == 1 and len(pb.dropped or []) == 1
    assert dataframe_fingerprint(pa.complete) == dataframe_fingerprint(pb.complete)   # ... the evaluated input does not


def test_the_scan_records_both_the_fetched_hash_and_the_evaluated_bars_hash(tmp_path):
    day = _first_day_with("LONG", tmp_path)
    svc, md, clock = _service(tmp_path / "h", day)
    r = scan_of(svc, "QQQ")[0].results[0]
    assert len(r.data["content_hash"]) == 64 and len(r.data["evaluated_hash"]) == 64
    ds = md.get_dataset("QQQ", start=day - timedelta(days=400), end=day, use_cache=False)
    assert r.data["content_hash"] == ds.content_hash                           # nothing was dropped on a closed session ...
    assert r.data["evaluated_hash"] == dataframe_fingerprint(prepare_bars(ds.candles, NYSECalendar(), clock()).complete)


# =============================================================== Q2
def _position_world(tmp_path):
    """An account that HOLDS a QQQ position, to show the same FLAT becomes a reviewable close."""
    from src.experiments.registry import ExperimentRegistry
    from src.portfolio.models import Portfolio
    from src.portfolio.position import Position, PositionSide
    exp = _risk_world(tmp_path)
    pos = Position(symbol="QQQ", side=PositionSide.LONG, quantity=10, entry_price=100.0,
                   entry_timestamp=pd.Timestamp("2026-06-01", tz="UTC"), entry_signal_id=uuid4())
    ExperimentRegistry(AppPaths.under(tmp_path).registry).save_portfolio(exp, Portfolio.reconstruct(cash=9_000.0, positions=[pos]))
    return exp


def test_one_flat_decision_maps_to_three_different_scan_outcomes_depending_on_the_account(tmp_path, monkeypatch):
    day = _first_day_with("FLAT", tmp_path)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: ["ema_cross"])

    # layer 1 -- strategy decision only (no account): the strategy's FLAT is reported as a signal, risk not assessed
    svc, *_ = _service(tmp_path / "l1", day)
    r1 = scan_of(svc, "QQQ")[0].results[0]
    assert (r1.status, r1.reason_code, r1.decision["direction"], r1.risk_state) == (ResultStatus.SIGNAL_FOUND, "SIGNAL_FOUND", "FLAT", RiskState.NOT_ASSESSED)

    # layer 2a -- end to end with an account that holds NO position: nothing to close
    exp = _risk_world(tmp_path / "flat")
    svc, *_ = _service(tmp_path / "flat", day)
    r2 = scan_of(svc, "QQQ", account=exp, assess_risk=True)[0].results[0]
    assert (r2.status, r2.reason_code, r2.decision["direction"], r2.risk_state) == (ResultStatus.NO_SIGNAL, "ALREADY_FLAT", "FLAT", RiskState.NOT_APPLICABLE)

    # layer 2b -- end to end with an account that HOLDS the symbol: the very same FLAT is a risk-checked, reviewable close
    exp = _position_world(tmp_path / "held")
    svc, *_ = _service(tmp_path / "held", day)
    v3 = scan_of(svc, "QQQ", account=exp, assess_risk=True)[0]
    r3 = v3.results[0]
    assert r3.decision["direction"] == "FLAT" and r3.status in (ResultStatus.SIGNAL_FOUND, ResultStatus.RISK_BLOCKED)
    assert r3.reason_code != "ALREADY_FLAT" and r3.risk_state in (RiskState.APPROVED, RiskState.REJECTED)


def test_none_of_these_diagnostic_scans_touch_paper_state(tmp_path, monkeypatch):
    day = _first_day_with("FLAT", tmp_path)
    monkeypatch.setattr("src.appsvc.paper.eligible_names", lambda paths=None: ["ema_cross"])
    exp = _position_world(tmp_path / "held")
    paths = AppPaths.under(tmp_path / "held")
    before = paths.registry.read_bytes()
    svc, *_ = _service(tmp_path / "held", day)
    scan_of(svc, "QQQ", account=exp, assess_risk=True)
    assert paths.registry.read_bytes() == before and not paths.paper_db.exists() and not paths.monitoring_db.exists()


def test_the_audit_script_labels_the_layer_each_comparison_verifies():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "scripts" / "scanner_audit.py").read_text()
    for needle in ("LAYER 1", "LAYER 2", "strategy decision", "end-to-end", "ALREADY_FLAT"):
        assert needle in text, needle


def test_a_full_precision_csv_snapshot_round_trips_to_the_identical_hash_only_with_exact_float_parsing(tmp_path):
    """Regression for the probe's snapshot mode: pandas' default CSV float parser can be one ulp off, which changes the hash."""
    df = canonicalize_candles(_frame(275, seed=7).astype(float) + 214.85000610351562)
    f = tmp_path / "snap.csv"
    df.to_csv(f, float_format="%.17g")

    def load(**kw):
        old = pd.read_csv(f, index_col=0, parse_dates=True, **kw)
        old.index = pd.DatetimeIndex(old.index).tz_convert("UTC") if old.index.tz is not None else pd.DatetimeIndex(old.index).tz_localize("UTC")
        old.index.name = "timestamp"
        return canonicalize_candles(old.astype("float64"))

    assert dataframe_fingerprint(load(float_precision="round_trip")) == dataframe_fingerprint(df)


def test_the_probe_reads_snapshots_with_exact_float_parsing():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "scripts" / "data_hash_probe.py").read_text()
    assert 'float_precision="round_trip"' in text
