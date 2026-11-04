"""AI tools over stored strategy research (src/ai/agents/strategy_research_tools.py, Sprint 23, ADR-0061)."""

from __future__ import annotations

import pathlib
import re

import pytest

from src.ai.agents.strategy_research_tools import GetStrategyResearchRunTool, ListStrategyResearchRunsTool
from src.ai.agents.tools import PERMISSION_EXPERIMENT_READS
from src.strategy_research import ResearchStore
from src.strategy_research.store import ResearchReader
from tests.test_strategy_research_service import SyntheticSource, declining, request, service

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def seeded(tmp_path):
    svc = service(tmp_path)
    good = svc.run(request("donchian_breakout"))
    svc.evaluate_holdout(good.run_id)
    bad = service(tmp_path).run(request(interval="1h"))
    wf = svc.walk_forward(request("donchian_breakout"), 200, 100)
    return svc, good, bad, wf


def dump(path):
    store = ResearchStore(path)
    return [(r, store.get_holdout(r["run_id"])) for r in store.list_runs()]


def test_reader_exposes_no_write_method():
    names = {n for n in dir(ResearchReader) if not n.startswith("_")}
    assert names == {"get_run", "list_runs", "get_holdout"}


def test_tools_only_read_and_leave_the_store_byte_for_byte_unchanged(tmp_path):
    _, good, bad, wf = seeded(tmp_path)
    before = dump(tmp_path / "r.db")
    reader = ResearchReader(tmp_path / "r.db")
    lister, getter = ListStrategyResearchRunsTool(reader), GetStrategyResearchRunTool(reader)
    assert lister.execute({}).status == "ok"
    for run_id in (good.run_id, bad.run_id, wf.run_id):
        assert getter.execute({"run_id": run_id}).status == "ok"
    assert getter.execute({"run_id": "nope"}).error_code == "NOT_FOUND"
    assert dump(tmp_path / "r.db") == before


def test_listing_filters_and_reports_each_runs_state(tmp_path):
    _, good, bad, wf = seeded(tmp_path)
    tool = ListStrategyResearchRunsTool(ResearchReader(tmp_path / "r.db"))
    rows = {r["run_id"]: r for r in tool.execute({}).data["runs"]}
    assert set(rows) == {good.run_id, bad.run_id, wf.run_id}
    assert rows[bad.run_id]["status"] == "NOT_EVALUABLE" and rows[wf.run_id]["research_state"] == "WALK_FORWARD"
    assert rows[good.run_id]["research_state"] in ("REJECTED", "OUT_OF_SAMPLE_EVALUATED")      # holdout was evaluated
    assert [r["run_id"] for r in tool.execute({"strategy_name": "nothing"}).data["runs"]] == []
    assert len(tool.execute({"limit": 1}).data["runs"]) == 1


def test_values_returned_are_exactly_the_stored_values_with_their_run_id(tmp_path):
    _, good, _, _ = seeded(tmp_path)
    data = GetStrategyResearchRunTool(ResearchReader(tmp_path / "r.db")).execute({"run_id": good.run_id}).data
    stored = good.record["attempts"][good.record["selection"]["selected_index"]]["scorecards"]["validation"]
    assert data["run_id"] == good.run_id and data["validation"]["net_return"] == stored["net_return"]
    assert data["validation"]["closed_trades"] == stored["closed_trades"]
    assert data["holdout"]["window"] == "holdout" and data["holdout_assessment"]
    assert data["provenance"]["dataset_hash"] == good.record["provenance"]["dataset_hash"]
    assert data["strategy_version"] == good.record["strategy"]["version"] and data["limitations"]
    assert data["selection"]["configurations_attempted"] == 1


def test_undefined_metrics_stay_undefined_for_the_model(tmp_path):
    svc = service(tmp_path, SyntheticSource(declining()))
    out = svc.run(request())
    data = GetStrategyResearchRunTool(ResearchReader(tmp_path / "r.db")).execute({"run_id": out.run_id}).data
    assert data["validation"]["closed_trades"] == 0
    assert data["validation"]["win_rate"]["status"] == "undefined" and data["validation"]["win_rate"]["value"] is None
    assert data["holdout"]["status"] == "LOCKED"


def test_a_run_that_was_not_evaluable_has_no_numbers_to_quote(tmp_path):
    _, _, bad, _ = seeded(tmp_path)
    data = GetStrategyResearchRunTool(ResearchReader(tmp_path / "r.db")).execute({"run_id": bad.run_id}).data
    assert data["reason_code"] == "UNSUPPORTED_TIMEFRAME" and "no performance results" in data["note"]
    assert "validation" not in data


def test_tools_use_the_read_permission_and_have_valid_schemas():
    reader = ResearchReader.__new__(ResearchReader)
    for tool in (ListStrategyResearchRunsTool(reader), GetStrategyResearchRunTool(reader)):
        assert tool.required_permission == PERMISSION_EXPERIMENT_READS
        assert tool.schema()["type"] == "object" and tool.name and tool.description
    assert GetStrategyResearchRunTool(reader).schema()["required"] == ["run_id"]


def test_the_default_registry_is_unchanged_and_only_gains_the_two_tools_when_a_reader_is_given(tmp_path):
    pytest.importorskip("joblib")
    from src.ai.agents.tools import default_tool_registry
    from src.experiments.registry import ExperimentRegistry

    base = {t.name for t in default_tool_registry(experiment_registry=ExperimentRegistry(db_path=":memory:")).all_tools()}
    assert len(base) == 7 and not any("strategy_research" in n for n in base)
    reader = ResearchReader(tmp_path / "r.db")
    more = {t.name for t in default_tool_registry(experiment_registry=ExperimentRegistry(db_path=":memory:"),
                                                  strategy_research_reader=reader).all_tools()}
    assert more - base == {"list_strategy_research_runs", "get_strategy_research_run"}


def test_the_tool_module_cannot_reach_writers_promotion_or_trading():
    text = (SRC / "ai" / "agents" / "strategy_research_tools.py").read_text()
    for forbidden in ("ResearchStore", "ResearchService", "add_run", "add_holdout", "evaluate_holdout", "promot", "register_strategy",
                      "submit_order", "PaperBroker", "src.broker", "src.execution"):
        assert forbidden not in re.sub(r'""".*?"""', "", text, flags=re.S), forbidden
