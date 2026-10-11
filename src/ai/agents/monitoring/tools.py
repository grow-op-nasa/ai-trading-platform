"""The seven monitoring agent tools -- src/ai/agents/monitoring/tools.py.

Every tool is typed, schema-validated and policy-gated by the Sprint 13
`ToolRegistry`. All are read-only with respect to platform state
(portfolio, risk, execution, strategies, models, experiments). The only
write any tool can cause is `run_monitoring_checks` appending a
monitoring run and finding lifecycle updates to the monitoring store --
the same effect as `python -m src.cli monitor --once`, produced by the
deterministic service, never by the model.

A reconciliation tool is deliberately omitted: no real reconciliation
data source exists yet, so `get_execution_summary` reports reconciliation
as unavailable instead of a tool pretending otherwise.
"""

from __future__ import annotations

from typing import Any

from src.ai.agents.tools import ToolRegistry, ToolResult
from src.monitoring.models import finding_to_dict, run_to_report
from src.monitoring.service import MonitoringService

MAX_FINDINGS = 50
MAX_POSITIONS = 25
MARKET_DATA_CHECKS = ("market_data_freshness", "market_data_integrity")

PERM_RUN = "allow_monitoring_runs"
PERM_FINDINGS = "allow_finding_reads"
PERM_RUNS_READ = "allow_run_reads"
PERM_PORTFOLIO = "allow_portfolio_reads"
PERM_EXECUTION = "allow_execution_reads"
PERM_MARKET = "allow_market_data_health_reads"


class _Tool:
    name = ""
    description = ""
    required_permission = ""

    def __init__(self, service: MonitoringService) -> None:
        self._svc = service

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, arguments: dict[str, Any]) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def _now(self):
        from src.monitoring.models import utc_now

        return utc_now()


class RunMonitoringChecksTool(_Tool):
    name = "run_monitoring_checks"
    required_permission = PERM_RUN
    description = (
        "Run every deterministic monitor once against the platform's persisted paper state "
        "and return the run status, per-check status (HEALTHY/WARNING/CRITICAL/"
        "NOT_EVALUABLE with reasons) and findings. A check that could not be evaluated is "
        "NOT healthy. Limited number of runs per session."
    )

    def execute(self, arguments):
        run = self._svc.run_once()
        report = self._svc.report(run)
        data = _report_dict(report)
        return ToolResult.ok(self.name, data)


class ListFindingsTool(_Tool):
    name = "list_findings"
    required_permission = PERM_FINDINGS
    description = "List stored monitoring findings (newest first), optionally filtered. Bounded."

    def schema(self):
        return {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["WARNING", "CRITICAL"]},
                "status": {"type": "string", "enum": ["OPEN", "RESOLVED"]},
                "category": {"type": "string"},
                "entity": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_FINDINGS},
            },
            "required": [],
        }

    def execute(self, arguments):
        limit = arguments.get("limit", 20)
        found = self._svc.store.list_findings(
            severity=arguments.get("severity"), category=arguments.get("category"),
            status=arguments.get("status"), entity=arguments.get("entity"), limit=limit,
        )
        return ToolResult.ok(
            self.name,
            {"count": len(found), "limit": limit,
             "findings": [finding_to_dict(f, include_evidence=False) for f in found]},
        )


class GetFindingTool(_Tool):
    name = "get_finding"
    required_permission = PERM_FINDINGS
    description = "Get one stored finding with its full evidence by finding_id."

    def schema(self):
        return {"type": "object", "properties": {"finding_id": {"type": "string"}}, "required": ["finding_id"]}

    def execute(self, arguments):
        f = self._svc.store.get_finding(arguments["finding_id"])
        if f is None:
            return ToolResult.error(self.name, "NOT_FOUND", f"no finding {arguments['finding_id']!r}")
        return ToolResult.ok(self.name, finding_to_dict(f, include_evidence=True))


