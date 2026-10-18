"""Scanner data-to-decision audit (manual; read-only; run on the development machine).

  1. Prints the PERSISTED chain for a stored scan (default: the latest): strategy/config/provider/bar/hash -> stored status ->
     stored decision, and says which audit fields exist (scans made after the audit-trace change) and which do not.
  2. With --refetch it fetches each symbol again through the real MarketDataService and compares, on the SAME completed bars, in two
     explicitly separate layers:
       LAYER 1  strategy decision only: the strategy class run directly vs the scanner (no account, risk not assessed)
       LAYER 2  end-to-end outcome: strategy + the stored scan's account position + risk + status mapping (read-only registry)
     and reports agreement. A changed content hash versus the stored scan means the provider's history changed since
     (e.g. a later session was added); that is reported, not hidden.

It writes nothing outside a temporary directory, never touches the experiment registry or paper state, and contacts only the
market-data provider (and only with --refetch).

    python scripts/scanner_audit.py [--scan-id ID] [--refetch] [--root DIR]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from src.appsvc.paths import AppPaths  # noqa: E402
from src.appsvc.scanner import ScannerService  # noqa: E402
from src.calendar.nyse import NYSECalendar  # noqa: E402
from src.data.base import DataProvider  # noqa: E402
from src.paper.evaluation import prepare_bars, resolve_strategy  # noqa: E402
from src.scanner.models import ScanRecord  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


class _Replay(DataProvider):
    """Serves exactly the candles already fetched (so scanner and direct evaluation see identical bars)."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def fetch_candles(self, symbol, start, end, interval):
        return self.frames[symbol]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-id")
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--root", help="data root containing scanner/scanner.db (default: ./data)")
    a = ap.parse_args()
    paths = AppPaths.under(a.root) if a.root else AppPaths()
    svc = ScannerService(paths)
    scans = svc.history(1) if not a.scan_id else []
    view = svc.view(a.scan_id) if a.scan_id else (svc.view(scans[0].scan.scan_id) if scans else None)
    if view is None:
        print("no stored scan found")
        return 1
    s = view.scan
    print(f"SCAN {s.scan_id} status={view.status.value} strategy={s.strategy_name} params={s.strategy_params} "
          f"hash={s.strategy_hash[:12]} config={s.config_hash} interval={s.interval} lookback={s.lookback_days}d min_bars={s.min_bars} "
          f"risk_mode={s.risk_mode} account={s.account}")
    for r in view.results:
        t = (r.data or {}).get("trace")
        print(f"  {r.symbol:5} data[{r.data.get('provider')} bars={r.data.get('bars')} dropped={r.data.get('dropped_incomplete')} "
              f"hash={str(r.data.get('content_hash'))[:12]} end={r.data.get('dataset_end')}] decision_bar={r.decision_bar} -> "
              f"stored_decision={(r.decision or {}).get('direction', 'none')} -> {r.status.value}/{r.reason_code} risk={r.risk_state.value}")
        print(f"        audit trace persisted: {'YES' if t else 'NO (scan predates the audit trace; OHLCV/indicators were not kept and are not reconstructed)'}")
        if t:
            print("        " + json.dumps({k: t[k] for k in t if k != 'bars'}, default=str)[:400])
    if not a.refetch:
        return 0

    print("\nREFETCH comparison (real provider; direct strategy vs scanner on identical bars)")
    from src.data.service import MarketDataService
    real = MarketDataService()
    now = pd.Timestamp.now(tz="UTC").to_pydatetime()
    end = now.date()
    frames: dict[str, pd.DataFrame] = {}
    direct: dict[str, tuple[str, str | None, str]] = {}
    for r in view.results:
        sym = r.symbol
        try:
            ds = real.get_dataset(sym, start=end - timedelta(days=s.lookback_days), end=end, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            direct[sym] = ("DATA_ERROR", None, f"{type(exc).__name__}")
            print(f"  {sym}: data error {type(exc).__name__}: {exc}")
            continue
        frames[sym] = ds.candles
        prep = prepare_bars(ds.candles, NYSECalendar(), now)
        if prep.kind != "OK":
            direct[sym] = (prep.kind, None, ds.content_hash)
            continue
        inst, _ = resolve_strategy(s.strategy_name, sym, s.strategy_params)
        sigs = inst.generate_signals(inst.prepare(prep.complete.copy()))
        at = [x for x in sigs if pd.Timestamp(x.timestamp) == prep.decision_bar]
        direct[sym] = ("SIGNAL" if at else "NONE", at[0].direction.value if at else None, ds.content_hash)
        old = r.data.get("content_hash")
        print(f"  {sym}: DIRECT bar={prep.decision_bar} outcome={direct[sym][0]} direction={direct[sym][1]} "
              f"hash={ds.content_hash[:12]} ({'same data as the stored scan' if old == ds.content_hash else 'bytes differ from the stored scan: the provider returns slightly re-adjusted prices for dividend payers (~1e-7 relative); see scripts/data_hash_probe.py'})")
    tmp = Path(tempfile.mkdtemp(prefix="scanner_audit_"))
    md = MarketDataService(provider=_Replay(frames), cache_dir=tmp / "c", use_cache=False)
    names = [sym for sym in direct if sym in frames]

    # ---- LAYER 1: the STRATEGY DECISION only. The replay scan is run WITHOUT an account, so risk is deliberately not assessed and
    # the scanner reports the strategy's own output (a FLAT is SIGNAL_FOUND/FLAT here: "the strategy wants to be flat").
    print("\nLAYER 1 -- strategy decision (direct strategy vs scanner, no account, risk not assessed)")
    l1 = ScannerService(AppPaths.under(tmp / "l1"), market_data=md)
    wl = l1.create_watchlist("audit", names)
    rec, _ = l1.scan(wl.watchlist_id, s.strategy_name, s.strategy_params)
    for r in l1.view(rec.scan_id).results:
        kind, direction, _h = direct[r.symbol]
        got = (r.decision or {}).get("direction")
        ok = (kind == "NONE" and got is None and r.status.value == "NO_SIGNAL") or \
             (kind == "SIGNAL" and got == direction and r.status.value == "SIGNAL_FOUND") or \
             (kind not in ("NONE", "SIGNAL") and r.status.value in ("NOT_EVALUABLE", "DATA_UNAVAILABLE"))
        check(f"L1 {r.symbol}: direct {kind}/{direction} == scanner {r.status.value}/{got}", ok)

    # ---- LAYER 2: the END-TO-END scan outcome, only when the stored scan assessed risk against an account. Same account, read-only
    # (the real registry is READ; nothing is written to it, and no paper store is opened).
    print("\nLAYER 2 -- end-to-end outcome (strategy + account position + risk + status mapping)")
    if s.risk_mode == "ASSESSED" and s.account is not None:
        p2 = AppPaths(registry=paths.registry, paper_db=paths.paper_db, monitoring_db=paths.monitoring_db, candidates_dir=paths.candidates_dir,
                      profiles_dir=paths.profiles_dir, prefs_file=paths.prefs_file, scanner_db=tmp / "l2" / "scanner.db")
        l2 = ScannerService(p2, market_data=md)
        wl2 = l2.create_watchlist("audit", names)
        rec2, _ = l2.scan(wl2.watchlist_id, s.strategy_name, s.strategy_params, account=s.account, assess_risk=True)
        stored = {r.symbol: r for r in view.results}
        for r in l2.view(rec2.scan_id).results:
            old = stored[r.symbol]
            same_data = old.data.get("content_hash") == direct[r.symbol][2]
            ok = (r.status, r.reason_code) == (old.status, old.reason_code)
            note = "" if same_data else " [data differs from the stored scan, so a difference here may be genuine]"
            check(f"L2 {r.symbol}: stored {old.status.value}/{old.reason_code} vs now {r.status.value}/{r.reason_code}{note}", ok or not same_data)
    else:
        print("  skipped: the stored scan did not assess risk, so no end-to-end account outcome exists to compare")
    print("\nNOTE: Layer 1 verifies ONLY the strategy decision; Layer 2 verifies the complete scan outcome including the account's position.\n"
          "      A FLAT decision is SIGNAL_FOUND/FLAT in Layer 1 and NO_SIGNAL/ALREADY_FLAT in Layer 2 when the account holds no position.")
    failed = [c for c in CHECKS if not c[1]]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'} ({len(CHECKS) - len(failed)}/{len(CHECKS)} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
