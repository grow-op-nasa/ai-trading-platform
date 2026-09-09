"""Paper execution -- src/execution.

    from src.execution import PaperBroker

    broker = PaperBroker(starting_cash=100_000)
    fill = broker.submit_signal(signal, symbol="SPY", fill_price=450.0, sizing_decision=decision)
    broker.account_state  # feed straight back into PositionSizer.size() for the next decision

Closes the loop `DECISIONS.md` ADR-0021 left open: `PositionSizer`
decides how much to risk, `PaperBroker` simulates actually taking that
position and tracks the resulting portfolio, and `account_state` is a
real `src.risk.AccountState` the next `PositionSizer.size()` call can
consume directly. Deliberately simple (`DECISIONS.md`, ADR-0022):
market orders only, filled immediately and completely at whatever price
the caller supplies (no slippage or commission modeled, matching
ADR-0011's Backtester simplifications), one open position per symbol at
a time (no scaling into or averaging an existing position), and no
live mark-to-market -- an open position's contribution to `equity` is
frozen at its own entry price until it's closed and the P&L is
realized into cash.
"""

from __future__ import annotations

from src.execution.models import Fill, Order, OrderSide, Position
from src.risk.models import AccountState, SizingDecision
from src.signals.models import Signal, SignalDirection


class PaperBroker:
    """A simulated broker: fills orders instantly, tracks cash and open
    positions per symbol, and exposes the result as an `AccountState`.
    """

    def __init__(self, starting_cash: float) -> None:
        if starting_cash <= 0:
            raise ValueError(f"starting_cash must be positive, got {starting_cash}")
        self._cash = starting_cash
        self._positions: dict[str, Position] = {}

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        """A defensive copy -- callers can't mutate internal state through it."""
        return dict(self._positions)

    @property
    def account_state(self) -> AccountState:
        """The broker's current state as a `src.risk.AccountState`,
        ready to feed straight into `PositionSizer.size()` for the next
        decision.

        `equity` values each open position at its own entry price, not
        a live mark-to-market (see the module docstring) -- floating
        P&L on an open position isn't reflected until it's closed and
        realized into cash.
        """
        market_value = sum(
            position.quantity * position.entry_price
            for position in self._positions.values()
        )
        open_exposure = sum(
            abs(position.quantity) * position.entry_price
            for position in self._positions.values()
        )
        return AccountState(equity=self._cash + market_value, open_exposure=open_exposure)

    def submit_signal(
        self,
        signal: Signal,
        symbol: str,
        fill_price: float,
        sizing_decision: SizingDecision | None = None,
    ) -> Fill:
        """Translate `signal` into an `Order` for `symbol` and fill it
        immediately at `fill_price`.

        Args:
            signal: `LONG`/`SHORT` opens a new position (requires an
                approved `sizing_decision`); `FLAT` closes whatever
                position this broker currently holds in `symbol`.
            symbol: which instrument this signal applies to.
            fill_price: the price to fill at -- like `PositionSizer`,
                this broker has no price feed of its own.
            sizing_decision: required (and must be `approved`) for
                `LONG`/`SHORT`; ignored for `FLAT`, since `PositionSizer`
                never sizes a `FLAT` signal in the first place.

        Returns:
            The resulting `Fill`.

        Raises:
            ValueError: `fill_price` isn't positive; a `LONG`/`SHORT`
                signal is missing an approved `sizing_decision`, or
                `symbol` already has an open position; or a `FLAT`
                signal is given for a `symbol` with no open position.
        """
        if fill_price <= 0:
            raise ValueError(f"fill_price must be positive, got {fill_price}")

        order = self._build_order(signal, symbol, sizing_decision)
        return self._fill(order, fill_price)

    def _build_order(
        self, signal: Signal, symbol: str, sizing_decision: SizingDecision | None
    ) -> Order:
        existing = self._positions.get(symbol)

        if signal.direction is SignalDirection.FLAT:
            if existing is None:
                raise ValueError(f"no open position in {symbol!r} to close")
            side = OrderSide.SELL if existing.quantity > 0 else OrderSide.BUY
            quantity = abs(existing.quantity)
        else:
            if existing is not None:
                raise ValueError(
                    f"{symbol!r} already has an open position -- close it "
                    "(FLAT) before opening a new one; scaling into an "
                    "existing position isn't supported yet"
                )
            if sizing_decision is None or not sizing_decision.approved:
                raise ValueError(
                    f"cannot open a position for a {signal.direction.value} "
                    "signal without an approved sizing_decision"
                )
            side = (
                OrderSide.BUY if signal.direction is SignalDirection.LONG else OrderSide.SELL
            )
            quantity = sizing_decision.position_size

        return Order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            signal_id=signal.id,
            timestamp=signal.timestamp,
        )

    def _fill(self, order: Order, fill_price: float) -> Fill:
        cash_delta = (
            -order.quantity * fill_price
            if order.side is OrderSide.BUY
            else order.quantity * fill_price
        )
        self._cash += cash_delta

        existing = self._positions.get(order.symbol)
        if existing is None:
            signed_quantity = (
                order.quantity if order.side is OrderSide.BUY else -order.quantity
            )
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                quantity=signed_quantity,
                entry_price=fill_price,
                entry_signal_id=order.signal_id,
            )
        else:
            del self._positions[order.symbol]

        return Fill(order=order, fill_price=fill_price, cash_delta=cash_delta)
