"""A curated research universe and coverage reporting (Sprint 23, ADR-0061).

This is a *proposal list of symbols to try*, not a claim that the provider supports each one or that each is suitable to
trade. It is kept separate from the user's saved watchlists, which are never modified. Every symbol that cannot be
evaluated is reported with its reason, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.strategy_research.service import COMPLETED, ResearchOutcome

RESEARCH_UNIVERSE: dict[str, tuple[str, ...]] = {
    "Major index ETFs": ("SPY", "QQQ", "IWM", "DIA"),
    "Sector ETFs": ("XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLC", "XLB", "SMH"),
    "Liquid individual equities": ("AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "GOOGL", "AVGO", "TSLA", "JPM", "NFLX", "ORCL"),
    "Other ETF exposures": ("TLT", "GLD", "HYG", "EEM"),
}
UNIVERSE_NAME = "Strategy research universe (curated, not a watchlist)"
INITIAL_COMPARISON = ("SPY", "QQQ", "IWM", "XLK", "XLF", "NVDA", "AAPL", "AMD")
MAX_BATCH_SYMBOLS = 12                    # sequential requests, bounded so provider rate limits are respected
POINT_IN_TIME_NOTE = ("Instruments are chosen today. Without point-in-time constituent data this is not an unbiased "
                      "historical stock-selection test; ETFs are the more historically consistent proxies.")

EVALUATED = "EVALUATED_WITH_TRADES"
NO_ACTIVITY = "EVALUATED_NO_TRADES"
EXCLUDED_HISTORY = "EXCLUDED_INSUFFICIENT_HISTORY"
UNAVAILABLE = "UNAVAILABLE_FROM_PROVIDER"
INVALID_STALE = "DATA_INVALID_OR_INCOMPLETE"
UNSUPPORTED = "UNSUPPORTED_BY_STRATEGY_OR_TIMEFRAME"
OTHER = "NOT_EVALUATED_OTHER"

_CODE_TO_STATUS = {
    "INSUFFICIENT_HISTORY": EXCLUDED_HISTORY,
    "PROVIDER_UNAVAILABLE": UNAVAILABLE, "DATA_UNAVAILABLE": UNAVAILABLE,
    "DATA_INVALID": INVALID_STALE, "DATA_GAPS": INVALID_STALE, "SNAPSHOT_FAILED": INVALID_STALE,
    "UNSUPPORTED_TIMEFRAME": UNSUPPORTED, "NOT_RESEARCHABLE": UNSUPPORTED, "INVALID_PARAMETERS": UNSUPPORTED,
    "HISTORY_REQUIREMENT_UNKNOWN": UNSUPPORTED, "UNKNOWN_STRATEGY": UNSUPPORTED,
}


@dataclass(frozen=True)
class CoverageRow:
    symbol: str
    status: str
    detail: str
    run_id: str | None


def all_universe_symbols() -> tuple[str, ...]:
    seen: list[str] = []
    for group in RESEARCH_UNIVERSE.values():
        for s in group:
            if s not in seen:
                seen.append(s)
    return tuple(seen)


def classify(symbol: str, outcome: ResearchOutcome) -> CoverageRow:
    """A completed run with zero closed trades is a valid, quiet strategy -- reported as such, never as a failure."""
    if outcome.status == COMPLETED and outcome.record is not None:
        rec = outcome.record
        card = rec["attempts"][rec["selection"]["selected_index"]]["scorecards"]
        trades = card["development"]["closed_trades"] + card["validation"]["closed_trades"]
        if trades == 0:
            return CoverageRow(symbol, NO_ACTIVITY, "evaluated successfully; the strategy made no closed trades in the "
                               "development and validation segments", outcome.run_id)
        return CoverageRow(symbol, EVALUATED, f"{trades} closed trades in development and validation", outcome.run_id)
    return CoverageRow(symbol, _CODE_TO_STATUS.get(outcome.reason_code or "", OTHER),
                       f"{outcome.reason_code}: {outcome.reason}", outcome.run_id)


def coverage(outcomes: dict[str, ResearchOutcome]) -> list[CoverageRow]:
    return [classify(s, o) for s, o in outcomes.items()]


def coverage_summary(rows: list[CoverageRow]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def describe_universe() -> dict[str, Any]:
    return {"name": UNIVERSE_NAME, "groups": {k: list(v) for k, v in RESEARCH_UNIVERSE.items()},
            "note": POINT_IN_TIME_NOTE, "initial_comparison": list(INITIAL_COMPARISON)}
