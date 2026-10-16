"""Sprint 18 operational validation of the paper-trading runtime (manual; run on the development machine).

Two clearly separated evidence classes -- never conflated in the output:

  PART 1  EXTERNAL DATA   the real `python -m src.cli paper ...` process, real `MarketDataService` (read-only
                          yfinance), registered production strategy `ema_cross`, real risk engine, `PaperBroker`,
                          persistence and monitoring. The strategy's own logic decides; if it emits no signal at the
                          latest closed bar the honest result is NO_ACTION. No signal is manufactured. Needs network;
                          if the provider is unreachable the result is reported as BLOCKED, not faked.
  PART 2  SYNTHETIC FIXTURE  the same production code path driven by a crafted, deterministic candle provider (clearly
                          labelled synthetic) so the full order -> fill -> portfolio -> reconciliation -> monitoring ->
                          restart/duplicate/crash-recovery path is exercised even when the real strategy is flat.
                          This is NOT a live-market integration test.

Everything lives in fresh temporary directories (nothing in your normal data/). No live broker is contacted (none is
reachable from this runtime) and no Anthropic key is read.

    python scripts/paper_runtime_validation.py [--skip-external] [--keep]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.calendar.nyse import NYSECalendar  # noqa: E402
from src.cli.paper import run_paper  # noqa: E402
from src.data.base import REQUIRED_COLUMNS, DataProvider, Interval  # noqa: E402
from src.data.service import MarketDataService  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402
from src.experiments.spec import ExperimentSpec  # noqa: E402
from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource  # noqa: E402
from src.monitoring.alerts.models import AlertEventSettings  # noqa: E402
from src.paper.faults import Faults, SimulatedCrash  # noqa: E402
from src.paper.runtime import PaperTradingRuntime  # noqa: E402
from src.paper.store import PaperStore  # noqa: E402
from src.portfolio.models import Portfolio  # noqa: E402
from src.strategies.identity import strategy_version  # noqa: E402
from src.strategies.registry import get_strategy_class  # noqa: E402
import src.strategies  # noqa: E402,F401

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def fingerprint(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            h.update(repr(conn.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall()).encode())
        return h.hexdigest()[:16]
    finally:
        conn.close()


def seed(registry: ExperimentRegistry, exposure_ceiling: float = 0.5) -> int:
    cls = get_strategy_class("ema_cross")
    exp = registry.log_experiment(strategy_name="ema_cross", changed={}, metrics_before={}, metrics_after={},
                                  decision="KEEP", notes="SYNTHETIC DISPOSABLE sprint18 paper validation fixture")
    registry.save_spec(exp, ExperimentSpec(
        strategy_name="ema_cross", strategy_version=strategy_version(cls), strategy_params={}, symbol="QQQ",
        interval=Interval.DAY_1, dataset_start=pd.Timestamp("2026-01-01", tz="UTC"),
        dataset_end=pd.Timestamp("2026-09-30", tz="UTC"), dataset_source="sprint18-validation",
        dataset_fingerprint="sprint18-validation", risk_config={"max_portfolio_exposure_pct": exposure_ceiling}))
    registry.save_portfolio(exp, Portfolio.reconstruct(cash=10_000.0, positions=[]))
    idx = pd.date_range("2026-09-01", periods=25, freq="D", tz="UTC")
    registry.save_equity_curve(exp, pd.Series([10_000.0 + i for i in range(25)], index=idx))
    return exp


def config_dict(exp: int) -> dict:
    return {"execution_mode": "paper", "experiment_id": exp, "strategy_name": "ema_cross", "symbol": "QQQ",
            "interval": "1d", "lookback_days": 400,
            "risk": {"allocation_per_trade_pct": 0.10, "max_portfolio_exposure_pct": 0.50, "risk_pct_per_trade": 0.01},
            "stop": {"type": "atr", "period": 14, "multiple": 2.0},
            "execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": 5.0, "fee_bps": 5.0, "fixed_fee": 0.0}}


def clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC", "ATP_ALERT"))}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO), os.environ.get("PYTHONPATH", "")) if p)
    return env


def cli(work: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "src.cli", "paper", *args], cwd=work, env=clean_env(),
                          capture_output=True, text=True, timeout=300)


# ------------------------------------------------------------------------------------------------ part 1
def part1(root: Path) -> None:
    print("\n=== PART 1: EXTERNAL DATA (real CLI process, yfinance, registered strategy ema_cross) ===")
    work = root / "external"; (work / "data").mkdir(parents=True)
    reg_path, paper_db, mon_db = work / "data/experiments.db", work / "data/paper/paper_runtime.db", work / "data/mon.db"
    exp = seed(ExperimentRegistry(reg_path))
    cfg = work / "paper.json"; cfg.write_text(json.dumps(config_dict(exp)))
    base = ["--config", str(cfg), "--registry", str(reg_path), "--paper-db", str(paper_db)]
    print("  cmd: python -m src.cli paper init|preflight|run --once|status --config <cfg> --registry <reg> --paper-db <db>")
    r = cli(work, "init", *base, "--label", "sprint18 external-data validation (disposable)")
    check("paper init (explicit)", r.returncode == 0, r.stdout.strip()[:120])
    before_reg = fingerprint(reg_path)
    r = cli(work, "preflight", *base, "--json")
    pre = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
    for c in pre.get("checks", []):
        print(f"    preflight {c['status']:<13} {c['name']}: {c['detail'][:100]}")
    data_ok = {c["name"]: c["status"] for c in pre.get("checks", [])}.get("market_data") == "PASS"
    if not data_ok:
        print("  NOTE: external market data unavailable here; reporting honestly, no cycle is faked.")
    check("preflight is read-only (registry unchanged)", fingerprint(reg_path) == before_reg)
    check("preflight confirms paper-only execution", any(c["name"] == "paper_only_execution" and c["status"] == "PASS"
                                                         for c in pre.get("checks", [])))
    r = cli(work, "run", "--once", *base, "--monitor-db", str(mon_db), "--json")
    rec = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
    print(f"  cycle {rec.get('cycle_id')}: status={rec.get('status')} reason={rec.get('reason_code')} "
          f"bar={rec.get('decision_bar')} exit={r.returncode}")
    print(f"  data provenance: {json.dumps({k: (rec.get('dataset') or {}).get(k) for k in ('provider', 'complete_bars_hash', 'bars_used', 'dropped_incomplete_bars')})}")
    print(f"  strategy hash {str(rec.get('strategy_hash'))[:16]}  config hash {str(rec.get('config_hash'))[:16]}")
    allowed = {"NO_ACTION", "RISK_REJECTED", "ORDER_FILLED", "NO_NEW_BAR", "BLOCKED"}
    check("cycle ended in an honest, documented status", rec.get("status") in allowed, str(rec.get("status")))
    if rec.get("status") == "NO_ACTION":
        check("no signal -> no order was manufactured", rec.get("client_order_id") is None and
              PaperStore.open_existing(paper_db).list_fills(exp) == [])
    if rec.get("status") == "ORDER_FILLED":
        print(f"  (the real strategy signalled) intent={rec['intent_id']} order={rec['client_order_id']} fill={rec['fill']}")
        print(f"  monitoring={rec.get('monitoring')}  reconciliation={rec.get('reconciliation_status')}")
    if rec.get("status") in ("NO_ACTION", "ORDER_FILLED", "RISK_REJECTED"):
        r2 = cli(work, "run", "--once", *base, "--monitor-db", str(mon_db), "--json")
        again = json.loads(r2.stdout)
        check("repeating the same decision bar is a replay (no duplicate order or fill)",
              again.get("replayed") is True and again.get("cycle_id") == rec.get("cycle_id"))
        st = cli(work, "status", *base, "--json")
        check("cycle history reloads from disk", json.loads(st.stdout)["cycles"][0]["cycle_id"] == rec.get("cycle_id"))


# ------------------------------------------------------------------------------------------------ part 2
class CraftedProvider(DataProvider):
    """SYNTHETIC deterministic daily candles: a long decline then a crafted tail, so the real `ema_cross` strategy
    emits LONG on the final bar (and FLAT when the tail crashes). Labelled synthetic everywhere."""

    def __init__(self, last_session: date, tail: list[float]) -> None:
        self.last_session, self.tail, self._cal = last_session, tail, NYSECalendar()

    def fetch_candles(self, symbol, start, end, interval):
        days = self._cal.sessions_between(start, min(end, self.last_session))
        px = list(np.linspace(150, 100, len(days) - len(self.tail))) + self.tail
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="timestamp")
        return pd.DataFrame({"open": px, "high": [p + 1 for p in px], "low": [p - 1 for p in px], "close": px,
                             "volume": 1e6}, index=idx)[REQUIRED_COLUMNS]


def part2(root: Path) -> None:
    print("\n=== PART 2: SYNTHETIC FIXTURE (crafted candles; real ema_cross, risk, PaperBroker, registry, monitoring) ===")
    work = root / "fixture"; work.mkdir()
    reg_path, paper_db, mon_db = work / "experiments.db", work / "paper" / "paper.db", work / "monitoring.db"
    registry = ExperimentRegistry(reg_path)
    exp = seed(registry, exposure_ceiling=0.001)          # monitoring ceiling far below the position -> a genuine finding
    cfg = work / "paper.json"; cfg.write_text(json.dumps(config_dict(exp)))
    clock = {"now": datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc)}
    provider = {"p": CraftedProvider(date(2026, 10, 7), [140.0])}
    out: list[str] = []

    def monitor():
        return MonitoringService(RegistryStateSource(ExperimentRegistry(reg_path), experiment_id=exp),
                                 MonitoringStore(mon_db), alerts=AlertEventSettings(enabled=True, channels=("email",)))

    def run(*argv) -> int:
        out.clear()
        md = MarketDataService(provider=provider["p"], cache_dir=work / "cache", use_cache=False)
        return run_paper([argv[0], "--config", str(cfg), "--registry", str(reg_path), "--paper-db", str(paper_db), *argv[1:]],
                         market_data=md, monitor_factory=monitor, clock=lambda: clock["now"], emit=out.append)

    check("init", run("init", "--label", "sprint18 synthetic fixture") == 0)
    check("preflight PASS", run("preflight", "--json") == 0 and json.loads("\n".join(out))["ready"])
    store = PaperStore.open_existing(paper_db)

    print("-- cycle 1: crafted LONG at the decision bar --")
    check("cycle 1 exit 0", run("run", "--once", "--json") == 0)
    c1 = json.loads("\n".join(out))
    print(f"   cycle {c1['cycle_id']}  status={c1['status']}  bar={c1['decision_bar']}")
    print(f"   signal {c1['signal']['id']}  risk approved qty={c1['risk_decision']['final_approved_quantity']} "
          f"limiting={c1['risk_decision'].get('limiting_constraint')}")
    print(f"   intent {c1['intent_id']}  client order {c1['client_order_id']}")
    print(f"   fill price {c1['fill']['fill_price']:.4f} (ref {c1['fill']['reference_price']:.2f}) fee {c1['fill']['fee']:.4f} "
          f"slippage {c1['fill']['slippage_amount']:.4f}")
    print(f"   reconciliation {c1['reconciliation_id']}={c1['reconciliation_status']}  monitoring={c1['monitoring']}")
    check("ORDER_FILLED via registered production strategy ema_cross", c1["status"] == "ORDER_FILLED" and c1["strategy_name"] == "ema_cross")
    f = store.list_fills(exp)[0]
    pf = registry.get_portfolio(exp)
    check("portfolio changed only by the recorded fill (cash incl. fee)",
          abs(pf.cash - (10_000.0 - f.quantity * f.fill_price - f.fee)) < 1e-6 and pf.positions["QQQ"].quantity == f.quantity)
    check("quantity <= risk-approved quantity", f.quantity <= c1["risk_decision"]["final_approved_quantity"])
    check("reconciliation MATCH", c1["reconciliation_status"] == "MATCH")
    mstore = MonitoringStore(mon_db)
    events = mstore.alerts.list_events(limit=50)
    check("monitoring ran on the post-cycle state and a genuine finding produced an alert event",
          c1["monitoring"]["persisted"] is True and len(events) >= 1,
          ", ".join(f"{e.event_type.value}:{e.rule_id}" for e in events))
    n_events = len(events)
    check("monitoring run id is recorded", bool(c1["monitoring"].get("run_id")))

    print("-- duplicate + restart --")
    check("same bar again -> replay", run("run", "--once", "--json") == 0 and json.loads("\n".join(out))["replayed"] is True)
    check("still exactly one intent and one fill", len(store.list_intents(exp)) == 1 and len(store.list_fills(exp)) == 1)
    check("alert events not duplicated", len(MonitoringStore(mon_db).alerts.list_events(limit=50)) == n_events)

    print("-- cycle 2: crafted crash -> FLAT, with the process dying right after the broker commits --")
    clock["now"] = datetime(2026, 10, 8, 22, 0, tzinfo=timezone.utc)
    provider["p"] = CraftedProvider(date(2026, 10, 8), [140.0, 60.0])
    faults = Faults().arm("broker_after_commit", SimulatedCrash("process died after the broker committed"))
    md = MarketDataService(provider=provider["p"], cache_dir=work / "cache", use_cache=False)
    from src.paper.config import PaperRunConfig
    rt = PaperTradingRuntime(PaperRunConfig.from_file(cfg), registry_path=reg_path, store_path=paper_db, market_data=md,
                             monitor_factory=monitor, clock=lambda: clock["now"], faults=faults)
    try:
        rt.run_cycle(); crashed = False
    except SimulatedCrash:
        crashed = True
    check("simulated process death after broker commit", crashed)
    unresolved = store.list_intents(exp, unresolved_only=True)
    print(f"   unresolved after crash: {[i.client_order_id for i in unresolved]}  fills in ledger: {len(store.list_fills(exp))}")
    check("crash left one unresolved intent and one ledger fill (portfolio not yet updated)",
          len(unresolved) == 1 and len(store.list_fills(exp)) == 2 and "QQQ" in registry.get_portfolio(exp).positions)
    check("status lists the unresolved intent", run("status", "--intents", "--json") == 0 and
          len(json.loads("\n".join(out))["unresolved_intents"]) == 1)
    check("restart recovers (lookup, never resubmit): ORDER_FILLED", run("run", "--once", "--json") == 0 and
          json.loads("\n".join(out))["status"] == "ORDER_FILLED")
    fills = store.list_fills(exp)
    pf = registry.get_portfolio(exp)
    gross = fills[0].quantity * (fills[1].fill_price - fills[0].fill_price)
    check("exactly two fills total; position closed once", len(fills) == 2 and not pf.positions)
    check("net cash = gross P&L minus both fees (no double fee, net != gross)",
          abs((pf.cash - 10_000.0) - (gross - fills[0].fee - fills[1].fee)) < 1e-6,
          f"gross {gross:.4f} fees {fills[0].fee + fills[1].fee:.4f}")
    check("reconciliation MATCH after recovery", store.latest_reconciliation(exp).status.value == "MATCH")
    check("no unresolved intents remain", store.list_intents(exp, unresolved_only=True) == [])

    print("-- data failure blocks, never executes --")
    clock["now"] = datetime(2026, 10, 9, 22, 0, tzinfo=timezone.utc)
    from src.data.fault_injection import Fault, FaultInjectingProvider, FaultKind
    provider["p"] = FaultInjectingProvider(CraftedProvider(date(2026, 10, 9), [140.0, 60.0, 60.0]), [Fault(FaultKind.TIMEOUT)])
    n_fills = len(store.list_fills(exp))
    check("data failure -> BLOCKED exit 3, no order", run("run", "--once", "--json") == 3 and
          json.loads("\n".join(out))["status"] == "BLOCKED" and len(store.list_fills(exp)) == n_fills)
    print("   (injected, synthetic data fault: not a real provider outage)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-external", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    root = Path(tempfile.mkdtemp(prefix="atp_paper_validation_"))
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    print(f"ISOLATED validation root (disposable): {root}\nrepo HEAD {git}; python {sys.version.split()[0]}")
    print("No live broker is reachable from the paper runtime; no Anthropic key is read; your data/ is not touched.")
    if not args.skip_external:
        part1(root)
    part2(root)
    failed = [c for c in CHECKS if not c[1]]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'} ({len(CHECKS) - len(failed)}/{len(CHECKS)} checks)")
    if args.keep or failed:
        print(f"validation data kept at {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
