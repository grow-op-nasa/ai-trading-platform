"""The AI Research Agent -- Sprint 13 (`DECISIONS.md`, ADR-0046).

The platform's first genuinely agentic loop: goal -> reason about
available research actions -> tool call -> observe result -> reason
again -> ... -> final research result. Deliberately layered above every
existing research capability (`src.experiments`, `src.analytics`,
`src.data`, `src.strategies`, `src.ai.registry`, `src.backtesting`)
rather than replacing or bypassing any of them -- this package owns no
domain logic of its own beyond orchestration, permission enforcement,
and audit.

    from src.ai.agents import ResearchAgent, ResearchAgentPolicy
    from src.ai.agents.tools import default_tool_registry

    agent = ResearchAgent(provider=..., tool_registry=default_tool_registry(), policy=ResearchAgentPolicy())
    run = agent.run("Investigate whether the EMA strategy's drawdown is concentrated in volatile regimes.")

This is a **research agent, not a trading agent** (Sprint 13 spec,
sections 1, 30-33, 92): it cannot place trades, approve trades, modify
risk, modify portfolio state, deploy models, or promote strategies.
Those capabilities are not merely denied by policy configuration -- the
tools that would perform them do not exist anywhere in this package
(section 92: "the answer must be structurally impossible because those
tools are not present").
"""

from __future__ import annotations

from src.ai.agents.agent import ResearchAgent
from src.ai.agents.models import (
    AgentEvidence,
    AgentResearchReport,
    AgentRun,
    AgentRunStatus,
    ToolCallRecord,
    TrialResult,
)
from src.ai.agents.policy import ResearchAgentPolicy
from src.ai.agents.provider import (
    FakeLLMProvider,
    LLMMessage,
    LLMProvider,
    LLMResponse,
    LLMToolCall,
    ProviderError,
)
from src.ai.agents.session import AgentSession
from src.ai.agents.tools import AgentTool, ToolRegistry, ToolResult, default_tool_registry

__all__ = [
    "ResearchAgent",
    "AgentEvidence",
    "AgentResearchReport",
    "AgentRun",
    "AgentRunStatus",
    "ToolCallRecord",
    "TrialResult",
    "ResearchAgentPolicy",
    "FakeLLMProvider",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMToolCall",
    "ProviderError",
    "AgentSession",
    "AgentTool",
    "ToolRegistry",
    "ToolResult",
    "default_tool_registry",
]
