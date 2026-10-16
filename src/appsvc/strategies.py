"""Strategy catalogue for the UI (Sprint 19). Eligibility = membership of the PRODUCTION registry, nothing else."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

from src.appsvc.paths import AppPaths


@dataclass(frozen=True)
class StrategyInfo:
    name: str
    class_path: str
    version_hash: str | None
    origin: str                   # "built-in" | "promoted (human-approved candidate, registered)"
    summary: str
    parameters: dict[str, object]
    experiments: tuple[tuple[int, str], ...] = ()     # (experiment id, decision) from the experiment registry
    paper_eligible: bool = True
    limitations: tuple[str, ...] = ("Daily bars only in the paper runtime; long-only (shorts fail closed).",)


@dataclass(frozen=True)
class CandidateInfo:
    candidate_id: str
    name: str
    status: str
    meaning: str
    paper_eligible: bool = False
    reason: str = "development candidates can never be run in paper mode; only strategies in the production registry can"


_CANDIDATE_MEANING = {
    "DRAFT": "being drafted", "VALIDATING": "static safety validation in progress", "VALIDATED": "passed safety validation",
    "DEVELOPMENT_TESTED": "tested on development data only", "FROZEN": "frozen, awaiting out-of-sample evaluation",
    "OUT_OF_SAMPLE_TESTED": "out-of-sample evaluated, awaiting review", "REVIEW_REQUIRED": "awaiting human review",
    "APPROVED": "human-approved but NOT registered (not usable)", "PROMOTED": "registered in production (appears in the list above)",
    "REJECTED": "rejected or abandoned",
}


def registered_strategies(paths: AppPaths | None = None) -> list[StrategyInfo]:
    import src.strategies  # noqa: F401  registers built-in + promoted strategies (the ordinary production path)
    from src.strategies.identity import strategy_version
    from src.strategies.promoted import read_manifest
    from src.strategies.registry import available_strategies, get_strategy_class

    paths = paths or AppPaths()
    try:
        promoted = {e.strategy_name for e in read_manifest()}
    except Exception:  # noqa: BLE001
        promoted = set()
    evidence: dict[str, list[tuple[int, str]]] = {}
    if paths.registry_exists():
        try:
            from src.experiments.registry import ExperimentRegistry
            for e in ExperimentRegistry(paths.registry).list_experiments():
                evidence.setdefault(e.strategy_name or "", []).append((e.id, e.decision))
        except Exception:  # noqa: BLE001
            pass
    out: list[StrategyInfo] = []
    for name in available_strategies():
        cls = get_strategy_class(name)
        try:
            vh = strategy_version(cls)
        except TypeError:
            vh = None
        params: dict[str, object] = {}
        try:
            for p in inspect.signature(cls.__init__).parameters.values():
                if p.name != "self" and p.default is not inspect.Parameter.empty:
                    params[p.name] = p.default
        except (TypeError, ValueError):
            pass
        doc = (inspect.getdoc(cls) or "").strip().splitlines()
        out.append(StrategyInfo(
            name=name, class_path=f"{cls.__module__}.{cls.__qualname__}", version_hash=vh,
            origin="promoted (human-approved candidate, registered)" if name in promoted else "built-in",
            summary=doc[0] if doc else "no description", parameters=params,
            experiments=tuple(evidence.get(name, ()))))
    return out


def eligible_names(paths: AppPaths | None = None) -> list[str]:
    return [s.name for s in registered_strategies(paths) if s.paper_eligible]


def candidates(paths: AppPaths | None = None) -> list[CandidateInfo]:
    """Development candidates, for context only. Never eligible for paper runs."""
    paths = paths or AppPaths()
    if not paths.candidates_dir.is_dir():
        return []
    try:
        from src.ai.agents.strategy_dev.workspace import CandidateRegistry
        reg = CandidateRegistry(paths.candidates_dir)
        out = []
        for cid in reg.list_ids():
            c = reg.get(cid)
            if c is not None:
                status = getattr(c.status, "value", str(c.status))
                out.append(CandidateInfo(cid, c.name, status, _CANDIDATE_MEANING.get(status, status)))
        return out
    except Exception:  # noqa: BLE001
        return []
