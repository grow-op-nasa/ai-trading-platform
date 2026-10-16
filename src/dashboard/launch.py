"""Local launcher (Sprint 19): `python -m src.dashboard.launch` (wrapped by scripts/start_app.sh).

Checks Python and dependencies, loads `.env` (existing environment variables win; values are never printed), validates the
alert configuration (a problem is reported, not fatal), refuses a non-loopback bind unless --allow-remote is given, then
starts the existing Streamlit app in this process. Starting the app starts nothing else: no scheduler, no paper cycle,
no monitoring run, no email, no AI call, and it never creates or resets any database.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.config.envfile import load_env_file

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "dashboard" / "app.py"
DEFAULT_ADDRESS, DEFAULT_PORT = "127.0.0.1", 8501
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MIN_PYTHON = (3, 12)
REQUIRED = ("streamlit", "pandas", "numpy", "yfinance", "loguru")


def check_environment(version=None) -> list[str]:
    v = version or sys.version_info
    problems = []
    if (v[0], v[1]) < MIN_PYTHON:
        problems.append(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (found {v[0]}.{v[1]}). Re-run scripts/setup_app.sh with a newer Python.")
    missing = [m for m in REQUIRED if importlib.util.find_spec(m) is None]
    if missing:
        problems.append("missing packages: " + ", ".join(missing) + ". Run scripts/setup_app.sh (or: pip install -r requirements.txt in the virtual environment).")
    return problems


def port_free(address: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((address, port))
        except OSError:
            return False
    return True


def streamlit_argv(address: str, port: int, *, open_browser: bool) -> list[str]:
    return ["streamlit", "run", str(APP), "--server.address", address, "--server.port", str(port),
            "--server.headless", "false" if open_browser else "true", "--browser.gatherUsageStats", "false",
            "--server.fileWatcherType", "none"]


def main(argv: Sequence[str] | None = None, *, environ: Mapping | None = None, run: Callable[[list[str]], int] | None = None,
         emit: Callable[[str], None] = print) -> int:
    p = argparse.ArgumentParser(prog="python -m src.dashboard.launch", description="Start the local paper-mode application")
    p.add_argument("--address", default=DEFAULT_ADDRESS, help="bind address (default 127.0.0.1, this machine only)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.add_argument("--allow-remote", action="store_true", help="REQUIRED to bind a non-loopback address; the app has no authentication")
    p.add_argument("--workdir", default=None, help="directory holding data/ (default: the project root); must already exist. "
                   "Used for disposable validation roots; nothing is created there")
    p.add_argument("--check", action="store_true", help="run the startup checks and exit without starting the server")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.address not in LOOPBACK and not args.allow_remote:
        emit(f"launch: refusing to bind {args.address}: the app has no authentication and can run paper cycles. "
             "Use 127.0.0.1, or pass --allow-remote if you understand that anyone who can reach the port can use the app.")
        return 2
    if args.address not in LOOPBACK:
        emit(f"launch: WARNING binding {args.address} exposes the app (no authentication) beyond this machine.")
    problems = check_environment()
    if problems:
        for pr in problems:
            emit("launch: " + pr)
        return 2

    env = os.environ if environ is None else environ
    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else ROOT
    if not workdir.is_dir():
        emit(f"launch: --workdir {workdir} does not exist (it is never created for you)")
        return 2
    os.chdir(workdir)        # data/ paths are relative to the working directory, exactly as for the CLI
    loaded, warnings = load_env_file(ROOT / ".env", env)
    if loaded:
        emit(f"launch: loaded {len(loaded)} setting(s) from .env (names only: {', '.join(sorted(loaded))})")
    for w in warnings:
        emit("launch: WARNING " + w)
    try:
        from src.monitoring.alerts.config import AlertConfig, AlertConfigError
        try:
            cfg = AlertConfig.from_env(env)
            probs = cfg.problems() if cfg.enabled else []
            emit("launch: alerting " + ("disabled" if not cfg.enabled else "ENABLED but invalid: " + "; ".join(probs)[:200] if probs else "enabled and valid (delivery is not verified)"))
        except AlertConfigError as exc:
            emit(f"launch: alerting configuration invalid: {str(exc)[:200]} (the app still starts; alerts will not work)")
    except Exception as exc:  # noqa: BLE001
        emit(f"launch: could not evaluate alert configuration ({type(exc).__name__})")
    emit("launch: paper mode only -- no live broker is reachable from this app.")
    if args.check:
        emit("launch: checks passed (nothing started)")
        return 0
    if not port_free(args.address, args.port):
        emit(f"launch: port {args.port} on {args.address} is already in use. Is the app already running? Use --port to choose another.")
        return 3
    url = f"http://{'localhost' if args.address in LOOPBACK else args.address}:{args.port}"
    emit(f"launch: starting at {url}  (press Ctrl+C to stop; nothing else keeps running afterwards)")
    argv_st = streamlit_argv(args.address, args.port, open_browser=not args.no_browser)
    if run is not None:
        return run(argv_st)
    from streamlit.web import cli as stcli
    sys.argv = argv_st
    try:
        stcli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        emit("launch: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
