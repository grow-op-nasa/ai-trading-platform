"""Performance Attribution.

Backtests shouldn't stop at "win rate 57%" -- they should explain why.
`PerformanceAttributor` takes a completed `BacktestResult` and the
candles it was run against, and produces an `AttributionReport`: trade
counts, average hold time, and which market regime (trend + volatility)
trades did best and worst in. Part of Sprint 3's research layer
(`DECISIONS.md`, ADR-0019).

Session-of-day attribution (morning/lunch/power-hour) is deliberately
not included yet -- it depends on candle timestamps reliably being in
market-local time, which `DECISIONS.md` ADR-0006 (timezone consistency)
hasn't landed yet. See ADR-0019 for why this was deferred rather than
shipped on an unverified assumption.
"""

from src.attribution.engine import PerformanceAttributor
from src.attribution.models import AttributionReport, RegimeStats

__all__ = ["PerformanceAttributor", "AttributionReport", "RegimeStats"]
