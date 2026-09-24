"""Sprint 14 -- AI Strategy Development Agent.

The platform's second AI agent capability. Where Sprint 13's
`src.ai.agents.agent.ResearchAgent` (`DECISIONS.md`, ADR-0046)
*investigates* existing, already-recorded evidence, this package's
`StrategyDevelopmentAgent` *creates and tests new candidate strategies*
-- a materially higher-risk capability, because it is the platform's
first exposure to AI-generated Python source code.

The core boundary this whole package exists to enforce (`DECISIONS.md`,
ADR-0047):

    AI can CREATE a candidate.
    AI can TEST a candidate.
    AI can ITERATE on validation evidence.
    AI can REPORT the evidence.

    AI cannot PROMOTE the candidate.
    AI cannot TRADE the candidate.
    AI cannot MODIFY the production strategy registry.

A candidate is never a production `Strategy` until a separate,
human-only mechanism (`src.cli.strategy_promote`, never an agent tool)
explicitly promotes it. This package re-uses Sprint 13's generic agent
infrastructure (`src.ai.agents.provider.LLMProvider`,
`src.ai.agents.tools.{AgentTool, ToolResult, ToolRegistry}`) rather than
duplicating it -- see `agent.py`'s module docstring for how the loop
mirrors `ResearchAgent._loop()`'s state machine while swapping in
candidate-specific budgets, tools, and report shape.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "StrategyDevelopmentAgent",
    "CandidateEvaluation",
    "CandidateStrategy",
    "CandidateStrategySpec",
    "CandidateStrategyStatus",
    "DevAgentRun",
    "DevResearchReport",
    "StrategyDevelopmentAgentPolicy",
    "ValidationResult",
    "validate_candidate_source",
    "default_dev_tool_registry",
    "CandidateRegistry",
    "CandidateWorkspace",
]

# Lazy attribute resolution (PEP 562), deliberately: `tools.py` (and
# `dev_agent.py`, which imports it) transitively pulls in
# `src.research.trial_service` -> `src.ai.registry.ModelRegistry` ->
# `joblib` -- a real, heavyweight dependency chain that has nothing to
# do with, say, `src.cli.strategy_promote` (a human-only command that
# only ever needs `CandidateRegistry`/`CandidateWorkspace` from
# `workspace.py` and the plain dataclasses in `models.py`). Eagerly
# importing everything here would force that unrelated dependency chain
# onto every consumer of this package merely by importing one of its
# submodules -- exactly the kind of accidental coupling ADR-0047 is
# meant to prevent between the agent and the human-only promotion path.
# `from src.ai.agents.strategy_dev import StrategyDevelopmentAgent` (the
# common case) still works unchanged; it just defers the heavy import
# until that name is actually accessed.
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "StrategyDevelopmentAgent": ("src.ai.agents.strategy_dev.dev_agent", "StrategyDevelopmentAgent"),
    "CandidateEvaluation": ("src.ai.agents.strategy_dev.models", "CandidateEvaluation"),
    "CandidateStrategy": ("src.ai.agents.strategy_dev.models", "CandidateStrategy"),
    "CandidateStrategySpec": ("src.ai.agents.strategy_dev.models", "CandidateStrategySpec"),
    "CandidateStrategyStatus": ("src.ai.agents.strategy_dev.models", "CandidateStrategyStatus"),
    "DevAgentRun": ("src.ai.agents.strategy_dev.models", "DevAgentRun"),
    "DevResearchReport": ("src.ai.agents.strategy_dev.models", "DevResearchReport"),
    "StrategyDevelopmentAgentPolicy": (
        "src.ai.agents.strategy_dev.policy",
        "StrategyDevelopmentAgentPolicy",
    ),
    "ValidationResult": ("src.ai.agents.strategy_dev.safety", "ValidationResult"),
    "validate_candidate_source": ("src.ai.agents.strategy_dev.safety", "validate_candidate_source"),
    "default_dev_tool_registry": ("src.ai.agents.strategy_dev.tools", "default_dev_tool_registry"),
    "CandidateRegistry": ("src.ai.agents.strategy_dev.workspace", "CandidateRegistry"),
    "CandidateWorkspace": ("src.ai.agents.strategy_dev.workspace", "CandidateWorkspace"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(__all__)
