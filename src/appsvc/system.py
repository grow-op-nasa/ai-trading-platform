"""Read-only system and readiness status (Sprint 19). Never exposes a secret; never calls a provider."""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.appsvc.paths import AppPaths

OK, WARN, FAIL, UNKNOWN, DISABLED = "OK", "WARN", "FAIL", "UNKNOWN", "DISABLED"
MIN_PYTHON = (3, 12)
_REQUIRED = ("streamlit", "pandas", "numpy", "yfinance", "loguru")
_OPTIONAL = {"anthropic": "only the explicit AI-agent CLI commands use it"}


@dataclass(frozen=True)
class StatusItem:
    name: str
    state: str           # OK | WARN | FAIL | UNKNOWN | DISABLED
    detail: str
    help: str = ""


def _git_commit(root: Path) -> str | None:
    """Short commit id read from .git without running git (no subprocess)."""
    try:
        head = (root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            p = root / ".git" / ref
            if p.is_file():
                return p.read_text().strip()[:7]
            for line in (root / ".git" / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:7]
            return None
        return head[:7]
    except OSError:
        return None


def scheduler_locks(directory: Path) -> list[tuple[str, bool, str]]:
    """(schedule id, running?, holder text) for each scheduler lock file. A shared, non-blocking flock probe: it
    fails only while a scheduler holds the exclusive lock, and it never writes to the file."""
    out: list[tuple[str, bool, str]] = []
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return out
    if not directory.is_dir():
        return out
    for p in sorted(directory.glob("monitoring_scheduler.*.lock")):
        sid = p.name[len("monitoring_scheduler."):-len(".lock")]
        try:
            with open(p, "r") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError:
                    out.append((sid, True, fh.read().strip() or "holder unknown"))
                    continue
                fcntl.flock(fh, fcntl.LOCK_UN)
                out.append((sid, False, ""))
        except OSError:
            out.append((sid, False, "lock file unreadable"))
    return out


def collect_status(paths: AppPaths | None = None, *, env: Mapping[str, str] | None = None,
                   root: Path | None = None) -> list[StatusItem]:
    paths = paths or AppPaths()
    env = os.environ if env is None else env
    root = root or Path(__file__).resolve().parents[2]
    items: list[StatusItem] = []
    add = lambda *a, **k: items.append(StatusItem(*a, **k))  # noqa: E731

    v = sys.version_info
    add("Python", OK if (v.major, v.minor) >= MIN_PYTHON else FAIL, f"{v.major}.{v.minor}.{v.micro} (requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
        "" if (v.major, v.minor) >= MIN_PYTHON else "Recreate the virtual environment with Python 3.12 or newer (scripts/setup_app.sh).")
    missing = [m for m in _REQUIRED if importlib.util.find_spec(m) is None]
    add("Dependencies", FAIL if missing else OK, ("missing: " + ", ".join(missing)) if missing else "all required packages importable",
        "Run scripts/setup_app.sh (or pip install -r requirements.txt inside the virtual environment)." if missing else "")
    for mod, why in _OPTIONAL.items():
        found = importlib.util.find_spec(mod) is not None
        add(f"Optional: {mod}", OK if found else DISABLED, ("installed; " if found else "not installed; ") + why)
    commit = _git_commit(root)
    add("Version", OK if commit else UNKNOWN, f"git commit {commit}" if commit else "git commit not readable (not a git checkout?)")

    # persistence -- reading never creates anything
    if paths.registry_exists():
        try:
            from src.experiments.registry import ExperimentRegistry
            n = ExperimentRegistry(paths.registry).count()
            add("Experiment registry", OK, f"{paths.registry} ({n} experiment(s))")
        except Exception as exc:  # noqa: BLE001
            add("Experiment registry", FAIL, f"{paths.registry} unreadable: {type(exc).__name__}: {str(exc)[:120]}")
    else:
        add("Experiment registry", WARN, f"not found at {paths.registry} (nothing is created implicitly)",
            "Create experiments with scripts/run_experiment.py; the app never creates the registry.")
    if paths.paper_exists():
        try:
            from src.paper.store import PaperStore
            sv = PaperStore.open_existing(paths.paper_db).schema_version()
            add("Paper database", OK, f"{paths.paper_db} (schema v{sv})")
        except Exception as exc:  # noqa: BLE001
            add("Paper database", FAIL, f"{paths.paper_db}: {type(exc).__name__}: {str(exc)[:120]}")
    else:
        add("Paper database", WARN, f"not found at {paths.paper_db}", "It is created by the explicit 'Initialise paper account' action (or `paper init`).")
    add("Monitoring database", OK if paths.monitoring_exists() else WARN,
        str(paths.monitoring_db) + ("" if paths.monitoring_exists() else " (not created yet; created by the first monitoring run)"))

    add("Market-data source", UNKNOWN, "yfinance (public provider, no key). Health is not checked here: no request is made until you run a preflight or valuation.")
    key = bool(env.get("ANTHROPIC_API_KEY"))
    add("AI provider", OK if key else DISABLED, "ANTHROPIC_API_KEY is set (value never shown)" if key else "no key configured -- not needed for the dashboard, monitoring, preflight or paper cycles")

    try:
        from src.monitoring.alerts.config import AlertConfig, AlertConfigError
        try:
            cfg = AlertConfig.from_env(env)
        except AlertConfigError as exc:
            add("Alert delivery", FAIL, f"invalid configuration: {str(exc)[:160]}", "See README 'Alerts' for the ATP_ALERT_* variables.")
        else:
            if not cfg.enabled:
                add("Alert delivery", DISABLED, "ATP_ALERTS_ENABLED is not true: no alert events are recorded and nothing is sent")
            else:
                problems = cfg.problems()
                add("Alert delivery", FAIL if problems else WARN,
                    ("; ".join(problems)[:200]) if problems else
                    f"enabled via {cfg.transport}; configuration is valid. Valid configuration does not prove delivery works.",
                    "Use `alerts test-send` (explicit, guarded) to verify a real mail path.")
    except Exception as exc:  # noqa: BLE001
        add("Alert delivery", UNKNOWN, f"could not evaluate: {type(exc).__name__}")

    locks = scheduler_locks(paths.monitoring_db.parent)
    if not locks:
        add("Monitoring scheduler", UNKNOWN, "no scheduler lock file found: no scheduler has run from this data directory, or none is running. The app does not start one.")
    for sid, running, holder in locks:
        add(f"Monitoring scheduler {sid}", OK if running else WARN, f"RUNNING ({holder})" if running else "not running (lock file present, not held)")
    return items


def overall(items: list[StatusItem]) -> str:
    states = {i.state for i in items}
    return FAIL if FAIL in states else WARN if (WARN in states) else OK
