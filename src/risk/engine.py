"""Position sizing -- src/risk.

    from src.portfolio import AccountState
    from src.risk import PositionSizer, RiskLimits

    sizer = PositionSizer(RiskLimits(allocation_per_trade_pct=0.10, max_portfolio_exposure_pct=0.5))
    decision = sizer.size(signal, AccountState(equity=100_000, open_exposure=20_000), price=150.0)

Deliberately standalone this round (`DECISIONS.md`, ADR-0021) --
`Backtester` keeps its existing single-unit execution model (ADR-0011)
unchanged; wiring `PositionSizer` into a backtest, or into a future
`src/execution`, is a deliberate future step, not built here. Sizing
uses a fixed fraction of account equity per trade -- capital allocation,
not maximum-loss risk (`DECISIONS.md`, ADR-0032) -- applied identically
regardless of `Signal.confidence`, no confidence-scaling in this first
version.
"""

from __future__ import annotations

from src.portfolio.models import AccountState
from src.risk.models import RiskLimits, SizingDecision
from src.signals.models import Signal, SignalDirection


class PositionSizer:
    """Turns a `Signal` + `AccountState` into a `SizingDecision`."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self._limits = limits or RiskLimits()

    def size(self, signal: Signal, account: AccountState, price: float) -> SizingDecision:
        """Size a position for `signal` given `account`'s current state.

        Args:
            signal: the `Signal` being sized. Must be `LONG` or `SHORT`
                -- a `FLAT` signal closes a position rather than opening
                one, so there's nothing to size (see "Raises").
            account: the account's current equity and open exposure.
            price: the instrument's current price, used to convert a
                dollar allocation into a unit count. Does not enforce
                whole-share lots -- fractional units are allowed here;
                rounding to a tradable lot size is an execution-layer
                concern, deferred the same way ADR-0011 deferred
                realistic execution mechanics out of `Backtester`.

        Returns:
            A `SizingDecision`: rejected (`approved=False`,
            `position_size=0.0`) when there's no portfolio exposure
            headroom left; sized down to whatever headroom remains when
            the full per-trade allocation doesn't fit; otherwise sized
            at the full `allocation_per_trade_pct` of equity. `LONG` and
            `SHORT` are sized identically -- both commit capital, just
            in opposite directions.

        Raises:
            ValueError: `signal.direction` is `FLAT`, or `price` isn't
                positive.
        """
        if signal.direction is SignalDirection.FLAT:
            raise ValueError(
                "cannot size a FLAT signal -- FLAT closes a position, it doesn't open one"
            )
        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")

        desired_capital = account.equity * self._limits.allocation_per_trade_pct
        max_exposure = account.equity * self._limits.max_portfolio_exposure_pct
        headroom = max_exposure - account.open_exposure

        if headroom <= 0:
            return SizingDecision(
                approved=False,
                position_size=0.0,
                capital_allocated=0.0,
                reason=(
                    f"portfolio exposure limit reached: {account.open_exposure:.2f} "
                    f"already committed of {max_exposure:.2f} allowed"
                ),
            )

        allocated_capital = min(desired_capital, headroom)
        position_size = allocated_capital / price

        if allocated_capital < desired_capital:
            reason = (
                f"sized down to remaining portfolio headroom "
                f"({headroom:.2f} available vs. {desired_capital:.2f} desired)"
            )
        else:
            reason = (
                f"sized at full per-trade allocation "
                f"({self._limits.allocation_per_trade_pct:.1%} of equity)"
            )

        return SizingDecision(
            approved=True,
            position_size=position_size,
            capital_allocated=allocated_capital,
            reason=reason,
        )
