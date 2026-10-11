"""Audit store for monitoring agent runs -- one JSON file per run under
`data/monitoring_agent_runs/` (covered by the repo's `data/*` ignore)."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

from src.ai.agents.models import ToolCallRecord
from src.ai.agents.monitoring.models import MonitoringAgentReport, MonitoringAgentRun, to_jsonable

DEFAULT_RUNS_DIR = Path("data/monitoring_agent_runs")


class MonitoringAgentRunStore:
    def __init__(self, base_dir: Path | str = DEFAULT_RUNS_DIR) -> None:
        self._base_dir = Path(base_dir)

    def _path(self, run_id: str) -> Path:
        return self._base_dir / f"{run_id}.json"

    def save(self, run: MonitoringAgentRun) -> Path:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(run.run_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(run), default=to_jsonable, indent=2))
        tmp.replace(path)
        return path

    def load(self, run_id: str) -> MonitoringAgentRun | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        rep = raw.get("final_report")
        report = None
        if rep is not None:
            report = MonitoringAgentReport(
                run_id=rep["run_id"], goal=rep["goal"], status=rep["status"], summary=rep["summary"],
                observed_facts=tuple(rep.get("observed_facts", [])),
                hypotheses=tuple(rep.get("hypotheses", [])),
                suggested_investigations=tuple(rep.get("suggested_investigations", [])),
                limitations=tuple(rep.get("limitations", [])),
                monitoring_run_ids=tuple(rep.get("monitoring_run_ids", [])),
                finding_ids=tuple(rep.get("finding_ids", [])),
                evidence_references=tuple(rep.get("evidence_references", [])),
            )
        return MonitoringAgentRun(
            run_id=raw["run_id"], goal=raw["goal"], provider=raw["provider"], model=raw["model"],
            status=raw["status"], started_at=datetime.fromisoformat(raw["started_at"]),
            finished_at=datetime.fromisoformat(raw["finished_at"]), step_count=raw["step_count"],
            tool_call_count=raw["tool_call_count"], monitoring_run_count=raw["monitoring_run_count"],
            system_prompt_version=raw["system_prompt_version"],
            tool_schema_version=raw["tool_schema_version"], policy_hash=raw["policy_hash"],
            tool_calls=[ToolCallRecord(**c) for c in raw.get("tool_calls", [])],
            monitoring_run_ids=list(raw.get("monitoring_run_ids", [])),
            final_report=report, error=raw.get("error"),
        )
