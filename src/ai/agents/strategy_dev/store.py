"""The dev agent run audit store -- src/ai/agents/strategy_dev/store.py.

Mirrors `src.ai.agents.store.AgentRunStore` exactly: one JSON file per
`DevAgentRun` under `data/strategy_dev_runs/` (already covered by the
repo's wholesale `data/*` `.gitignore` rule).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.ai.agents.models import ToolCallRecord
from src.ai.agents.strategy_dev.models import DevAgentRun, DevResearchReport

DEFAULT_DEV_RUNS_DIR = Path("data/strategy_dev_runs")


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value)!r}")


class DevAgentRunStore:
    def __init__(self, base_dir: Path | str = DEFAULT_DEV_RUNS_DIR) -> None:
        self._base_dir = Path(base_dir)

    def _path(self, run_id: str) -> Path:
        return self._base_dir / f"{run_id}.json"

    def save(self, run: DevAgentRun) -> Path:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(run.run_id)
        path.write_text(json.dumps(dataclasses.asdict(run), default=_default, indent=2))
        return path

    def load(self, run_id: str) -> DevAgentRun | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        final_report_raw = raw.get("final_report")
        final_report = None
        if final_report_raw is not None:
            final_report = DevResearchReport(
                run_id=final_report_raw["run_id"],
                goal=final_report_raw["goal"],
                status=final_report_raw["status"],
                summary=final_report_raw["summary"],
                candidates_created=tuple(final_report_raw.get("candidates_created", [])),
                candidates_frozen=tuple(final_report_raw.get("candidates_frozen", [])),
                candidate_reports=tuple(final_report_raw.get("candidate_reports", [])),
                baseline_comparisons=tuple(final_report_raw.get("baseline_comparisons", [])),
                limitations=tuple(final_report_raw.get("limitations", [])),
                recommended_review_questions=tuple(
                    final_report_raw.get("recommended_review_questions", [])
                ),
            )
        return DevAgentRun(
            run_id=raw["run_id"],
            goal=raw["goal"],
            provider=raw["provider"],
            model=raw["model"],
            status=raw["status"],
            started_at=datetime.fromisoformat(raw["started_at"]),
            finished_at=datetime.fromisoformat(raw["finished_at"]),
            step_count=raw["step_count"],
            tool_call_count=raw["tool_call_count"],
            candidates_created_count=raw["candidates_created_count"],
            candidate_revisions_count=raw["candidate_revisions_count"],
            validation_backtests_count=raw["validation_backtests_count"],
            final_test_evaluations_count=raw["final_test_evaluations_count"],
            system_prompt_version=raw["system_prompt_version"],
            tool_schema_version=raw["tool_schema_version"],
            policy_hash=raw["policy_hash"],
            tool_calls=[ToolCallRecord(**c) for c in raw.get("tool_calls", [])],
            candidate_ids=list(raw.get("candidate_ids", [])),
            final_report=final_report,
            error=raw.get("error"),
        )
