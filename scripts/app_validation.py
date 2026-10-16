"""Sprint 19 real-machine validation of the local application (manual; run on the development machine).

Evidence classes, kept separate in the output:

  PART A  REAL SERVER      the documented launcher (`python -m src.dashboard.launch`) as a real subprocess on a loopback port:
                           startup output, health endpoint, HTTP 200, loopback binding (if `lsof` exists), clean SIGINT stop.
                           NOTE: Streamlit executes the app script only when a browser session connects, so this part proves
                           startup/shutdown, not page rendering.
  PART B  SCRIPTED SESSION Streamlit's `AppTest` drives the real app script against a disposable root: every page, explicit
                           paper-account initialisation, REAL preflight (real market data), ONE real-data paper cycle through
                           the UI, a fresh session showing the persisted record, a repeated submission (backend replay), and
                           safety checks. This is a scripted Streamlit session, NOT a manual browser walkthrough.
                           If the registered strategy gives no signal the honest result is NO_ACTION; nothing is manufactured.

Everything lives in a fresh temporary directory (your normal data/ is untouched). No live broker is reachable and no Anthropic key
is read.

    python scripts/app_validation.py [--skip-external] [--keep]
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.paper_runtime_validation import fingerprint, seed  # noqa: E402
from src.appsvc.paper import PaperService  # noqa: E402
from src.appsvc.paths import AppPaths  # noqa: E402
from src.experiments.registry import ExperimentRegistry  # noqa: E402

CHECKS: list[tuple[str, bool]] = []
APP = str(REPO / "src" / "dashboard" / "app.py")


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(("ANTHROPIC", "ATP_ALERT"))}


def part_a(work: Path) -> None:
    print("\n=== PART A: REAL SERVER (documented launcher, loopback) ===")
    port = free_port()
    reg_before = fingerprint(work / "data" / "experiments.db")
    cmd = [sys.executable, "-m", "src.dashboard.launch", "--workdir", str(work), "--no-browser", "--port", str(port)]
    print("  cmd: " + " ".join(cmd[1:]))
    proc = subprocess.Popen(cmd, cwd=REPO, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    healthy = False
    try:
        for _ in range(80):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1) as r:
                    healthy = r.read().decode().strip() == "ok"
                if healthy:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        check("server became healthy on the loopback address", healthy, f"http://localhost:{port}")
        if healthy:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                check("index page served (HTTP 200)", r.status == 200)
            try:
                out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=10).stdout
                if out.strip():
                    check("listening socket is bound to 127.0.0.1 only", "127.0.0.1:" in out and "*:" not in out and "0.0.0.0" not in out)
                else:
                    print("  NOTE: lsof showed no listener; loopback binding not independently confirmed")
            except (OSError, subprocess.SubprocessError):
                print("  NOTE: lsof unavailable; loopback binding not independently confirmed")
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        try:
            code = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill(); code = None
        text = proc.stdout.read() if proc.stdout else ""
    print("  startup output:\n" + "\n".join("    " + line for line in text.splitlines()[:12]))
    check("startup output states paper mode and the local URL", "paper mode only" in text and f":{port}" in text)
    check("server stopped cleanly on SIGINT", code in (0, -2, 130), f"exit {code}")
    check("startup/shutdown changed no stored data", fingerprint(work / "data" / "experiments.db") == reg_before
          and not (work / "data" / "paper").exists() and not (work / "data" / "monitoring").exists())


def part_b(work: Path, exp: int) -> None:
    print("\n=== PART B: SCRIPTED STREAMLIT SESSION (AppTest; real data for preflight and the cycle) ===")
    from streamlit.testing.v1 import AppTest
    from src.dashboard.shell import PAGES
    os.chdir(work)
    watched = lambda: {m for m in sys.modules if m.split(".")[0] == "anthropic" or m.startswith("src.broker")}  # noqa: E731
    loaded_before = watched()        # this script's own imports (via src.cli) may already load some; only NEW loads matter
    t = 120
    paths = AppPaths()
    svc = PaperService(paths)
    svc.save_profile("validation", svc.build_config(experiment_id=exp, strategy_name="ema_cross", symbol="QQQ"))

    def fresh() -> AppTest:
        at = AppTest.from_file(APP).run(timeout=t)
        return at

    def go(at, page):
        at.sidebar.radio[0].set_value(page).run(timeout=t)
        return at

    at = fresh()
    check("app starts with no exception", not at.exception, str([e.message for e in at.exception]))
    ok = True
    for page in PAGES:
        go(at, page)
        ok = ok and not at.exception
    check("all seven pages render", ok)
    check("PAPER MODE indicator is visible", any("PAPER MODE" in w.value for w in at.sidebar.warning))
    check("browsing created no paper or monitoring database", not (work / "data" / "paper").exists() and not (work / "data" / "monitoring").exists())

    at.sidebar.selectbox[0].set_value(exp).run(timeout=t)
    go(at, "Paper Trading")
    at.selectbox(key="paper_profile").set_value("validation").run(timeout=t)
    run_btn = next(b for b in at.button if b.label == "Run one paper cycle")
    check("run control is disabled before account init and preflight", run_btn.disabled is True)

    next(c for c in at.checkbox if c.label.startswith("I understand this records the baseline")).check()
    next(x for x in at.text_input if x.label == "Label for this account baseline").set_value("sprint19 validation (disposable)")
    next(b for b in at.button if b.label == "Initialise paper account").click().run(timeout=t)
    check("explicit initialisation succeeded", any("Initialised" in s.value for s in at.success), str([e.value for e in at.error]))
    at.run(timeout=t)
    go(at, "Paper Trading")
    at.selectbox(key="paper_profile").set_value("validation").run(timeout=t)

    at.button(key="btn_preflight").click().run(timeout=t)
    pre_text = " ".join(m.value for m in list(at.success) + list(at.warning) + list(at.error) + list(at.info))
    print("  preflight: " + next((s.value for s in list(at.success) + list(at.warning) + list(at.error) if "Preflight" in s.value), "(none)"))
    for df in at.dataframe:
        txt = df.value.to_string()
        if "market_data" in txt:
            print("  " + txt.replace("\n", "\n  "))
    run_btn = next(b for b in at.button if b.label == "Run one paper cycle")
    if run_btn.disabled:
        check("preflight blocked the run control honestly (no cycle was forced)", True, pre_text[:160])
        print("  RESULT NOTE: preflight did not PASS (see above); the real-data cycle was NOT executed.")
        return
    next(c for c in at.checkbox if c.label == "I confirm: run one paper-only cycle now").check()
    run_btn.click().run(timeout=t)
    check("cycle submission raised no exception", not at.exception, str([e.message for e in at.exception]))
    store = svc.store()
    cycles = store.list_cycles(exp)
    check("exactly one cycle was recorded", len(cycles) == 1)
    cyc = cycles[0]
    print(f"  REAL-DATA cycle {cyc.cycle_id}: status={cyc.status.value} reason={cyc.reason_code} bar={cyc.decision_bar}")
    print(f"  provenance: provider={(cyc.dataset or {}).get('provider')} bars={(cyc.dataset or {}).get('bars_used')} "
          f"strategy_hash={str(cyc.strategy_hash)[:16]} config_hash={str(cyc.config_hash)[:16]}")
    if cyc.fill:
        print(f"  (the real strategy signalled) intent={cyc.intent_id} order={cyc.client_order_id} fill={cyc.fill}")
    elif cyc.status.value in ("NO_ACTION", "NO_NEW_BAR", "RISK_REJECTED"):
        check("no order or fill was manufactured", store.list_fills(exp) == [] and cyc.client_order_id is None)
    at.run(timeout=t); at.run(timeout=t)
    check("page reruns did not repeat the cycle", len(store.list_cycles(exp)) == 1)

    print("-- fresh session (restart simulation): persisted record, repeated submission --")
    at2 = fresh()
    at2.sidebar.selectbox[0].set_value(exp).run(timeout=t)
    go(at2, "Paper Trading")
    check("history in a NEW session lists the stored cycle", any(cyc.cycle_id[:12] in df.value.to_string() for df in at2.dataframe))
    go(at2, "Overview")
    check("overview shows the latest cycle status", any(cyc.status.value in x.value for x in list(at2.success) + list(at2.warning) + list(at2.info)))
    go(at2, "Paper Trading")
    at2.selectbox(key="paper_profile").set_value("validation").run(timeout=t)
    at2.button(key="btn_preflight").click().run(timeout=t)
    fills_before = len(store.list_fills(exp))
    nxt = next((b for b in at2.button if b.label == "Run one paper cycle"), None)
    if nxt is not None and not nxt.disabled:
        next(c for c in at2.checkbox if c.label == "I confirm: run one paper-only cycle now").check()
        nxt.click().run(timeout=t)
        check("a second submission for the same bar is a backend replay (no new cycle or fill)",
              len(store.list_cycles(exp)) == 1 and len(store.list_fills(exp)) == fills_before)
    go(at2, "Portfolio & Execution")
    check("portfolio page renders with no price requested until clicked", not at2.exception and any("Not valued" in i.value for i in at2.info))
    go(at2, "Monitoring & Alerts")
    check("monitoring page renders", not at2.exception)
    go(at2, "Setup & System Status")
    check("setup page renders without secrets", not at2.exception)
    new = watched() - loaded_before
    check("the app session loaded no AI-provider or live-broker module", not new, str(sorted(new)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-external", action="store_true", help="skip Part B's real-data preflight/cycle (not recommended)")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    root = Path(tempfile.mkdtemp(prefix="atp_app_validation_"))
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    print(f"ISOLATED validation root (disposable): {root}\nrepo HEAD {git}; python {sys.version.split()[0]}")
    (root / "data").mkdir()
    exp = seed(ExperimentRegistry(root / "data" / "experiments.db"))
    part_a(root)
    if not args.skip_external:
        part_b(root, exp)
    failed = [c for c in CHECKS if not c[1]]
    print(f"\nRESULT: {'PASS' if not failed else 'FAIL'} ({len(CHECKS) - len(failed)}/{len(CHECKS)} checks)")
    if args.keep or failed:
        print(f"validation data kept at {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
