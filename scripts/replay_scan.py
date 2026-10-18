"""Offline replay of a stored scan (Sprint 22, ADR-0059). Read-only; contacts no provider, broker, SMTP or AI service.

For each symbol it separates three layers and says which one differs, if any:
  DATA      stored snapshot intact? same dataset and same complete bars as recorded?
  STRATEGY  the registered strategy, run on those bars, gives the recorded outcome/signal?
            (a changed strategy source is reported as such, never as the original decision)
  RISK      risk classification re-derived from the CAPTURED limits/stop/portfolio (not today's)?
A scan recorded before snapshots existed is reported NOT_REPLAYABLE (legacy); nothing is guessed.

It writes nothing: no scanner/paper/registry/promotion state, no intents, cycles, proposals or fills.

    python scripts/replay_scan.py --scan-id ID [--root DIR] [--json] [--symbol SYM]

Exit status: 0 every replayed symbol REPRODUCED or NOT_REPLAYABLE-by-design (legacy); 1 any DIFFERENT; 2 usage/lookup error.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.appsvc.paths import AppPaths  # noqa: E402
from src.appsvc.scanner import ScannerService  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scan-id", required=True)
    ap.add_argument("--root", help="data root containing scanner/scanner.db and snapshots/ (default: ./data)")
    ap.add_argument("--symbol", help="replay only this symbol")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    paths = AppPaths.under(a.root) if a.root else AppPaths()
    if not paths.scanner_db.is_file():
        print(f"no scanner database at {paths.scanner_db}", file=sys.stderr)
        return 2
    svc = ScannerService(paths)
    try:
        rep = svc.replay(a.scan_id)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rows = [r for r in rep.results if not a.symbol or r.symbol == a.symbol.upper()]
    if a.json:
        print(json.dumps({"scan_id": rep.scan_id, "legacy": rep.legacy, "notes": rep.notes, "counts": rep.counts,
                          "results": [asdict(r) for r in rows]}, indent=2, default=str))
    else:
        print(f"Replay of scan {rep.scan_id}  (offline; no provider contacted; nothing written)")
        for n in rep.notes:
            print(f"  note: {n}")
        for r in rows:
            print(f"\n{r.symbol:8s} {r.overall}  -- {r.reason}")
            for label, layer in (("data", r.data), ("strategy", r.strategy), ("risk", r.risk)):
                print(f"    {label:9s}{layer.status:18s}{layer.detail}")
            for n in r.notes:
                print(f"    note: {n}")
        print(f"\nSummary: {rep.counts}")
    return 1 if rep.counts["DIFFERENT"] else 0


if __name__ == "__main__":
    sys.exit(main())
