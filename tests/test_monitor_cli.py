"""Tests for the deterministic monitoring CLI (src/cli/monitor.py).

No LLM provider or API key is needed (or used) anywhere in this file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd

from src.cli.monitor import run_monitor
from src.experiments.registry import ExperimentRegistry
from src.monitoring import (
    MonitoringService,
    MonitoringStore,
    PlatformState,
    RegistryStateSource,
    StaticStateSource,
)
from src.portfolio.models import Portfolio
from src.portfolio.position import Position, PositionSide

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 7, 21, 30, tzinfo=timezone.utc)


def _factory_for(state=None, registry=None):
    def factory(experiment_id, db_path):
        source = RegistryStateSource(registry, experiment_id=experiment_id) if registry is not None else StaticStateSource(
            state or PlatformState(as_of=NOW, source="test"))
        return MonitoringService(source, MonitoringStore(":memory:"), clock=lambda: NOW)

    return factory


def test_monitor_runs_without_any_llm_key_and_reports_structure(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = run_monitor(["--once"], service_factory=_factory_for())
    out = capsys.readouterr().out
    assert "Monitoring run:" in out and "Run status: UNKNOWN" in out
    assert "Checks evaluated: 0 / 7" in out
    assert "Unknown/not evaluable: 7" in out
    assert "does not mean the system is healthy" in out
    assert rc == 3  # EXIT_UNKNOWN


def test_empty_registry_is_reported_honestly_not_replaced_by_a_sample_portfolio(tmp_path, capsys):
    registry = ExperimentRegistry(db_path=tmp_path / "empty.db")
    rc = run_monitor(["--once"], service_factory=_factory_for(registry=registry))
    out = capsys.readouterr().out
    assert "no experiment with a saved paper portfolio" in out
    assert "Findings: none" in out
    assert rc == 3


def test_real_registry_state_is_used_when_a_paper_portfolio_exists(tmp_path, capsys):
    registry = ExperimentRegistry(db_path=tmp_path / "e.db")
    exp_id = registry.log_experiment(strategy_name="ema_cross", changed={}, metrics_before={}, metrics_after={},
                                     decision="KEEP")
    pos = Position(symbol="QQQ", side=PositionSide.LONG, quantity=10, entry_price=100.0,
                   entry_timestamp=pd.Timestamp("2026-10-01", tz="UTC"), entry_signal_id=uuid4())
    registry.save_portfolio(exp_id, Portfolio.reconstruct(cash=9000.0, positions=[pos]))
    rc = run_monitor(["--once"], service_factory=_factory_for(registry=registry))
    out = capsys.readouterr().out
    assert f"experiment_id={exp_id}" in out
    assert "[HEALTHY] portfolio_valuation" not in out  # no market data service configured -> honest unknown
    assert "persisted portfolio snapshot" in out
    assert rc in (1, 3)


def test_json_output_is_structured(capsys):
    rc = run_monitor(["--once", "--json"], service_factory=_factory_for())
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNKNOWN" and payload["counts"]["checks_run"] == 7
    assert {c["check_id"] for c in payload["checks"]} >= {"market_data_freshness", "risk_exposure"}
    assert rc == 3


def test_once_is_required_because_no_daemon_exists(capsys):
    assert run_monitor([], service_factory=_factory_for()) == 2
    assert "on-demand" in capsys.readouterr().out


def test_failure_to_run_returns_an_error_code(capsys):
    def boom(experiment_id, db_path):
        raise RuntimeError("cannot open registry")

    assert run_monitor(["--once"], service_factory=boom) == 2
    assert "failed to run" in capsys.readouterr().out


def test_persistence_failure_is_a_nonzero_exit(capsys):
    class Broken(MonitoringStore):
        def apply(self, run, findings):
            raise OSError("read-only filesystem")

    def factory(experiment_id, db_path):
        return MonitoringService(StaticStateSource(PlatformState(as_of=NOW, source="t")), Broken(":memory:"),
                                 clock=lambda: NOW)

    assert run_monitor(["--once"], service_factory=factory) == 2
    assert "persisted: False" in capsys.readouterr().out


def test_monitor_module_imports_without_llm_or_ui_or_ml_packages():
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('anthropic', 'streamlit', 'joblib', 'sklearn', 'yfinance_blocked'):\n"
        "            raise ImportError('blocked ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import src.cli.monitor, src.monitoring\n"
        "assert 'src.ai.agents' not in sys.modules, 'monitoring pulled in the agent runtime'\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                         env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{REPO_ROOT}:/tmp/stubs"},
                         capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr[-500:]
