"""The agent policy -- src/ai/agents/policy.py.

Sprint 13 spec, sections 3 (Phase 3), 60-62: permissions are enforced by
code, never by prompt language alone. `ToolRegistry` (`tools.py`) consults
`ResearchAgentPolicy` to decide which tools a given `ResearchAgent`
instance is even offered -- a tool the policy doesn't permit is not
merely refused at execution time, it is never included in what the LLM
provider is told exists (Sprint 13 spec, section 62: "the policy should
determine the actual executable tools").

**Structural safety, not just configuration** (Sprint 13 spec, sections
30-33, 92): the flags below gate the handful of *research* tools this
package implements. There is no `submit_order`/`modify_portfolio`/
`train_model`/`write_strategy`/`execute_shell`/`arbitrary_http` tool
anywhere in `src.ai.agents.tools` for any policy to permit -- setting a
hypothetical `allow_live_trading=True` would have no effect, because no
tool checks that flag (there is no live-trading tool to gate). The
flags exist so this module's own intent is explicit and testable, and
so a *future* tool (a Sprint 14+ Strategy Development Agent, say) has an
established place to plug a real permission check into, not because
today's tool surface needs gating beyond "is this tool registered."
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_BACKTESTS = 4


@dataclass(frozen=True)
class ResearchAgentPolicy:
    """Explicit, code-enforced permissions and budgets for one
    `ResearchAgent` instance (Sprint 13 spec, section 60).

    Args:
        max_steps: maximum number of agent-loop iterations before the
            run terminates with `AgentRunStatus.BUDGET_EXHAUSTED`.
        max_backtests: maximum number of `run_historical_backtest` tool
            calls permitted in one run. Enforced by the agent runtime
            itself (Sprint 13 spec, section 50), never left to the
            model to self-limit.
        allow_historical_backtests: whether `run_historical_backtest` is
            offered at all.
        allow_experiment_reads: whether `list_experiments`/
            `get_experiment`/`analyze_experiment`/`compare_experiments`
            are offered.
        allow_model_reads: whether `list_strategies`/
            `get_model_metadata` are offered.
        allow_live_trading: always `False` in this sprint -- no tool
            exists to gate (Sprint 13 spec, section 31). Kept as an
            explicit field so the platform's intent is visible and
            testable rather than merely implied by an absent tool.
        allow_portfolio_mutation: always `False` -- no tool exists to
            gate (section 32).
        allow_strategy_generation: always `False` -- no tool exists to
            gate (section 57).
        allow_model_training: always `False` -- no tool exists to gate
            (section 58).

    Raises:
        ValueError: `max_steps` or `max_backtests` isn't a positive
            integer, or any of the four dangerous flags is `True`
            (Sprint 13 spec, section 61: "the default policy must
            guarantee... live execution = denied" -- this is not an
            optional configuration choice for this sprint's agent).
    """

    max_steps: int = DEFAULT_MAX_STEPS
    max_backtests: int = DEFAULT_MAX_BACKTESTS
    allow_historical_backtests: bool = True
    allow_experiment_reads: bool = True
    allow_model_reads: bool = True
    allow_live_trading: bool = False
    allow_portfolio_mutation: bool = False
    allow_strategy_generation: bool = False
    allow_model_training: bool = False

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError(f"max_steps must be a positive integer, got {self.max_steps}")
        if self.max_backtests < 1:
            raise ValueError(f"max_backtests must be a positive integer, got {self.max_backtests}")
        for flag_name in (
            "allow_live_trading",
            "allow_portfolio_mutation",
            "allow_strategy_generation",
            "allow_model_training",
        ):
            if getattr(self, flag_name):
                raise ValueError(
                    f"{flag_name} must be False -- Sprint 13's research agent has no "
                    f"tool implementing this capability at all (DECISIONS.md, ADR-0046); "
                    f"there is nothing this flag could safely enable"
                )

    def describe(self) -> dict:
        """A JSON-safe, reproducible description of this policy -- what
        `config_hash()` hashes, and what `AgentRun` provenance records
        alongside `system_prompt_version`/`tool_schema_version`."""
        return asdict(self)

    def config_hash(self) -> str:
        """A stable SHA-256 hash of `describe()` -- this policy's
        identity for `AgentRun.policy_hash` (Sprint 13 spec, section
        12): two policies with identical settings hash identically;
        any change to any field changes the hash."""
        canonical = json.dumps(self.describe(), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
