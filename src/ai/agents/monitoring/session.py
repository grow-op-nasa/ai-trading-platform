"""In-memory state of one monitoring agent run (no cross-run memory)."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.ai.agents.models import ToolCallRecord
from src.ai.agents.provider import LLMMessage


@dataclass
class MonitoringAgentSession:
    goal: str
    messages: list[LLMMessage] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    monitoring_run_ids: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    evidence_references: list[str] = field(default_factory=list)
    retrieved_finding_ids: list[str] = field(default_factory=list)      # validated get_finding results
    retrieval_failures: dict[str, str] = field(default_factory=dict)    # finding_id -> reason
    runtime_attempted: set[str] = field(default_factory=set)            # ids the runtime already tried
    step_count: int = 0
    monitoring_run_count: int = 0

    def add_message(self, message: LLMMessage) -> None:
        self.messages.append(message)

    def record_tool_call(self, record: ToolCallRecord) -> None:
        self.tool_calls.append(record)

    def note_run(self, run_id: str) -> None:
        if run_id not in self.monitoring_run_ids:
            self.monitoring_run_ids.append(run_id)

    def note_findings(self, ids) -> None:
        for i in ids:
            if i and i not in self.finding_ids:
                self.finding_ids.append(i)

    def note_evidence(self, refs) -> None:
        for r in refs:
            if r and r not in self.evidence_references:
                self.evidence_references.append(r)
