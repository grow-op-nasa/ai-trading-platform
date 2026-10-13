"""Sprint 16 failure-state live integration test (manual, not pytest).

EVIDENCE LABELS (kept separate on purpose):
  * the market-data failure here is a SYNTHETIC, INJECTED fault (src.data.fault_injection,
    message tagged "[fault-injection]"). It is NOT a real third-party outage.
  * the unevaluable check arises naturally from production code (equity curve shorter
    than the drawdown minimum).
  * the agent phase (optional) is a GENUINE Anthropic run through the real
    `market-monitor-agent` CLI that inspects the STORED run.

Phases
  1  repo state
  2  seed an isolated persisted paper portfolio: 80% exposure vs a 50% ceiling
     (CRITICAL finding) and a 10-point equity curve (drawdown NOT_EVALUABLE)
  3  deterministic monitoring run through the production data path with the fault
     injected at the provider boundary; persisted into the isolated monitoring DB;
     independent expectation computed from a separate in-memory twin run
  4  real-provider agent run (needs ANTHROPIC_API_KEY; never printed; redacted)
  5  independent verification + read-only before/after comparison

    python scripts/monitoring_failure_live_integration.py --skip-agent   # phases 1-3, no key
    python scripts/monitoring_failure_live_integration.py                # all phases

Nothing is committed or pushed, no broker is contacted, findings are never written
by hand, and your real data/ directory is never touched (everything lives in a temp cwd).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import monitoring_agent_live_smoke as smoke  # noqa: E402
import monitoring_live_integration as live  # noqa: E402  (sh, redact, git, finish helpers)
from src.ai.agents.monitoring import MonitoringAgentRunStore  # noqa: E402
from src.data.base import Interval  # noqa: E402
from src.data.fault_injection import Fault, FaultInjectingProvider, FaultKind, SyntheticCandleProvider  # noqa: E402
from src.data.service import MarketDataService  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402
from src.experiments.spec import ExperimentSpec  # noqa: E402
from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource  # noqa: E402
from src.portfolio.models import Portfolio  # noqa: E402
from src.portfolio.position import Position, PositionSide  # noqa: E402
from src.strategies.identity import strategy_version  # noqa: E402
from src.strategies.registry import get_strategy_class  # noqa: E402

GOAL = (
    "Inspect the most recent stored monitoring run and its findings. For each finding, fetch the full "
    "detail and explain what the evidence shows. Say precisely which checks were NOT evaluable and why, "
    "and describe any market-data problem exactly as the stored evidence states it. Keep observed facts "
    "(cite finding and run ids), hypotheses, and suggested further investigations clearly separated. "
    "Do not recommend trades or any action on the portfolio."
)
RETRIEVAL_RULE = "market_data.retrieval_failed"


def seed_failure_scenario(registry: ExperimentRegistry) -> int:
    cls = get_strategy_class("ema_cross")
    exp = registry.log_experiment(strategy_name="ema_cross", changed={}, metrics_before={}, metrics_after={},
                                  decision="KEEP", notes="sprint16 failure-scenario seed")
    registry.save_spec(exp, ExperimentSpec(
        strategy_name="ema_cross", strategy_version=strategy_version(cls), strategy_params={},
        symbol="QQQ", interval=Interval.DAY_1, dataset_start=pd.Timestamp("2026-08-01", tz="UTC"),
        dataset_end=pd.Timestamp("2026-09-30", tz="UTC"), dataset_source="failure-seed",
        dataset_fingerprint="failure-seed", risk_config={"max_portfolio_exposure_pct": 0.5}))
    pos = Position(symbol="QQQ", side=PositionSide.LONG, quantity=80, entry_price=100.0,
                   entry_timestamp=pd.Timestamp("2026-09-01", tz="UTC"), entry_signal_id=uuid4())
    registry.save_portfolio(exp, Portfolio.reconstruct(cash=2000.0, positions=[pos]))
    idx = pd.date_range("2026-09-21", periods=10, freq="D", tz="UTC")      # 10 points < drawdown minimum
    registry.save_equity_curve(exp, pd.Series([10000 + 25 * i for i in range(10)], index=idx, dtype=float))
    return exp


def faulty_service(registry, exp, store) -> tuple[MonitoringService, FaultInjectingProvider]:
    provider = FaultInjectingProvider(SyntheticCandleProvider(last_session=date.today()), [Fault(FaultKind.TIMEOUT)])
    md = MarketDataService(provider=provider, cache_dir=Path(tempfile.mkdtemp(prefix="md_cache_")), use_cache=False)
    return MonitoringService(RegistryStateSource(registry, experiment_id=exp), store, market_data=md), provider


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-agent", action="store_true")
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args(argv)
    result: dict = {"started": datetime.now(timezone.utc).isoformat(), "phases": {},
                    "evidence_labels": {"market_data_failure": "SYNTHETIC injected fault (not a real outage)",
                                        "unevaluable_check": "production behaviour on a short equity curve",
                                        "agent_phase": "genuine Anthropic run (only if executed)"}}

    commit, dirty = live.git("rev-parse", "--short", "HEAD"), live.git("status", "--short")
    print(f"[1] commit {commit}; working tree: {'clean' if not dirty else 'dirty'}")
    result["commit"] = commit

    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="monitor_failure_"))
    (work / "data" / "monitoring").mkdir(parents=True, exist_ok=True)
    reg_path, mon_db, runs_dir = work / "data" / "experiments.db", work / "data" / "monitoring" / "monitoring.db", work / "data" / "monitoring_agent_runs"
    print(f"    isolated cwd: {work}")

    registry = ExperimentRegistry(db_path=reg_path)
    exp = seed_failure_scenario(registry)
    before = smoke.fingerprint(registry, exp, reg_path)
    extra = lambda r: {"trades": len(r.get_trades(exp)), "signals": len(r.get_signals(exp))}  # noqa: E731
    before_exec = extra(registry)
    print(f"[2] seeded experiment {exp}: 80 QQQ @100 (80% exposure vs 50% ceiling), 10-point equity curve")

    # independent expectation from a separate in-memory twin
    twin_svc, _ = faulty_service(registry, exp, MonitoringStore(":memory:"))
    twin = twin_svc.run_once()
    expected = {f.rule_id: f.severity.value for f in (twin_svc.store.get_finding(i) for i in twin.finding_ids)}
    print(f"    independent expectation (twin, in-memory): run status {twin.status.value}; findings {expected}")

    # the persisted run the agent will inspect
    store = MonitoringStore(mon_db)
    svc, provider = faulty_service(registry, exp, store)
    run = svc.run_once()
    checks = {c.check_id: c for c in run.checks}
    print(f"[3] persisted monitoring run {run.run_id}: status={run.status.value} persisted={run.persisted}   "
          "[market-data failure = SYNTHETIC INJECTED FAULT]")
    for c in run.checks:
        print(f"      [{c.status.value}] {c.check_id}" + (f" -- {c.reason}" if getattr(c, 'reason', None) else ""))
    persisted = {f.rule_id: f for f in store.list_findings(limit=100)}
    ev = persisted[RETRIEVAL_RULE].evidence if RETRIEVAL_RULE in persisted else []
    ev_text = json.dumps([getattr(e, "__dict__", str(e)) for e in ev], default=str)
    ph3 = [
        ("run persisted and reloadable", run.persisted and store.get_run(run.run_id) is not None),
        ("run status is CRITICAL (not healthy/unknown)", run.status.value == "CRITICAL"),
        ("drawdown check is NOT_EVALUABLE", checks["portfolio_drawdown"].status.value == "NOT_EVALUABLE"),
        ("freshness check is NOT_EVALUABLE (no observation to age)", checks["market_data_freshness"].status.value == "NOT_EVALUABLE"),
        ("no NOT_EVALUABLE check reported as healthy",
         all(c.status.value != "HEALTHY" for c in run.checks if c.status.value == "NOT_EVALUABLE")),
        ("retrieval-failure finding persisted with provider evidence", RETRIEVAL_RULE in persisted and "DataProviderError" in ev_text),
        ("exposure finding persisted", "risk.portfolio_exposure" in persisted),
        ("injected fault is labelled in the evidence", "[fault-injection]" in ev_text),
        ("provider boundary was actually exercised", len(provider.calls) >= 1),
        ("persisted rules match the independent twin expectation", set(expected) <= set(persisted)),
    ]
    for n, ok in ph3:
        print(f"    [{'PASS' if ok else 'FAIL'}] {n}")
    result["phases"]["3"] = {"run_id": run.run_id, "status": run.status.value, "expected": expected,
                             "persisted_rules": sorted(persisted), "checks": {n: ok for n, ok in ph3}}
    if not all(ok for _, ok in ph3):
        result["verdict"] = "FAIL"
        return live.finish(result, work, 1)
    open_before = {f.fingerprint: f.finding_id for f in store.list_findings(status="OPEN", limit=100)}

    if args.skip_agent:
        print("\n--skip-agent: deterministic phases only; genuine-provider phases NOT run -> PARTIAL")
        result["verdict"] = "PARTIAL"
        return live.finish(result, work, 0)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[4] ANTHROPIC_API_KEY not set; provider phase NOT run -> PARTIAL")
        result.update(verdict="PARTIAL", note="no API key in environment")
        return live.finish(result, work, 0)

    print("[4] $ python -m src.cli market-monitor-agent --goal <goal> --experiment-id", exp, "(cwd=isolated dir)")
    rc4, out4 = live.sh([sys.executable, "-m", "src.cli", "market-monitor-agent", "--goal", GOAL,
                         "--experiment-id", str(exp)], work)
    print(out4)
    m = re.search(r"Agent Run ID:\s*([0-9a-f]{32})", out4)
    if not m:
        result.update(verdict="PARTIAL", agent_cli_exit=rc4, note="no agent run id (startup/config failure)")
        print("    no agent run id -> PARTIAL")
        return live.finish(result, work, 2)
    agent_store = MonitoringAgentRunStore(runs_dir)
    arun = agent_store.load(m.group(1))
    if arun is None:
        result["verdict"] = "FAIL"
        return live.finish(result, work, 1)
    if arun.status == "FAILED" and not arun.tool_calls:
        result.update(verdict="PARTIAL", agent_run_id=arun.run_id, agent_error=live.redact(str(arun.error)),
                      note="provider failure before any tool call")
        print(f"    provider failed before tool use ({live.redact(str(arun.error))}) -> PARTIAL")
        return live.finish(result, work, 2)

    print("[5] verification")
    after_reg = ExperimentRegistry(db_path=reg_path)
    after = smoke.fingerprint(after_reg, exp, reg_path)
    vrep = smoke.verify(arun, expected, MonitoringStore(mon_db), agent_store, before, after)
    sup = smoke.Report()
    is_real = arun.provider == "anthropic" and not arun.model.startswith("fake")
    sup.add("provider is the real Anthropic provider", is_real, f"{arun.provider}/{arun.model}")
    sup.add("execution/state rows unchanged", extra(after_reg) == before_exec, f"{before_exec} -> {extra(after_reg)}")
    ids = smoke.tool_ids(arun.tool_calls)
    sup.add("agent read the persisted run created in phase 3", run.run_id in ids, run.run_id)
    open_after = {f.fingerprint: f.finding_id for f in MonitoringStore(mon_db).list_findings(status="OPEN", limit=100)}
    sup.add("no duplicate/new findings created by the agent", open_before.items() <= open_after.items(),
            f"open before={len(open_before)} after={len(open_after)}")
    rep = arun.final_report
    expected_ids = set(open_before.values())
    fcalls = [c for c in arun.tool_calls if c.tool_name == "get_finding"]
    ok_ids = {c.arguments.get("finding_id") for c in fcalls if not c.is_error
              and c.result_summary.get("finding_id") == c.arguments.get("finding_id")
              and isinstance(c.result_summary.get("evidence"), list)}
    sup.add("every expected finding had its full detail returned by the registered get_finding handler",
            expected_ids <= ok_ids, f"{len(expected_ids & ok_ids)} of {len(expected_ids)} (retrieval failures: "
            f"{sum(1 for c in fcalls if c.is_error)})")
    sup.add("report evidence metadata (derived by code) agrees: complete, all expected ids retrieved, none incomplete",
            bool(rep) and rep.evidence_complete and expected_ids <= set(rep.findings_retrieved)
            and not rep.findings_incomplete, f"retrieved={len(rep.findings_retrieved) if rep else 0}")
    by = {c.initiated_by for c in arun.tool_calls}
    sup.add("audit distinguishes initiators; runtime calls are get_finding only",
            by <= {"model", "runtime"} and all(c.tool_name == "get_finding" for c in arun.tool_calls
                                               if c.initiated_by == "runtime"),
            f"model={sum(1 for c in arun.tool_calls if c.initiated_by == 'model')} "
            f"runtime={sum(1 for c in arun.tool_calls if c.initiated_by == 'runtime')}")
    sup.add("runtime did not rerun monitoring", arun.monitoring_run_count == 0
            and not any(c.tool_name == "run_monitoring_checks" and c.initiated_by == "runtime" for c in arun.tool_calls), "")
    text = " ".join([rep.summary, *rep.observed_facts]).lower() if rep else ""
    lims = " ".join(rep.limitations).lower() if rep else ""
    sup.add("drawdown not-evaluable is a limitation and not described as healthy",
            "portfolio_drawdown" in lims and not re.search(r"drawdown[^.]{0,60}(?<!not )\bhealthy\b", text), "")
    sup.add("market-data failure described from stored evidence (error type or finding id)",
            bool(rep) and ("dataprovidererror" in text or "retrieval" in text or "unavailable" in text), "")
    sup.add("agent states the failure was injected/synthetic OR does not claim a real outage",
            not re.search(r"\b(yahoo|yfinance|provider outage|vendor outage)\b", text) or "inject" in text, "",
            required=False)
    sup.add("no recovery/trading instructions", not re.search(
        r"\b(restart|reboot|switch provider|rerun the scheduler|buy|sell|liquidate)\b",
        " ".join([*rep.suggested_investigations, *rep.hypotheses]).lower() if rep else ""), "", required=False)
    rows = vrep.rows + sup.rows
    req_fail = [r for r in rows if r["required"] and not r["ok"]]
    verdict = "PASS" if (not req_fail and is_real) else ("FAIL" if req_fail else "PARTIAL")
    print(f"\nRESULT: {verdict} ({sum(1 for r in rows if r['required'] and r['ok'])}/{sum(1 for r in rows if r['required'])} required checks)")
    result.update(verdict=verdict, agent_run_id=arun.run_id, provider=arun.provider, model=arun.model,
                  monitoring_run_ids=arun.monitoring_run_ids, checks=rows,
                  finding_ids_in_report=rep.finding_ids if rep else [])
    return live.finish(result, work, 0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
