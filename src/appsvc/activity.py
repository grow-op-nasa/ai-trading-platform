"""Unified activity timeline (Sprint 20): a chronological READ-ONLY merge of records the platform already stores.

Sources: paper cycles, proposals, execution intents, paper-broker fills, reconciliations (paper store); monitoring runs,
findings, alert events and deliveries (monitoring store). Every event carries the identifiers it genuinely has. A link
between two events exists only when a stored identifier says so (cycle.intent_id / client_order_id / monitoring.run_id /
reconciliation.cycle_id / proposal.cycle_id / alert_event.run_id+finding_id). Nothing is inferred from timestamps or
similarity, and an event without a recorded relationship is shown on its own. Building the timeline writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.appsvc import explain
from src.appsvc import monitoring as mon
from src.appsvc.paths import AppPaths

CATEGORIES = ("Cycle", "Proposal", "Order", "Fill", "Reconciliation", "Monitoring run", "Finding", "Alert event", "Delivery")


@dataclass(frozen=True)
class Event:
    when: datetime                       # tz-aware UTC
    category: str
    title: str
    status: str                          # the canonical machine status of the underlying record
    detail: str = ""
    severity: str = ""                   # only where the source record has one
    refs: dict[str, Any] = field(default_factory=dict)       # genuine identifiers only
    source: str = ""                     # which store the event came from


def _dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _refs(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v not in (None, "")}


def paper_events(paths: AppPaths, account: int, limit: int = 200) -> list[Event]:
    if not paths.paper_exists():
        return []
    try:
        from src.paper.store import PaperStore
        s = PaperStore.open_existing(paths.paper_db)
    except Exception:  # noqa: BLE001 -- an unreadable store yields no events, never a guess
        return []
    out: list[Event] = []
    for c in s.list_cycles(account, limit):
        when = _dt(c.finished_at) or _dt(c.started_at)
        if when is None:
            continue
        ex = explain.cycle(c.status.value, c.reason_code)
        out.append(Event(when, "Cycle", f"Paper cycle {c.status.value}", c.status.value, ex.meaning, "", _refs(
            cycle_id=c.cycle_id, intent_id=c.intent_id, client_order_id=c.client_order_id,
            monitoring_run_id=(c.monitoring or {}).get("run_id"), reconciliation_id=c.reconciliation_id),
            "paper store"))
    for p in s.list_proposals(account, None, limit):
        made = _dt(p.created_at)
        if made:
            out.append(Event(made, "Proposal", f"Proposal created: {p.side} {p.quantity:g} {p.symbol}", "PENDING",
                             f"reference price {p.reference_price:g} (decision bar {p.decision_bar}); no order existed at this point",
                             "", _refs(proposal_id=p.proposal_id, cycle_id=p.cycle_id), "paper store"))
        decided = _dt(p.decided_at)
        if decided and p.status.value != "PENDING":
            out.append(Event(decided, "Proposal", f"Proposal {p.status.value.lower()}", p.status.value,
                             explain.proposal(p.status.value).meaning + (f" ({p.decision_reason})" if p.decision_reason else ""),
                             "", _refs(proposal_id=p.proposal_id, cycle_id=p.cycle_id), "paper store"))
    for i in s.list_intents(account, limit=limit):
        when = _dt(i.created_at)
        if when:
            out.append(Event(when, "Order", f"Paper order intent recorded: {i.side} {i.quantity:g} {i.symbol}", i.state.value,
                             "A durable execution intent. It is a request, not a fill.", "",
                             _refs(intent_id=i.intent_id, client_order_id=i.client_order_id, cycle_id=i.cycle_id), "paper store"))
    for f in s.list_fills(account)[-limit:]:
        when = _dt(f.fill_timestamp) or _dt(f.order_timestamp)
        if when:
            out.append(Event(when, "Fill", f"Simulated paper fill: {f.side} {f.quantity:g} {f.symbol} @ {f.fill_price:g}", "FILLED",
                             f"fee {f.fee:g}. Simulated by the paper broker; not a real-market execution.", "",
                             _refs(client_order_id=f.client_order_id), "paper store"))
    for r in s.list_reconciliations(account, limit):
        when = _dt(r.created_at)
        if when:
            ex = explain.reconciliation(r.status.value)
            out.append(Event(when, "Reconciliation", f"Reconciliation {r.status.value}", r.status.value,
                             ex.meaning + (f" {len(r.discrepancies)} discrepancy(ies)." if r.discrepancies else ""), "",
                             _refs(reconciliation_id=r.recon_id, cycle_id=r.cycle_id), "paper store"))
    return out


def monitoring_events(paths: AppPaths, limit: int = 100) -> list[Event]:
    """Monitoring is platform-wide in the store model (not per paper account); events are included with their own ids only."""
    out: list[Event] = []
    for r in mon.runs(paths, limit):
        when = _dt(getattr(r, "finished_at", None)) or _dt(getattr(r, "started_at", None))
        if when:
            out.append(Event(when, "Monitoring run", f"Monitoring run {r.status.value}", r.status.value,
                             f"{len(r.finding_ids)} finding(s) touched", "", _refs(monitoring_run_id=r.run_id), "monitoring store"))
    for f in mon.findings(paths, limit=limit):
        out.append(Event(f.first_seen, "Finding", f"Finding opened: {f.title}", "OPEN" if f.status.value == "OPEN" else f.status.value,
                         f.description[:200], f.severity.value, _refs(finding_id=f.finding_id, monitoring_run_id=f.last_run_id), "monitoring store"))
        if f.resolved_at:
            out.append(Event(f.resolved_at, "Finding", f"Finding resolved: {f.title}", "RESOLVED", "", f.severity.value,
                             _refs(finding_id=f.finding_id), "monitoring store"))
    for e in mon.alert_events(paths, limit):
        out.append(Event(e.occurred_at, "Alert event", f"Alert event {e.event_type.value}: {e.title}", e.finding_status.value,
                         "A notification-worthy finding transition (delivery is separate).", e.severity.value,
                         _refs(alert_event_id=e.event_id, finding_id=e.finding_id, monitoring_run_id=e.run_id), "monitoring store"))
    for d in mon.deliveries(paths, None, limit):
        when = d.delivered_at or d.updated_at
        out.append(Event(when, "Delivery", f"Alert delivery {d.status.value} via {d.channel}", d.status.value,
                         (d.last_error or "")[:160], "", _refs(delivery_id=d.delivery_id, alert_event_id=d.event_id), "monitoring store"))
    return out


def timeline(paths: AppPaths, account: int | None, *, categories: tuple[str, ...] | None = None, statuses: tuple[str, ...] | None = None,
             since: datetime | None = None, until: datetime | None = None, limit: int = 300) -> list[Event]:
    """Newest first. `account` scopes paper events; monitoring events are platform-wide. Filters only hide rows."""
    events = (paper_events(paths, account, limit) if account is not None else []) + monitoring_events(paths, limit)
    if categories:
        events = [e for e in events if e.category in categories]
    if statuses:
        events = [e for e in events if e.status in statuses]
    if since:
        events = [e for e in events if e.when >= since]
    if until:
        events = [e for e in events if e.when <= until]
    events.sort(key=lambda e: e.when, reverse=True)
    return events[:limit]
