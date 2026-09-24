"""The read-only research tools -- src/ai/agents/research_tools.py.

Sprint 13 spec, sections 16-22 (Phase 5): `list_experiments`,
`get_experiment`, `analyze_experiment`, `compare_experiments`,
`list_strategies`, `get_model_metadata`. Every tool consumes an
existing platform application service (`src.experiments.registry.
ExperimentRegistry`, `src.analytics.service.AnalyticsService`,
`src.strategies.registry`, `src.ai.registry.ModelRegistry`) -- none of
them recomputes a metric, re-derives a comparison, or duplicates any
calculation the platform already has an authoritative implementation
of (Sprint 13 spec, sections 19-20: "use the existing AnalyticsService
rather than recreating metrics in the tool").

Every result is compact and JSON-safe (Sprint 13 spec, section 101) --
no raw DataFrame, full trade list, or full equity curve ever crosses
this boundary; a tool returns summaries and lets the agent ask a more
specific tool for more detail when it genuinely needs it.
"""

from __future__ import annotations

import inspect
from typing import Any

from src.ai.agents.tools import (
    PERMISSION_EXPERIMENT_READS,
    PERMISSION_MODEL_READS,
    ToolResult,
)
from src.analytics.models import BacktestAnalytics, Metric
from src.analytics.service import AnalyticsService, compare_experiments
from src.experiments.registry import ExperimentRegistry

MAX_LIST_LIMIT = 50
DEFAULT_LIST_LIMIT = 20
MAX_COMPARE_EXPERIMENTS = 5
MAX_NARRATIVE_CHARS = 600


def _metric_dict(metric: Metric) -> dict[str, Any]:
    """`Metric` -> a compact JSON-safe dict -- `status`/`reason` travel
    with `value` so the model can never mistake `None`/undefined for a
    real zero (mirrors `Metric`'s own "never fabricate" contract)."""
    return {"value": metric.value, "status": metric.status.value, "reason": metric.reason}


def _analytics_dict(analytics: BacktestAnalytics) -> dict[str, Any]:
    """A compact, JSON-safe summary of a `BacktestAnalytics` -- every
    field the Sprint 13 spec's `analyze_experiment` tool asks for
    (section 19: return, P&L, Sharpe, drawdown, win rate, profit
    factor, expectancy, trade count, execution costs), identity fields
    included so the result is self-describing evidence on its own."""
    return {
        "experiment_id": analytics.experiment_id,
        "strategy_name": analytics.strategy_name,
        "strategy_version": analytics.strategy_version,
        "symbol": analytics.symbol,
        "interval": analytics.interval.value if analytics.interval else None,
        "dataset_fingerprint": analytics.dataset_fingerprint,
        "total_pnl": _metric_dict(analytics.total_pnl),
        "total_return": _metric_dict(analytics.total_return),
        "sharpe_ratio": _metric_dict(analytics.sharpe_ratio),
        "max_drawdown": _metric_dict(analytics.max_drawdown),
        "win_rate": _metric_dict(analytics.win_rate),
        "trade_count": analytics.trade_count,
        "profit_factor": _metric_dict(analytics.profit_factor),
        "expectancy": _metric_dict(analytics.expectancy),
        "volatility": _metric_dict(analytics.volatility),
        "has_trade_detail": analytics.has_trade_detail,
        "has_equity_curve": analytics.has_equity_curve,
        "has_quantity_detail": analytics.has_quantity_detail,
        "net_pnl_dollars": _metric_dict(analytics.net_pnl_dollars),
        "has_execution_cost_detail": analytics.has_execution_cost_detail,
        "total_fees_dollars": _metric_dict(analytics.total_fees_dollars),
        "total_slippage_cost_dollars": _metric_dict(analytics.total_slippage_cost_dollars),
        "net_pnl_after_costs_dollars": _metric_dict(analytics.net_pnl_after_costs_dollars),
    }


