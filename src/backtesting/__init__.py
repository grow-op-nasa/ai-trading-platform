"""The Backtesting Framework.

Not a strategy -- the framework: run strategy -> collect trades ->
calculate metrics -> generate report. Part of the Sprint 2 research
engine (`DECISIONS.md`, ADR-0009). Public entry point: `Backtester`.

Two execution modes as of Sprint 11 (`DECISIONS.md`, ADR-0044):
`Backtester.run()` (the original, unit-sized model, `RiskMode.
LEGACY_UNIT`, unchanged since ADR-0011) and `Backtester.run_portfolio()`
(the new portfolio-aware, risk-sized model, `RiskMode.PORTFOLIO_RISK`,
via `BacktestConfig` + `StopPolicy`) -- both available side by side.
"""

from src.backtesting.config import BacktestConfig, RiskMode
from src.backtesting.engine import Backtester
from src.backtesting.models import BacktestResult, Trade
from src.backtesting.risk_audit import RiskAuditSummary, SignalOutcome, summarize_outcomes
from src.backtesting.stop_policy import ATRStopPolicy, StopPolicy, StopResult

__all__ = [
    "Backtester",
    "BacktestResult",
    "Trade",
    "BacktestConfig",
    "RiskMode",
    "StopPolicy",
    "StopResult",
    "ATRStopPolicy",
    "SignalOutcome",
    "RiskAuditSummary",
    "summarize_outcomes",
]
