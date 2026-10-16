"""The paper broker gateway (Sprint 18, ADR-0053): the ONLY execution route of the paper runtime.

`PaperBroker` is purely in-memory and has no order ids, so the durable "broker side" of the paper account is the
`paper_broker_fills` ledger, keyed by a deterministic client order id. The gateway:

* rebuilds a `PaperBroker` for every operation from the account's persisted baseline plus the recorded fills
  (`PaperBroker.from_state` + replay through `submit_signal`, so there is one accounting implementation);
* submits idempotently: a client order id already in the ledger returns the recorded fill instead of executing
  again (`lookup`), which is what makes recovery after a lost acknowledgement safe;
* is hard-bound to `src.execution.PaperBroker`. There is no factory, no configuration choosing an adapter, and this
  module imports no live broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pandas as pd

from src.execution.engine import PaperBroker
from src.execution.models import Fill, Order, OrderSide
from src.execution.models import Position as BrokerPosition
from src.paper.faults import NoFaults
from src.paper.models import FillRecord, IntentRecord
from src.paper.store import PaperStore
from src.risk.models import SizingDecision
from src.signals.models import Signal, SignalDirection

ADAPTER_NAME = "PaperBroker"


class BrokerRejected(Exception):
    """The paper broker refused the order (nothing was recorded)."""


class LedgerReplayError(RuntimeError):
    """The recorded ledger cannot be replayed onto the baseline (it is internally inconsistent)."""


@dataclass(frozen=True)
class BrokerState:
    cash: float
    positions: dict[str, tuple[float, float]]    # symbol -> (signed quantity, entry price)


def signal_from_record(rec: dict) -> Signal:
    return Signal(timestamp=pd.Timestamp(rec["timestamp"]), symbol=rec["symbol"],
                  direction=SignalDirection(rec["direction"]), confidence=float(rec.get("confidence", 1.0)),
                  metadata=dict(rec.get("metadata") or {}), id=UUID(rec["id"]))


def fill_from_record(rec: FillRecord) -> Fill:
    order = Order(symbol=rec.symbol, side=OrderSide(rec.side), quantity=rec.quantity, signal_id=UUID(rec.signal_id),
                  timestamp=pd.Timestamp(rec.order_timestamp))
    return Fill(order=order, fill_price=rec.fill_price, cash_delta=rec.cash_delta, reference_price=rec.reference_price,
                fill_timestamp=pd.Timestamp(rec.fill_timestamp) if rec.fill_timestamp else None,
                slippage_amount=rec.slippage_amount, fee=rec.fee)


def fill_to_record(client_order_id: str, direction: SignalDirection, fill: Fill) -> FillRecord:
    return FillRecord(
        client_order_id=client_order_id, symbol=fill.order.symbol, side=fill.order.side.value, direction=direction.value,
        quantity=float(fill.order.quantity), fill_price=float(fill.fill_price),
        reference_price=None if fill.reference_price is None else float(fill.reference_price), fee=float(fill.fee),
        slippage_amount=float(fill.slippage_amount), cash_delta=float(fill.cash_delta), signal_id=str(fill.order.signal_id),
        order_timestamp=pd.Timestamp(fill.order.timestamp).isoformat(),
        fill_timestamp=None if fill.fill_timestamp is None else pd.Timestamp(fill.fill_timestamp).isoformat())


class PaperBrokerGateway:
    def __init__(self, store: PaperStore, experiment_id: int, faults=None) -> None:
        self._store = store
        self._exp = experiment_id
        self._faults = faults or NoFaults()

    @property
    def adapter_name(self) -> str:
        return ADAPTER_NAME

    # ------------------------------------------------------------------ state
    def _baseline_broker(self) -> PaperBroker:
        account = self._store.get_account(self._exp)
        if account is None:
            raise LedgerReplayError(f"no paper account for experiment {self._exp}")
        positions = [BrokerPosition(symbol=p["symbol"], quantity=float(p["quantity"]),
                                    entry_price=float(p["entry_price"]), entry_signal_id=UUID(p["entry_signal_id"]))
                     for p in account["positions"]]
        return PaperBroker.from_state(float(account["baseline_cash"]), positions)

    def _replay(self, broker: PaperBroker, rec: FillRecord) -> None:
        signal = Signal(timestamp=pd.Timestamp(rec.order_timestamp), symbol=rec.symbol,
                        direction=SignalDirection(rec.direction), confidence=1.0, id=UUID(rec.signal_id))
        sizing = SizingDecision(approved=True, position_size=rec.quantity,
                                capital_allocated=rec.quantity * rec.fill_price, reason="ledger replay")
        try:
            replayed = broker.submit_signal(
                signal, rec.symbol, fill_price=rec.fill_price, sizing_decision=sizing, fee=rec.fee,
                reference_price=rec.reference_price,
                fill_timestamp=pd.Timestamp(rec.fill_timestamp) if rec.fill_timestamp else None)
        except ValueError as exc:
            raise LedgerReplayError(f"ledger fill {rec.client_order_id} cannot be replayed: {exc}") from exc
        if abs(replayed.order.quantity - rec.quantity) > 1e-9:
            raise LedgerReplayError(f"ledger fill {rec.client_order_id} quantity differs on replay")

    def _state_of(self, broker: PaperBroker) -> BrokerState:
        return BrokerState(cash=broker.cash,
                           positions={s: (p.quantity, p.entry_price) for s, p in broker.positions.items()})

    def state(self, upto_seq: int | None = None, include_upto: bool = True) -> BrokerState:
        """Broker state = baseline + recorded fills (optionally only those before/through `upto_seq`)."""
        broker = self._baseline_broker()
        for rec in self._store.list_fills(self._exp):
            if upto_seq is not None and (rec.seq > upto_seq or (rec.seq == upto_seq and not include_upto)):
                break
            self._replay(broker, rec)
        return self._state_of(broker)

    # ------------------------------------------------------------------ lookup / submit
    def lookup(self, client_order_id: str) -> FillRecord | None:
        """Query the broker ledger for an existing order by client order id (the authoritative existence check)."""
        return self._store.get_fill(client_order_id)

    def submit(self, intent: IntentRecord, signal: Signal, fill: Fill) -> FillRecord:
        existing = self.lookup(intent.client_order_id)
        if existing is not None:                      # idempotent: never execute the same client order twice
            self._store.record_broker_fill(intent.intent_id, self._exp, existing)
            return existing
        if fill.order.quantity > intent.approved_quantity + 1e-9:
            raise BrokerRejected("order quantity exceeds the risk-approved quantity")
        self._faults.fire("broker_before_commit")
        broker = self._baseline_broker()
        for rec in self._store.list_fills(self._exp):
            self._replay(broker, rec)
        sizing = SizingDecision(approved=True, position_size=float(fill.order.quantity),
                                capital_allocated=fill.order.quantity * fill.fill_price, reason="risk-approved intent")
        try:
            executed = broker.submit_signal(signal, signal.symbol, fill_price=fill.fill_price, sizing_decision=sizing,
                                            fee=fill.fee, reference_price=fill.reference_price,
                                            fill_timestamp=fill.fill_timestamp)
        except ValueError as exc:
            raise BrokerRejected(str(exc)) from exc
        record = self._store.record_broker_fill(
            intent.intent_id, self._exp, fill_to_record(intent.client_order_id, signal.direction, executed))
        self._faults.fire("broker_after_commit")      # lost acknowledgement: ledger is committed, caller not told
        return record
