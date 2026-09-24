"""The agent run audit store -- src/ai/agents/store.py.

Sprint 13 spec, section 42: research trials are ephemeral, but the
agent's own run is auditable. `AgentRunStore` persists one JSON file per
`AgentRun` under a controlled runtime-data location (`data/agent_runs/`
by default, matching `src.experiments.registry.ExperimentRegistry`'s
own `data/experiments.db` convention) -- never committed to Git (`data/`
is already `.gitignore`d wholesale, `.gitignore`'s existing `data/*`
rule, so no new ignore rule is needed for this specific subdirectory).

Contains no hidden chain-of-thought (Sprint 13 spec, section 9) -- only
what `AgentRun` itself carries: goal, provider/model, configuration
provenance, tool calls, tool arguments, tool result summaries, trial/
experiment ids, the final report, and any error.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.ai.agents.models import (
    AgentEvidence,
    AgentResearchReport,
    AgentRun,
    AgentRunStatus,
    ToolCallRecord,
)

DEFAULT_AGENT_RUNS_DIR = Path("data/agent_runs")


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, AgentRunStatus):
        return value.value
    raise TypeError(f"cannot serialize {type(value)!r}")


class AgentRunStore:
    """Filesystem-backed store of `AgentRun` audit records, one JSON
    file per run -- mirrors `src.ai.registry.ModelRegistry`'s own
    "one small, self-contained record per id" posture rather than a
    database schema.

    Args:
        base_dir: root directory. Created on first use if missing.
    """

    def __init__(self, base_dir: Path | str = DEFAULT_AGENT_RUNS_DIR) -> None:
        self._base_dir = Path(base_dir)

    def _path(self, run_id: str) -> Path:
        return self._base_dir / f"{run_id}.json"

    def save(self, run: AgentRun) -> Path:
        """Persist `run` and return the path it was written to."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(run.run_id)
        path.write_text(json.dumps(dataclasses.asdict(run), default=_default, indent=2))
        return path

    def load(self, run_id: str) -> AgentRun | None:
        """Load a previously-saved `AgentRun`, or `None` if none was
        ever saved under `run_id`."""
        path = self._path(run_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        return _run_from_dict(raw)


def _run_from_dict(raw: dict[str, Any]) -> AgentRun:
    final_report_raw = raw.get("final_report")
    final_report = None
    if final_report_raw is not None:
        final_report = AgentResearchReport(
            run_id=final_report_raw["run_id"],
            goal=final_report_raw["goal"],
            status=AgentRunStatus(final_report_raw["status"]),
            summary=final_report_raw["summary"],
            observations=list(final_report_raw.get("observations", [])),
            hypotheses=list(final_report_raw.get("hypotheses", [])),
            experiments=list(final_report_raw.get("experiments", [])),
            evidence=[AgentEvidence(**e) for e in final_report_raw.get("evidence", [])],
            limitations=list(final_report_raw.get("limitations", [])),
            suggested_next_experiments=list(
                final_report_raw.get("suggested_next_experiments", [])
            ),
        )
    return AgentRun(
        run_id=raw["run_id"],
        goal=raw["goal"],
        provider=raw["provider"],
        model=raw["model"],
        status=AgentRunStatus(raw["status"]),
        started_at=datetime.fromisoformat(raw["started_at"]),
        finished_at=datetime.fromisoformat(raw["finished_at"]),
        step_count=raw["step_count"],
        tool_call_count=raw["tool_call_count"],
        backtest_count=raw["backtest_count"],
        system_prompt_version=raw["system_prompt_version"],
        tool_schema_version=raw["tool_schema_version"],
        policy_hash=raw["policy_hash"],
        tool_calls=[ToolCallRecord(**c) for c in raw.get("tool_calls", [])],
        trial_ids=list(raw.get("trial_ids", [])),
        experiment_ids=list(raw.get("experiment_ids", [])),
        final_report=final_report,
        error=raw.get("error"),
    )
