"""Sprint 21 real-machine validation of the Opportunity Scanner (manual; run on the development machine).

  PART A  REAL PROVIDER   a scan of QQQ and SPY with the registered `ema_cross` strategy through the real MarketDataService
                          (network required). A signal is NEVER forced: NO_SIGNAL is a valid, honest outcome, as are
                          DATA_UNAVAILABLE / NOT_EVALUABLE if the provider or calendar says so. Reports each symbol's status,
                          decision bar, data provider and hash, then re-reads the scan from a fresh service (persistence).
  PART B  SAFETY          after the scan: no paper database, cycle, intent, proposal or fill exists; the experiment registry is
                          byte-identical; the scanner database holds only scanner tables.

Everything lives in a fresh temporary directory (your normal data/ is untouched). No broker is reachable.

    python scripts/scanner_validation.py [--keep] [--symbols QQQ SPY] [--strategy ema_cross]
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.paper_runtime_validation import seed  # noqa: E402
from src.appsvc.paths import AppPaths  # noqa: E402
from src.appsvc.scanner import ScannerService, explain_result  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    return bool(ok)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--symbols", nargs="+", default=["QQQ", "SPY"])
    ap.add_argument("--strategy", default="ema_cross")
    args = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="scanner_validation_"))
    paths = AppPaths.under(root)
    exp = seed(ExperimentRegistry(paths.registry))
    reg_before = sha(paths.registry)
    print(f"disposable root: {root}\n\nPART A  real provider scan ({args.strategy} over {' '.join(args.symbols)})")

    svc = ScannerService(paths)
    wl = svc.create_watchlist("validation", args.symbols)
    check("watchlist stored and normalised", wl.symbols == tuple(s.upper() for s in args.symbols))
    rec, created = svc.scan(wl.watchlist_id, args.strategy, {}, account=exp, assess_risk=True, request_token="validation")
    check("scan created and finished", created and rec.status.value in {"COMPLETED", "PARTIAL", "FAILED"}, rec.status.value)
    view = ScannerService(paths).view(rec.scan_id)           # fresh service = persistence
    check("scan re-read from a fresh service", view is not None and view.scan.scan_id == rec.scan_id and len(view.results) == len(args.symbols))
    for r in view.results:
        print(f"    {r.symbol}: {r.status.value} ({r.reason_code}) bar={r.decision_bar} provider={r.data.get('provider')} "
              f"bars={r.data.get('bars')} hash={str(r.data.get('content_hash'))[:12]} risk={r.risk_state.value}")
        for line in explain_result(r):
            print(f"        {line}")
        if r.error:
            print(f"        technical: {r.error}")
    check("every result has a typed status and a reason", all(r.status and r.reason for r in view.results))
    check("provenance recorded (strategy hash, config hash, snapshot)", bool(view.scan.strategy_hash and view.scan.config_hash and view.scan.symbols))
    again, created2 = svc.scan(wl.watchlist_id, args.strategy, {}, account=exp, assess_risk=True, request_token="validation")
    check("repeated submission with the same token does not rescan", not created2 and again.scan_id == rec.scan_id)

    print("\nPART B  safety")
    check("no paper database exists", not paths.paper_db.exists())
    check("experiment registry byte-identical", sha(paths.registry) == reg_before)
    conn = sqlite3.connect(paths.scanner_db)
    tables = {t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    check("scanner database holds only scanner tables", tables == {"scanner_meta", "watchlists", "scans", "scan_results"}, str(sorted(tables)))

    failed = [c for c in CHECKS if not c[1]]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'} ({len(CHECKS) - len(failed)}/{len(CHECKS)} checks)")
    if args.keep or failed:
        print(f"validation data kept at {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
