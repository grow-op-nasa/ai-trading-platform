"""CLI entry point for the AI Strategy Development Agent --
src/cli/strategy_dev.py.

    python -m src.cli strategy-dev --goal "Develop a candidate strategy \
that reduces the EMA strategy's drawdown during high-volatility regimes"

Mirrors `src.cli.research_agent`'s own structure exactly: fails clearly
(exit code 2) without `ANTHROPIC_API_KEY`, never silently substitutes a
deterministic fallback and calls it agent output.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from src.ai.agents.provider import ProviderError
from src.ai.agents.strategy_dev.dev_agent import StrategyDevelopmentAgent
from src.ai.agents.strategy_dev.models import DevAgentRun
from src.ai.agents.strategy_dev.policy import StrategyDevelopmentAgentPolicy
from src.ai.agents.strategy_dev.tools import default_dev_tool_registry


def run_strategy_dev_agent(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli strategy-dev")
    parser.add_argument("--goal", required=True, help="the strategy development objective")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--max-candidate-revisions", type=int, default=None)
    parser.add_argument("--max-validation-backtests", type=int, default=None)
    parser.add_argument("--max-final-test-evaluations", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        from src.ai.agents.anthropic_provider import AnthropicLLMProvider

        provider = AnthropicLLMProvider()
    except ProviderError as exc:
        print(f"strategy-dev: configuration error ({exc.code}): {exc.message}")
        print(
            "Set the ANTHROPIC_API_KEY environment variable to use the Strategy "
            "Development Agent (see DECISIONS.md, ADR-0047)."
        )
        return 2

    policy_kwargs: dict[str, int] = {}
    for flag, attr in (
        ("max_steps", "max_steps"),
        ("max_candidates", "max_candidates"),
        ("max_candidate_revisions", "max_candidate_revisions"),
        ("max_validation_backtests", "max_validation_backtests"),
        ("max_final_test_evaluations", "max_final_test_evaluations"),
    ):
        value = getattr(args, flag)
        if value is not None:
            policy_kwargs[attr] = value
    policy = StrategyDevelopmentAgentPolicy(**policy_kwargs)

    agent = StrategyDevelopmentAgent(
        provider=provider, tool_registry=default_dev_tool_registry(), policy=policy
    )
    run = agent.run(args.goal)
    _print_run(run)
    return 0 if run.status == "COMPLETED" else 1


def _print_run(run: DevAgentRun) -> None:
    print(f"Dev Agent Run ID: {run.run_id}")
    print(f"Goal: {run.goal}")
    print(f"Status: {run.status}")
    print(f"Provider/model: {run.provider}/{run.model}")
    print(
        f"Steps: {run.step_count}  Tool calls: {run.tool_call_count}  "
        f"Candidates: {run.candidates_created_count}  "
        f"Revisions: {run.candidate_revisions_count}  "
        f"Validation backtests: {run.validation_backtests_count}  "
        f"Final tests: {run.final_test_evaluations_count}"
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
        if report.candidates_created:
            print(f"\nCandidates created: {list(report.candidates_created)}")
        if report.candidates_frozen:
            print(f"Candidates frozen: {list(report.candidates_frozen)}")
        if report.limitations:
            print("\nLimitations:")
            for limitation in report.limitations:
                print(f"  - {limitation}")
        print("\nFinal summary:")
        print(report.summary)
        print(
            "\nNote: no candidate above has been promoted. Promotion is a "
            "separate, human-only step -- run:\n"
            "  python -m src.cli strategy-promote --candidate-id <id>\n"
            "after reviewing the candidate's evidence and source."
        )

    if run.error:
        print(f"\nError: {run.error}")
