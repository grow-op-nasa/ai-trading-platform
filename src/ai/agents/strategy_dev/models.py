"""Candidate strategy domain models -- src/ai/agents/strategy_dev/models.py.

Sprint 14 spec, sections 1, 21-23: a candidate is a first-class,
provenance-tracked object distinct from a production `Strategy`
(`src.strategies.base.Strategy`). It never lives in
`src.strategies.registry.StrategyRegistry` -- that remains the
authoritative registry of human-approved strategies (Sprint 14 spec,
section 91).

Every timestamp is timezone-aware UTC, mirroring
`src.ai.agents.models.utc_now()`'s own convention (re-exported here so
this package has one source of truth for "now").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.ai.agents.models import AgentEvidence, ToolCallRecord

__all__ = [
    "utc_now",
    "CandidateStrategyStatus",
    "CandidateStrategySpec",
    "CandidateStrategy",
    "CandidateEvaluation",
    "DevResearchReport",
    "DevAgentRun",
    "AgentEvidence",
    "ToolCallRecord",
]


def utc_now() -> datetime:
    """Timezone-aware UTC "now" -- the only clock this package uses."""
    return datetime.now(timezone.utc)


class CandidateStrategyStatus(str, Enum):
    """The candidate lifecycle (Sprint 14 spec, section 2).

    Progression is strictly forward:

        DRAFT -> VALIDATING -> VALIDATED -> DEVELOPMENT_TESTED ->
        FROZEN -> OUT_OF_SAMPLE_TESTED -> REVIEW_REQUIRED

    with two terminal states reachable from anywhere in that chain:
    `REJECTED` (validation/testing failed, or the researcher/agent gave
    up on this candidate) and `PROMOTED` (a human explicitly approved
    it -- Sprint 14 spec, section 57; never reachable by the agent
    itself, see `CandidateRegistry.promote()`'s own guard).

    `FROZEN` is the one-way gate (Sprint 14 spec, sections 5, 35-36):
    once a candidate reaches it, `source_hash`/`spec_hash` become
    immutable and the final out-of-sample test may run exactly once. A
    revised idea after that point is a new candidate, not a mutation of
    this one (Sprint 14 spec, section 85).
    """

    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    DEVELOPMENT_TESTED = "DEVELOPMENT_TESTED"
    FROZEN = "FROZEN"
    OUT_OF_SAMPLE_TESTED = "OUT_OF_SAMPLE_TESTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
    PROMOTED = "PROMOTED"


#: States reachable *after* which the candidate's source/spec must never
#: change again (Sprint 14 spec, section 5). Checked by
#: `CandidateRegistry` before any mutating operation.
IMMUTABLE_AFTER = frozenset(
    {
        CandidateStrategyStatus.FROZEN,
        CandidateStrategyStatus.OUT_OF_SAMPLE_TESTED,
        CandidateStrategyStatus.REVIEW_REQUIRED,
        CandidateStrategyStatus.REJECTED,
        CandidateStrategyStatus.PROMOTED,
    }
)

#: Terminal states -- no further lifecycle transition is permitted.
TERMINAL_STATES = frozenset({CandidateStrategyStatus.REJECTED, CandidateStrategyStatus.PROMOTED})


@dataclass(frozen=True)
class CandidateStrategySpec:
    """The structured specification a candidate's code must trace back
    to (Sprint 14 spec, section 21) -- required *before* generated code,
    never inferred from it after the fact.

    Args:
        research_question: the human research objective this candidate
            is meant to answer (Sprint 14 spec, section 82).
        hypothesis: what the researcher/agent believes and why.
        strategy_name: a short, human-readable candidate name (not a
            production strategy registry key -- candidates are never
            registered there).
        strategy_description: a longer prose description.
        entry_logic / exit_logic: plain-language description of when
            the candidate goes long/short/flat.
        indicator_dependencies: existing `IndicatorEngine` indicator
            names this candidate relies on (Sprint 14 spec, section
            41) -- e.g. `["EMA", "RSI"]`.
        parameters: the candidate's tunable constructor parameters and
            their default values (mirrors `Strategy.params`).
        symbol: the instrument this candidate is designed for (Sprint
            14 spec, section 43).
        interval: the timeframe this candidate is designed for.
        signal_semantics: how this candidate uses `SignalDirection`
            (e.g. "LONG/FLAT only, no SHORT").
        expected_behavior: what a human reviewer should expect to see.
        known_limitations: the researcher/agent's own stated caveats.
        base_strategy: an existing registered strategy name this
            candidate builds on/compares against, if any (`None`
            otherwise).
    """

    research_question: str
    hypothesis: str
    strategy_name: str
    strategy_description: str
    entry_logic: str
    exit_logic: str
    indicator_dependencies: tuple[str, ...]
    parameters: dict[str, Any]
    symbol: str
    interval: str
    signal_semantics: str
    expected_behavior: str
    known_limitations: str = ""
    base_strategy: str | None = None

    def __post_init__(self) -> None:
        for attr in ("research_question", "hypothesis", "strategy_name", "entry_logic", "exit_logic"):
            if not getattr(self, attr):
                raise ValueError(f"{attr} must be non-empty")

    def describe(self) -> dict[str, Any]:
        """A JSON-safe, canonical description -- what `spec_hash()`
        hashes."""
        return {
            "research_question": self.research_question,
            "hypothesis": self.hypothesis,
            "strategy_name": self.strategy_name,
            "strategy_description": self.strategy_description,
            "entry_logic": self.entry_logic,
            "exit_logic": self.exit_logic,
            "indicator_dependencies": list(self.indicator_dependencies),
            "parameters": self.parameters,
            "symbol": self.symbol,
            "interval": self.interval,
            "signal_semantics": self.signal_semantics,
            "expected_behavior": self.expected_behavior,
            "known_limitations": self.known_limitations,
            "base_strategy": self.base_strategy,
        }


@dataclass
class CandidateStrategy:
    """One candidate's identity, provenance, and current lifecycle state
    (Sprint 14 spec, section 1). Mutable only through
    `CandidateRegistry`'s own guarded methods -- never edited directly
    once past `DRAFT` (Sprint 14 spec, section 5).

    Args:
        candidate_id: a fresh, unique identifier (an event, not a
            content hash -- matches `AgentRun.run_id`'s own reasoning;
            two textually-identical candidates created at different
            times are still different candidates).
        name / description: from the originating `CandidateStrategySpec`.
        source_hash: SHA-256 of the exact candidate source text (Sprint
            14 spec, section 22). Changes if a single line of logic
            changes.
        spec_hash: SHA-256 of `CandidateStrategySpec.describe()`.
        strategy_interface_version: which version of the canonical
            `Strategy` contract this candidate targets (currently
            always `"v1"` -- the contract has had one shape since
            Sprint 3/ADR-0015).
        created_at: tz-aware UTC.
        created_by: always `"agent"` in this sprint -- there is no
            human-authored candidate path yet, but the field exists so
            a future human-drafted candidate isn't a schema change.
        status: current lifecycle state.
        base_strategy: an existing registered strategy this candidate
            builds on/compares against, if any.
        configuration: the candidate's resolved constructor parameters
            (mirrors `Strategy.params`).
        feature_dependencies: indicator/feature names this candidate
            depends on.
        agent_run_id / provider / model / system_prompt_version /
            tool_schema_version / policy_hash: full generation
            provenance (Sprint 14 spec, section 23) -- never rely on
            the candidate name alone.
        parent_candidate_id: the candidate this one revises, if any
            (Sprint 14 spec, section 86) -- `None` for a first attempt.
        revision_reason / changes_summary: populated only when
            `parent_candidate_id` is set.
        development_evaluation / validation_evaluation /
            final_test_evaluation: `CandidateEvaluation` results at
            each stage (`None` until that stage has run).
        rejection_reason: populated only when `status ==
            CandidateStrategyStatus.REJECTED`.
    """

    candidate_id: str
    name: str
    description: str
    source_hash: str
    spec_hash: str
    strategy_interface_version: str
    created_at: datetime
    created_by: str
    status: CandidateStrategyStatus
    base_strategy: str | None
    configuration: dict[str, Any]
    feature_dependencies: tuple[str, ...]
    agent_run_id: str
    provider: str
    model: str
    system_prompt_version: str
    tool_schema_version: str
    policy_hash: str
    parent_candidate_id: str | None = None
    revision_reason: str | None = None
    changes_summary: str | None = None
    development_evaluation: "CandidateEvaluation | None" = None
    validation_evaluation: "CandidateEvaluation | None" = None
    final_test_evaluation: "CandidateEvaluation | None" = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")

    def is_immutable(self) -> bool:
        """`True` once this candidate has passed the freeze gate (Sprint
        14 spec, section 5) -- source/spec/configuration must never
        change again."""
        return self.status in IMMUTABLE_AFTER

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES


@dataclass(frozen=True)
class CandidateEvaluation:
    """One backtest evaluation of a candidate at one lifecycle stage
    (development, validation, or final out-of-sample test) -- Sprint 14
    spec, section 49. Composed entirely of existing platform result
    types (`src.analytics.models.BacktestAnalytics`, serialized), never
    a new parallel analytics shape (Sprint 14 spec, section 50).

    Args:
        trial_id: the ephemeral `ResearchTrialService` trial this
            evaluation came from.
        stage: one of `"development"`, `"validation"`, `"final_test"`.
        dataset_fingerprint / dataset_start / dataset_end: which
            candles were actually used.
        risk_config / execution_config: the exact risk/execution
            assumptions in effect (Sprint 14 spec, sections 113-115 --
            a candidate cannot hide costs or alter risk to look
            better).
        analytics: the JSON-safe `BacktestAnalytics` payload (same
            shape `src.ai.agents.backtest_tool` already returns).
        trade_count: how many trades this evaluation produced.
        warnings: e.g. "no trades generated", "insufficient sample".
    """

    trial_id: str
    stage: str
    dataset_fingerprint: str
    dataset_start: str
    dataset_end: str
    risk_config: dict[str, Any]
    execution_config: dict[str, Any]
    analytics: dict[str, Any]
    trade_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DevResearchReport:
    """The Strategy Development Agent's final output for one run (Sprint
    14 spec, section 119) -- deliberately distinct from Sprint 13's
    `AgentResearchReport`: this one is about candidates, not existing
    experiments, and must never contain a composite "strategy quality
    score" (Sprint 14 spec, sections 52, 119) or an auto-promotion claim
    (section 122).

    `status` is one of the terminal `DevAgentRunStatus` values (below).
    """

    run_id: str
    goal: str
    status: str
    summary: str
    candidates_created: tuple[str, ...]
    candidates_frozen: tuple[str, ...]
    candidate_reports: tuple[dict[str, Any], ...]
    baseline_comparisons: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...] = ()
    recommended_review_questions: tuple[str, ...] = ()


class DevAgentRunStatus(str, Enum):
    """Terminal statuses for one `StrategyDevelopmentAgent.run()` call
    -- mirrors `src.ai.agents.models.AgentRunStatus`'s three-way split
    (Sprint 13 spec, section 8; Sprint 14 spec has no reason to
    diverge)."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


@dataclass
class DevAgentRun:
    """The audit/provenance record for one Strategy Development Agent
    run -- mirrors `src.ai.agents.models.AgentRun`'s shape and
    reasoning (tz-aware UTC, full configuration provenance, no hidden
    chain-of-thought), extended with candidate-specific budget usage
    (Sprint 14 spec, section 116).
    """

    run_id: str
    goal: str
    provider: str
    model: str
    status: str
    started_at: datetime
    finished_at: datetime
    step_count: int
    tool_call_count: int
    candidates_created_count: int
    candidate_revisions_count: int
    validation_backtests_count: int
    final_test_evaluations_count: int
    system_prompt_version: str
    tool_schema_version: str
    policy_hash: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    final_report: DevResearchReport | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("started_at/finished_at must be timezone-aware")
