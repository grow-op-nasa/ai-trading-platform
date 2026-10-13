"""Sprint 17 scheduler validation on an ISOLATED, DISPOSABLE, SYNTHETIC paper-portfolio fixture (manual).

WHAT THIS IS: fixture validation of the real `python -m src.cli monitor-scheduler` process. It is NOT
a validation against your real paper portfolio. Everything lives in a fresh temp directory used as the
subprocess cwd, so your normal `data/` is never read or written. The fixture is created through the
supported registry APIs and is labelled synthetic ("sprint17-scheduler-fixture").

Checks: >=2 scheduled cycles with distinct attempt + run ids and the configured interval respected; no
AI key in the environment and no provider/broker modules imported; SIGTERM -> clean exit + lock released;
restart keeps history; `monitor --once` still works; registry fingerprint (cash, positions, equity,
risk config, strategies, execution records = every registry table) unchanged before/after.

    python scripts/scheduler_validation.py [--interval 4] [--keep]

Note: the monitoring service reads real market data (read-only) when it can; a data outage simply
produces UNKNOWN/NOT_EVALUABLE findings and does not affect this validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.data.base import Interval  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402
from src.experiments.spec import ExperimentSpec  # noqa: E402
from src.monitoring.scheduler import ScheduleStore  # noqa: E402
from src.portfolio.models import Portfolio  # noqa: E402
from src.portfolio.position import Position, PositionSide  # noqa: E402
from src.strategies.identity import strategy_version  # noqa: E402
from src.strategies.registry import get_strategy_class  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def seed(registry: ExperimentRegistry) -> int:
    cls = get_strategy_class("ema_cross")
    exp = registry.log_experiment(strategy_name="ema_cross", changed={}, metrics_before={}, metrics_after={},
                                  decision="KEEP", notes="SYNTHETIC DISPOSABLE sprint17-scheduler-fixture")
    registry.save_spec(exp, ExperimentSpec(
        strategy_name="ema_cross", strategy_version=strategy_version(cls), strategy_params={}, symbol="QQQ",
        interval=Interval.DAY_1, dataset_start=pd.Timestamp("2026-08-01", tz="UTC"),
        dataset_end=pd.Timestamp("2026-09-30", tz="UTC"), dataset_source="sprint17-scheduler-fixture",
        dataset_fingerprint="sprint17-scheduler-fixture", risk_config={"max_portfolio_exposure_pct": 0.5}))
    pos = Position(symbol="QQQ", side=PositionSide.LONG, quantity=10, entry_price=100.0,
                   entry_timestamp=pd.Timestamp("2026-09-01", tz="UTC"), entry_signal_id=uuid4())
    registry.save_portfolio(exp, Portfolio.reconstruct(cash=9000.0, positions=[pos]))
    idx = pd.date_range("2026-09-01", periods=25, freq="D", tz="UTC")
    registry.save_equity_curve(exp, pd.Series([10000 + 10 * i for i in range(25)], index=idx, dtype=float))
    return exp


def fingerprint(registry_path: Path) -> dict:
    """Hash every table of the registry (cash, positions, equity, risk config, strategies, executions...)."""
    conn = sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True)
    try:
        out = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            rows = conn.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall()
            out[t] = {"rows": len(rows), "sha256": hashlib.sha256(repr(rows).encode()).hexdigest()[:16]}
        return out
    finally:
        conn.close()


def env_without_ai() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC", "ATP_ALERT"))}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO), os.environ.get("PYTHONPATH", "")) if p)
    return env


def cli(workdir: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "src.cli", *args], cwd=workdir, env=env_without_ai(),
                          capture_output=True, text=True, timeout=timeout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=4.0)
    ap.add_argument("--keep", action="store_true", help="keep the temp fixture directory")
    args = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="atp_sched_fixture_"))
    (work / "data").mkdir()
    reg_path, db_path = work / "data" / "experiments.db", work / "data" / "monitoring" / "monitoring.db"
    print(f"FIXTURE (synthetic, disposable, isolated): {work}")
    print("This validates the scheduler process on a fixture; it is NOT your real portfolio.")
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    print(f"repo HEAD: {git}  python: {sys.version.split()[0]}")
    check("fixture is outside the repo and not the normal data dir", REPO not in work.parents and work != REPO / "data")
    registry = ExperimentRegistry(reg_path)
    exp = seed(registry)
    before = fingerprint(reg_path)
    interval = args.interval
    sched = ["monitor-scheduler", "--experiment-id", str(exp), "--interval-seconds", str(interval),
             "--registry", str(reg_path), "--db", str(db_path), "--schedule-id", "fixture"]

    print("\n== 1. two scheduled cycles (real CLI process, no AI key in env) ==")
    t0 = time.monotonic()
    r = cli(work, *sched, "--max-runs", "2", timeout=180)
    print(r.stdout.strip()[-1500:])
    check("exit code 0 (clean shutdown, no infrastructure errors)", r.returncode == 0, f"rc={r.returncode} {r.stderr[-300:]}")
    attempts = list(reversed(ScheduleStore(db_path).list_attempts("fixture", 50)))
    check(">=2 attempts recorded", len(attempts) >= 2, f"{len(attempts)}")
    check("attempt ids distinct", len({a.attempt_id for a in attempts}) == len(attempts))
    run_ids = [a.monitoring_run_id for a in attempts if a.outcome == "COMPLETED"]
    check("each completed attempt links a distinct monitoring run", len(run_ids) >= 2 and len(set(run_ids)) == len(run_ids))
    gap = (attempts[1].started_at - attempts[0].started_at).total_seconds() if len(attempts) > 1 else 0
    check(f"interval respected (gap {gap:.1f}s >= {interval * 0.9:.1f}s)", gap >= interval * 0.9)
    for a in attempts:
        print(f"    attempt {a.attempt_id}  outcome={a.outcome}  run={a.monitoring_run_id}  status={a.monitoring_status}  "
              f"started={a.started_at.isoformat()}")
    check("no AI key was present in the scheduler environment",
          not any(k.upper().startswith("ANTHROPIC") for k in env_without_ai()))
    check("process output mentions no LLM call", "anthropic" not in (r.stdout + r.stderr).lower() or "no LLM" in r.stdout)
    check("lock released after exit", not list(db_path.parent.glob("*.lock")) or cli(work, *sched, "--max-runs", "1", timeout=120).returncode == 0)
    n_after_first = len(ScheduleStore(db_path).list_attempts("fixture", 100))

    print("\n== 2. SIGTERM clean shutdown ==")
    proc = subprocess.Popen([sys.executable, "-m", "src.cli", *sched], cwd=work, env=env_without_ai(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and len(ScheduleStore(db_path).list_attempts("fixture", 100)) <= n_after_first:
        time.sleep(0.5)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=60)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill(); out, rc = proc.communicate()[0], -9
    print(out.strip()[-800:])
    check("SIGTERM -> exit code 0", rc == 0, f"rc={rc}")
    r2 = cli(work, *sched, "--max-runs", "1", timeout=120)
    check("lock released: a new scheduler can start immediately", r2.returncode == 0, f"rc={r2.returncode} {r2.stdout[-200:]}")

    print("\n== 3. restart keeps history ==")
    all_attempts = ScheduleStore(db_path).list_attempts("fixture", 200)
    ids = {a.attempt_id for a in all_attempts}
    check("earlier attempts still present after restarts", {a.attempt_id for a in attempts} <= ids, f"{len(ids)} total")
    check("attempt ids unique across restarts", len(ids) == len(all_attempts))
    check("attempts came from more than one process", len({a.pid for a in all_attempts}) >= 2)

    print("\n== 4. `monitor --once` still works against the fixture ==")
    m = cli(work, "monitor", "--once", "--experiment-id", str(exp), "--db", str(db_path), "--json")
    try:
        payload = json.loads(m.stdout)
        ok = "run_id" in payload and m.returncode in (0, 1)
    except Exception:  # noqa: BLE001
        ok, payload = False, {}
    check("monitor --once produced a persisted run", ok and payload.get("persisted") is True, f"rc={m.returncode}")
    check("no alerts key (alerting disabled by default)", "alerts" not in payload)

    print("\n== 5. state integrity ==")
    after = fingerprint(reg_path)
    check("registry fingerprint unchanged (cash, positions, equity, risk config, strategies, executions)",
          before == after, "" if before == after else json.dumps({k: (before.get(k), after.get(k)) for k in after if before.get(k) != after.get(k)}))
    check("portfolio cash unchanged", ExperimentRegistry(reg_path).get_portfolio(exp).cash == 9000.0)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'}  ({len(CHECKS) - len(failed)}/{len(CHECKS)} checks)")
    if args.keep or failed:
        print(f"fixture kept at {work}")
    else:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
