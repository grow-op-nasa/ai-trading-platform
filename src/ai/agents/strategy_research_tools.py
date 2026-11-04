"""Read-only tools over stored strategy research results (Sprint 23, ADR-0061).

The agent can list and read research runs that the deterministic research service already produced. It cannot create,
alter or delete a run, change a metric, tune parameters, promote a strategy or place an order: the tools receive a
``ResearchReader`` that has no write method, and every number they return is copied from the stored record with its
run id so any statement the agent makes can be traced to stored evidence.
"""

from __future__ import annotations

from typing import Any

from src.ai.agents.tools import PERMISSION_EXPERIMENT_READS, ToolResult
from src.strategy_research.scorecard import Scorecard
from src.strategy_research.service import research_state
from src.strategy_research.store import ResearchReader

MAX_LIST_LIMIT = 50
DEFAULT_LIST_LIMIT = 20
_KEYS = ("closed_trades", "open_trades_at_end", "sample", "win_rate", "expectancy", "profit_factor", "gross_return",
         "net_return", "net_return_at_stress_costs", "max_drawdown", "benchmark_return", "total_costs")


def _metric(m: Any) -> Any:
    return {"value": m.value, "status": m.status.value, "reason": m.reason}


def _summary(card_dict: dict[str, Any]) -> dict[str, Any]:
    card = Scorecard.from_dict(card_dict)
    out: dict[str, Any] = {"window": card.label, "first_bar": card.first_bar, "last_bar": card.last_bar, "bars": card.bars}
    for k in _KEYS:
        v = getattr(card, k)
        out[k] = v if isinstance(v, (int, str)) else _metric(v)
    out["notes"] = list(card.notes)[:4]
    return out


class ListStrategyResearchRunsTool:
    name = "list_strategy_research_runs"
    description = ("List stored historical strategy research runs (compact: run id, strategy, instrument, status and research "
                   "state). Read-only; these are deterministic stored results, not live signals.")
    required_permission = PERMISSION_EXPERIMENT_READS

    def __init__(self, reader: ResearchReader) -> None:
        self._reader = reader

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"strategy_name": {"type": "string"}, "symbol": {"type": "string"},
                                                 "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT}},
                "required": []}

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        limit = min(int(arguments.get("limit", DEFAULT_LIST_LIMIT)), MAX_LIST_LIMIT)
        rows = []
        for run in self._reader.list_runs():
            if arguments.get("strategy_name") and run["strategy_name"] != arguments["strategy_name"]:
                continue
            if arguments.get("symbol") and run["symbol"] != arguments["symbol"]:
                continue
            if run["kind"] == "SINGLE":
                state, _ = research_state(run, self._reader.get_holdout(run["run_id"]))
            else:
                state = "WALK_FORWARD"
            rows.append({"run_id": run["run_id"], "kind": run["kind"], "strategy": run["strategy_name"], "symbol": run["symbol"],
                         "status": run["status"], "research_state": state, "created_at": run["created_at"]})
            if len(rows) >= limit:
                break
        return ToolResult.ok(self.name, {"runs": rows, "count": len(rows)})


class GetStrategyResearchRunTool:
    name = "get_strategy_research_run"
    description = ("Read one stored strategy research run: configuration(s) attempted, selection, development/validation/holdout "
                   "scorecards, research state, limitations and data provenance. Quote values with the run id.")
    required_permission = PERMISSION_EXPERIMENT_READS

    def __init__(self, reader: ResearchReader) -> None:
        self._reader = reader

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        run = self._reader.get_run(arguments["run_id"])
        if run is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no research run {arguments['run_id']!r}")
        rec = run["payload"]
        base = {"run_id": run["run_id"], "kind": run["kind"], "strategy": run["strategy_name"], "symbol": run["symbol"],
                "status": run["status"]}
        if rec["status"] != "COMPLETED":
            return ToolResult.ok(self.name, {**base, "reason_code": rec.get("reason_code"), "reason": rec.get("reason"),
                                             "note": "not evaluable: there are no performance results for this run"})
        prov = rec["provenance"]
        common = {**base, "strategy_version": rec["strategy"]["version"], "family": rec["strategy"]["family"],
                  "provenance": {k: prov.get(k) for k in ("provider", "dataset_hash", "snapshot_id", "first_bar", "last_bar",
                                                           "valid_bars")},
                  "assumptions": rec["assumptions"], "limitations": rec["limitations"]}
        if run["kind"] == "WALK_FORWARD":
            return ToolResult.ok(self.name, {**common, "aggregate": rec["aggregate"], "method": rec["method"],
                                             "folds": [{"fold": f["fold"], "selected_params": f["selected_params"],
                                                        "test": _summary(f["test_scorecard"])} for f in rec["folds"][:12]]})
        holdout = self._reader.get_holdout(run["run_id"])
        state, why = research_state(run, holdout)
        sel = rec["selection"]["selected_index"]
        attempts = [{"index": a["index"], "params": a["params"], "params_hash": a["params_hash"],
                     "validation_net_return": a["scorecards"]["validation"]["net_return"]} for a in rec["attempts"]]
        data = {**common, "research_state": state, "research_state_reason": why, "selection": rec["selection"],
                "configurations": attempts,
                "development": _summary(rec["attempts"][sel]["scorecards"]["development"]),
                "validation": _summary(rec["attempts"][sel]["scorecards"]["validation"]),
                "holdout": _summary(holdout["payload"]["scorecard"]) if holdout else {"status": "LOCKED", "note": "not evaluated"},
                "holdout_assessment": holdout["payload"]["assessment"] if holdout else []}
        return ToolResult.ok(self.name, data)
