"""Side-by-side comparison of stored research runs (Sprint 23, ADR-0061).

Rows are returned exactly as stored, in the order given. There is no composite score or ranking: a comparison can be
sorted or filtered by any single stored field, but never reduced to one invented number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategy_research.scorecard import Scorecard
from src.strategy_research.service import COMPLETED, KIND_SINGLE, research_state


@dataclass(frozen=True)
class ComparisonRow:
    run_id: str
    strategy: str
    family: str
    symbol: str
    params: dict[str, Any]
    period: str
    closed_trades: int | None
    win_rate: float | None
    expectancy: float | None
    profit_factor: float | None
    net_return: float | None
    max_drawdown: float | None
    holdout_net_return: float | None
    holdout_trades: int | None
    state: str
    selection_attempts: int | None


def _v(card: Scorecard, name: str) -> float | None:
    m = getattr(card, name)
    return m.value if m.is_defined else None


def row_for(run: dict[str, Any], holdout: dict[str, Any] | None) -> ComparisonRow:
    rec = run["payload"]
    state, _ = research_state(run, holdout)
    if rec["status"] != COMPLETED or run["kind"] != KIND_SINGLE:
        return ComparisonRow(run["run_id"], run["strategy_name"], "", run["symbol"], {}, "-", None, None, None, None, None,
                             None, None, None, state if run["kind"] == KIND_SINGLE else "WALK_FORWARD", None)
    sel = rec["selection"]["selected_index"]
    val = Scorecard.from_dict(rec["attempts"][sel]["scorecards"]["validation"])
    dev = Scorecard.from_dict(rec["attempts"][sel]["scorecards"]["development"])
    ho = Scorecard.from_dict(holdout["payload"]["scorecard"]) if holdout else None
    closed = dev.closed_trades + val.closed_trades
    return ComparisonRow(
        run_id=run["run_id"], strategy=rec["strategy"]["id"], family=rec["strategy"]["family"], symbol=rec["request"]["symbol"],
        params=rec["selected"]["params"], period=f"{dev.first_bar[:10]} to {val.last_bar[:10]}", closed_trades=closed,
        win_rate=_v(val, "win_rate"), expectancy=_v(val, "expectancy"), profit_factor=_v(val, "profit_factor"),
        net_return=_v(val, "net_return"), max_drawdown=_v(val, "max_drawdown"),
        holdout_net_return=_v(ho, "net_return") if ho else None, holdout_trades=ho.closed_trades if ho else None,
        state=state, selection_attempts=rec["selection"]["configurations_attempted"])


def compare_runs(runs: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> tuple[list[ComparisonRow], list[str]]:
    """Rows plus warnings for material differences that make a naive comparison misleading (shown, never blocked)."""
    rows = [row_for(r, h) for r, h in runs]
    warnings: list[str] = []
    done = [r for r in rows if r.closed_trades is not None]
    if len({r.symbol for r in done}) > 1:
        warnings.append("These runs are on different instruments; returns are not directly comparable.")
    if len({r.period for r in done}) > 1:
        warnings.append("These runs cover different historical periods.")
    return rows, warnings
