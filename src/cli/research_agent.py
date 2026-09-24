"""CLI entry point for the AI Research Agent -- src/cli/research_agent.py.

Sprint 13 spec, sections 70-72 (Phase 13):

    python -m src.cli research-agent --goal "Investigate whether the EMA \
strategy's drawdown is concentrated in volatile regimes"

Calls the exact same `src.ai.agents.agent.ResearchAgent` application
service any other caller (a notebook, a future API) would use --
nothing here duplicates agent logic. Fails clearly, with an actionable
message, when `ANTHROPIC_API_KEY` is absent (Sprint 13 spec, section
72) -- it never silently falls back to the deterministic
`ResearchReporter` and claims an agent ran.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from src.ai.agents.agent import ResearchAgent
from src.ai.agents.models import AgentRun, AgentRunStatus
from src.ai.agents.policy import ResearchAgentPolicy
from src.ai.agents.provider import ProviderError
from src.ai.agents.tools import default_tool_registry


def run_research_agent(argv: Sequence[str] | None = None) -> int:
    """Parse `argv`, run one `ResearchAgent` investigation, print a
    concise report, and return a process exit code (`0` on
    `AgentRunStatus.COMPLETED`, `1` otherwise -- including
    `BUDGET_EXHAUSTED`/`FAILED`, `2` for a configuration error that
    prevented the agent from running at all)."""
    parser = argparse.ArgumentParser(prog="python -m src.cli research-agent")
    parser.add_argument("--goal", required=True, help="the research objective to investigate")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-backtests", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        from src.ai.agents.anthropic_provider import AnthropicLLMProvider

        provider = AnthropicLLMProvider()
    except ProviderError as exc:
        print(f"research-agent: configuration error ({exc.code}): {exc.message}")
        print(
            "Set the ANTHROPIC_API_KEY environment variable to use the AI research "
            "agent (see DECISIONS.md, ADR-0046)."
        )
        return 2

    policy_kwargs: dict[str, int] = {}
    if args.max_steps is not None:
        policy_kwargs["max_steps"] = args.max_steps
    if args.max_backtests is not None:
        policy_kwargs["max_backtests"] = args.max_backtests
    policy = ResearchAgentPolicy(**policy_kwargs)

    agent = ResearchAgent(provider=provider, tool_registry=default_tool_registry(), policy=policy)
    run = agent.run(args.goal)
    _print_run(run)
    return 0 if run.status is AgentRunStatus.COMPLETED else 1


def _print_run(run: AgentRun) -> None:
    print(f"Agent Run ID: {run.run_id}")
    print(f"Goal: {run.goal}")
    print(f"Status: {run.status.value}")
    print(f"Provider/model: {run.provider}/{run.model}")
    print(
        f"Steps: {run.step_count}  Tool calls: {run.tool_call_count}  "
        f"Backtests: {run.backtest_count}"
    )

    if run.tool_calls:
        print("\nTool activity:")
        for call in run.tool_calls:
            if call.is_error:
                print(f"  x {call.tool_name}({call.arguments}) -- {call.error_code}: {call.error_message}")
            else:
                print(f"  -> {call.tool_name}({call.arguments})")

    if run.final_report is not None:
        report = run.final_report
        if report.observations:
            print("\nKey observations:")
            for observation in report.observations:
                print(f"  - {observation}")
        if report.hypotheses:
            print("\nHypotheses:")
            for hypothesis in report.hypotheses:
                print(f"  - {hypothesis}")
        if report.limitations:
            print("\nLimitations:")
            for limitation in report.limitations:
                print(f"  - {limitation}")
        if report.suggested_next_experiments:
            print("\nSuggested next experiments:")
            for suggestion in report.suggested_next_experiments:
                print(f"  - {suggestion}")
        print("\nFinal research summary:")
        print(report.summary)

    if run.error:
        print(f"\nError: {run.error}")
