"""Real-data strategy research run (Sprint 23). Runs on YOUR machine; it calls the real market-data provider.

    python scripts/run_strategy_research.py [--start 2015-01-01] [--end YYYY-MM-DD] [--symbols SPY QQQ ...]
                                            [--strategies ema_cross rsi_mean_reversion donchian_breakout]
                                            [--data-dir DIR] [--holdout]

Each (strategy, symbol) is an independent, default-parameter, daily-bar research run under the platform's recorded cost
assumptions. Results are stored in the research database (data/research/research.db unless --data-dir is given) and printed.
The holdout is LOCKED unless --holdout is passed; it can be evaluated once per run, and the evaluation cannot be undone.

This script reports what happened. It never promotes a strategy, creates an order/intent/proposal, or claims an edge.
Exit status: 0 completed (some runs may be NOT_EVALUABLE, shown with reasons); 2 usage error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from loguru import logger  # noqa: E402

from src.appsvc.paths import AppPaths  # noqa: E402
from src.appsvc.strategy_research import StrategyResearchService  # noqa: E402

DEFAULT_SYMBOLS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "NVDA", "AAPL", "AMD"]
DEFAULT_STRATEGIES = ["ema_cross", "rsi_mean_reversion", "donchian_breakout"]


def _value(metric: dict) -> str:
    if metric is None:
        return "n/a"
    if str(metric.get("status")).lower() == "ok" and metric.get("value") is not None:
        return f"{metric['value']:.4f}"
    return f"n/a ({metric.get('reason') or metric.get('status')})"


def _line(window: str, sc: dict) -> str:
    return (f"    {window:<11} trades={sc['closed_trades']:<4} gross={_value(sc['gross_return'])} net={_value(sc['net_return'])} "
            f"net@3xcosts={_value(sc['net_return_at_stress_costs'])} | exposure={_value(sc['exposure_time'])} "
            f"sharpe={_value(sc['sharpe'])} maxDD={_value(sc['max_drawdown'])} | benchmark={_value(sc['benchmark_return'])} "
            f"excess={_value(sc['excess_return'])} sample={sc['sample']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=None, help="default: yesterday")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--strategies", nargs="+", default=DEFAULT_STRATEGIES)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--holdout", action="store_true", help="evaluate the locked holdout ONCE per completed run")
    ap.add_argument("--verbose", action="store_true", help="show the platform's DEBUG log lines (very long)")
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
    paths = AppPaths.under(Path(args.data_dir)) if args.data_dir else AppPaths()
    svc = StrategyResearchService(paths)
    print(f"Research window {start} -> {end}; symbols {args.symbols}; strategies {args.strategies}")
    print("Daily bars only. Default parameters. Provider: the platform's market-data service (see each run's record).\n")
    for strategy in args.strategies:
        outcomes, cov = svc.run_batch(strategy, args.symbols, start, end)
        print(f"=== {strategy} ===")
        for sym, out in outcomes.items():
            if out.status != "COMPLETED" or not out.record:
                print(f"  {sym}: NOT_EVALUABLE [{out.reason_code}] {out.reason}")
                continue
            rec = out.record
            sel = rec["attempts"][rec["selection"]["selected_index"]]
            print(f"  {sym}: run {out.run_id}")
            for window in ("development", "validation"):
                print(_line(window, sel["scorecards"][window]))
            if args.holdout:
                h = svc.evaluate_holdout(out.run_id)
                sc = h["scorecard"]
                print(_line("holdout", sc))
                print("    assessment: " + "; ".join(h["assessment"]))
        print()
    print("Benchmark = buy-and-hold at full capital; strategy returns reflect risk-based position sizing and partial exposure, so "
          "raw excess is not like-for-like. Compare Sharpe and drawdown, not just return.")
    print("Coverage and reasons are stored per run. Nothing here is a recommendation; small samples and survivorship bias apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