class ListExperimentsTool:
    """Sprint 13 spec, section 17 -- compact experiment metadata, never
    a full-table dump."""

    name = "list_experiments"
    description = (
        "List existing platform experiments, optionally filtered by decision, "
        "strategy name, symbol, or interval. Returns compact metadata only."
    )
    required_permission = PERMISSION_EXPERIMENT_READS

    def __init__(self, experiment_registry: ExperimentRegistry) -> None:
        self._registry = experiment_registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["KEEP", "DISCARD", "INCONCLUSIVE"]},
                "strategy_name": {"type": "string"},
                "symbol": {"type": "string"},
                "interval": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT},
            },
            "required": [],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        limit = min(int(arguments.get("limit", DEFAULT_LIST_LIMIT)), MAX_LIST_LIMIT)
        experiments = self._registry.list_experiments(
            decision=arguments.get("decision"), strategy_name=arguments.get("strategy_name")
        )
        symbol_filter = arguments.get("symbol")
        interval_filter = arguments.get("interval")

        rows: list[dict[str, Any]] = []
        for experiment in experiments:
            spec = self._registry.get_spec(experiment.id) if experiment.id is not None else None
            if symbol_filter is not None and (spec is None or spec.symbol != symbol_filter):
                continue
            if interval_filter is not None and (
                spec is None or spec.interval.value != interval_filter
            ):
                continue
            rows.append(
                {
                    "experiment_id": experiment.id,
                    "created_at": experiment.created_at,
                    "strategy_name": experiment.strategy_name,
                    "decision": experiment.decision,
                    "symbol": spec.symbol if spec else None,
                    "interval": spec.interval.value if spec else None,
                }
            )
            if len(rows) >= limit:
                break

        return ToolResult.ok(self.name, {"experiments": rows, "count": len(rows)})


