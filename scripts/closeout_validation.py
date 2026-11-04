"""Sprint 23 closeout: expanded REAL-DATA research validation (8 instruments x 3 strategies = 24 combinations).

    python scripts/closeout_validation.py [--start 2015-01-01] [--end YYYY-MM-DD] [--data-dir /tmp/atp-sprint23-research]
                                          [--train-bars 504] [--test-bars 126]

Runs on YOUR machine because it calls the real market-data provider. Everything is written to an isolated directory
(default /tmp/atp-sprint23-research): its own research database and snapshot store (the provider's price cache, which
holds only downloaded prices, is the platform's shared one). Your own research history, scanner records and paper-trading state are never touched.

For every combination it records: a data audit of the stored snapshot, the development and validation scorecards,
a walk-forward evaluation (fixed, un-tuned train/test sizes), a replay (reproduction) check, and a classification.

THE LOCKED HOLDOUT IS NEVER EVALUATED, and this script has no option that could do so. Default parameters only; nothing
is tuned, selected or re-run to improve a result. Synthetic data is never substituted: if the real provider cannot supply
a combination it is reported as such.

Outputs: <data-dir>/closeout_report.json and <data-dir>/closeout_report.md.
Exit status: 0 the harness completed (individual combinations may be non-completed, with reasons); 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from src.appsvc.paths import AppPaths  # noqa: E402
from src.appsvc.strategy_research import StrategyResearchService  # noqa: E402
from src.snapshots import SnapshotStore  # noqa: E402
from src.data.validation import _PRICE_TOLERANCE as TOL  # noqa: E402
from src.strategy_research.service import ENGINE_VERSION  # noqa: E402

SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD"]
STRATEGIES = ["ema_cross", "rsi_mean_reversion", "donchian_breakout"]
DEFAULT_DIR = "/tmp/atp-sprint23-research"
SYNTHETIC_PROVIDERS = {"synthetic", "fixture", "fake", "test"}

CLASSIFICATION = {
    "DATA_INVALID": "DATA_QUALITY_FAILURE", "DATA_GAPS": "DATA_QUALITY_FAILURE", "SNAPSHOT_FAILED": "DATA_QUALITY_FAILURE",
    "PROVIDER_UNAVAILABLE": "PROVIDER_AVAILABILITY", "DATA_UNAVAILABLE": "PROVIDER_AVAILABILITY",
    "INSUFFICIENT_HISTORY": "INSUFFICIENT_HISTORY",
    "UNKNOWN_STRATEGY": "NOT_SUPPORTED", "NOT_RESEARCHABLE": "NOT_SUPPORTED", "UNSUPPORTED_TIMEFRAME": "NOT_SUPPORTED",
    "INVALID_PARAMETERS": "STRATEGY_OR_ENGINE_DEFECT", "HISTORY_REQUIREMENT_UNKNOWN": "STRATEGY_OR_ENGINE_DEFECT",
    "INVALID_PROTOCOL": "INSUFFICIENT_HISTORY", "TOO_MANY_CONFIGS": "STRATEGY_OR_ENGINE_DEFECT",
}


def metric(m: dict | None) -> float | None:
    if m and str(m.get("status")).lower() == "ok" and m.get("value") is not None:
        return float(m["value"])
    return None


def fmt(m: dict | None, pct: bool = False) -> str:
    if not m:
        return "n/a"
    v = metric(m)
    if v is None:
        return f"undefined ({m.get('reason') or m.get('status')})"
    return f"{v:.2%}" if pct else f"{v:.3f}"


def audit_snapshot(snapshots: SnapshotStore, snapshot_id: str | None, prov: dict, start: date, end: date) -> dict:
    """Structural checks on the exact bars the research used (loaded back from the content-addressed snapshot)."""
    out: dict = {"snapshot_id": snapshot_id, "provider": prov.get("provider"), "requested": [str(start), str(end)],
                 "dataset_hash": prov.get("dataset_hash"), "checks": {}, "ok": False}
    if not snapshot_id:
        out["error"] = "no snapshot stored"
        return out
    try:
        df = snapshots.load(snapshot_id).candles
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        out["error"] = f"snapshot unreadable: {type(exc).__name__}: {exc}"
        return out
    idx = df.index
    o, h, low, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    checks = {
        "bars": int(len(df)),
        "first_bar": idx[0].isoformat(), "last_bar": idx[-1].isoformat(),
        "strictly_increasing": bool(idx.is_monotonic_increasing and idx.is_unique),
        "duplicate_timestamps": int(idx.duplicated().sum()),
        "nan_cells": int(df[["open", "high", "low", "close", "volume"]].isna().sum().sum()),
        "non_finite_cells": int((~np.isfinite(df[["open", "high", "low", "close", "volume"]].to_numpy(float))).sum()),
        "non_positive_prices": int(((o <= 0) | (h <= 0) | (low <= 0) | (c <= 0)).sum()),
        "negative_volume": int((df["volume"].to_numpy(float) < 0).sum()),
        # Same relative tolerance as the platform's own validation (adjusted-price float noise ~1e-13). Bars that are
        # inconsistent only within that noise are counted separately, visibly, and do not fail the audit.
        "high_below_open_close_or_low": int(((h < np.maximum(o, c) * (1 - TOL)) | (h < low * (1 - TOL))).sum()),
        "low_above_open_close_or_high": int(((low > np.minimum(o, c) * (1 + TOL)) | (low > h * (1 + TOL))).sum()),
        "float_noise_bars_within_tolerance": int((((h < np.maximum(o, c)) | (h < low) | (low > np.minimum(o, c)) | (low > h))).sum()),
        "dropped_incomplete_bars": prov.get("dropped_incomplete", []),
        "latest_completed_bar": prov.get("last_bar"),
        "returned_range": [prov.get("first_bar"), prov.get("last_bar")],
        "gap_sessions": prov.get("gap_sessions", prov.get("gaps")),
    }
    out["checks"] = checks
    bad = [k for k in ("duplicate_timestamps", "nan_cells", "non_finite_cells", "non_positive_prices", "negative_volume",
                       "high_below_open_close_or_low", "low_above_open_close_or_high") if checks[k]]
    if not checks["strictly_increasing"]:
        bad.append("strictly_increasing")
    out["failed_checks"] = bad
    out["ok"] = not bad
    return out


def classify(status: str, code: str | None, audit: dict, replay: dict | None, provider: str | None) -> str:
    if status != "COMPLETED":
        return CLASSIFICATION.get(code or "", "STRATEGY_OR_ENGINE_DEFECT")
    if not audit.get("ok"):
        return "DATA_QUALITY_FAILURE"
    if replay and replay.get("backtest") not in ("REPRODUCED",):
        return "STRATEGY_OR_ENGINE_DEFECT"
    return "COMPLETED"


def window_row(sc: dict) -> dict:
    keys = ("closed_trades", "sample", "gross_return", "net_return", "net_return_at_stress_costs", "annualised_volatility",
            "sharpe", "sortino", "max_drawdown", "exposure_time", "capital_utilisation_mean", "average_holding_days",
            "benchmark_return", "benchmark_sharpe", "benchmark_max_drawdown", "exposure_matched_return",
            "excess_return", "excess_vs_exposure_matched")
    return {k: sc[k] for k in keys if k in sc} | {"first_bar": sc["first_bar"], "last_bar": sc["last_bar"], "bars": sc["bars"],
                                                  "notes": sc.get("notes", [])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=None, help="default: yesterday")
    ap.add_argument("--data-dir", default=DEFAULT_DIR)
    ap.add_argument("--train-bars", type=int, default=504)
    ap.add_argument("--test-bars", type=int, default=126)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    if not args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    except ValueError as exc:
        print(f"usage error: {exc}")
        return 2
    root = Path(args.data_dir)
    if root.resolve() == (REPO / "data").resolve():
        print("usage error: refusing to write into the project's own data directory; use an isolated directory")
        return 2
    root.mkdir(parents=True, exist_ok=True)
    paths = AppPaths.under(root)
    svc = StrategyResearchService(paths)
    snapshots = SnapshotStore(paths.snapshots_dir)
    print(f"Closeout validation {start} -> {end}; engine v{ENGINE_VERSION}; isolated dir {root}")
    print("Holdout: NOT evaluated (no code path in this script can). Default parameters only.\n")

    rows: list[dict] = []
    for strategy in STRATEGIES:
        for symbol in SYMBOLS:
            row: dict = {"strategy": strategy, "symbol": symbol, "timeframe": "1d"}
            try:
                out = svc.run(svc.default_request(strategy, symbol, start, end, notes="sprint23 closeout validation"))
            except Exception as exc:  # noqa: BLE001 - an unexpected exception is itself a finding
                row.update(status="EXCEPTION", classification="STRATEGY_OR_ENGINE_DEFECT",
                           reason=f"{type(exc).__name__}: {exc}")
                rows.append(row)
                print(f"{strategy:<20}{symbol:<6} EXCEPTION {row['reason']}")
                continue
            row.update(status=out.status, run_id=out.run_id, reason_code=out.reason_code, reason=out.reason)
            if out.status != "COMPLETED" or not out.record:
                row["classification"] = classify(out.status, out.reason_code, {}, None, None)
                rows.append(row)
                print(f"{strategy:<20}{symbol:<6} {row['classification']} [{out.reason_code}] {out.reason}")
                continue
            rec = out.record
            prov = rec["provenance"]
            provider = prov.get("provider")
            audit = audit_snapshot(snapshots, prov.get("snapshot_id"), prov, start, end)
            sel = rec["attempts"][rec["selection"]["selected_index"]]
            try:
                replay = svc.reproduce(out.run_id)
            except Exception as exc:  # noqa: BLE001
                replay = {"backtest": f"ERROR {type(exc).__name__}: {exc}", "data": "ERROR", "strategy": "ERROR", "detail": []}
            wf = None
            try:
                wf_out = svc.walk_forward(svc.default_request(strategy, symbol, start, end, notes="sprint23 closeout walk-forward"),
                                          args.train_bars, args.test_bars)
                if wf_out.status == "COMPLETED" and wf_out.record:
                    agg = wf_out.record["aggregate"]
                    tests = [f["test_scorecard"] for f in wf_out.record["folds"]]
                    wf = {"run_id": wf_out.run_id, "folds": agg["folds"],
                          "out_of_sample_net_return_chained": agg["out_of_sample_net_return_chained"],
                          "out_of_sample_closed_trades": agg["out_of_sample_closed_trades"],
                          "profitable_folds": sum(1 for t in tests if (metric(t["net_return"]) or 0) > 0),
                          "train_bars": args.train_bars, "test_bars": args.test_bars}
                else:
                    wf = {"status": wf_out.status, "reason_code": wf_out.reason_code, "reason": wf_out.reason}
            except Exception as exc:  # noqa: BLE001
                wf = {"status": "EXCEPTION", "reason": f"{type(exc).__name__}: {exc}"}
            data_label = "SYNTHETIC" if str(provider).lower() in SYNTHETIC_PROVIDERS else "REAL"
            row.update(
                classification=classify("COMPLETED", None, audit, replay, provider), data_label=data_label, provider=provider,
                params=rec["selected"]["params"], strategy_source_hash=rec["strategy"]["version"], engine_version=rec["engine"]["version"],
                snapshot_id=prov.get("snapshot_id"), spec_hash=rec.get("spec_hash"), requested_range=[str(start), str(end)],
                returned_range=[prov.get("first_bar"), prov.get("last_bar")], data_audit=audit,
                development=window_row(sel["scorecards"]["development"]), validation=window_row(sel["scorecards"]["validation"]),
                walk_forward=wf, reproduction={k: replay.get(k) for k in ("data", "strategy", "backtest")},
                boundaries={k: rec["boundaries"][k] for k in ("development", "validation", "holdout")},
                holdout_status="LOCKED (never evaluated by this script)")
            rows.append(row)
            v = row["validation"]
            print(f"{strategy:<20}{symbol:<6} {row['classification']:<22} {data_label} trades(dev/val)="
                  f"{row['development']['closed_trades']}/{v['closed_trades']} val net={fmt(v['net_return'], True)} "
                  f"B&H={fmt(v['benchmark_return'], True)} exp-matched={fmt(v['exposure_matched_return'], True)} "
                  f"replay={row['reproduction']['backtest']}")

    summary: dict[str, int] = {}
    for r in rows:
        summary[r["classification"]] = summary.get(r["classification"], 0) + 1
    report = {"generated": pd.Timestamp.now(tz="UTC").isoformat(), "engine_version": ENGINE_VERSION, "window": [str(start), str(end)],
              "isolated_dir": str(root), "holdout": "LOCKED -- not evaluated", "combinations": len(rows), "summary": summary, "rows": rows}
    (root / "closeout_report.json").write_text(json.dumps(report, indent=2, default=str))
    (root / "closeout_report.md").write_text(render_markdown(report))
    print(f"\n{len(rows)} combinations: {summary}")
    print(f"Reports: {root / 'closeout_report.json'} and {root / 'closeout_report.md'}")
    print("Positive returns are not evidence of an edge. Small samples, survivorship and the single-path nature of history apply.")
    return 0


def render_markdown(report: dict) -> str:
    lines = [f"# Sprint 23 closeout validation (engine v{report['engine_version']})", "",
             f"Window {report['window'][0]} to {report['window'][1]}. Holdout: **{report['holdout']}**. Summary: `{report['summary']}`.", "",
             "| Strategy | Symbol | Class | Data | Dev trades | Val trades | Val net | Val B&H | Val exp-matched | Val Sharpe | Val maxDD | Val exposure | WF OOS chained | Replay |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["rows"]:
        if "development" not in r:
            lines.append(f"| {r['strategy']} | {r['symbol']} | {r['classification']} | - | - | - | - | - | - | - | - | - | - | {r.get('reason_code') or r.get('reason')} |")
            continue
        d, v, wf = r["development"], r["validation"], r.get("walk_forward") or {}
        wfv = wf.get("out_of_sample_net_return_chained")
        lines.append(f"| {r['strategy']} | {r['symbol']} | {r['classification']} | {r['data_label']} | {d['closed_trades']} | "
                     f"{v['closed_trades']} | {fmt(v['net_return'], True)} | {fmt(v['benchmark_return'], True)} | "
                     f"{fmt(v['exposure_matched_return'], True)} | {fmt(v['sharpe'])} | {fmt(v['max_drawdown'], True)} | "
                     f"{fmt(v['exposure_time'], True)} | {'n/a' if wfv is None else f'{wfv:.2%}'} | {r['reproduction']['backtest']} |")
    lines += ["", "Data audit failures:", ""]
    bad = [r for r in report["rows"] if r.get("data_audit") and not r["data_audit"].get("ok")]
    lines += [f"- {r['strategy']} {r['symbol']}: {r['data_audit'].get('failed_checks') or r['data_audit'].get('error')}" for r in bad] or ["- none"]
    lines += ["", "Benchmarks: *B&H* is full buy-and-hold (fully invested); *exp-matched* holds the instrument only on the bars the "
              "strategy was invested, cash otherwise (retrospective comparator). Strategy returns use risk-based sizing, so they "
              "are not like-for-like with either. No holdout figure appears anywhere in this report."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
