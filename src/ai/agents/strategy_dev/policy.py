"""The Strategy Development Agent's policy -- src/ai/agents/strategy_dev/policy.py.

Sprint 14 spec, sections 32, 65-66: candidate generation is a
materially higher-risk capability than Sprint 13's read-only research
tools, so this policy enforces *more* budgets, not fewer -- every
budget is runtime-enforced (`StrategyDevelopmentAgent`, never the
model) and every dangerous capability is a hard-coded `False` the
constructor refuses to let a caller override, exactly matching
`src.ai.agents.policy.ResearchAgentPolicy`'s own established pattern
(there is deliberately no code duplication of *how* that enforcement
works, just a policy shaped for this agent's own tool surface).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

DEFAULT_MAX_STEPS = 16
DEFAULT_MAX_CANDIDATES = 3
DEFAULT_MAX_CANDIDATE_REVISIONS = 5
DEFAULT_MAX_VALIDATION_BACKTESTS = 8
DEFAULT_MAX_FINAL_TEST_EVALUATIONS = 1

_DANGEROUS_FLAGS = (
    "allow_live_trading",
    "allow_paper_trading",
    "allow_portfolio_mutation",
    "allow_risk_mutation",
    "allow_execution_mutation",
    "allow_model_training",
    "allow_production_strategy_mutation",
    "allow_production_strategy_registration",
    "allow_git_mutation",
    "allow_auto_promotion",
)


@dataclass(frozen=True)
class StrategyDevelopmentAgentPolicy:
    """Explicit, code-enforced permissions and budgets for one
    `StrategyDevelopmentAgent` run (Sprint 14 spec, section 65).

    Args:
        max_steps: maximum agent-loop iterations (Sprint 14 spec,
            section 32 -- starting value, not immutable).
        max_candidates: maximum distinct `candidate_id`s one run may
            create (section 39: exact-duplicate specs/source are
            rejected before consuming this budget).
        max_candidate_revisions: maximum total revisions across all
            candidates in one run (a revision is a new candidate with
            `parent_candidate_id` set).
        max_validation_backtests: maximum `run_candidate_backtest`
            calls against the development/validation split, across all
            candidates in one run.
        max_final_test_evaluations: maximum out-of-sample final test
            evaluations in one run (default 1 -- Sprint 14 spec,
            sections 34-36, 55: the whole point of the test lock is
            that this number is small and enforced).
        allow_model_reads / allow_experiment_reads /
            allow_indicator_reads: which read-only tool groups are
            offered. `allow_model_reads` gates `list_strategies`/
            `get_strategy_metadata` (named to match
            `src.ai.agents.tools.PERMISSION_MODEL_READS`, since this
            policy reuses Sprint 13's own `ListStrategiesTool` directly
            rather than duplicating it).
        allow_candidate_creation / allow_candidate_validation /
            allow_candidate_testing / allow_candidate_backtests /
            allow_candidate_comparison / allow_candidate_freeze /
            allow_candidate_reporting: which candidate-lifecycle tool
            groups are offered.
        allow_live_trading / allow_paper_trading /
            allow_portfolio_mutation / allow_risk_mutation /
            allow_execution_mutation / allow_model_training /
            allow_production_strategy_mutation /
            allow_production_strategy_registration / allow_git_mutation
            / allow_auto_promotion: always `False` -- Sprint 14 spec,
            sections 62-63, 92-93, 122: there is no tool in this
            package's registry implementing any of these, so this flag
            could not safely enable anything even if set (mirrors
            `ResearchAgentPolicy`'s own reasoning, `DECISIONS.md`
            ADR-0046 decision 4).

    Raises:
        ValueError: any budget is not a positive integer, or any
            dangerous flag is `True`.
    """

    max_steps: int = DEFAULT_MAX_STEPS
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_candidate_revisions: int = DEFAULT_MAX_CANDIDATE_REVISIONS
    max_validation_backtests: int = DEFAULT_MAX_VALIDATION_BACKTESTS
    max_final_test_evaluations: int = DEFAULT_MAX_FINAL_TEST_EVALUATIONS
    allow_model_reads: bool = True
    allow_experiment_reads: bool = True
    allow_indicator_reads: bool = True
    allow_candidate_creation: bool = True
    allow_candidate_validation: bool = True
    allow_candidate_testing: bool = True
    allow_candidate_backtests: bool = True
    allow_candidate_comparison: bool = True
    allow_candidate_freeze: bool = True
    allow_candidate_reporting: bool = True
    allow_live_trading: bool = False
    allow_paper_trading: bool = False
    allow_portfolio_mutation: bool = False
    allow_risk_mutation: bool = False
    allow_execution_mutation: bool = False
    allow_model_training: bool = False
    allow_production_strategy_mutation: bool = False
    allow_production_strategy_registration: bool = False
    allow_git_mutation: bool = False
    allow_auto_promotion: bool = False

    def __post_init__(self) -> None:
        for budget_name in (
            "max_steps",
            "max_candidates",
            "max_candidate_revisions",
            "max_validation_backtests",
            "max_final_test_evaluations",
        ):
            value = getattr(self, budget_name)
            if value < 1:
                raise ValueError(f"{budget_name} must be a positive integer, got {value}")
        for flag_name in _DANGEROUS_FLAGS:
            if getattr(self, flag_name):
                raise ValueError(
                    f"{flag_name} must be False -- the Strategy Development Agent has no "
                    f"tool implementing this capability at all (DECISIONS.md, ADR-0047); "
                    f"there is nothing this flag could safely enable. Promotion is a "
                    f"separate, human-only mechanism (src.cli.strategy_promote), never an "
                    f"agent tool."
                )

    def describe(self) -> dict:
        """A JSON-safe, reproducible description -- what `config_hash()`
        hashes, and what `DevAgentRun.policy_hash` records."""
        return asdict(self)

    def config_hash(self) -> str:
        """A stable SHA-256 hash of `describe()` -- this policy's
        identity for `DevAgentRun.policy_hash`."""
        canonical = json.dumps(self.describe(), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
