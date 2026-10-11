"""Monitoring configuration -- src/monitoring/config.py (ADR-0050).

Every threshold the deterministic monitors use lives here, validated on
construction. The LLM agent can read monitoring results but has no tool
that can change a threshold.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

CONFIG_VERSION = "monitoring-config-v1"


@dataclass(frozen=True)
class MonitoringConfig:
    """Args:
        daily_stale_warning_sessions / daily_stale_critical_sessions:
            for daily bars, how many *expected trading sessions* may be
            missing before WARNING / CRITICAL. Counted in sessions, so
            weekends/holidays never count.
        daily_close_grace_minutes: after the session close, how long a
            provider may take to publish the day's bar before it is
            expected.
        intraday_stale_warning_bars / intraday_stale_critical_bars: for
            intraday bars, the age of the latest bar -- measured against
            the later of "now" (market open) or the last session close
            (market closed) -- in multiples of the bar interval.
        market_data_lookback_days: how far back the freshness probe asks.
        min_bars: fewer bars than this is "insufficient data" (WARNING).
        exposure_warning_ratio: WARNING when exposure/limit >= this
            ratio (1.0 = at the ceiling); CRITICAL when strictly above 1.0.
        drawdown_warning / drawdown_critical: positive fractions (0.10 =
            10% below the running peak).
        drawdown_min_points: minimum equity observations before a
            drawdown is trusted.
        reconciliation_slippage_warning_pct: |price_slippage_pct|
            (a fraction) above which a reconciliation is material.
    """

    daily_stale_warning_sessions: int = 1
    daily_stale_critical_sessions: int = 3
    daily_close_grace_minutes: int = 60
    intraday_stale_warning_bars: int = 3
    intraday_stale_critical_bars: int = 10
    market_data_lookback_days: int = 30
    min_bars: int = 20
    exposure_warning_ratio: float = 1.0
    drawdown_warning: float = 0.10
    drawdown_critical: float = 0.20
    drawdown_min_points: int = 20
    reconciliation_slippage_warning_pct: float = 0.005

    def __post_init__(self) -> None:
        def positive(name: str) -> None:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")

        for name in (
            "daily_stale_warning_sessions",
            "daily_stale_critical_sessions",
            "intraday_stale_warning_bars",
            "intraday_stale_critical_bars",
            "market_data_lookback_days",
            "min_bars",
            "drawdown_min_points",
        ):
            positive(name)
        if self.daily_close_grace_minutes < 0:
            raise ValueError("daily_close_grace_minutes must be non-negative")
        if self.daily_stale_warning_sessions > self.daily_stale_critical_sessions:
            raise ValueError("daily warning threshold must not exceed the critical threshold")
        if self.intraday_stale_warning_bars > self.intraday_stale_critical_bars:
            raise ValueError("intraday warning threshold must not exceed the critical threshold")
        if not 0.5 <= self.exposure_warning_ratio <= 1.0:
            raise ValueError("exposure_warning_ratio must be within [0.5, 1.0]")
        if not 0 < self.drawdown_warning <= self.drawdown_critical < 1:
            raise ValueError("require 0 < drawdown_warning <= drawdown_critical < 1")
        if self.reconciliation_slippage_warning_pct < 0:
            raise ValueError("reconciliation_slippage_warning_pct must be non-negative")

    def to_dict(self) -> dict:
        return asdict(self)
