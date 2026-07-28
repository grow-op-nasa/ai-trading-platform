"""The Backtesting Framework.

Not a strategy -- the framework: run strategy -> collect trades ->
calculate metrics -> generate report. Part of the Sprint 2 research
engine (`DECISIONS.md`, ADR-0009). Public entry point: `Backtester`.
"""

from src.backtesting.engine import Backtester
from src.backtesting.models import BacktestResult, Trade

__all__ = ["Backtester", "BacktestResult", "Trade"]
