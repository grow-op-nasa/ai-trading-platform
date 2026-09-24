"""Agent session state -- src/ai/agents/session.py.

Sprint 13 spec, sections 13-14: short-term state across tool calls
only -- the current research task, current tool results, current
session evidence. Deliberately in-memory and ephemeral for the
lifetime of one `ResearchAgent.run()` call, and deliberately *not* a
general-purpose agent memory system (section 14): no vector database,
no embeddings, no semantic memory store, no persistent conversational
memory across separate runs. A second `run()` call gets a fresh
`AgentSession`, with no visibility into any prior run's state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.ai.agents.models import AgentEvidence, ToolCallRecord, TrialResult
from src.ai.agents.provider import LLMMessage


@dataclass
class AgentSession:
    """One `ResearchAgent.run()` call's working state.

    Args:
        goal: the research objective this session exists to investigate.
        messages: the full conversation history sent to/received from
            the provider so far, in order.
        tool_calls: every `ToolCallRecord` made so far, in order.
        trials: every ephemeral `TrialResult` this session has produced,
            keyed by `trial_id`.
        evidence: every `AgentEvidence` accumulated so far.
        experiment_ids: every persisted experiment id this session has
            read (via any of the read-only research tools).
        step_count: how many agent-loop iterations have run so far.
        backtest_count: how many `run_historical_backtest` calls have
            actually executed so far -- checked against
            `ResearchAgentPolicy.max_backtests` before each new one.
    """

    goal: str
    messages: list[LLMMessage] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    trials: dict[str, TrialResult] = field(default_factory=dict)
    evidence: list[AgentEvidence] = field(default_factory=list)
    experiment_ids: list[str] = field(default_factory=list)
    step_count: int = 0
    backtest_count: int = 0

    def add_message(self, message: LLMMessage) -> None:
        self.messages.append(message)

    def record_tool_call(self, record: ToolCallRecord) -> None:
        self.tool_calls.append(record)

    def record_trial(self, trial: TrialResult) -> None:
        self.trials[trial.trial_id] = trial

    def record_evidence(self, evidence: AgentEvidence) -> None:
        self.evidence.append(evidence)

    def record_experiment_id(self, experiment_id: str) -> None:
        if experiment_id not in self.experiment_ids:
            self.experiment_ids.append(experiment_id)
