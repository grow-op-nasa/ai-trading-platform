"""Sprint 15 live monitoring findings integration test (manual, not pytest).

Runs the project's REAL CLIs end to end in an isolated working directory:

    python -m src.cli monitor --once --json
    python -m src.cli market-monitor-agent --goal "..."

Both CLIs resolve their data through paths relative to the working
directory (data/experiments.db, data/monitoring/monitoring.db,
data/monitoring_agent_runs/), so running them with cwd=<temp dir> gives them
the SAME persistent data source while never touching your real data.

Phases: seed a persisted paper portfolio through the registry's own save
methods -> baseline -> deterministic `monitor --once` -> real-provider
agent run (needs ANTHROPIC_API_KEY in the environment; never printed, and
any occurrence in captured output is redacted) -> verification and
read-only comparison -> PASS / PARTIAL / FAIL.

    python scripts/monitoring_live_integration.py                # phases 1-5
    python scripts/monitoring_live_integration.py --skip-agent   # phases 1-3 only (no key needed)

No fake provider is used here, nothing is committed or pushed, no broker
is contacted, and findings are never written directly to the store.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import monitoring_agent_live_smoke as smoke  # noqa: E402  (reuses seed/fingerprint/verify)
from src.ai.agents.monitoring import MonitoringAgentRunStore  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402
from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource  # noqa: E402

GOAL = (
    "Inspect the most recent stored monitoring run and its findings. For each finding, fetch the full "
    "detail and explain what the evidence shows. Keep observed facts (cite finding and run ids), "
    "hypotheses, and suggested further investigations clearly separated, and state everything you "
    "could not evaluate and why. Do not recommend trades or any action on the portfolio."
)
KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def redact(text: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return KEY_PATTERN.sub("[REDACTED]", text)


def sh(cmd: list[str], cwd: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    return p.returncode, redact(p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else ""))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True).stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-agent", action="store_true", help="stop after the deterministic phases")
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args(argv)
    result: dict = {"started": datetime.now(timezone.utc).isoformat(), "phases": {}}

    # ---- Phase 1: repo state ------------------------------------------------
    commit, dirty = git("rev-parse", "--short", "HEAD"), git("status", "--short")
    print(f"[1] commit {commit}; working tree: {'clean' if not dirty else 'dirty -> ' + dirty.replace(chr(10), '; ')}")
    result["commit"], result["working_tree"] = commit, dirty or "clean"

    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="monitor_integration_"))
    (work / "data").mkdir(parents=True, exist_ok=True)
    print(f"    isolated cwd for both CLIs: {work}")
    reg_path = work / "data" / "experiments.db"
    mon_db = work / "data" / "monitoring" / "monitoring.db"
    runs_dir = work / "data" / "monitoring_agent_runs"

    # ---- Phase 2: isolated persisted paper portfolio -------------------------
    registry = ExperimentRegistry(db_path=reg_path)
    exp = smoke.seed(registry)
    extra_state = lambda r: {"trades": len(r.get_trades(exp)), "signals": len(r.get_signals(exp)),  # noqa: E731
                             "has_research_report": r.get_research_report(exp) is not None}
    before = smoke.fingerprint(registry, exp, reg_path)
    before_exec = extra_state(registry)
    truth_svc = MonitoringService(RegistryStateSource(registry, experiment_id=exp), MonitoringStore(":memory:"))
    truth = truth_svc.run_once()
    expected = {f.rule_id: f.severity.value for f in (truth_svc.store.get_finding(i) for i in truth.finding_ids)}
    print(f"[2] seeded experiment {exp} (QQQ 80 sh @100, cash 2000, 50% exposure ceiling, 30-pt equity curve)")
    print(f"    baseline: cash={before['cash']} positions={before['positions']} risk={before['spec_risk_config']} "
          f"strategies={len(before['strategies'])} execution/state rows={before_exec}")
    print(f"    independent deterministic expectation (no LLM, separate in-memory store): {expected}")
    result["seed"] = {"experiment_id": exp, "expected_findings": expected, "baseline_execution_state": before_exec}
    if not expected:
        print("    seed produced no findings -> test cannot exercise the findings path; stopping")
        result["verdict"] = "FAIL"
        return finish(result, work, 1)

    # ---- Phase 3: real `monitor --once` CLI -----------------------------------
    cmd3 = [sys.executable, "-m", "src.cli", "monitor", "--once", "--json", "--experiment-id", str(exp)]
    print(f"[3] $ {' '.join(['python', '-m', 'src.cli', 'monitor', '--once', '--json', '--experiment-id', str(exp)])}   (cwd=isolated dir)")
    rc3, out3 = sh(cmd3, work)
    try:
        rep3 = json.loads(out3[: out3.rindex("}") + 1])
    except Exception:  # noqa: BLE001
        print(f"    could not parse monitor output (exit {rc3}):\n{out3[:1500]}")
        result["verdict"] = "FAIL"
        return finish(result, work, 1)
    store = MonitoringStore(mon_db)
    persisted = store.list_findings(limit=50)
    cli_fids = {f.finding_id for f in persisted}
    nev = [c for c in rep3["checks"] if c["status"] == "NOT_EVALUABLE"]
    ph3 = {
        "exit_code": rc3, "run_id": rep3["run_id"], "status": rep3["status"],
        "check_statuses": {c["check_id"]: c["status"] for c in rep3["checks"]},
        "persisted_finding_count": len(persisted),
        "persisted_rules": sorted(f.rule_id for f in persisted),
    }
    print(f"    exit={rc3} run_id={rep3['run_id']} status={rep3['status']} persisted findings={len(persisted)}")
    for c in rep3["checks"]:
        print(f"      [{c['status']}] {c['check_id']}" + (f" -- {c['reason']}" if c.get("reason") else ""))
    checks3 = [
        ("exit code signals attention (1) for CRITICAL/WARNING", rc3 == 1),
        ("run persisted", bool(rep3["persisted"]) and store.get_run(rep3["run_id"]) is not None),
        ("expected findings persisted and retrievable from the DB", set(expected) <= {f.rule_id for f in persisted}),
        ("findings not fabricated: each persisted finding has evidence", all(f.evidence for f in persisted)),
        ("NOT_EVALUABLE checks are not reported healthy",
         all(c["status"] != "HEALTHY" for c in nev) and (not nev or rep3["status"] != "HEALTHY")),
    ]
    for name, ok in checks3:
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
    ph3["checks"] = {n: ok for n, ok in checks3}
    result["phases"]["3_deterministic_monitor"] = ph3
    if not all(ok for _, ok in checks3):
        result["verdict"] = "FAIL"
        return finish(result, work, 1)
    open_before = {f.fingerprint: f.finding_id for f in store.list_findings(status="OPEN", limit=50)}

    if args.skip_agent:
        print("\n--skip-agent: phases 1-3 done; the real-provider phases were NOT run -> overall PARTIAL")
        result["verdict"] = "PARTIAL"
        return finish(result, work, 0)

    # ---- Phase 4: real provider through the real CLI --------------------------
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[4] ANTHROPIC_API_KEY is not set in this environment; real-provider phase NOT run -> PARTIAL")
        result["verdict"] = "PARTIAL"
        result["note"] = "provider phase not executed: no API key in environment"
        return finish(result, work, 0)
    print("[4] $ python -m src.cli market-monitor-agent --goal \"<goal>\" --experiment-id", exp, "  (cwd=isolated dir, key from env)")
    rc4, out4 = sh([sys.executable, "-m", "src.cli", "market-monitor-agent", "--goal", GOAL,
                    "--experiment-id", str(exp)], work)
    print(out4)
    m = re.search(r"Agent Run ID:\s*([0-9a-f]{32})", out4)
    if not m:
        print("    no agent run id in output (configuration or startup failure) -> PARTIAL")
        result["verdict"] = "PARTIAL"
        result["agent_cli_exit"] = rc4
        return finish(result, work, 2)
    agent_store = MonitoringAgentRunStore(runs_dir)
    run = agent_store.load(m.group(1))
    if run is None:
        print("    audit record missing -> FAIL")
        result["verdict"] = "FAIL"
        return finish(result, work, 1)
    if run.status == "FAILED" and not run.tool_calls:
        print(f"    provider call failed before any tool use ({run.error}) -> the test could not run: PARTIAL, not a system failure")
        result.update({"verdict": "PARTIAL", "agent_run_id": run.run_id, "agent_error": redact(str(run.error)),
                       "note": "provider/API failure before any tool call; findings path not exercised"})
        return finish(result, work, 2)
    is_real = run.provider == "anthropic" and not run.model.startswith("fake")
    print(f"    agent run {run.run_id}: status={run.status} provider/model={run.provider}/{run.model} "
          f"audit={runs_dir / (run.run_id + '.json')}")

    # ---- Phase 5: verification ---------------------------------------------------
    print("[5] verification")
    after_reg = ExperimentRegistry(db_path=reg_path)
    after = smoke.fingerprint(after_reg, exp, reg_path)
    after_exec = extra_state(after_reg)
    vrep = smoke.verify(run, expected, MonitoringStore(mon_db), agent_store, before, after)
    open_after = {f.fingerprint: f.finding_id for f in MonitoringStore(mon_db).list_findings(status="OPEN", limit=50)}
    sup = smoke.Report()
    sup.add("provider is the real Anthropic provider (not fake)", is_real, f"{run.provider}/{run.model}")
    seen_ids = smoke.tool_ids(run.tool_calls)
    inspected = sorted(set(open_before.values()) & seen_ids)
    sup.add("agent inspected genuine persisted findings from the earlier CLI run", bool(inspected),
            f"{len(inspected)} of {len(open_before)} pre-existing finding ids appear in tool results")
    sup.add("agent did not create duplicate findings (lifecycle dedupe: same ids reused)",
            open_before.items() <= open_after.items(), f"open before={len(open_before)} after={len(open_after)}")
    sup.add("execution/state rows unchanged (trades, signals, research report)", before_exec == after_exec,
            f"{before_exec} -> {after_exec}")
    sup.add("no budget interruption", run.status == "COMPLETED", f"status={run.status}")
    rows = vrep.rows + sup.rows
    req_fail = [r for r in rows if r["required"] and not r["ok"]]
    verdict = "PASS" if (not req_fail and is_real and inspected) else ("FAIL" if req_fail else "PARTIAL")
    print(f"\nRESULT: {verdict} ({sum(1 for r in rows if r['required'] and r['ok'])}/"
          f"{sum(1 for r in rows if r['required'])} required checks)")
    result.update({
        "verdict": verdict, "agent_run_id": run.run_id, "provider": run.provider, "model": run.model,
        "monitoring_run_ids": run.monitoring_run_ids, "finding_ids_in_report": (run.final_report.finding_ids if run.final_report else []),
        "audit_record": str(runs_dir / f"{run.run_id}.json"), "checks": rows,
    })
    return finish(result, work, 0 if verdict == "PASS" else 1)


def finish(result: dict, work: Path, code: int) -> int:
    result["finished"] = datetime.now(timezone.utc).isoformat()
    out = work / "integration_result.json"
    out.write_text(json.dumps(result, indent=2, default=str))
    print(f"\nVerdict: {result.get('verdict')}   Result file (no secrets): {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
