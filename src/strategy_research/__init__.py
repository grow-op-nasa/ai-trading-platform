"""Strategy research -- consistent, reproducible, chronologically validated strategy evaluation (Sprint 23, ADR-0061).

Read-only with respect to the trading system: nothing here creates orders, intents, proposals, paper cycles or fills,
and nothing here promotes a strategy.
"""

from src.strategy_research.assumptions import ResearchAssumptions
from src.strategy_research.protocol import ValidationProtocol
from src.strategy_research.scorecard import Scorecard
from src.strategy_research.service import (
    ResearchOutcome,
    ResearchRequest,
    ResearchService,
    research_state,
)
from src.strategy_research.store import HoldoutAlreadyEvaluated, ResearchStore

__all__ = ["ResearchAssumptions", "ValidationProtocol", "Scorecard", "ResearchOutcome", "ResearchRequest",
           "ResearchService", "ResearchStore", "HoldoutAlreadyEvaluated", "research_state"]
