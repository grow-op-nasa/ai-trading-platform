"""Strategy Research application service (Sprint 23, ADR-0061).

The one place the Strategy Research page talks to. It wires the catalogue, the research service, the append-only store
and the existing market-data/snapshot services together. Read-only with respect to trading: it never creates orders,
intents, proposals, paper cycles or promotions, and it never touches the user's saved watchlists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.appsvc.paths import AppPaths
from src.snapshots import SnapshotStore
from src.strategies.catalogue import CatalogueEntry, StrategyFamily, build_catalogue, family_coverage
from src.strategy_research import (
    ResearchAssumptions,
    ResearchOutcome,
    ResearchRequest,
    ResearchService,
    ResearchStore,
    ValidationProtocol,
    research_state,
)
from src.strategy_research.compare import ComparisonRow, compare_runs
from src.strategy_research.universe import (
    MAX_BATCH_SYMBOLS,
    POINT_IN_TIME_NOTE,
    CoverageRow,
    coverage,
    coverage_summary,
    describe_universe,
)


__all__ = ["StrategyResearchService", "CatalogueView", "MAX_BATCH_SYMBOLS", "POINT_IN_TIME_NOTE", "default_assumptions"]


def default_assumptions() -> ResearchAssumptions:
    """The shared research assumptions every strategy receives (for display; the page never builds its own)."""
    return ResearchAssumptions()


@dataclass(frozen=True)
class CatalogueView:
    entries: list[CatalogueEntry]
    families: dict[StrategyFamily, list[str]]
    research_states: dict[str, str]          # strategy id -> best research state across its completed runs


_STATE_ORDER = ("ELIGIBLE_FOR_PAPER_EVALUATION", "OUT_OF_SAMPLE_EVALUATED", "RESEARCH_CANDIDATE", "VALIDATION_PENDING",
                "REJECTED", "EXPERIMENTAL")


class StrategyResearchService:
    def __init__(self, paths: AppPaths | None = None, *, market_data: Any = None) -> None:
        self.paths = paths or AppPaths()
        self._market_data = market_data
        self._store: ResearchStore | None = None

    # ------------------------------------------------------------ plumbing
    def store(self) -> ResearchStore:
        if self._store is None:
            self._store = ResearchStore(self.paths.research_db)
        return self._store

    def _service(self) -> ResearchService:
        md = self._market_data
        if md is None:
            from src.data.service import MarketDataService
            md = MarketDataService()
        return ResearchService(md, self.store(), SnapshotStore(self.paths.snapshots_dir))

    def has_store(self) -> bool:
        return self.paths.research_db.is_file()

    # ------------------------------------------------------------- catalogue
    def catalogue(self) -> CatalogueView:
        entries = build_catalogue()
        states: dict[str, str] = {e.strategy_id: "EXPERIMENTAL" for e in entries}
        if self.has_store():
            st = self.store()
            for run in st.list_runs():
                if run["kind"] != "SINGLE":
                    continue
                state, _ = research_state(run, st.get_holdout(run["run_id"]))
                current = states.get(run["strategy_name"], "EXPERIMENTAL")
                if _STATE_ORDER.index(state) < _STATE_ORDER.index(current):
                    states[run["strategy_name"]] = state
        return CatalogueView(entries, family_coverage(entries), states)

    def universe(self) -> dict[str, Any]:
        return describe_universe()

    def watchlists(self) -> dict[str, tuple[str, ...]]:
        """Existing saved watchlists, read-only, so their symbols can be reused. Never created, changed or deleted here."""
        if not self.paths.scanner_db.is_file():        # no scanner database means no saved watchlists; do not create one
            return {}
        from src.appsvc.scanner import ScannerService
        try:
            return {w.name: tuple(w.symbols) for w in ScannerService(self.paths).watchlists()}
        except Exception:  # noqa: BLE001 - no scanner database yet simply means no saved watchlists
            return {}

    # ------------------------------------------------------------------ runs
    def default_request(self, strategy: str, symbol: str, start: date, end: date, configs: list[dict[str, Any]] | None = None,
                        assumptions: ResearchAssumptions | None = None, protocol: ValidationProtocol | None = None,
                        notes: str = "") -> ResearchRequest:
        return ResearchRequest(strategy_name=strategy, symbol=symbol.strip().upper(), start=start, end=end,
                               configs=tuple(configs or [{}]), assumptions=assumptions or ResearchAssumptions(),
                               protocol=protocol, notes=notes)

    def run(self, request: ResearchRequest) -> ResearchOutcome:
        return self._service().run(request)

    def run_batch(self, strategy: str, symbols: list[str], start: date, end: date, configs: list[dict[str, Any]] | None = None,
                  assumptions: ResearchAssumptions | None = None,
                  progress: Callable[[int, int, str], None] | None = None) -> tuple[dict[str, ResearchOutcome], list[CoverageRow]]:
        """Independent per-instrument evaluations under identical assumptions. Not a combined portfolio result."""
        symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        if len(symbols) > MAX_BATCH_SYMBOLS:
            raise ValueError(f"at most {MAX_BATCH_SYMBOLS} instruments per batch (requests run one at a time to respect provider limits)")
        svc = self._service()
        out: dict[str, ResearchOutcome] = {}
        for i, sym in enumerate(symbols):
            if progress:
                progress(i, len(symbols), sym)
            out[sym] = svc.run(self.default_request(strategy, sym, start, end, configs, assumptions))
        if progress:
            progress(len(symbols), len(symbols), "")
        return out, coverage(out)

    def walk_forward(self, request: ResearchRequest, train_bars: int, test_bars: int) -> ResearchOutcome:
        return self._service().walk_forward(request, train_bars, test_bars)

    def evaluate_holdout(self, run_id: str) -> dict[str, Any]:
        return self._service().evaluate_holdout(run_id)

    def reproduce(self, run_id: str) -> dict[str, Any]:
        return self._service().reproduce(run_id)

    # ------------------------------------------------------------- inspection
    def runs(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.has_store():
            return []
        st = self.store()
        out = []
        for r in st.list_runs(kind):
            ho = st.get_holdout(r["run_id"]) if r["kind"] == "SINGLE" else None
            state, why = research_state(r, ho) if r["kind"] == "SINGLE" else ("WALK_FORWARD", "walk-forward evaluation")
            out.append({**r, "holdout": ho, "state": state, "state_reason": why})
        return out

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        if not self.has_store():
            return None
        st = self.store()
        r = st.get_run(run_id)
        if r is None:
            return None
        ho = st.get_holdout(run_id) if r["kind"] == "SINGLE" else None
        state, why = research_state(r, ho) if r["kind"] == "SINGLE" else ("WALK_FORWARD", "walk-forward evaluation")
        return {**r, "holdout": ho, "state": state, "state_reason": why}

    def compare(self, run_ids: list[str]) -> tuple[list[ComparisonRow], list[str]]:
        st = self.store()
        pairs = []
        for rid in run_ids:
            r = st.get_run(rid)
            if r is not None:
                pairs.append((r, st.get_holdout(rid) if r["kind"] == "SINGLE" else None))
        return compare_runs(pairs)

    @staticmethod
    def coverage_summary(rows: list[CoverageRow]) -> dict[str, int]:
        return coverage_summary(rows)
