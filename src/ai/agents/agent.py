"""The agent loop -- src/ai/agents/agent.py.

Sprint 13 spec, sections 63-64 (Phase 8), the platform's first genuine
agentic loop -- an explicit, bounded state machine, not a hard-coded
sequence of calls with an LLM merely narrating it (section 83):

    INITIAL -> SEND_TO_MODEL -> (MODEL_REQUESTS_TOOL -> VALIDATE_TOOL ->
    CHECK_POLICY -> EXECUTE_TOOL -> APPEND_RESULT -> SEND_TO_MODEL)* ->
    FINAL_RESPONSE

`ResearchAgent` decides nothing about *which* research action to take --
that is the model's job, mediated entirely through
`src.ai.agents.provider.LLMProvider`. `ResearchAgent` is responsible
only for: driving the loop, enforcing budgets and policy (Sprint 13
spec, section 50: "the agent runtime itself must enforce these limits,"
never the model), validating and executing tool calls via
`src.ai.agents.tools.ToolRegistry`, accumulating session evidence, and
producing a structured `AgentRun` + `AgentResearchReport` at the end.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from src.ai.agents.models import (
    AgentEvidence,
    AgentResearchReport,
    AgentRun,
    AgentRunStatus,
    ToolCallRecord,
    TrialResult,
    utc_now,
)
from src.ai.agents.policy import ResearchAgentPolicy
from src.ai.agents.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from src.ai.agents.provider import LLMMessage, LLMProvider, LLMToolCall, ProviderError
from src.ai.agents.session import AgentSession
from src.ai.agents.store import AgentRunStore
from src.ai.agents.tools import ToolRegistry, ToolResult

_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class ResearchAgent:
    """Orchestrates one bounded research investigation.

    Args:
        provider: the `LLMProvider` to reason with -- injected, never
            constructed internally (Sprint 13 spec, section 5).
        tool_registry: which tools exist and how to execute them
            (`src.ai.agents.tools.ToolRegistry`).
        policy: budgets and permissions. Defaults to
            `ResearchAgentPolicy()` -- the research-only default.
        system_prompt / system_prompt_version: the versioned system
            prompt. Defaults to `src.ai.agents.prompts.SYSTEM_PROMPT`.
        run_store: where to persist the resulting `AgentRun` audit
            record. Defaults to `AgentRunStore()`
            (`data/agent_runs/`). Pass `None` explicitly via
            `persist=False` to skip persistence entirely (mainly for
            tests that don't want filesystem side effects).
        persist: whether `run()` saves the resulting `AgentRun` via
            `run_store`. Defaults to `True`.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        policy: ResearchAgentPolicy | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        system_prompt_version: str = SYSTEM_PROMPT_VERSION,
        run_store: AgentRunStore | None = None,
        persist: bool = True,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._policy = policy or ResearchAgentPolicy()
        self._system_prompt = system_prompt
        self._system_prompt_version = system_prompt_version
        self._run_store = run_store or AgentRunStore()
        self._persist = persist

    def run(self, goal: str) -> AgentRun:
        """Investigate `goal` and return a complete `AgentRun`.

        Never raises: every failure mode (provider failure, tool
        failure, budget exhaustion) is captured as a structured
        `AgentRun.status`/`error` instead (Sprint 13 spec, section 8).
        """
        run_id = uuid.uuid4().hex
        started_at = utc_now()
        session = AgentSession(goal=goal)
        session.add_message(LLMMessage(role="user", content=goal))

        tool_defs = self._tools.allowed_definitions(self._policy)
        status = AgentRunStatus.COMPLETED
        error: str | None = None
        final_report: AgentResearchReport | None = None

        try:
            final_report = self._loop(run_id, goal, session, tool_defs)
            status = AgentRunStatus(final_report.status)
        except ProviderError as exc:
            status = AgentRunStatus.FAILED
            error = f"{exc.code}: {exc.message}"
        except _BudgetExhausted:
            status = AgentRunStatus.BUDGET_EXHAUSTED
            final_report = self._build_report(
                run_id,
                goal,
                AgentRunStatus.BUDGET_EXHAUSTED,
                "Research budget (steps or backtests) was exhausted before a "
                "final answer could be produced.",
                session,
                limitations=[
                    "This investigation stopped due to step/backtest budget "
                    "exhaustion -- any observations above are based on "
                    "incomplete research, not a completed investigation."
                ],
            )

        finished_at = utc_now()
        run = AgentRun(
            run_id=run_id,
            goal=goal,
            provider=self._provider.name,
            model=self._provider.model,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            step_count=session.step_count,
            tool_call_count=len(session.tool_calls),
            backtest_count=session.backtest_count,
            system_prompt_version=self._system_prompt_version,
            tool_schema_version=self._tools.schema_hash(self._policy),
            policy_hash=self._policy.config_hash(),
            tool_calls=list(session.tool_calls),
            trial_ids=list(session.trials),
            experiment_ids=list(session.experiment_ids),
            final_report=final_report,
            error=error,
        )
        if self._persist:
            self._run_store.save(run)
        return run

    # ------------------------------------------------------------------
    # The loop itself.
    # ------------------------------------------------------------------

    def _loop(
        self, run_id: str, goal: str, session: AgentSession, tool_defs: list[dict[str, Any]]
    ) -> AgentResearchReport:
        while True:
            if session.step_count >= self._policy.max_steps:
                raise _BudgetExhausted()
            session.step_count += 1

            response = self._provider.generate(
                system=self._system_prompt, messages=session.messages, tools=tool_defs
            )

            if not response.tool_calls:
                text = response.text or ""
                session.add_message(LLMMessage(role="assistant", content=text))
                return self._build_report(run_id, goal, AgentRunStatus.COMPLETED, text, session)

            session.add_message(
                LLMMessage(
                    role="assistant", content=response.text or "", tool_calls=response.tool_calls
                )
            )
            for call in response.tool_calls:
                result = self._handle_tool_call(call, session)
                session.add_message(
                    LLMMessage(
                        role="tool",
                        content=json.dumps(_tool_message_payload(result)),
                        tool_call_id=call.id,
                    )
                )
                if result.error_code == _BUDGET_EXHAUSTED:
                    raise _BudgetExhausted()

    def _handle_tool_call(self, call: LLMToolCall, session: AgentSession) -> ToolResult:
        if call.name == "run_historical_backtest" and session.backtest_count >= self._policy.max_backtests:
            result = ToolResult.error(
                call.name,
                _BUDGET_EXHAUSTED,
                f"max_backtests ({self._policy.max_backtests}) has already been reached "
                f"for this run -- no further historical backtests may be run",
            )
        else:
            result = self._tools.execute(call.name, call.arguments, self._policy)

        session.record_tool_call(
            ToolCallRecord(
                step=session.step_count,
                tool_name=call.name,
                arguments=dict(call.arguments) if isinstance(call.arguments, dict) else {},
                is_error=result.is_error,
                result_summary=result.data if not result.is_error else {},
                error_code=result.error_code,
                error_message=result.error_message,
            )
        )
        if not result.is_error:
            self._absorb_result(call.name, result, session)
        return result

    def _absorb_result(self, tool_name: str, result: ToolResult, session: AgentSession) -> None:
        """Turn a successful tool result into session evidence/trials
        (Sprint 13 spec, sections 39, 66) -- generic bookkeeping shared
        by every tool, kept here rather than duplicated per tool."""
        data = result.data

        if tool_name == "run_historical_backtest":
            trial = TrialResult(
                trial_id=data["trial_id"],
                strategy_name=data["strategy_name"],
                strategy_version=data["strategy_version"],
                symbol=data["symbol"],
                interval=data["interval"],
                dataset_fingerprint=data["dataset_fingerprint"],
                dataset_start=data["dataset_start"],
                dataset_end=data["dataset_end"],
                risk_config=data["risk_config"],
                execution_config=data["execution_config"],
                analytics=data["analytics"],
                trade_count=data["trade_count"],
                model_id=data.get("model_id"),
                feature_set_id=data.get("feature_set_id"),
                label_set_id=data.get("label_set_id"),
            )
            session.record_trial(trial)
            session.backtest_count += 1
            session.record_evidence(
                AgentEvidence(
                    kind="trial",
                    reference=trial.trial_id,
                    description=(
                        f"Historical backtest trial for {trial.strategy_name} on "
                        f"{trial.symbol}/{trial.interval} ({trial.dataset_start} to "
                        f"{trial.dataset_end})"
                    ),
                    data=data["analytics"],
                )
            )
        elif tool_name in ("get_experiment", "analyze_experiment"):
            experiment_id = data.get("experiment_id")
            if experiment_id is not None:
                session.record_experiment_id(str(experiment_id))
                session.record_evidence(
                    AgentEvidence(
                        kind="experiment",
                        reference=str(experiment_id),
                        description=f"Evidence from experiment {experiment_id}",
                        data=data,
                    )
                )
        elif tool_name == "compare_experiments":
            ids = [str(row["experiment_id"]) for row in data.get("rows", []) if row.get("experiment_id") is not None]
            for experiment_id in ids:
                session.record_experiment_id(experiment_id)
            session.record_evidence(
                AgentEvidence(
                    kind="comparison",
                    reference=",".join(ids),
                    description=f"Comparison across experiments {', '.join(ids)}",
                    data=data,
                )
            )
        elif tool_name == "list_experiments":
            for row in data.get("experiments", []):
                if row.get("experiment_id") is not None:
                    session.record_experiment_id(str(row["experiment_id"]))
        elif tool_name == "get_model_metadata":
            session.record_evidence(
                AgentEvidence(
                    kind="model",
                    reference=str(data.get("model_id", "")),
                    description=f"Model metadata for {data.get('model_id', '')}",
                    data=data,
                )
            )

    def _build_report(
        self,
        run_id: str,
        goal: str,
        status: AgentRunStatus,
        text: str,
        session: AgentSession,
        limitations: list[str] | None = None,
    ) -> AgentResearchReport:
        observations, hypotheses, suggestions = _parse_sections(text)
        experiments = list(dict.fromkeys(list(session.experiment_ids) + list(session.trials)))
        return AgentResearchReport(
            run_id=run_id,
            goal=goal,
            status=status,
            summary=text,
            observations=observations,
            hypotheses=hypotheses,
            experiments=experiments,
            evidence=list(session.evidence),
            limitations=limitations or [],
            suggested_next_experiments=suggestions,
        )


class _BudgetExhausted(Exception):
    """Internal control-flow signal only -- never escapes `run()`."""


def _tool_message_payload(result: ToolResult) -> dict[str, Any]:
    if result.is_error:
        return {"status": "error", "error_code": result.error_code, "error_message": result.error_message}
    return {"status": "ok", "data": result.data}


def _parse_sections(text: str) -> tuple[list[str], list[str], list[str]]:
    """Extract labeled `Observed:`/`Hypothesis:`/suggested-next-experiment
    lines from the model's final free text (Sprint 13 spec, section 37)
    -- a deliberately simple, line-based convention (established by
    `SYSTEM_PROMPT`'s own instructions to the model), not an attempt at
    general natural-language parsing."""
    observations: list[str] = []
    hypotheses: list[str] = []
    suggestions: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered.startswith("observed:"):
            observations.append(line)
        elif lowered.startswith("hypothesis:"):
            hypotheses.append(line)
        elif lowered.startswith("suggested next experiment") or lowered.startswith(
            "suggested next experiments"
        ):
            suggestions.append(line)
    return observations, hypotheses, suggestions
