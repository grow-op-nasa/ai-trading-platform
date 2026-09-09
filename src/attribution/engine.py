"""The Performance Attribution engine.

    from src.attribution import PerformanceAttributor

    report = PerformanceAttributor().run(result, candles)
    print(report.report())

Independently recomputes market regime from `candles` via
`MarketRegimeEngine` rather than trusting a strategy to have tagged its
`Signal.metadata` with regime info -- this way attribution works for
every backtest, regardless of what a given strategy chose to record
(`DECISIONS.md`, ADR-0019). `candles` must be the same DataFrame (or an
equivalent one, same index) that was passed to `Backtester.run()` --
regime is looked up by each trade's `entry_time` against it.
"""

from __future__ import annotations

import pandas as pd

from src.attribution.models import UNKNOWN_REGIME, AttributionReport, RegimeStats
from src.backtesting.models import BacktestResult, Trade
from src.regime.engine import MarketRegimeEngine


class PerformanceAttributor:
    """Turns a completed `BacktestResult` into an `AttributionReport`."""

    def run(
        self, result: BacktestResult, candles: pd.DataFrame, **regime_kwargs
    ) -> AttributionReport:
        """Attribute `result`'s trades against the regimes in `candles`.

        Args:
            result: a completed `Backtester.run()` result.
            candles: the same candles `result` was produced from.
            **regime_kwargs: forwarded to `MarketRegimeEngine.score()`
                (e.g. `trend_fast`, `volatility_lookback`) -- mainly
                useful for tests running against a small candle set
                that needs shorter warmup windows than the defaults.
        """
        trades = result.trades
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t.return_pct > 0)
        win_rate = winning_trades / total_trades if total_trades else None
        average_hold = self._average_hold(trades)
        regime_breakdown = self._regime_breakdown(trades, candles, regime_kwargs)
        best_regime, worst_regime = self._best_and_worst(regime_breakdown)

        return AttributionReport(
            total_trades=total_trades,
            winning_trades=winning_trades,
            win_rate=win_rate,
            average_hold=average_hold,
            regime_breakdown=regime_breakdown,
            best_regime=best_regime,
            worst_regime=worst_regime,
        )

    def _average_hold(self, trades: list[Trade]) -> pd.Timedelta | None:
        if not trades:
            return None
        durations = [t.exit_time - t.entry_time for t in trades]
        return sum(durations, pd.Timedelta(0)) / len(durations)

    def _regime_breakdown(
        self, trades: list[Trade], candles: pd.DataFrame, regime_kwargs: dict
    ) -> dict[str, RegimeStats]:
        if not trades:
            return {}

        engine = MarketRegimeEngine(candles)
        scores = engine.score(**regime_kwargs)
        dominant = engine.dominant(scores)

        buckets: dict[str, list[Trade]] = {}
        for trade in trades:
            key = self._bucket_key(trade.entry_time, scores, dominant)
            buckets.setdefault(key, []).append(trade)

        breakdown: dict[str, RegimeStats] = {}
        for key, bucket_trades in buckets.items():
            wins = sum(1 for t in bucket_trades if t.return_pct > 0)
            avg_return = sum(t.return_pct for t in bucket_trades) / len(bucket_trades)
            breakdown[key] = RegimeStats(
                label=self._display_label(key),
                trade_count=len(bucket_trades),
                win_rate=wins / len(bucket_trades),
                avg_return_pct=avg_return,
            )
        return breakdown

    def _bucket_key(
        self, entry_time: pd.Timestamp, scores: pd.DataFrame, dominant: pd.DataFrame
    ) -> str:
        """The regime bucket for a trade, based on the regime at entry.

        Joins the trend and volatility axes only -- the risk axis is
        excluded since it's always "unknown" until ADR-0010's VIX gap
        closes, which would make every bucket end in a useless
        "+ unknown". A trade entering during the indicator warmup
        period (trend/volatility scores still NaN) is bucketed as
        `UNKNOWN_REGIME` rather than trusting `dominant()`'s NaN
        comparison, which would otherwise silently default to
        "ranging"/"low_volatility".
        """
        if entry_time not in scores.index:
            return UNKNOWN_REGIME
        if pd.isna(scores.loc[entry_time, "trending"]) or pd.isna(
            scores.loc[entry_time, "volatile"]
        ):
            return UNKNOWN_REGIME
        row = dominant.loc[entry_time]
        return f"{row['trend_regime']}_{row['volatility_regime']}"

    def _display_label(self, key: str) -> str:
        if key == UNKNOWN_REGIME:
            return "Unknown"
        trend_part, volatility_part = key.split("_", 1)
        return f"{trend_part.title()} + {volatility_part.replace('_', ' ').title()}"

    def _best_and_worst(
        self, breakdown: dict[str, RegimeStats]
    ) -> tuple[str | None, str | None]:
        candidates = {
            key: stats
            for key, stats in breakdown.items()
            if key != UNKNOWN_REGIME and stats.avg_return_pct is not None
        }
        if not candidates:
            return None, None
        best = max(candidates.values(), key=lambda s: s.avg_return_pct)
        worst = min(candidates.values(), key=lambda s: s.avg_return_pct)
        return best.label, worst.label
