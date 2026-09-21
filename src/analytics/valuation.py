"""Read-only portfolio valuation -- src/analytics/valuation.py.

The platform's own paper-trading path (`PaperBroker`/`Portfolio`) never
marks an open position to market on its own (`DECISIONS.md`, ADR-0022:
`Position.valuation_price` falls back to the frozen `entry_price` when
`current_price` is `None`, and nothing sets `current_price`
automatically). Sprint 9's Paper Portfolio view needs the *actual*
current value, not that frozen one -- `PortfolioValuationService` is
the one place that combines an already-saved `Portfolio` with a live
market price to compute that, without mutating the `Portfolio` and
without ever fabricating a price for a symbol none is available for
(Sprint 9 spec, sections 20-24).

Market-data boundary (section 23): `latest_price()` is the *only*
touchpoint in `src.analytics` (or, downstream, `src.dashboard`) that
reaches toward market data, and it goes through `MarketDataService` --
never `yfinance` or the on-disk cache directly
(`tests/test_architecture.py` enforces the dashboard half of this).
"""

from __future__ import annotations

from typing import Callable

from src.analytics.models import Metric, PortfolioSnapshot, PositionValuation
from src.data.exceptions import DataProviderError
from src.data.service import MarketDataService
from src.portfolio.models import Portfolio

PriceLookup = Callable[[str], "float | None"]


def default_price_lookup() -> PriceLookup:
    """A `PriceLookup` backed by a fresh `MarketDataService` -- the
    dashboard's usual way to get one, so `src.dashboard` never has to
    import or construct `MarketDataService` itself (Sprint 9 spec,
    section 23's boundary chain: Dashboard -> Analytics/Portfolio
    Valuation -> MarketDataService -> Canonical Data;
    `tests/test_architecture.py` enforces that `MarketDataService` is
    constructed only inside this module).
    """
    service = MarketDataService()
    return lambda symbol: latest_price(symbol, service)


def latest_price(symbol: str, market_data_service: MarketDataService) -> float | None:
    """The most recent close available for `symbol`, or `None` if none
    can be determined.

    Deliberately swallows the ordinary "no data for this symbol right
    now" conditions (`DataProviderError` and its `NoDataError`
    subclass) rather than letting them propagate -- one symbol lacking
    a price must not blow up an entire portfolio valuation (Sprint 9
    spec, section 22). A short 5-day daily lookback is enough to find
    the latest available close without requesting a full history.
    """
    try:
        candles = market_data_service.get_history(symbol, period="5d", interval="1d")
    except DataProviderError:
        return None
    if candles.empty:
        return None
    return float(candles["close"].iloc[-1])


class PortfolioValuationService:
    """Values a `Portfolio` snapshot against current market prices.

    Read-only by construction: `value()` never assigns to
    `Position.current_price` or calls any `Portfolio` mutator (there is
    no `open_position()`/`close_position()` call anywhere in this
    class) -- every derived number is computed into a fresh
    `PortfolioSnapshot` from `portfolio.positions`/`closed_positions`,
    both of which are already defensive copies
    (`src.portfolio.models.Portfolio`). Calling `value()` twice in a
    row on the same `Portfolio` therefore never changes it (Sprint 9
    spec, section 24, "no hidden mutation").
    """

    def value(self, portfolio: Portfolio, price_lookup: PriceLookup) -> PortfolioSnapshot:
        """Args:
            portfolio: the snapshot to value (e.g. from
                `ExperimentRegistry.get_portfolio()`).
            price_lookup: `symbol -> current price, or None if
                unavailable`. Typically
                `functools.partial(latest_price, market_data_service=...)`,
                but injectable directly for tests (network-free, per
                Sprint 9 spec section 33).
        """
        realized_pnl = sum(
            position.realized_pnl
            for position in portfolio.closed_positions
            if position.realized_pnl is not None
        )

        position_valuations: list[PositionValuation] = []
        warnings: list[str] = []
        all_priced = True
        market_value_total = 0.0
        unrealized_total = 0.0
        exposure_total = 0.0

        for position in portfolio.positions.values():
            price = price_lookup(position.symbol)
            if price is None:
                all_priced = False
                warnings.append(
                    f"no current market price available for {position.symbol!r} -- "
                    f"excluded from market value/unrealized P&L/exposure"
                )
                position_valuations.append(
                    PositionValuation(
                        symbol=position.symbol,
                        side=position.side.value,
                        quantity=position.quantity,
                        entry_price=position.entry_price,
                        market_price=None,
                        market_value=None,
                        unrealized_pnl=None,
                        price_available=False,
                        warning="market price unavailable",
                    )
                )
                continue

            market_value = position.quantity * price
            unrealized = (price - position.entry_price) * position.quantity
            market_value_total += market_value
            unrealized_total += unrealized
            exposure_total += abs(position.quantity * price)
            position_valuations.append(
                PositionValuation(
                    symbol=position.symbol,
                    side=position.side.value,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    market_price=price,
                    market_value=market_value,
                    unrealized_pnl=unrealized,
                    price_available=True,
                )
            )

        if all_priced:
            market_value_metric = Metric.of(market_value_total)
            unrealized_metric = Metric.of(unrealized_total)
            exposure_metric = Metric.of(exposure_total)
            equity_metric = Metric.of(portfolio.cash + market_value_total)
            total_pnl_metric = Metric.of(realized_pnl + unrealized_total)
        else:
            reason = (
                "one or more open positions have no current market price "
                "available -- showing a partial total would be misleading"
            )
            market_value_metric = Metric.undefined(reason)
            unrealized_metric = Metric.undefined(reason)
            exposure_metric = Metric.undefined(reason)
            equity_metric = Metric.undefined(reason)
            total_pnl_metric = Metric.undefined(reason)

        return PortfolioSnapshot(
            cash=portfolio.cash,
            market_value=market_value_metric,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_metric,
            total_pnl=total_pnl_metric,
            equity=equity_metric,
            exposure=exposure_metric,
            positions=position_valuations,
            warnings=warnings,
        )
