"""Experiment catalogue for the UI (Sprint 19). Read-only; analytics stay in `src.analytics`."""

from __future__ import annotations

from dataclasses import dataclass

from src.appsvc.paths import AppPaths


@dataclass(frozen=True)
class ExperimentSummary:
    experiment_id: int
    strategy_name: str
    created_at: str
    decision: str
    notes: str
    has_spec: bool
    has_portfolio: bool
    has_equity_curve: bool


def experiments(paths: AppPaths) -> list[ExperimentSummary]:
    if not paths.registry_exists():
        return []
    from src.experiments.registry import ExperimentRegistry
    reg = ExperimentRegistry(paths.registry)
    out = []
    for e in reg.list_experiments():
        out.append(ExperimentSummary(e.id, e.strategy_name or "unknown", e.created_at, e.decision, e.notes or "",
                                     reg.get_spec(e.id) is not None, reg.get_portfolio(e.id) is not None,
                                     not reg.get_equity_curve(e.id).empty))
    return out
