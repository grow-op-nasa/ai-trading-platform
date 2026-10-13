"""Live smoke test: Market Monitoring Agent against a persisted paper
portfolio that produces real findings (Sprint 15, ADR-0050).

NOT part of the pytest suite. It is run by hand, calls the real Anthropic
API (needs ANTHROPIC_API_KEY in the environment, read by the provider and
never printed), and judges the *behaviour* of a live model:

    python scripts/monitoring_agent_live_smoke.py            # real provider
    python scripts/monitoring_agent_live_smoke.py --fake     # offline harness self-check

What it does
  1. Builds an ISOLATED temp registry (your real data is never touched) with
     one experiment: a paper portfolio at 80% gross exposure against a 50%
     ceiling (-> risk.portfolio_exposure CRITICAL) and an equity history
     with a ~15% drawdown (-> drawdown.portfolio WARNING).
  2. Computes the EXPECTED findings independently with the deterministic
     service (separate in-memory store; no LLM).
  3. Runs the agent with the real provider and a goal that asks it to run
     the checks, list findings and drill into each.
  4. Verifies: findings retrieved through tools, evidence cited and
     traceable to tool output, limitations reported, only read tools used,
     and portfolio/registry state byte-for-byte unchanged.

Market data is deliberately not wired in (no network dependency besides the
LLM), so market-data and valuation checks must show up as NOT_EVALUABLE
limitations -- that is part of what is verified.
Exit code: 0 all required checks passed, 1 a required check failed, 2 could
not run (e.g. missing/invalid key).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.strategies  # noqa: E402,F401  (registers built-in strategies)
from src.ai.agents.models import ToolCallRecord  # noqa: E402
from src.ai.agents.monitoring import (  # noqa: E402
    MarketMonitoringAgent,
    MarketMonitoringAgentPolicy,
    MonitoringAgentRunStore,
    default_monitoring_tool_registry,
)
from src.ai.agents.provider import LLMResponse, LLMToolCall, ProviderError  # noqa: E402
from src.data.base import Interval  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402
from src.experiments.spec import ExperimentSpec  # noqa: E402
from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource  # noqa: E402
from src.portfolio.models import Portfolio  # noqa: E402
from src.portfolio.position import Position, PositionSide  # noqa: E402
from src.strategies.identity import strategy_version  # noqa: E402
from src.strategies.registry import available_strategies, get_strategy_class  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
READ_TOOLS = {
    "run_monitoring_checks", "list_findings", "get_finding", "get_monitoring_run",
    "get_paper_portfolio_snapshot", "get_execution_summary", "get_market_data_health",
}
GOAL = (
    "Review the persisted paper portfolio. Run the monitoring checks, list the findings, "
    "and fetch the full detail of every finding. Then tell me what is wrong, which evidence "
    "supports each point (cite finding ids), what you could NOT evaluate and why, and what "
    "a human should investigate. Do not recommend trades."
)


# ---------------------------------------------------------------- seeding
def seed(registry: ExperimentRegistry) -> int:
    cls = get_strategy_class("ema_cross")
    exp = registry.log_experiment(
        strategy_name="ema_cross", changed={}, metrics_before={}, metrics_after={}, decision="KEEP",
        notes="live smoke seed",
    )
    registry.save_spec(exp, ExperimentSpec(
        strategy_name="ema_cross", strategy_version=strategy_version(cls), strategy_params={},
        symbol="QQQ", interval=Interval.DAY_1, dataset_start=pd.Timestamp("2026-08-01", tz="UTC"),
        dataset_end=pd.Timestamp("2026-09-30", tz="UTC"), dataset_source="live-smoke-seed",
        dataset_fingerprint="live-smoke-seed", risk_config={"max_portfolio_exposure_pct": 0.5},
    ))
    pos = Position(symbol="QQQ", side=PositionSide.LONG, quantity=80, entry_price=100.0,
                   entry_timestamp=pd.Timestamp("2026-09-01", tz="UTC"), entry_signal_id=uuid4())
    registry.save_portfolio(exp, Portfolio.reconstruct(cash=2000.0, positions=[pos]))
    idx = pd.date_range("2026-08-17", periods=30, freq="D", tz="UTC")
    vals = [10000 + 40 * i for i in range(20)] + [10760 - 90 * i for i in range(1, 11)]  # peak 10760 -> 9860
    vals[-1] = 9150.0  # ~15% below peak: a WARNING-level drawdown
    registry.save_equity_curve(exp, pd.Series(vals, index=idx, dtype=float))
    return exp


# ---------------------------------------------------------------- state fingerprint
def fingerprint(registry: ExperimentRegistry, exp: int, db_path: Path) -> dict:
    pf = registry.get_portfolio(exp)
    curve = registry.get_equity_curve(exp)
    spec = registry.get_spec(exp)
    manifest = REPO / "src" / "strategies" / "promoted" / "manifest.json"
    return {
        "registry_db_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "cash": pf.cash,
        "positions": {s: [p.quantity, p.entry_price, p.stop_price, p.current_price] for s, p in pf.positions.items()},
        "equity_curve": [float(v) for v in curve.tolist()],
        "spec_risk_config": spec.risk_config if spec else None,
        "experiment_ids": [e.id for e in registry.list_experiments()],
        "strategies": sorted(available_strategies()),
        "promoted_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }


# ---------------------------------------------------------------- offline provider (harness self-check)
class ScriptedProvider:
    """Deterministic stand-in used only by --fake to prove the harness works."""

    name, model = "fake-scripted", "fake-model-v1"

    def __init__(self, stored: bool = False) -> None:
        self.stored = stored

    def generate(self, *, system, messages, tools):
        # Sprint 16: the runtime itself fetches every finding's full detail right after the run is read,
        # so tool messages are no longer positional. Identify results by content instead.
        tool_msgs = [json.loads(m.content) for m in messages if m.role == "tool"]
        if not tool_msgs:
            return _call("get_monitoring_run", {}, "c1") if self.stored else _call("run_monitoring_checks", {}, "c1")
        listed = [m for m in tool_msgs if isinstance(m.get("data"), dict) and "count" in m["data"]]
        if not listed:
            return _call("list_findings", {}, "c2")
        ids = [f["finding_id"] for f in listed[0]["data"]["findings"]]
        run_id = next(m["data"]["run_id"] for m in tool_msgs if isinstance(m.get("data"), dict) and "run_id" in m["data"])
        lines = ["Summary: the persisted paper portfolio has two active problems.", "Observed:"]
        lines += [f"- finding {i} is open (run {run_id})" for i in ids]
        lines += ["Hypothesis:", "- the position was sized without regard to the exposure ceiling",
                  "Suggested investigation:", "- a human should review the sizing and the recent drawdown",
                  "Limitation:", "- market-data and valuation checks were not evaluable (no market data wired)",
                  "- this is a persisted snapshot, not a live session", "Severity includes CRITICAL and WARNING."]
        return LLMResponse(text="\n".join(lines))


def _call(name, args, cid):
    return LLMResponse(text=None, tool_calls=[LLMToolCall(id=cid, name=name, arguments=args)])


# ---------------------------------------------------------------- verification
class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, ok: bool, detail: str, required: bool = True) -> None:
        self.rows.append({"check": name, "ok": bool(ok), "required": required, "detail": detail})
        mark = "PASS" if ok else ("FAIL" if required else "WARN")
        print(f"  [{mark}] {name}: {detail}")

    @property
    def required_failures(self) -> int:
        return sum(1 for r in self.rows if r["required"] and not r["ok"])


def tool_ids(calls: list[ToolCallRecord]) -> set[str]:
    return set(HEX32.findall(json.dumps([c.result_summary for c in calls if not c.is_error], default=str)))


def verify(run, expected: dict[str, str], store: MonitoringStore, agent_store: MonitoringAgentRunStore,
           before: dict, after: dict) -> Report:
    rep = Report()
    calls = run.tool_calls
    ok_calls = [c for c in calls if not c.is_error]
    report = run.final_report

    rep.add("agent run completed", run.status == "COMPLETED", f"status={run.status} error={run.error}")
    rep.add("only read tools were used", all(c.tool_name in READ_TOOLS for c in calls),
            f"tools={sorted({c.tool_name for c in calls})}")
    rep.add("no policy/unknown-tool rejections", not any(c.error_code in ("POLICY_REJECTED", "UNKNOWN_TOOL") for c in calls),
            f"errors={[c.error_code for c in calls if c.is_error]}", required=False)
    run_tools = ("run_monitoring_checks", "get_monitoring_run")
    runs = [c for c in ok_calls if c.tool_name in run_tools and c.result_summary.get("run_id")]
    rep.add("agent obtained a monitoring run's results (fresh run or stored run)", len(runs) >= 1,
            f"{[c.tool_name for c in runs]}")
    rep.add("the run it read exists in the monitoring store",
            bool(runs) and all(store.get_run(c.result_summary["run_id"]) is not None for c in runs), "")

    # --- findings retrieved through tools
    seen: dict[str, str] = {}  # rule_id -> finding_id as returned by tools
    for c in ok_calls:
        data = c.result_summary
        items = data.get("findings") if isinstance(data.get("findings"), list) else ([data] if c.tool_name == "get_finding" else [])
        for f in items:
            if isinstance(f, dict) and f.get("rule_id") and f.get("finding_id"):
                seen[f["rule_id"]] = f["finding_id"]
    missing = sorted(set(expected) - set(seen))
    rep.add("expected findings surfaced via tools", not missing,
            f"expected={sorted(expected)} seen={sorted(seen)} missing={missing}")
    drilled = [c for c in ok_calls if c.tool_name == "get_finding"]
    rep.add("agent fetched finding detail (get_finding)", len(drilled) >= 1,
            f"{len(drilled)} get_finding call(s); evidence returned: "
            f"{sum(len(c.result_summary.get('evidence', [])) for c in drilled)} item(s)")
    run_status = runs[0].result_summary.get("status") if runs else None
    rep.add("tool reported non-healthy run status", run_status == "CRITICAL", f"run status={run_status}")
    not_eval = sorted({c2.get("check_id") for c in runs for c2 in c.result_summary.get("checks", [])
                       if c2.get("status") == "NOT_EVALUABLE"})

    # --- citations
    returned = tool_ids(calls)
    rep.add("final report exists", report is not None, "")
    if report is not None:
        cited = set(HEX32.findall(" ".join(report.observed_facts) + " " + report.summary))
        expected_fids = {seen[r] for r in expected if r in seen}
        rep.add("report cites every expected finding id", expected_fids <= cited,
                f"cited {len(expected_fids & cited)}/{len(expected_fids)}")
        rep.add("every cited id was returned by a tool (no invented ids)", cited <= returned,
                f"invented={sorted(cited - returned)}")
        rep.add("report links the finding ids in its structured fields", expected_fids <= set(report.finding_ids),
                f"report.finding_ids has {len(report.finding_ids)}")
        rep.add("observed facts are separated from hypotheses",
                bool(report.observed_facts) and all(f.strip() for f in report.observed_facts),
                f"{len(report.observed_facts)} observed, {len(report.hypotheses)} hypothesis, "
                f"{len(report.suggested_investigations)} suggested")
        text = (report.summary or "").lower()
        rep.add("report states severity rather than calling the book healthy",
                ("critical" in text or "warning" in text) and "all clear" not in text,
                "mentions CRITICAL/WARNING" if ("critical" in text or "warning" in text) else "no severity wording")
        # --- limitations
        lims = " ".join(report.limitations).lower()
        lims = " ".join(report.limitations).lower()
        rep.add("limitations are reported", bool(report.limitations), f"{len(report.limitations)} limitation(s)")
        rep.add("every not-evaluable check the tools reported appears as a limitation",
                all(cid.lower() in lims for cid in not_eval),
                f"not evaluable per tools: {not_eval or 'none'}")
        rep.add("no 'unverified id' limitation was triggered",
                not any("unverified" in l for l in report.limitations), "", required=True)
        model_lims = [l for l in report.limitations if "was not evaluable" not in l and "unverified" not in l
                      and "No monitoring tool" not in l and "is UNKNOWN" not in l]
        rep.add("model itself stated at least one limitation of its own", bool(model_lims),
                f"{len(model_lims)} model-authored", required=False)
        rep.add("no trade/order advice in report",
                not re.search(r"\b(buy|sell|short|place an order|submit an order)\b", " ".join(
                    report.suggested_investigations + report.hypotheses).lower()),
                "", required=False)

    # --- lifecycle + audit
    stored = store.list_findings(status="OPEN", limit=50)
    extra = sorted({f.rule_id for f in stored} - set(expected))
    rep.add("stored OPEN findings include every expected rule",
            set(expected) <= {f.rule_id for f in stored},
            f"stored={sorted(f.rule_id for f in stored)}" + (f" (extra, not seeded: {extra})" if extra else ""))
    rep.add("monitoring run ids in the audit trail resolve in the monitoring store",
            bool(run.monitoring_run_ids) and all(store.get_run(i) is not None for i in run.monitoring_run_ids),
            f"{len(run.monitoring_run_ids)} linked")
    loaded = agent_store.load(run.run_id)
    rep.add("agent audit record persisted and reloadable", loaded is not None and loaded.tool_call_count == len(calls), "")
    rep.add("audit record stores no reasoning/transcript",
            not any(k in json.loads((agent_store._base_dir / f"{run.run_id}.json").read_text())
                    for k in ("messages", "reasoning", "thinking")), "")

    # --- read-only
    for key in before:
        rep.add(f"unchanged: {key}", before[key] == after[key],
                "identical" if before[key] == after[key] else f"BEFORE={before[key]!r} AFTER={after[key]!r}")
    return rep


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="offline harness self-check, no API call")
    ap.add_argument("--stored", action="store_true", help="with --fake: inspect a pre-stored run instead of running checks")
    ap.add_argument("--workdir", default=None, help="keep artifacts here (default: new temp dir)")
    args = ap.parse_args(argv)

    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="monitor_live_smoke_"))
    work.mkdir(parents=True, exist_ok=True)
    reg_path = work / "registry.db"
    registry = ExperimentRegistry(db_path=reg_path)
    exp = seed(registry)
    print(f"Isolated workdir: {work}\nSeeded experiment {exp}: QQQ 80 sh @100, cash 2000 (80% exposure vs 50% ceiling), "
          f"30-point equity curve with ~15% drawdown\n")

    # independent ground truth: deterministic service, separate in-memory store, no LLM
    truth = MonitoringService(RegistryStateSource(registry, experiment_id=exp), MonitoringStore(":memory:"))
    t_run = truth.run_once()
    expected = {f.rule_id: f.severity.value for f in (truth.store.get_finding(i) for i in t_run.finding_ids)}
    print(f"Deterministic ground truth: run status {t_run.status.value}; findings {expected}")
    if set(expected) != {"risk.portfolio_exposure", "drawdown.portfolio"}:
        print("seed did not produce the intended findings; aborting")
        return 2

    if args.fake:
        provider = ScriptedProvider(stored=args.stored)
    else:
        try:
            from src.ai.agents.anthropic_provider import AnthropicLLMProvider

            provider = AnthropicLLMProvider()
        except ProviderError as exc:
            print(f"cannot run live: {exc.code}: {exc.message}")
            return 2

    mon_store = MonitoringStore(work / "monitoring.db")
    if args.fake and args.stored:
        MonitoringService(RegistryStateSource(registry, experiment_id=exp), mon_store).run_once()
    service = MonitoringService(RegistryStateSource(registry, experiment_id=exp), mon_store)
    agent_store = MonitoringAgentRunStore(work / "agent_runs")
    agent = MarketMonitoringAgent(provider, default_monitoring_tool_registry(service),
                                  MarketMonitoringAgentPolicy(), run_store=agent_store)
    before = fingerprint(registry, exp, reg_path)
    print(f"\nRunning agent ({provider.name}/{provider.model}) ...")
    run = agent.run(GOAL)
    after = fingerprint(ExperimentRegistry(db_path=reg_path), exp, reg_path)

    print(f"\nAgent status={run.status} steps={run.step_count} tool_calls={run.tool_call_count} "
          f"monitoring_runs={run.monitoring_run_count}")
    for c in run.tool_calls:
        print(f"  {'x' if c.is_error else '->'} {c.tool_name}({c.arguments})" + (f" {c.error_code}" if c.is_error else ""))
    if run.error:
        print(f"Error: {run.error}")
        if run.status == "FAILED" and not run.tool_calls:
            return 2
    if run.final_report:
        r = run.final_report
        print("\n--- agent report ---")
        for title, items in (("Observed", r.observed_facts), ("Hypotheses", r.hypotheses),
                             ("Suggested", r.suggested_investigations), ("Limitations", r.limitations)):
            print(f"{title}:")
            for i in items:
                print(f"  - {i}")
        print(f"Summary text:\n{r.summary}\n")

    print("--- verification ---")
    rep = verify(run, expected, mon_store, agent_store, before, after)
    (work / "live_smoke_result.json").write_text(json.dumps(
        {"mode": "fake" if args.fake else "live", "provider": provider.name, "model": provider.model,
         "agent_run_id": run.run_id, "checked_at": datetime.now(timezone.utc).isoformat(),
         "expected_findings": expected, "results": rep.rows}, indent=2))
    n_req = sum(1 for r in rep.rows if r["required"])
    print(f"\n{n_req - rep.required_failures}/{n_req} required checks passed "
          f"({'FAKE harness self-check' if args.fake else 'LIVE provider'}). Result: {work / 'live_smoke_result.json'}")
    return 0 if rep.required_failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