class GetExperimentTool:
    """Sprint 13 spec, section 18 -- one experiment's full, but still
    compact, evidence package."""

    name = "get_experiment"
    description = (
        "Get a structured evidence package for one experiment by id: metadata, "
        "strategy/dataset/risk/execution identity, analytics summary, trade "
        "count, and research report summary (when available)."
    )
    required_permission = PERMISSION_EXPERIMENT_READS

    def __init__(self, experiment_registry: ExperimentRegistry) -> None:
        self._registry = experiment_registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"experiment_id": {"type": "integer", "minimum": 1}},
            "required": ["experiment_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        experiment_id = arguments["experiment_id"]
        experiment = self._registry.get_experiment(experiment_id)
        if experiment is None:
            return ToolResult.error(
                self.name, "NOT_FOUND", f"no experiment with id {experiment_id}"
            )

        spec = self._registry.get_spec(experiment_id)
        trades = self._registry.get_trades(experiment_id)
        report = self._registry.get_research_report(experiment_id)
        analytics = AnalyticsService().analyze_experiment(self._registry, experiment_id)

        data: dict[str, Any] = {
            "experiment_id": experiment_id,
            "created_at": experiment.created_at,
            "strategy_name": experiment.strategy_name,
            "decision": experiment.decision,
            "notes": experiment.notes,
            "trade_count": len(trades),
            "spec": None,
            "analytics": _analytics_dict(analytics) if analytics is not None else None,
            "research_report_summary": None,
        }
        if spec is not None:
            data["spec"] = {
                "strategy_name": spec.strategy_name,
                "strategy_version": spec.strategy_version,
                "strategy_params": spec.strategy_params,
                "symbol": spec.symbol,
                "interval": spec.interval.value,
                "dataset_start": spec.dataset_start.isoformat(),
                "dataset_end": spec.dataset_end.isoformat(),
                "dataset_source": spec.dataset_source,
                "dataset_fingerprint": spec.dataset_fingerprint,
                "risk_config": spec.risk_config,
                "backtest_config": spec.backtest_config,
            }
        if report is not None:
            narrative = report.narrative
            if len(narrative) > MAX_NARRATIVE_CHARS:
                narrative = narrative[:MAX_NARRATIVE_CHARS] + "..."
            data["research_report_summary"] = {
                "narrative": narrative,
                "rendered_by": report.rendered_by,
                "recommendation": report.findings.recommendation,
            }
        return ToolResult.ok(self.name, data)


class AnalyzeExperimentTool:
    """Sprint 13 spec, section 19 -- delegates entirely to
    `AnalyticsService`, never recomputes a metric."""

    name = "analyze_experiment"
    description = (
        "Compute the standard analytics summary (return, P&L, Sharpe, drawdown, "
        "win rate, profit factor, expectancy, trade count, execution costs) for "
        "one existing experiment, via the platform's AnalyticsService."
    )
    required_permission = PERMISSION_EXPERIMENT_READS

    def __init__(self, experiment_registry: ExperimentRegistry) -> None:
        self._registry = experiment_registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"experiment_id": {"type": "integer", "minimum": 1}},
            "required": ["experiment_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        experiment_id = arguments["experiment_id"]
        analytics = AnalyticsService().analyze_experiment(self._registry, experiment_id)
        if analytics is None:
            return ToolResult.error(
                self.name, "NOT_FOUND", f"no experiment with id {experiment_id}"
            )
        return ToolResult.ok(self.name, _analytics_dict(analytics))


class CompareExperimentsTool:
    """Sprint 13 spec, section 20 -- preserves comparison rows and
    warnings; never invents a composite "best strategy" score."""

    name = "compare_experiments"
    description = (
        "Compare 2-5 existing experiments side by side, via the platform's "
        "comparison logic. Returns each experiment's analytics plus any "
        "material-difference warnings (different symbol, timeframe, dataset, "
        "or strategy implementation). Never computes or returns a 'best "
        "strategy' ranking."
    )
    required_permission = PERMISSION_EXPERIMENT_READS

    def __init__(self, experiment_registry: ExperimentRegistry) -> None:
        self._registry = experiment_registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "experiment_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "maxItems": MAX_COMPARE_EXPERIMENTS,
                }
            },
            "required": ["experiment_ids"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        experiment_ids = arguments["experiment_ids"]
        if len(experiment_ids) < 2:
            return ToolResult.error(
                self.name, "INVALID_ARGUMENTS", "experiment_ids must contain at least 2 ids"
            )

        analytics_list = []
        for experiment_id in experiment_ids:
            analytics = AnalyticsService().analyze_experiment(self._registry, experiment_id)
            if analytics is None:
                return ToolResult.error(
                    self.name, "NOT_FOUND", f"no experiment with id {experiment_id}"
                )
            analytics_list.append(analytics)

        comparison = compare_experiments(analytics_list)
        return ToolResult.ok(
            self.name,
            {
                "rows": [_analytics_dict(row) for row in comparison.rows],
                "warnings": [
                    {"field": w.field, "values": list(w.values)} for w in comparison.warnings
                ],
            },
        )


class ListStrategiesTool:
    """Sprint 13 spec, section 21 -- registered strategies plus enough
    metadata to choose one, never raw source code."""

    name = "list_strategies"
    description = (
        "List every strategy registered on this platform, with its name, "
        "constructor parameters (name/type/default only), and a short "
        "description. Never returns strategy source code."
    )
    required_permission = PERMISSION_MODEL_READS

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        from src.strategies.registry import available_strategies, get_strategy_class

        rows = []
        for strategy_name in available_strategies():
            strategy_cls = get_strategy_class(strategy_name)
            doc = inspect.getdoc(strategy_cls) or ""
            description = doc.splitlines()[0] if doc else ""
            parameters = []
            try:
                signature = inspect.signature(strategy_cls.__init__)
                for param_name, param in signature.parameters.items():
                    if param_name in ("self", "symbol"):
                        continue
                    default = None if param.default is inspect.Parameter.empty else param.default
                    parameters.append({"name": param_name, "default": _json_safe(default)})
            except (TypeError, ValueError):
                parameters = []
            rows.append(
                {
                    "strategy_name": strategy_name,
                    "class_name": strategy_cls.__name__,
                    "description": description,
                    "parameters": parameters,
                }
            )
        return ToolResult.ok(self.name, {"strategies": rows, "count": len(rows)})


class GetModelMetadataTool:
    """Sprint 13 spec, section 22 -- trained-model provenance, never a
    filesystem path or the model binary itself."""

    name = "get_model_metadata"
    description = (
        "Get provenance metadata for one trained ML model by model_id: model "
        "type, feature/label configuration, dataset fingerprint, symbol/"
        "interval, and train/validation/test date ranges. Never returns the "
        "model artifact itself or a filesystem path."
    )
    required_permission = PERMISSION_MODEL_READS

    def __init__(self, model_registry: Any) -> None:
        self._registry = model_registry

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"model_id": {"type": "string"}},
            "required": ["model_id"],
        }

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        model_id = arguments["model_id"]
        metadata = self._registry.get_metadata(model_id)
        if metadata is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no model registered under {model_id!r}")
        return ToolResult.ok(
            self.name,
            {
                "model_id": metadata.model_id,
                "model_type": metadata.model_type,
                "hyperparameters": metadata.hyperparameters,
                "feature_set_id": metadata.feature_set_id,
                "feature_columns": metadata.feature_columns,
                "label_spec_id": metadata.label_spec_id,
                "classes": metadata.classes,
                "dataset_fingerprint": metadata.dataset_fingerprint,
                "dataset_source": metadata.dataset_source,
                "symbol": metadata.symbol,
                "interval": metadata.interval,
                "train_start": metadata.train_start,
                "train_end": metadata.train_end,
                "validation_start": metadata.validation_start,
                "validation_end": metadata.validation_end,
                "test_start": metadata.test_start,
                "test_end": metadata.test_end,
                "artifact_hash": metadata.artifact_hash,
                "created_at": metadata.created_at,
            },
        )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
