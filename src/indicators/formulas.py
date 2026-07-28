"""Indicator formulas.

Every function here is pure: candles DataFrame (plus keyword params) in,
a Series or DataFrame of the same length out. No I/O, no side effects,
no knowledge of `MarketDataService` or where the candles came from.
Each is registered under a name via `@register_indicator` so
`IndicatorEngine.calculate(name, **params)` can dispatch to it -- the
registration is the only thing that couples a formula to the engine.

Indicators that build on other indicators (MACD on two EMAs, ATR
implicitly on true range) call the underlying *functions* directly
rather than going back through the engine, since they need those
values as an implementation detail, not as something a caller asked
for by name.
"""

from __future__ import annotations

import pandas as pd

from src.indicators.registry import register_indicator


@register_indicator("SMA")
def simple_moving_average(candles: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple moving average of the close price over `period` candles."""
    return candles["close"].rolling(window=period).mean().rename(f"SMA_{period}")


@register_indicator("EMA")
def exponential_moving_average(candles: pd.DataFrame, period: int = 20) -> pd.Series:
    """Exponential moving average of the close price over `period` candles."""
    return candles["close"].ewm(span=period, adjust=False).mean().rename(f"EMA_{period}")


@register_indicator("RSI")
def relative_strength_index(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index, in [0, 100].

    Uses the standard exponential-smoothing approximation of Wilder's
    RSI (`ewm(alpha=1/period)` on gains and losses) rather than Wilder's
    original recursive formula -- the two converge quickly and this
    version is simpler to reason about and vectorize.
    """
    delta = candles["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # Where there have been no losses at all, RS is undefined (division
    # by zero) but RSI should read 100, not NaN.
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi.rename(f"RSI_{period}")


@register_indicator("ATR")
def average_true_range(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range: a rolling mean of the true range over `period`.

    True range for a candle is the largest of: high - low, |high - previous
    close|, |low - previous close|.
    """
    high, low, close = candles["high"], candles["low"], candles["close"]
    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return true_range.rolling(window=period).mean().rename(f"ATR_{period}")


@register_indicator("MACD")
def macd(
    candles: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """Moving Average Convergence Divergence.

    Returns a DataFrame with columns `macd`, `signal`, and `histogram`.
    Built directly on `exponential_moving_average` rather than routing
    back through the engine, since the two EMAs here are an
    implementation detail of MACD, not something the caller asked for.
    """
    ema_fast = exponential_moving_average(candles, period=fast)
    ema_slow = exponential_moving_average(candles, period=slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": histogram}
    )


@register_indicator("VWAP")
def volume_weighted_average_price(candles: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price, cumulative over the whole DataFrame.

    Note: VWAP is conventionally reset at the start of each trading
    session (it's a session-relative measure). This implementation
    computes one cumulative VWAP over whatever range of candles it's
    given -- if session-reset behavior is needed later, that's a
    parameter to add, not a reason to change the formula here.
    """
    typical_price = (candles["high"] + candles["low"] + candles["close"]) / 3
    cumulative_volume = candles["volume"].cumsum()
    cumulative_turnover = (typical_price * candles["volume"]).cumsum()
    return (cumulative_turnover / cumulative_volume).rename("VWAP")
