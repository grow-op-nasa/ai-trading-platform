"""Paper broker vs portfolio reconciliation (Sprint 18, ADR-0053).

The existing `src.reconciliation` compares a simulated paper fill against a REAL broker order; it cannot compare a
paper account with the authoritative portfolio, so this is the narrow extension. It compares what the paper
broker's ledger says (baseline + recorded fills, replayed through `PaperBroker`) with the registry's persisted paper
portfolio, plus identity consistency, and it only ever REPORTS: a mismatch is persisted, never auto-corrected.
"""

from __future__ import annotations

import math
from datetime import datetime

from src.paper.gateway import BrokerState, LedgerReplayError, PaperBrokerGateway
from src.paper.models import IntentState, ReconRecord, ReconStatus, UNRESOLVED_INTENT
from src.paper.store import PaperStore
from src.portfolio.models import Portfolio

TOLERANCE = 1e-6


def portfolio_state(portfolio: Portfolio) -> BrokerState:
    return BrokerState(cash=portfolio.cash,
                       positions={s: (p.quantity, p.entry_price) for s, p in portfolio.positions.items()})


def states_match(a: BrokerState, b: BrokerState) -> bool:
    return not diff_states(a, b)


def _close(x: float, y: float) -> bool:
    return math.isclose(x, y, rel_tol=TOLERANCE, abs_tol=TOLERANCE)


def diff_states(portfolio: BrokerState, broker: BrokerState) -> list[dict]:
    out: list[dict] = []
    if not _close(portfolio.cash, broker.cash):
        out.append({"kind": "CASH", "portfolio": portfolio.cash, "broker": broker.cash,
                    "difference": portfolio.cash - broker.cash})
    for symbol in sorted(set(portfolio.positions) | set(broker.positions)):
        p, b = portfolio.positions.get(symbol), broker.positions.get(symbol)
        if p is None or b is None:
            out.append({"kind": "POSITION_PRESENCE", "symbol": symbol, "portfolio": p, "broker": b})
        elif not _close(p[0], b[0]) or not _close(p[1], b[1]):
            out.append({"kind": "POSITION_VALUES", "symbol": symbol, "portfolio": list(p), "broker": list(b)})
    return out


def reconcile(store: PaperStore, gateway: PaperBrokerGateway, experiment_id: int, portfolio: Portfolio,
              now: datetime, cycle_id: str | None = None, persist: bool = True) -> ReconRecord:
    discrepancies: list[dict] = []
    summary: dict = {}
    try:
        broker_state = gateway.state()
    except LedgerReplayError as exc:
        broker_state = None
        discrepancies.append({"kind": "LEDGER_REPLAY_FAILED", "detail": str(exc)[:300]})
    pstate = portfolio_state(portfolio)
    summary["portfolio"] = {"cash": pstate.cash, "positions": {k: list(v) for k, v in pstate.positions.items()}}
    if broker_state is not None:
        summary["broker"] = {"cash": broker_state.cash, "positions": {k: list(v) for k, v in broker_state.positions.items()}}
        discrepancies.extend(diff_states(pstate, broker_state))

    intents = store.list_intents(experiment_id, limit=10_000)
    fills = {f.client_order_id: f for f in store.list_fills(experiment_id)}
    pending = [i for i in intents if i.state in UNRESOLVED_INTENT]
    for i in intents:
        has_fill = i.client_order_id in fills
        if i.state in (IntentState.APPLIED, IntentState.FILLED_AT_BROKER) and not has_fill:
            discrepancies.append({"kind": "MISSING_BROKER_FILL", "client_order_id": i.client_order_id})
        if i.state in (IntentState.REJECTED, IntentState.INTENT_RECORDED) and has_fill:
            discrepancies.append({"kind": "UNEXPECTED_BROKER_FILL", "client_order_id": i.client_order_id,
                                  "intent_state": i.state.value})
    known = {i.client_order_id for i in intents}
    for coid in sorted(set(fills) - known):
        discrepancies.append({"kind": "ORPHAN_BROKER_FILL", "client_order_id": coid})
    summary["intents"] = {"total": len(intents), "unresolved": len(pending), "fills": len(fills)}

    if discrepancies:
        status = ReconStatus.MISMATCH
    elif pending:
        status = ReconStatus.PENDING
        summary["pending_intents"] = [i.client_order_id for i in pending]
    else:
        status = ReconStatus.MATCH
    rec = ReconRecord(experiment_id=experiment_id, status=status, created_at=now.isoformat(), cycle_id=cycle_id,
                      discrepancies=discrepancies, summary=summary)
    return store.add_reconciliation(rec) if persist else rec
