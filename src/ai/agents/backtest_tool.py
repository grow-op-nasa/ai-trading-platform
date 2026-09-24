"""The `run_historical_backtest` tool -- src/ai/agents/backtest_tool.py.

Sprint 13 spec, sections 16, 23-29 (Phase 7): the most important agent
tool, and the only one that produces new evidence rather than reading
existing evidence. Uses the platform's existing architecture end to end
via `src.research.trial_service.ResearchTrialService` --

    MarketDataService -> canonical dataset -> Strategy -> Signal ->
    PortfolioBacktestEngine -> PortfolioRiskEngine -> ExecutionModel ->
    Portfolio -> Analytics

-- and implements no second backtest pipeline of its own (Sprint 13
spec, section 24: never `scripts/run_experiment.py`, never a subprocess).

**Bounded, always** (Sprint 13 spec, sections 26-28): a fixed maximum
date range, historical-only dates (enforced twice -- once here, once
inside `ResearchTrialService` itself, since both are cheap and a
security boundary should never depend on a single check succeeding),
and every numeric input range-checked by `AgentTool.schema()` before
this class's `execute()` is ever called. There is no way to pass a
filesystem path, URL, shell command, broker name, or credential through
this tool's schema -- `properties` lists exactly the fields below and
`ToolRegistry.execute()` rejects anything else (Sprint 13 spec, section
27).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from src.ai.agents.tools import PERMISSION_HISTORICAL_BACKTESTS, ToolResult
from src.data.base import Interval
from src.data.exceptions import DataProviderError, DataValidationError, NoDataError
from src.research.trial_service import ResearchTrialService

MAX_DATE_RANGE_DAYS = 3650  # 10 years -- generous for research, still bounded.
VALID_INTERVALS = tuple(interval.value for interval in Interval)
VALID_TIMINGS = ("SIGNAL_BAR_CLOSE", "NEXT_BAR_OPEN")
VALID_STRATEGY_PARAM_TYPES = (str, int, float, bool)
MAX_STRATEGY_PARAMS = 10


def _analytics_summary(analytics: Any) -> dict[str, Any]:
    def m(metric: Any) -> dict[str, Any]:
        return {"value": metric.value, "status": metric.status.value, "reason": metric.reason}

    return {
        "total_pnl": m(analytics.total_pnl),
        "total_return": m(analytics.total_return),
        "sharpe_ratio": m(analytics.sharpe_ratio),
        "max_drawdown": m(analytics.max_drawdown),
        "win_rate": m(analytics.win_rate),
        "trade_count": analytics.trade_count,
        "profit_factor": m(analytics.profit_factor),
        "expectancy": m(analytics.expectancy),
        "net_pnl_dollars": m(analytics.net_pnl_dollars),
        "has_execution_cost_detail": analytics.has_execution_cost_detail,
        "total_fees_dollars": m(analytics.total_fees_dollars),
        "total_slippage_cost_dollars": m(analytics.total_slippage_cost_dollars),
        "net_pnl_after_costs_dollars": m(analytics.net_pnl_after_costs_dollars),
    }


class RunHistoricalBacktestTool:
    """Runs one bounded, historical, portfolio-aware backtest trial and
    returns a compact, provenance-complete summary.

    Args:
        research_trial_service: the platform's own
            `ResearchTrialService` -- this class implements no backtest
            logic itself.
    """

    name = "run_historical_backtest"
    description = (
        "Run one bounded historical backtest trial for a registered strategy "
        "against real historical market data, through the platform's existing "
        "portfolio-aware risk and execution simulation. Returns a compact "
        "analytics summary and a trial_id. Historical data only -- never live "
        "quotes, live orders, or live account state. Does not place trades, "
        "approve trades, or modify any risk/portfolio/account state."
    )
    required_permission = PERMISSION_HISTORICAL_BACKTESTS

    def __init__(self, research_trial_service: ResearchTrialService) -> None:
        self._service = research_trial_service

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "strategy_name": {"type": "string"},
                "strategy_params": {"type": "object"},
                "symbol": {"type": "string"},
                "interval": {"type": "string", "enum": list(VALID_INTERVALS)},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "initial_cash": {"type": "number", "minimum": 1.0},
                "allocation_per_trade_pct": {"type": "number", "minimum": 0.0001, "maximum": 1.0},
                "max_portfolio_exposure_pct": {"type": "number", "minimum": 0.0001, "maximum": 1.0},
                "risk_pct_per_trade": {"type": "number", "minimum": 0.0001, "maximum": 1.0},
                "execution_timing": {"type": "string", "enum": list(VALID_TIMINGS)},
                "slippage_bps": {"type": "number", "minimum": 0.0},
                "fee_bps": {"type": "number", "minimum": 0.0},
                "fixed_fee": {"type": "number", "minimum": 0.0},
            },
            "required": ["strategy_name", "symbol", "interval", "start", "end"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        error = self._validate_semantics(arguments)
        if error is not None:
            code, message = error
            return ToolResult.error(self.name, code, message)

        start = datetime.strptime(arguments["start"], "%Y-%m-%d").date()
        end = datetime.strptime(arguments["end"], "%Y-%m-%d").date()

        try:
            outcome = self._service.run_trial(
                strategy_name=arguments["strategy_name"],
                strategy_params=dict(arguments.get("strategy_params", {})),
                symbol=arguments["symbol"],
                interval=arguments["interval"],
                start=start,
                end=end,
                initial_cash=arguments.get("initial_cash", 100_000.0),
                allocation_per_trade_pct=arguments.get("allocation_per_trade_pct", 0.10),
                max_portfolio_exposure_pct=arguments.get("max_portfolio_exposure_pct", 0.50),
                risk_pct_per_trade=arguments.get("risk_pct_per_trade", 0.01),
                execution_timing=arguments.get("execution_timing", "SIGNAL_BAR_CLOSE"),
                slippage_bps=arguments.get("slippage_bps", 0.0),
                fee_bps=arguments.get("fee_bps", 0.0),
                fixed_fee=arguments.get("fixed_fee", 0.0),
            )
        except KeyError as exc:
            return ToolResult.error(self.name, "UNKNOWN_STRATEGY", str(exc))
        except NoDataError as exc:
            return ToolResult.error(self.name, "NO_DATA", str(exc))
        except (DataValidationError, DataProviderError) as exc:
            return ToolResult.error(self.name, "DATA_ERROR", str(exc))
        except ValueError as exc:
            return ToolResult.error(self.name, "INVALID_ARGUMENTS", str(exc))
        except TypeError as exc:
            return ToolResult.error(self.name, "INVALID_STRATEGY_PARAMS", str(exc))

        spec = outcome.spec
        return ToolResult.ok(
            self.name,
            {
                "trial_id": outcome.trial_id,
                "strategy_name": spec.strategy_name,
                "strategy_version": spec.strategy_version,
                "symbol": spec.symbol,
                "interval": spec.interval.value,
                "dataset_fingerprint": spec.dataset_fingerprint,
                "dataset_start": spec.dataset_start.isoformat(),
                "dataset_end": spec.dataset_end.isoformat(),
                "risk_config": spec.risk_config,
                "execution_config": spec.backtest_config.get("execution", {}),
                "trade_count": len(outcome.result.trades),
                "analytics": _analytics_summary(outcome.analytics),
                "model_id": outcome.model_id,
                "feature_set_id": outcome.feature_set_id,
                "label_set_id": outcome.label_set_id,
            },
        )

    def _validate_semantics(self, arguments: dict[str, Any]) -> tuple[str, str] | None:
        # Schema validation (ToolRegistry.execute()) already guarantees
        # types/enums/numeric ranges and that no unexpected field (a
        # filesystem path, URL, shell command, broker name, or
        # credential) is present at all -- this method only checks the
        # semantic constraints a JSON-schema subset can't express.
        strategy_params = arguments.get("strategy_params", {})
        if not isinstance(strategy_params, dict):
            return "INVALID_ARGUMENTS", "strategy_params must be an object"
        if len(strategy_params) > MAX_STRATEGY_PARAMS:
            return (
                "INVALID_ARGUMENTS",
                f"strategy_params must have at most {MAX_STRATEGY_PARAMS} entries",
            )
        for key, value in strategy_params.items():
            if not isinstance(key, str) or not isinstance(value, VALID_STRATEGY_PARAM_TYPES):
                return (
                    "INVALID_ARGUMENTS",
                    f"strategy_params[{key!r}] must be a plain string/number/bool value",
                )

        try:
            start = datetime.strptime(arguments["start"], "%Y-%m-%d").date()
        except ValueError:
            return "INVALID_DATE_RANGE", f"start must be an ISO date (YYYY-MM-DD), got {arguments['start']!r}"
        try:
            end = datetime.strptime(arguments["end"], "%Y-%m-%d").date()
        except ValueError:
            return "INVALID_DATE_RANGE", f"end must be an ISO date (YYYY-MM-DD), got {arguments['end']!r}"

        if start > end:
            return "INVALID_DATE_RANGE", f"start ({start}) must not be after end ({end})"
        if end > date.today():
            return (
                "HISTORICAL_ONLY",
                f"end ({end}) is in the future -- this tool only runs against "
                f"historical data, never live quotes or live orders",
            )
        if (end - start).days > MAX_DATE_RANGE_DAYS:
            return (
                "INVALID_DATE_RANGE",
                f"date range ({(end - start).days} days) exceeds the maximum of "
                f"{MAX_DATE_RANGE_DAYS} days",
            )

        symbol = arguments["symbol"]
        if not symbol or not all(c.isalnum() or c in ".-" for c in symbol) or len(symbol) > 16:
            return "INVALID_ARGUMENTS", f"symbol {symbol!r} is not a valid ticker symbol"

        return None
