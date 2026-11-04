"""scripts/closeout_validation.py -- exercised on SYNTHETIC data only (no network), proving the harness's safety properties."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.appsvc.strategy_research import StrategyResearchService
from tests.test_strategy_research_service import SyntheticSource, synth

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "closeout_validation.py"


def load_script():
    spec = importlib.util.spec_from_file_location("closeout_validation", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["closeout_validation"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def harness(monkeypatch):
    mod = load_script()
    monkeypatch.setattr(mod, "SYMBOLS", ["SPY", "QQQ"])
    monkeypatch.setattr(mod, "STRATEGIES", ["ema_cross"])
    source = SyntheticSource(synth())
    original = StrategyResearchService.__init__

    def init(self, paths=None, *, market_data=None):
        original(self, paths, market_data=source)

    monkeypatch.setattr(mod.StrategyResearchService, "__init__", init)
    calls = []
    real = StrategyResearchService.evaluate_holdout
    monkeypatch.setattr(StrategyResearchService, "evaluate_holdout", lambda self, rid: calls.append(rid) or real(self, rid))
    return mod, calls


def test_harness_labels_synthetic_data_never_evaluates_the_holdout_and_writes_reports(harness, tmp_path):
    mod, holdout_calls = harness
    out_dir = tmp_path / "iso"
    rc = mod.main(["--start", "2019-01-02", "--end", "2022-06-30", "--data-dir", str(out_dir), "--train-bars", "252", "--test-bars", "63"])
    assert rc == 0 and holdout_calls == []
    report = json.loads((out_dir / "closeout_report.json").read_text())
    assert report["combinations"] == 2 and report["holdout"].startswith("LOCKED")
    for row in report["rows"]:
        assert row["data_label"] == "SYNTHETIC"                                 # never presented as real
        assert row["classification"] == "COMPLETED" and row["data_audit"]["ok"]
        assert row["reproduction"]["backtest"] == "REPRODUCED" and row["engine_version"] == 3
        assert row["holdout_status"].startswith("LOCKED") and "holdout" not in {k for k in row if k.endswith("scorecard")}
        assert row["walk_forward"]["folds"] >= 1 and len(row["spec_hash"]) == 64
    md = (out_dir / "closeout_report.md").read_text()
    assert "never" not in md.lower() or "No holdout figure" in md
    # The isolated directory holds the research database; no holdout rows exist in it.
    from src.strategy_research import ResearchStore
    store = ResearchStore(out_dir / "research" / "research.db") if (out_dir / "research" / "research.db").exists() else None
    if store is not None:
        assert all(store.get_holdout(r["run_id"]) is None for r in store.list_runs())


def test_harness_refuses_the_projects_own_data_directory():
    mod = load_script()
    assert mod.main(["--data-dir", str(mod.REPO / "data")]) == 2


def test_non_completed_outcomes_are_classified_not_hidden():
    mod = load_script()
    assert mod.classify("NOT_EVALUABLE", "DATA_INVALID", {}, None, None) == "DATA_QUALITY_FAILURE"
    assert mod.classify("NOT_EVALUABLE", "PROVIDER_UNAVAILABLE", {}, None, None) == "PROVIDER_AVAILABILITY"
    assert mod.classify("NOT_EVALUABLE", "INSUFFICIENT_HISTORY", {}, None, None) == "INSUFFICIENT_HISTORY"
    assert mod.classify("NOT_EVALUABLE", "UNSUPPORTED_TIMEFRAME", {}, None, None) == "NOT_SUPPORTED"
    assert mod.classify("NOT_EVALUABLE", "SOMETHING_NEW", {}, None, None) == "STRATEGY_OR_ENGINE_DEFECT"
    assert mod.classify("COMPLETED", None, {"ok": False}, {"backtest": "REPRODUCED"}, "yfinance") == "DATA_QUALITY_FAILURE"
    assert mod.classify("COMPLETED", None, {"ok": True}, {"backtest": "DIFFERENT"}, "yfinance") == "STRATEGY_OR_ENGINE_DEFECT"
    assert mod.classify("COMPLETED", None, {"ok": True}, {"backtest": "REPRODUCED"}, "yfinance") == "COMPLETED"
