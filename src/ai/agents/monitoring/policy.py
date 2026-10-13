"""The Market Monitoring Agent's policy -- src/ai/agents/monitoring/policy.py.

Sprint 15 (ADR-0050). Budgets are enforced by the agent runtime, never by
the model. Every dangerous capability is a hard-coded False that the
constructor refuses to let a caller enable: the agent has no tool that
implements any of them, so a True flag could not safely enable anything
(it is rejected at configuration time instead of being silently ignored).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_MONITORING_RUNS = 3
DEFAULT_MAX_MANDATORY_FINDING_RETRIEVALS = 20

FORBIDDEN_FLAGS = (
    "allow_live_trading",
    "allow_paper_trading",
    "allow_order_submission",
    "allow_order_cancellation",
    "allow_portfolio_mutation",
    "allow_risk_mutation",
    "allow_execution_mutation",
    "allow_recovery_actions",
    "allow_finding_mutation",
    "allow_config_mutation",
    "allow_model_training",
    "allow_strategy_registration",
    "allow_candidate_promotion",
    "allow_git_mutation",
    "allow_web_access",
    "allow_shell_access",
    "allow_filesystem_access",
    "allow_http_access",
)


@dataclass(frozen=True)
class MarketMonitoringAgentPolicy:
    """Code-enforced permissions and budgets for one agent run.

    Args:
        max_steps: maximum loop iterations (provider calls).
        max_monitoring_runs: maximum `run_monitoring_checks` executions.
        max_mandatory_finding_retrievals: cap on runtime-initiated `get_finding` calls per run;
            findings beyond it are reported as incomplete evidence, never silently skipped.
        allow_monitoring_runs / allow_finding_reads / allow_run_reads /
            allow_portfolio_reads / allow_execution_reads /
            allow_market_data_health_reads: which read tool groups are
            offered. Reads never mutate platform state.
        allow_*: every remaining flag must be False.
    """

    max_steps: int = DEFAULT_MAX_STEPS
    max_monitoring_runs: int = DEFAULT_MAX_MONITORING_RUNS
    max_mandatory_finding_retrievals: int = DEFAULT_MAX_MANDATORY_FINDING_RETRIEVALS
    allow_monitoring_runs: bool = True
    allow_finding_reads: bool = True
    allow_run_reads: bool = True
    allow_portfolio_reads: bool = True
    allow_execution_reads: bool = True
    allow_market_data_health_reads: bool = True
    allow_live_trading: bool = False
    allow_paper_trading: bool = False
    allow_order_submission: bool = False
    allow_order_cancellation: bool = False
    allow_portfolio_mutation: bool = False
    allow_risk_mutation: bool = False
    allow_execution_mutation: bool = False
    allow_recovery_actions: bool = False
    allow_finding_mutation: bool = False
    allow_config_mutation: bool = False
    allow_model_training: bool = False
    allow_strategy_registration: bool = False
    allow_candidate_promotion: bool = False
    allow_git_mutation: bool = False
    allow_web_access: bool = False
    allow_shell_access: bool = False
    allow_filesystem_access: bool = False
    allow_http_access: bool = False

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_monitoring_runs", "max_mandatory_finding_retrievals"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        for flag in FORBIDDEN_FLAGS:
            if getattr(self, flag):
                raise ValueError(
                    f"{flag} must be False -- the Market Monitoring Agent has no tool "
                    f"implementing this capability (ADR-0050); it observes and explains, "
                    f"it never acts"
                )

    def describe(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.describe(), sort_keys=True).encode("utf-8")).hexdigest()