class GetMonitoringRunTool(_Tool):
    name = "get_monitoring_run"
    required_permission = PERM_RUNS_READ
    description = "Get a stored monitoring run (default: the most recent) with check statuses and findings."

    def schema(self):
        return {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": []}

    def execute(self, arguments):
        run = _resolve_run(self._svc, arguments.get("run_id"))
        if run is None:
            return ToolResult.error(self.name, "NOT_FOUND", "no such monitoring run (none stored yet?)")
        return ToolResult.ok(self.name, _stored_report(self._svc, run))


class GetMarketDataHealthTool(_Tool):
    name = "get_market_data_health"
    required_permission = PERM_MARKET
    description = (
        "Market-data freshness and integrity as recorded by a stored monitoring run "
        "(default: most recent). Reads the stored result; fetches nothing."
    )

    def schema(self):
        return {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": []}

    def execute(self, arguments):
        run = _resolve_run(self._svc, arguments.get("run_id"))
        if run is None:
            return ToolResult.error(
                self.name, "NOT_FOUND", "no monitoring run stored; call run_monitoring_checks first"
            )
        checks = [c for c in run.checks if c.check_id in MARKET_DATA_CHECKS]
        ids = [i for c in checks for i in c.finding_ids]
        findings = [f for f in (self._svc.store.get_finding(i) for i in ids) if f is not None]
        return ToolResult.ok(
            self.name,
            {
                "run_id": run.run_id,
                "targets": run.source.get("market_data_targets", []),
                "checks": [
                    {"check_id": c.check_id, "status": c.status.value, "reason": c.reason,
                     "summary": c.summary, "notes": list(c.notes)}
                    for c in checks
                ],
                "findings": [finding_to_dict(f, include_evidence=False) for f in findings],
            },
        )


class GetPaperPortfolioSnapshotTool(_Tool):
    name = "get_paper_portfolio_snapshot"
    required_permission = PERM_PORTFOLIO
    description = (
        "Read-only snapshot of the persisted paper portfolio (cash, positions, exposure). "
        "This is a saved snapshot of an experiment, not a live trading session."
    )

    def execute(self, arguments):
        state = self._svc.state_source.load(self._now())
        pf = state.portfolio
        if pf is None:
            return ToolResult.ok(
                self.name,
                {"available": False, "reason": "no paper portfolio available", "notes": list(state.notes)},
            )
        positions = []
        for sym, pos in sorted(pf.positions.items())[:MAX_POSITIONS]:
            positions.append(
                {"symbol": sym, "side": pos.side.value, "quantity": pos.quantity,
                 "entry_price": pos.entry_price, "stop_price": pos.stop_price,
                 "current_price": pos.current_price}
            )
        return ToolResult.ok(
            self.name,
            {
                "available": True,
                "experiment_id": state.experiment_id,
                "as_snapshot": True,
                "open_position_count": pf.position_count,
                "positions": positions,
                "positions_truncated": pf.position_count > MAX_POSITIONS,
                "total_exposure": pf.total_exposure,
                "notes": list(state.notes),
            },
        )


class GetExecutionSummaryTool(_Tool):
    name = "get_execution_summary"
    required_permission = PERM_EXECUTION
    description = (
        "Read-only summary of what execution evidence exists (orders/fills/trades/"
        "reconciliations). Reports unavailable evidence explicitly instead of guessing."
    )

    def execute(self, arguments):
        state = self._svc.state_source.load(self._now())
        ex = state.execution
        return ToolResult.ok(
            self.name,
            {
                "experiment_id": state.experiment_id,
                "orders": {"available": ex is not None, "count": len(ex.orders) if ex else None},
                "fills": {"available": ex is not None, "count": len(ex.fills) if ex else None},
                "trades": {"available": state.trades is not None,
                           "count": len(state.trades) if state.trades is not None else None},
                "reconciliations": {
                    "available": state.reconciliations is not None,
                    "count": len(state.reconciliations) if state.reconciliations is not None else None,
                },
                "notes": list(state.notes),
            },
        )


def _resolve_run(svc: MonitoringService, run_id: str | None):
    if run_id:
        return svc.store.get_run(run_id)
    runs = svc.store.list_runs(limit=1)
    return runs[0] if runs else None


def _stored_report(svc: MonitoringService, run) -> dict[str, Any]:
    findings = [f for f in (svc.store.get_finding(i) for i in run.finding_ids) if f is not None]
    return _report_dict(run_to_report(run, findings))


def _report_dict(report) -> dict[str, Any]:
    return {
        "run_id": report.run_id, "status": report.status, "persisted": report.persisted,
        "counts": report.counts, "checks": report.checks, "findings": report.findings[:MAX_FINDINGS],
        "errors": report.errors, "source": report.source,
        "persistence_error": report.persistence_error,
    }


def default_monitoring_tool_registry(service: MonitoringService) -> ToolRegistry:
    return ToolRegistry(
        [
            RunMonitoringChecksTool(service),
            ListFindingsTool(service),
            GetFindingTool(service),
            GetMonitoringRunTool(service),
            GetPaperPortfolioSnapshotTool(service),
            GetExecutionSummaryTool(service),
            GetMarketDataHealthTool(service),
        ]
    )
