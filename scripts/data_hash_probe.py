"""Data-hash reproducibility probe (manual; read-only; needs the real provider).

Fetches the SAME symbol and period N times through the real MarketDataService (use_cache=False, exactly what the scanner does)
and answers, with measurements rather than assumptions: did the DATA change between fetches, or only the hash?

For each pair of fetches it prints: row-count and timestamp differences, every OHLCV cell that differs (exact and relative),
the content hash of everything returned vs the hash of only the COMPLETE bars, and which bars were still forming.
Identical normalised data always gives an identical hash (see tests/test_data_hash_reproducibility.py); a different hash means
different bytes, and this probe shows which.

    python scripts/data_hash_probe.py [SYMBOL ...] [--repeats 2] [--sleep 5] [--days 400]

Fetches seconds apart usually look identical even when the provider's data drifts over minutes. To catch that, save a snapshot and
compare a LATER run against it (cell by cell, full precision):

    python scripts/data_hash_probe.py QQQ AMD --repeats 1 --snapshot /tmp/hashprobe
    (wait a few minutes)
    python scripts/data_hash_probe.py QQQ AMD --repeats 1 --against /tmp/hashprobe
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from src.calendar.nyse import NYSECalendar  # noqa: E402
from src.data.canonical import canonicalize_candles  # noqa: E402
from src.data.service import MarketDataService  # noqa: E402
from src.paper.evaluation import prepare_bars  # noqa: E402
from src.utils.hashing import dataframe_fingerprint  # noqa: E402


def _report(label: str, d1: pd.DataFrame, d2: pd.DataFrame) -> int:
    only1, only2 = d1.index.difference(d2.index), d2.index.difference(d1.index)
    print(f"  timestamps only in {label[0]}: {len(only1)} {list(only1[:3])}; only in {label[1]}: {len(only2)} {list(only2[:3])}")
    common = d1.index.intersection(d2.index)
    diffs = []
    for col in d1.columns:
        a1, a2 = d1.loc[common, col], d2.loc[common, col]
        for ts in a1[a1 != a2].index:
            diffs.append((ts, col, float(a1[ts]), float(a2[ts])))
    print(f"  differing cells on common timestamps: {len(diffs)}")
    for ts, col, v1, v2 in diffs[:15]:
        rel = abs(v1 - v2) / abs(v1) if v1 else float("inf")
        print(f"    {ts} {col}: {v1!r} -> {v2!r}  (abs {abs(v1 - v2):.3g}, rel {rel:.3g})")
    if diffs:
        spread = sorted({d[0] for d in diffs})
        rels = [abs(d[2] - d[3]) / abs(d[2]) for d in diffs if d[2]]
        print(f"  affected bars: {len(spread)} from {spread[0]} to {spread[-1]}; largest relative difference {max(rels):.3g}; "
              f"by column: { {c: sum(1 for d in diffs if d[1] == c) for c in d1.columns} }")
    return len(diffs) + len(only1) + len(only2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=["QQQ", "AMD"])
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--sleep", type=float, default=5.0)
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--snapshot", help="directory to save the first fetch of each symbol to (full-precision CSV)")
    ap.add_argument("--against", help="directory of a previous --snapshot; the first fetch is compared to it cell by cell")
    a = ap.parse_args()
    md, cal = MarketDataService(), NYSECalendar()
    exit_code = 0
    for sym in a.symbols:
        print(f"\n=== {sym} ===")
        runs = []
        for k in range(a.repeats):
            now = pd.Timestamp.now(tz="UTC").to_pydatetime()
            end = now.date()
            ds = md.get_dataset(sym, start=end - timedelta(days=a.days), end=end, use_cache=False)
            prep = prepare_bars(ds.candles, cal, now)
            complete = prep.complete if prep.complete is not None else ds.candles
            runs.append((now, ds.candles, ds.content_hash, dataframe_fingerprint(complete), prep))
            print(f"fetch {k + 1} at {now:%H:%M:%S}Z: rows={len(ds.candles)} complete={len(complete)} dropped_forming={len(prep.dropped or [])} "
                  f"last={ds.candles.index[-1]} content_hash={ds.content_hash[:12]} complete_hash={runs[-1][3][:12]}")
            if k + 1 < a.repeats:
                time.sleep(a.sleep)
        if a.snapshot:
            out = Path(a.snapshot); out.mkdir(parents=True, exist_ok=True)
            runs[0][1].to_csv(out / f"{sym}.csv", float_format="%.17g")
            print(f"snapshot saved: {out / (sym + '.csv')} (content_hash {runs[0][2][:12]})")
        if a.against:
            f = Path(a.against) / f"{sym}.csv"
            if not f.exists():
                print(f"no snapshot for {sym} in {a.against}")
            else:
                old = pd.read_csv(f, index_col=0, parse_dates=True, float_precision="round_trip")   # default parser can be 1 ulp off
                old.index = pd.DatetimeIndex(old.index).tz_convert("UTC") if old.index.tz is not None else pd.DatetimeIndex(old.index).tz_localize("UTC")
                old.index.name = "timestamp"
                print(f"\ncompare SNAPSHOT vs now: snapshot rows={len(old)} now rows={len(runs[0][1])}")
                snap_hash = dataframe_fingerprint(canonicalize_candles(old.astype("float64")))
                print(f"  snapshot hash {snap_hash[:12]} vs now {runs[0][2][:12]}: {'SAME (bit-identical data)' if snap_hash == runs[0][2] else 'DIFFERENT'}")
                n = 0 if snap_hash == runs[0][2] else _report(("snapshot", "now"), old.astype("float64"), runs[0][1])
                print("  => data IDENTICAL to the snapshot" if n == 0 else "  => the provider's data CHANGED since the snapshot (the hash is faithfully reporting it)")
        for k in range(1, len(runs)):
            _, d1, h1, c1, _p = runs[0]
            _, d2, h2, c2, _q = runs[k]
            print(f"\ncompare fetch 1 vs fetch {k + 1}: content_hash {'SAME' if h1 == h2 else 'DIFFERENT'}; complete-bars hash {'SAME' if c1 == c2 else 'DIFFERENT'}")
            only1, only2 = d1.index.difference(d2.index), d2.index.difference(d1.index)
            print(f"  timestamps only in fetch 1: {len(only1)} {list(only1[:3])}; only in fetch {k + 1}: {len(only2)} {list(only2[:3])}")
            common = d1.index.intersection(d2.index)
            diffs = []
            for col in d1.columns:
                a1, a2 = d1.loc[common, col], d2.loc[common, col]
                bad = a1[a1 != a2].index
                for ts in bad:
                    diffs.append((ts, col, float(a1[ts]), float(a2[ts])))
            print(f"  differing cells on common timestamps: {len(diffs)}")
            for ts, col, v1, v2 in diffs[:15]:
                rel = abs(v1 - v2) / abs(v1) if v1 else float("inf")
                print(f"    {ts} {col}: {v1!r} -> {v2!r}  (abs {abs(v1 - v2):.3g}, rel {rel:.3g})")
            if diffs:
                spread = sorted({d[0] for d in diffs})
                print(f"  affected bars: {len(spread)} from {spread[0]} to {spread[-1]}")
            if h1 != h2 and not (only1.size or only2.size or diffs):
                print("  !! hashes differ but timestamps and every OHLCV cell are identical -> hashing is NOT purely content-based (investigate)")
                exit_code = 1
            elif h1 != h2:
                print("  => the DATA changed between fetches (the provider returned different bytes); the hash is reporting that faithfully.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
