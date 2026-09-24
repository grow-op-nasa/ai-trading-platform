"""Short-term, in-memory session state for one Strategy Development
Agent run -- src/ai/agents/strategy_dev/session.py.

Mirrors `src.ai.agents.session.AgentSession`'s own posture (Sprint 13
spec, section 9: no long-term memory, vector store, or embeddings --
just the current run's messages, tool history, and budget counters),
shaped for candidate bookkeeping instead of trial/experiment
bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.ai.agents.models import ToolCallRecord
from src.ai.agents.provider import LLMMessage


@dataclass
class DevAgentSession:
    goal: str
    messages: list[LLMMessage] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    frozen_candidate_ids: list[str] = field(default_factory=list)
    candidate_reports: list[dict] = field(default_factory=list)
    baseline_comparisons: list[dict] = field(default_factory=list)
    step_count: int = 0
    candidates_created_count: int = 0
    candidate_revisions_count: int = 0
    validation_backtests_count: int = 0
    final_test_evaluations_count: int = 0

    def add_message(self, message: LLMMessage) -> None:
        self.messages.append(message)

    def record_tool_call(self, record: ToolCallRecord) -> None:
        self.tool_calls.append(record)

    def record_candidate(self, candidate_id: str, is_revision: bool) -> None:
        if candidate_id not in self.candidate_ids:
            self.candidate_ids.append(candidate_id)
        self.candidates_created_count += 1
        if is_revision:
            self.candidate_revisions_count += 1

    def record_freeze(self, candidate_id: str) -> None:
        if candidate_id not in self.frozen_candidate_ids:
            self.frozen_candidate_ids.append(candidate_id)
