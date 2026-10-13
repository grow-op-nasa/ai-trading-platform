"""CLI for the Market Monitoring Agent -- src/cli/market_monitor_agent.py.

    python -m src.cli market-monitor-agent --goal "Is the paper portfolio healthy?"

Needs ANTHROPIC_API_KEY. Without it the command fails clearly (exit 2) and
never pretends an agent ran. The deterministic `monitor --once` command
needs no key. The agent can only observe and explain; see ADR-0050.
"""

from __future__ import annotations

import argparse
from typing import Callable, Sequence

from src.ai.agents.monitoring.agent import MarketMonitoringAgent
from src.ai.agents.monitoring.models import STATUS_COMPLETED, MonitoringAgentRun
from src.ai.agents.monitoring.policy import MarketMonitoringAgentPolicy
from src.ai.agents.monitoring.tools import default_monitoring_tool_registry
from src.ai.agents.provider import LLMProvider, ProviderError


def _default_provider() -> LLMProvider:
    from src.ai.agents.anthropic_provider import AnthropicLLMProvider

    # Long failure-state reports exceeded the 2048 default and were truncated (Sprint 16 live run).
    return AnthropicLLMProvider(max_tokens=4096)


def _default_service(experiment_id: int | None, db_path: str | None):
    from src.cli.monitor import _default_service as build

    return build(experiment_id, db_path)


def run_market_monitor_agent(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: Callable[[], LLMProvider] | None = None,
    service_factory: Callable[[int | None, str | None], object] | None = None,
    run_store=None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli market-monitor-agent")
    parser.add_argument("--goal", required=True, help="what to investigate")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-monitoring-runs", type=int, default=None)
    parser.add_argument("--experiment-id", type=int, default=None)
    parser.add_argument("--db", default=None, help="monitoring database path")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        provider = (provider_factory or _default_provider)()
    except ProviderError as exc:
        print(f"market-monitor-agent: configuration error ({exc.code}): {exc.message}")
        print("Set ANTHROPIC_API_KEY to use the agent. `python -m src.cli monitor --once` needs no key.")
        return 2

    kwargs = {}
    if args.max_steps is not None:
        kwargs["max_steps"] = args.max_steps
    if args.max_monitoring_runs is not None:
        kwargs["max_monitoring_runs"] = args.max_monitoring_runs
    try:
        policy = MarketMonitoringAgentPolicy(**kwargs)
        service = (service_factory or _default_service)(args.experiment_id, args.db)
    except Exception as exc:  # noqa: BLE001
        print(f"market-monitor-agent: failed to start: {type(exc).__name__}: {exc}")
        return 2

    agent = MarketMonitoringAgent(
        provider, default_monitoring_tool_registry(service), policy,
        **({"run_store": run_store} if run_store is not None else {}),
    )
    run = agent.run(args.goal)
    _print_run(run)
    return 0 if run.status == STATUS_COMPLETED else 1


def _print_run(run: MonitoringAgentRun) -> None:
    print(f"Agent Run ID: {run.run_id}")
    print(f"Goal: {run.goal}")
    print(f"Status: {run.status}")
    print(f"Provider/model: {run.provider}/{run.model}")
    print(f"Steps: {run.step_count}  Tool calls: {run.tool_call_count}  Monitoring runs: {run.monitoring_run_count}")
    if run.monitoring_run_ids:
        print("Monitoring runs referenced: " + ", ".join(run.monitoring_run_ids))
    for c in run.tool_calls:
        if c.is_error:
            print(f"  x {c.tool_name}({c.arguments}) -- {c.error_code}: {c.error_message}")
        else:
            print(f"  -> {c.tool_name}({c.arguments})")
    r = run.final_report
    if r is not None:
        for title, items in (
            ("Observed facts", r.observed_facts), ("Hypotheses (unproven)", r.hypotheses),
            ("Suggested investigations", r.suggested_investigations), ("Limitations", r.limitations),
        ):
            if items:
                print(f"\n{title}:")
                for i in items:
                    print(f"  - {i}")
        print("\nSummary:")
        print(r.summary)
    if run.error:
        print(f"\nError: {run.error}")
