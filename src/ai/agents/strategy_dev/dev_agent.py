"""The Strategy Development Agent loop -- src/ai/agents/strategy_dev/dev_agent.py.

Sprint 14 spec, section 2: reuses Sprint 13's genuinely generic
primitives directly -- `src.ai.agents.provider.{LLMProvider,
LLMMessage, LLMToolCall, ProviderError}` and
`src.ai.agents.tools.{ToolRegistry, ToolResult}` are used *unmodified*,
not duplicated. What differs from `src.ai.agents.agent.ResearchAgent`
is the bookkeeping a bounded state machine like this needs once its
domain is candidates rather than research trials: extra
budget checks on `create_candidate_strategy`/`run_candidate_backtest`,
and a `DevResearchReport` shape with no `trade_recommendation`-style
field, ever (Sprint 14 spec, section 52, 122).

A full generic `BoundedAgentLoop` base class shared by both agents was
considered (Sprint 14 spec, section 2) and deliberately not built this
sprint: `ResearchAgent._absorb_result()`/`_build_report()` are already
tightly coupled to research-report shape, and forcing both agents
through one shared loop implementation now would mean bending
`AgentSession`'s fields to fit two unrelated domains. Both agents
already share every piece that *is* domain-neutral (`LLMProvider`,
`ToolRegistry`, `ToolResult`, `AgentTool`) without any duplication of
*how* those work -- only the state-machine glue code (~150 lines) is
written twice, which this docstring flags as a real, explicit,
deliberate scope decision, not an oversight (`DECISIONS.md`, ADR-0047).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from src.ai.agents.models import ToolCallRecord, utc_now
from src.ai.agents.provider import LLMMessage, LLMProvider, LLMToolCall, ProviderError
from src.ai.agents.strategy_dev.models import DevAgentRun, DevResearchReport
from src.ai.agents.strategy_dev.policy import StrategyDevelopmentAgentPolicy
from src.ai.agents.strategy_dev.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from src.ai.agents.strategy_dev.session import DevAgentSession
from src.ai.agents.strategy_dev.store import DevAgentRunStore
from src.ai.agents.tools import ToolRegistry, ToolResult

_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class StrategyDevelopmentAgent:
    """Orchestrates one bounded strategy-development investigation.

    Args:
        provider: the `LLMProvider` to reason with -- injected, same
            contract Sprint 13's `ResearchAgent` uses.
        tool_registry: the candidate tool surface
            (`src.ai.agents.strategy_dev.tools.default_dev_tool_registry()`).
        policy: budgets and permissions. Defaults to
            `StrategyDevelopmentAgentPolicy()`.
        run_store: where `DevAgentRun` audit records are persisted.
        persist: whether `run()` saves the resulting `DevAgentRun`.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        policy: StrategyDevelopmentAgentPolicy | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        system_prompt_version: str = SYSTEM_PROMPT_VERSION,
        run_store: DevAgentRunStore | None = None,
        persist: bool = True,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._policy = policy or StrategyDevelopmentAgentPolicy()
        self._system_prompt = system_prompt
        self._system_prompt_version = system_prompt_version
        self._run_store = run_store or DevAgentRunStore()
        self._persist = persist

    def run(self, goal: str) -> DevAgentRun:
        run_id = uuid.uuid4().hex
        started_at = utc_now()
        session = DevAgentSession(goal=goal)
        session.add_message(LLMMessage(role="user", content=goal))

        run_context = {
            "run_id": run_id,
            "provider": self._provider.name,
            "model": self._provider.model,
            "system_prompt_version": self._system_prompt_version,
            "tool_schema_version": self._tools.schema_hash(self._policy),
            "policy_hash": self._policy.config_hash(),
        }
        # A few tools need to stamp provenance onto candidates they create;
        # they were constructed with an empty run_context dict at wiring
        # time (see tools.default_dev_tool_registry) -- fill it in now,
        # once per run, rather than requiring a new ToolRegistry per run.
        for tool in self._tools.all_tools():
            context = getattr(tool, "_run_context", None)
            if isinstance(context, dict):
                context.update(run_context)

        tool_defs = self._tools.allowed_definitions(self._policy)
        status = "COMPLETED"
        error: str | None = None
        final_report: DevResearchReport | None = None

        try:
            final_report = self._loop(run_id, goal, session, tool_defs)
            status = final_report.status
        except ProviderError as exc:
            status = "FAILED"
            error = f"{exc.code}: {exc.message}"
        except _BudgetExhausted:
            status = "BUDGET_EXHAUSTED"
            final_report = self._build_report(
                run_id,
                goal,
                status,
                "Development budget (steps, candidates, revisions, or backtests) "
                "was exhausted before a final answer could be produced.",
                session,
                limitations=[
                    "This development run stopped due to budget exhaustion -- any "
                    "candidates above reflect incomplete research, not a completed "
                    "development cycle."
                ],
            )

        finished_at = utc_now()
        run = DevAgentRun(
            run_id=run_id,
            goal=goal,
            provider=self._provider.name,
            model=self._provider.model,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            step_count=session.step_count,
            tool_call_count=len(session.tool_calls),
            candidates_created_count=session.candidates_created_count,
            candidate_revisions_count=session.candidate_revisions_count,
            validation_backtests_count=session.validation_backtests_count,
            final_test_evaluations_count=session.final_test_evaluations_count,
            system_prompt_version=self._system_prompt_version,
            tool_schema_version=run_context["tool_schema_version"],
            policy_hash=run_context["policy_hash"],
            tool_calls=list(session.tool_calls),
            candidate_ids=list(session.candidate_ids),
            final_report=final_report,
            error=error,
        )
        if self._persist:
            self._run_store.save(run)
        return run

    def _loop(
        self, run_id: str, goal: str, session: DevAgentSession, tool_defs: list[dict[str, Any]]
    ) -> DevResearchReport:
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
                return self._build_report(run_id, goal, "COMPLETED", text, session)

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

    def _handle_tool_call(self, call: LLMToolCall, session: DevAgentSession) -> ToolResult:
        budget_error = self._check_extra_budget(call, session)
        if budget_error is not None:
            result = budget_error
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
            self._absorb_result(call.name, call.arguments, result, session)
        return result

    def _check_extra_budget(self, call: LLMToolCall, session: DevAgentSession) -> ToolResult | None:
        """Runtime-enforced budgets beyond simple tool permission (Sprint
        14 spec, sections 32-33, 66) -- never trusted to the model."""
        if call.name == "create_candidate_strategy":
            if session.candidates_created_count >= self._policy.max_candidates:
                return ToolResult.error(
                    call.name,
                    _BUDGET_EXHAUSTED,
                    f"max_candidates ({self._policy.max_candidates}) has already been "
                    f"reached for this run -- no further candidates may be created",
                )
            is_revision = bool(
                isinstance(call.arguments, dict) and call.arguments.get("parent_candidate_id")
            )
            if is_revision and session.candidate_revisions_count >= self._policy.max_candidate_revisions:
                return ToolResult.error(
                    call.name,
                    _BUDGET_EXHAUSTED,
                    f"max_candidate_revisions ({self._policy.max_candidate_revisions}) has "
                    f"already been reached for this run",
                )
        elif call.name == "run_candidate_backtest" and isinstance(call.arguments, dict):
            stage = call.arguments.get("stage")
            if stage in ("development", "validation"):
                if session.validation_backtests_count >= self._policy.max_validation_backtests:
                    return ToolResult.error(
                        call.name,
                        _BUDGET_EXHAUSTED,
                        f"max_validation_backtests ({self._policy.max_validation_backtests}) "
                        f"has already been reached for this run",
                    )
            elif stage == "final_test":
                if session.final_test_evaluations_count >= self._policy.max_final_test_evaluations:
                    return ToolResult.error(
                        call.name,
                        _BUDGET_EXHAUSTED,
                        f"max_final_test_evaluations "
                        f"({self._policy.max_final_test_evaluations}) has already been "
                        f"reached for this run",
                    )
        return None

    def _absorb_result(
        self, tool_name: str, arguments: Any, result: ToolResult, session: DevAgentSession
    ) -> None:
        data = result.data
        if tool_name == "create_candidate_strategy":
            candidate_id = data.get("candidate_id")
            if candidate_id:
                is_revision = bool(isinstance(arguments, dict) and arguments.get("parent_candidate_id"))
                session.record_candidate(candidate_id, is_revision)
        elif tool_name == "freeze_candidate":
            candidate_id = data.get("candidate_id")
            if candidate_id:
                session.record_freeze(candidate_id)
        elif tool_name == "run_candidate_backtest":
            stage = data.get("stage")
            if stage in ("development", "validation"):
                session.validation_backtests_count += 1
            elif stage == "final_test":
                session.final_test_evaluations_count += 1
        elif tool_name == "get_candidate_report":
            session.candidate_reports.append(data)
        elif tool_name == "compare_candidate_to_baseline":
            session.baseline_comparisons.append(data)

    def _build_report(
        self,
        run_id: str,
        goal: str,
        status: str,
        text: str,
        session: DevAgentSession,
        limitations: list[str] | None = None,
    ) -> DevResearchReport:
        return DevResearchReport(
            run_id=run_id,
            goal=goal,
            status=status,
            summary=text,
            candidates_created=tuple(session.candidate_ids),
            candidates_frozen=tuple(session.frozen_candidate_ids),
            candidate_reports=tuple(session.candidate_reports),
            baseline_comparisons=tuple(session.baseline_comparisons),
            limitations=tuple(limitations or []),
            recommended_review_questions=(),
        )


class _BudgetExhausted(Exception):
    """Internal control-flow signal only -- never escapes `run()`."""


def _tool_message_payload(result: ToolResult) -> dict[str, Any]:
    if result.is_error:
        return {"status": "error", "error_code": result.error_code, "error_message": result.error_message}
    return {"status": "ok", "data": result.data}
