"""Audit and report models -- src/ai/agents/monitoring/models.py (ADR-0050).

No chain-of-thought is stored anywhere: only the goal, tool calls with
their compact results, counters, provenance and the final report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.ai.agents.models import ToolCallRecord

STATUS_COMPLETED = "COMPLETED"
STATUS_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
STATUS_FAILED = "FAILED"


@dataclass(frozen=True)
class MonitoringAgentReport:
    """What the agent concluded. Observed facts, hypotheses and suggested
    investigations are separated; there is deliberately no trade, order,
    recovery or promotion recommendation field."""

    run_id: str
    goal: str
    status: str
    summary: str
    observed_facts: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    suggested_investigations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    monitoring_run_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    # Evidence completeness is DERIVED BY CODE from successful registered `get_finding` results
    # (ADR-0051); nothing the model writes can change these fields.
    evidence_complete: bool = False
    findings_required: tuple[str, ...] = ()
    findings_retrieved: tuple[str, ...] = ()
    findings_incomplete: tuple[str, ...] = ()
    evidence_incomplete_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitoringAgentRun:
    run_id: str
    goal: str
    provider: str
    model: str
    status: str
    started_at: datetime
    finished_at: datetime
    step_count: int
    tool_call_count: int
    monitoring_run_count: int
    system_prompt_version: str
    tool_schema_version: str
    policy_hash: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    monitoring_run_ids: list[str] = field(default_factory=list)
    final_report: MonitoringAgentReport | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value)!r}")
