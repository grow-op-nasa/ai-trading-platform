"""Domain models for the AI Research Agent -- src/ai/agents/models.py.

Plain, mostly-frozen dataclasses, no behavior beyond simple derived
properties -- `src.ai.agents.agent.ResearchAgent` does the actual
orchestration. Every timestamp here is timezone-aware UTC
(Sprint 13 spec, section 10); wall-clock time is never used for
analytical calculations elsewhere in this package, only recorded here
as run metadata.

Two identity concerns Sprint 13 explicitly separates (spec sections
11-12, 39, 66):

1. **Run identity** (`AgentRun.run_id`) -- a fresh UUID per invocation.
   An agent run is an event/session, not a reusable dataset or model
   specification, so it does not need a deterministic content hash the
   way `ExperimentSpec`/`ModelMetadata` do.
2. **Configuration provenance** (`AgentRun.system_prompt_version`,
   `tool_schema_version`, `policy_hash`, `provider`, `model`) -- fully
   deterministic and hashed where practical, so two runs against
   identical platform data can still be told apart if the agent's own
   configuration (prompt, tool surface, policy, provider, model)
   differed. An `AgentResearchReport` is never identified merely as
   `"research_agent_v1"` without this provenance traveling with it.

Nothing here persists hidden chain-of-thought (Sprint 13 spec, section
9): `AgentRun`/`ToolCallRecord` capture goal, tool calls, tool
arguments, tool result *summaries*, trial/experiment references, the
final answer, and errors -- never a model's private reasoning tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> datetime:
    """The current time, timezone-aware UTC -- the one place this
    package reads the wall clock (run metadata only, never anything
    analytical, Sprint 13 spec section 10)."""
    return datetime.now(timezone.utc)


class AgentRunStatus(str, Enum):
    """The terminal (or in-flight) state of one `AgentRun` (Sprint 13
    spec, sections 8, 47, 102) -- a `str` subclass, matching every other
    stable enum in this codebase (`src.data.base.Interval`,
    `src.backtesting.config.RiskMode`), so it serializes cleanly with no
    separate encode/decode step.

    `RUNNING`: the loop is still in progress -- never the status of a
        returned `AgentRun` (`ResearchAgent.run()` only returns once one
        of the terminal states below is reached).
    `COMPLETED`: the agent produced a final research answer within
        budget.
    `FAILED`: a fatal, unrecoverable error occurred before or during
        research (missing/invalid provider configuration, a provider
        error, a malformed provider response) -- distinct from
        `BUDGET_EXHAUSTED` (Sprint 13 spec, section 8: "the caller must
        be able to distinguish completed from failed before research
        and stopped because budget was exhausted").
    `BUDGET_EXHAUSTED`: `max_steps` or `max_backtests` was reached
        before the agent produced a final answer. The run still carries
        whatever partial evidence/summary was gathered -- never
        presented as a complete investigation (Sprint 13 spec, section
        47).
    """

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


@dataclass(frozen=True)
class ToolCallRecord:
    """One tool invocation the agent made, for the audit log (Sprint 13
    spec, sections 9, 42-43, 48) -- arguments and a *compact* result
    summary only, never a raw DataFrame, full trade list, or full
    equity curve.

    Args:
        step: which agent loop iteration this call happened on (1-indexed).
        tool_name: the tool that was invoked.
        arguments: the (validated) arguments the tool was called with.
        is_error: whether this call resulted in a structured tool error
            rather than a successful result.
        result_summary: the tool's own compact, JSON-safe output on
            success, or `{}` on error.
        error_code: a short machine-readable code on error (e.g.
            `"INVALID_DATE_RANGE"`, `"UNKNOWN_TOOL"`,
            `"POLICY_REJECTED"`), `None` on success.
        error_message: a short human-readable message on error, `None`
            on success. Never a raw stack trace (Sprint 13 spec,
            section 48) -- detailed diagnostics are logged separately,
            not handed to the model or persisted here.
    """

    step: int
    tool_name: str
    arguments: dict[str, Any]
    is_error: bool
    result_summary: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AgentEvidence:
    """One piece of platform evidence the agent's final report cites
    (Sprint 13 spec, sections 39, 66-67) -- every substantive numerical
    claim in an `AgentResearchReport` must be traceable to one of these.

    Args:
        kind: what this evidence references -- `"experiment"`,
            `"trial"`, `"model"`, `"comparison"`, or `"strategy"`.
        reference: the experiment id / trial id / model id / strategy
            name this evidence points at, as a string.
        description: a short, human-readable statement of what this
            evidence shows.
        data: the compact, JSON-safe supporting data (e.g. the metric
            values referenced) -- never a raw DataFrame.
    """

    kind: str
    reference: str
    description: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrialResult:
    """One ephemeral historical research trial the agent ran via
    `run_historical_backtest` (Sprint 13 spec, sections 23, 40-41, 55)
    -- exists only for the lifetime of the `AgentSession` that produced
    it, never automatically persisted to `src.experiments.
    ExperimentRegistry` (that remains a human-facing, explicit-save
    workflow, future work).

    Carries the same experiment-provenance fields
    `src.experiments.spec.ExperimentSpec`/`src.analytics.models.
    BacktestAnalytics` already establish (Sprint 13 spec, section 39),
    so a trial's evidence is exactly as auditable as a persisted
    experiment's, just not (yet) written to the registry.

    Args:
        trial_id: a fresh identifier for this trial (not a content
            hash -- an ephemeral research trial is an event, matching
            `AgentRun.run_id`'s own reasoning).
        strategy_name / strategy_version: which strategy implementation
            produced this trial's signals.
        symbol / interval: the instrument/timeframe traded.
        dataset_fingerprint: the exact candle data used
            (`src.utils.hashing.dataframe_fingerprint`).
        dataset_start / dataset_end: ISO-8601 bounds of the data
            actually used.
        risk_config / execution_config: the exact risk/execution
            assumptions in effect (`BacktestConfig.describe()`'s own
            sub-dicts) -- an agent trial's research conclusions are
            meaningless without knowing these (Sprint 13 spec, section
            29: "never give the agent a hidden zero-cost default and
            describe the result as though execution costs were
            included").
        model_id / feature_set_id / label_set_id: populated only when
            the strategy that produced this trial was an
            `AISignalStrategy` (`None` otherwise) -- Sprint 13 spec,
            section 55.
        analytics: the compact `BacktestAnalytics` summary
            (`src.analytics.metrics`-derived numbers only, never raw
            trades/equity) as a JSON-safe dict.
        trade_count: how many trades this trial produced.
    """

    trial_id: str
    strategy_name: str
    strategy_version: str
    symbol: str
    interval: str
    dataset_fingerprint: str
    dataset_start: str
    dataset_end: str
    risk_config: dict[str, Any]
    execution_config: dict[str, Any]
    analytics: dict[str, Any]
    trade_count: int
    model_id: str | None = None
    feature_set_id: str | None = None
    label_set_id: str | None = None


@dataclass(frozen=True)
class AgentResearchReport:
    """The final output of one `ResearchAgent.run()` call (Sprint 13
    spec, section 44) -- a research recommendation for a person to
    weigh, never a trading decision (mirrors `ResearchReport`'s own
    posture, `DECISIONS.md` ADR-0017).

    Deliberately carries no `trade_recommendation`/
    `buy_sell_recommendation` field (Sprint 13 spec, sections 45-46) --
    `suggested_next_experiments` may only ever propose further
    research.

    Args:
        run_id: the `AgentRun.run_id` this report belongs to.
        goal: the research objective, verbatim.
        status: the terminal `AgentRunStatus` of the run that produced
            this report.
        summary: readable prose synthesizing the investigation --
            everything else on this dataclass is structured.
        observations: statements the evidence directly supports (Sprint
            13 spec, section 37) -- e.g. "Observed: maximum drawdown was
            larger in the high-volatility sample."
        hypotheses: statements the evidence *suggests* but does not
            prove (section 37) -- e.g. "Hypothesis: the strategy may be
            more sensitive to volatility shocks." Never silently
            promoted to an observation.
        experiments: experiment/trial identifiers this report's
            evidence references (a subset of `evidence`'s own
            references, kept redundantly here for a quick-glance list).
        evidence: every `AgentEvidence` a numerical claim in `summary`/
            `observations`/`hypotheses` is traceable to.
        limitations: what this investigation did *not* establish --
            always populated when `status` is `BUDGET_EXHAUSTED`
            (Sprint 13 spec, section 47: "the final output should say
            that the conclusion is based on incomplete investigation").
        suggested_next_experiments: research recommendations only
            (Sprint 13 spec, section 46) -- e.g. "Investigate whether
            excluding high-volatility entries improves robustness,"
            never an execution instruction.
    """

    run_id: str
    goal: str
    status: AgentRunStatus
    summary: str
    observations: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    experiments: list[str] = field(default_factory=list)
    evidence: list[AgentEvidence] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    suggested_next_experiments: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentRun:
    """The complete audit record of one `ResearchAgent.run()` invocation
    (Sprint 13 spec, sections 10, 12, 42) -- the reproducibility and
    accountability record `data/agent_runs/` persists one of per run
    (`ResearchAgent`/a run store, never committed to Git).

    Configuration provenance (Sprint 13 spec, sections 11-12): `provider`,
    `model`, `system_prompt_version`, `tool_schema_version`, and
    `policy_hash` together let a reader answer "could two runs against
    identical platform data have behaved differently because the agent
    itself was configured differently" without re-reading source.

    Args:
        run_id: a fresh UUID4 hex string, unique per invocation -- an
            agent run is an event/session, not a reusable
            dataset/model specification (Sprint 13 spec, section 11),
            so this is not a deterministic content hash.
        goal: the research objective, verbatim, as given to `run()`.
        provider: the `LLMProvider` implementation name (e.g.
            `"anthropic"`, `"fake"`).
        model: the underlying model identifier the provider used (e.g.
            `"claude-sonnet-5"`) -- never hard-coded by the agent core
            (Sprint 13 spec, section 6).
        status: the terminal `AgentRunStatus`.
        started_at / finished_at: timezone-aware UTC timestamps.
        step_count: how many agent-loop iterations actually ran.
        tool_call_count: how many tool calls were actually executed
            (successful or structured-error, each counts once).
        backtest_count: how many `run_historical_backtest` calls were
            actually executed -- always `<= policy.max_backtests`.
        system_prompt_version: the versioned system prompt's own
            version string (`src.ai.agents.prompts.SYSTEM_PROMPT_VERSION`).
        tool_schema_version: a hash of the tool schemas actually offered
            to the model this run (`ToolRegistry.schema_hash()`).
        policy_hash: `ResearchAgentPolicy.config_hash()`'s output for
            the policy this run enforced.
        tool_calls: every `ToolCallRecord`, in order.
        trial_ids: every `TrialResult.trial_id` this run produced.
        experiment_ids: every persisted experiment id this run read
            (via `list_experiments`/`get_experiment`/
            `analyze_experiment`/`compare_experiments`).
        final_report: the `AgentResearchReport`, when `status` is
            `COMPLETED` or `BUDGET_EXHAUSTED` (a partial report is still
            produced on budget exhaustion) -- `None` when `status` is
            `FAILED` before any research occurred.
        error: a structured description of what went wrong -- populated
            whenever `status` is `FAILED` (Sprint 13 spec, section 102:
            "do not collapse everything into 'agent failed'"), `None`
            otherwise. Never contains a secret (an API key, credential,
            or connection string) -- see `tests/test_ai_agent_security.py`.
    """

    run_id: str
    goal: str
    provider: str
    model: str
    status: AgentRunStatus
    started_at: datetime
    finished_at: datetime
    step_count: int
    tool_call_count: int
    backtest_count: int
    system_prompt_version: str
    tool_schema_version: str
    policy_hash: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    trial_ids: list[str] = field(default_factory=list)
    experiment_ids: list[str] = field(default_factory=list)
    final_report: AgentResearchReport | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        if self.finished_at.tzinfo is None:
            raise ValueError("finished_at must be timezone-aware")
