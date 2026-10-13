"""Alert operator CLI -- src/cli/alerts.py (Sprint 17, ADR-0052).

    python -m src.cli alerts config                      # validate settings (never sends)
    python -m src.cli alerts preview --event-id ID | --latest | --sample NEW|ESCALATED|RESOLVED
    python -m src.cli alerts status [--finding-id ID] [--event-id ID] [--all] [--json]
    python -m src.cli alerts dispatch --once [--limit N] [--event-id ID]
    python -m src.cli alerts retry (--all-failed | --event-id ID | --delivery-id ID)
    python -m src.cli alerts test-send --to ADDR [--confirm-send]

Nothing here starts automatically, calls an LLM, or touches portfolio/risk/strategy/execution
state. `dispatch` and `test-send` are the ONLY commands that can contact a mail server:
`dispatch` needs ATP_ALERTS_ENABLED=true + valid ATP_ALERT_* settings; `test-send` additionally
needs `--confirm-send` and a `--to` address listed in ATP_ALERT_TEST_RECIPIENTS (no default
destination, never the production recipients unless you deliberately list them).

Exit codes
    0  OK (config valid / preview rendered / nothing pending or failed / test-send delivered
       / dry-run preview of test-send)
    1  completed, but deliveries are still pending or scheduled for retry
    2  invalid configuration, usage, or missing database (nothing was sent)
    3  one or more deliveries are FAILED (permanent or exhausted), or a test-send failed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4

from src.monitoring.alerts.config import AlertConfig, AlertConfigError
from src.monitoring.alerts.dispatcher import AlertDispatcher
from src.monitoring.alerts.format import render_message, render_test_message
from src.monitoring.alerts.models import AlertEvent, AlertEventType, DeliveryStatus
from src.monitoring.alerts.transports import build_transport
from src.monitoring.models import FindingSeverity, FindingStatus
from src.monitoring.store import DEFAULT_DB_PATH, MonitoringStore

EXIT_OK, EXIT_PENDING, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2, 3


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sample_event(kind: str) -> AlertEvent:
    now = _utc_now()
    prev = FindingSeverity.WARNING if kind == "ESCALATED" else None
    return AlertEvent(
        event_id="SAMPLE-" + uuid4().hex[:8], idempotency_key="sample", event_type=AlertEventType(kind),
        finding_id="0" * 32, fingerprint="f" * 32, rule_id="risk.portfolio_exposure",
        severity=FindingSeverity.CRITICAL if kind != "RESOLVED" else FindingSeverity.WARNING,
        previous_severity=prev,
        finding_status=FindingStatus.RESOLVED if kind == "RESOLVED" else FindingStatus.OPEN,
        title="SAMPLE finding (synthetic, for previewing the message format only)", run_id="0" * 32, sequence=1,
        occurred_at=now, created_at=now,
        snapshot={"category": "risk", "description": "Synthetic sample; no real finding exists.",
                  "observed_value": 1.6, "expected_value": "< 1.0", "first_seen": now.isoformat(),
                  "evidence": [{"reference": "portfolio:sample", "kind": "portfolio", "summary": "sample"}],
                  "source": {"source": "experiment_registry", "experiment_id": 0}, "is_paper_snapshot": True},
    )


def _event_json(e: AlertEvent) -> dict:
    return {"event_id": e.event_id, "type": e.event_type.value, "finding_id": e.finding_id, "rule_id": e.rule_id,
            "severity": e.severity.value, "previous_severity": e.previous_severity.value if e.previous_severity else None,
            "run_id": e.run_id, "created_at": e.created_at.isoformat(), "reason": e.reason}


def run_alerts(
    argv: Sequence[str] | None = None,
    *,
    env=None,
    emit: Callable[[str], None] | None = None,
    transport_factory: Callable[[AlertConfig], object] | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    emit = emit or (lambda line: print(line, flush=True))
    p = argparse.ArgumentParser(prog="python -m src.cli alerts")
    p.add_argument("--db", default=None, help="monitoring database path (default data/monitoring/monitoring.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config", help="validate alert configuration (never sends)")
    pv = sub.add_parser("preview", help="render a message without sending it")
    g = pv.add_mutually_exclusive_group(required=True)
    g.add_argument("--event-id")
    g.add_argument("--latest", action="store_true")
    g.add_argument("--sample", choices=["NEW", "ESCALATED", "RESOLVED"])
    st = sub.add_parser("status", help="inspect events and deliveries (read-only)")
    st.add_argument("--finding-id")
    st.add_argument("--event-id")
    st.add_argument("--all", action="store_true", help="include delivered deliveries")
    st.add_argument("--limit", type=int, default=20)
    st.add_argument("--json", action="store_true")
    dp = sub.add_parser("dispatch", help="deliver eligible pending alerts")
    dp.add_argument("--once", action="store_true", help="required: process the outbox once and exit")
    dp.add_argument("--limit", type=int, default=None, help="max delivery attempts (default ATP_ALERT_BATCH_LIMIT)")
    dp.add_argument("--event-id")
    dp.add_argument("--json", action="store_true")
    rt = sub.add_parser("retry", help="requeue FAILED deliveries (same event ids; no new events)")
    rg = rt.add_mutually_exclusive_group(required=True)
    rg.add_argument("--all-failed", action="store_true")
    rg.add_argument("--event-id")
    rg.add_argument("--delivery-id")
    ts_ = sub.add_parser("test-send", help="send ONE clearly-marked test message to an authorised test recipient")
    ts_.add_argument("--to", required=True)
    ts_.add_argument("--confirm-send", action="store_true", help="actually send (without it: dry run only)")
    args = p.parse_args(list(argv) if argv is not None else None)

    try:
        cfg = AlertConfig.from_env(env)
    except AlertConfigError as exc:
        emit(f"alerts: invalid configuration: {exc}")
        return EXIT_CONFIG

    # ---- config (never sends)
    if args.cmd == "config":
        problems = cfg.problems()
        emit(json.dumps(cfg.describe(), indent=2))
        if not cfg.enabled:
            emit("alerting is DISABLED (set ATP_ALERTS_ENABLED=true to enable); nothing will be recorded or sent.")
            return EXIT_OK
        if problems:
            for pr in problems:
                emit(f"PROBLEM: {pr}")
            return EXIT_CONFIG
        emit("configuration is valid. No message was sent.")
        return EXIT_OK

    # ---- preview / test-send need no database for samples
    db_path = Path(args.db) if args.db else Path(DEFAULT_DB_PATH)
    if args.cmd == "preview" and args.sample:
        msg = render_message(_sample_event(args.sample), cfg)
        emit(f"Subject: {msg.subject}\n\n{msg.body}")
        emit("(preview only: nothing was sent)")
        return EXIT_OK

    if args.cmd == "test-send":
        return _test_send(args, cfg, emit, transport_factory)

    if str(db_path) != ":memory:" and not db_path.exists():
        emit(f"alerts: monitoring database not found at {db_path}; refusing to create one.")
        return EXIT_CONFIG
    store = MonitoringStore(db_path)
    alerts = store.alerts

    if args.cmd == "preview":
        ev = alerts.get_event(args.event_id) if args.event_id else (alerts.list_events(limit=1) or [None])[0]
        if ev is None:
            emit("alerts: no such event." if args.event_id else "alerts: no alert events recorded yet.")
            return EXIT_CONFIG
        msg = render_message(ev, cfg)
        emit(f"Subject: {msg.subject}\n\n{msg.body}")
        emit("(preview only: nothing was sent)")
        return EXIT_OK

    if args.cmd == "status":
        return _status(args, alerts, emit)

    if args.cmd == "retry":
        n = alerts.requeue_failed(clock(), delivery_id=args.delivery_id, event_id=args.event_id)
        emit(f"requeued {n} failed delivery(ies) for retry (original event ids preserved). "
             "Deliver with: python -m src.cli alerts dispatch --once")
        return EXIT_OK

    # ---- dispatch
    if not args.once:
        emit("alerts dispatch: pass --once (no background dispatcher exists; use monitor-scheduler "
             "--dispatch-alerts for periodic delivery).")
        return EXIT_CONFIG
    try:
        if not cfg.enabled:
            raise AlertConfigError("alerting is not enabled (ATP_ALERTS_ENABLED=true required)")
        cfg.validate()
        transport = (transport_factory or build_transport)(cfg)
        summary = AlertDispatcher(alerts, transport, cfg, clock=clock).dispatch(limit=args.limit, event_id=args.event_id)
    except AlertConfigError as exc:
        emit(f"alerts dispatch: {exc}")
        return EXIT_CONFIG
    counts = alerts.counts()
    if args.json:
        emit(json.dumps({"attempted": summary.attempted, "delivered": summary.delivered,
                         "retry_scheduled": summary.retry_scheduled, "failed": summary.failed,
                         "stopped_reason": summary.stopped_reason, "counts": counts}))
    else:
        for line in summary.lines:
            emit(line)
        emit(summary.one_line())
        emit(f"outbox: pending={counts['PENDING']} retry_pending={counts['RETRY_PENDING']} "
             f"in_progress={counts['IN_PROGRESS']} failed={counts['FAILED']} delivered={counts['DELIVERED']}")
    return _exit_for(counts)


def _exit_for(counts: dict[str, int]) -> int:
    if counts["FAILED"]:
        return EXIT_FAILED
    if counts["PENDING"] or counts["RETRY_PENDING"] or counts["IN_PROGRESS"]:
        return EXIT_PENDING
    return EXIT_OK


def _status(args, alerts, emit) -> int:
    counts = alerts.counts()
    deliveries = alerts.list_deliveries(event_id=args.event_id, finding_id=args.finding_id, limit=args.limit * 5)
    if not args.all:
        deliveries = [d for d in deliveries if d.status != DeliveryStatus.DELIVERED]
    deliveries = deliveries[: args.limit]
    rows = []
    for d in deliveries:
        ev = alerts.get_event(d.event_id)
        rows.append({
            "delivery_id": d.delivery_id, "event_id": d.event_id, "channel": d.channel, "status": d.status.value,
            "attempts_in_cycle": d.attempt_count, "next_attempt_at": d.next_attempt_at.isoformat() if d.next_attempt_at else None,
            "last_error": d.last_error, "external_id": d.external_id,
            "event_type": ev.event_type.value if ev else None, "finding_id": ev.finding_id if ev else None,
            "run_id": ev.run_id if ev else None,
            "attempts": [{"n": a.attempt_no, "outcome": a.outcome, "finished_at": a.finished_at.isoformat() if a.finished_at else None,
                          "error": a.error} for a in alerts.attempts_for(d.delivery_id)],
        })
    if args.json:
        emit(json.dumps({"counts": counts, "initialized": alerts.initialized_info(), "deliveries": rows}, indent=2))
    else:
        info = alerts.initialized_info()
        emit(f"alert events: {counts['events']}   initialized: {info.get('initialized_at', 'no')} "
             f"(existing-findings policy: {info.get('initial_policy', '-')})")
        emit(f"deliveries: pending={counts['PENDING']} retry_pending={counts['RETRY_PENDING']} "
             f"in_progress={counts['IN_PROGRESS']} failed={counts['FAILED']} delivered={counts['DELIVERED']}")
        for r in rows:
            emit(f"  {r['status']:<13} event={r['event_id']} {r['event_type']} finding={r['finding_id']} run={r['run_id']} "
                 f"channel={r['channel']} attempts={r['attempts_in_cycle']} next={r['next_attempt_at']} "
                 f"error={r['last_error'] or '-'}")
    return _exit_for(counts)


def _test_send(args, cfg: AlertConfig, emit, transport_factory) -> int:
    if not cfg.enabled:
        emit("alerts test-send: alerting is not enabled; nothing sent.")
        return EXIT_CONFIG
    try:
        cfg.validate()
    except AlertConfigError as exc:
        emit(f"alerts test-send: invalid configuration: {exc}")
        return EXIT_CONFIG
    if args.to not in cfg.test_recipients:
        emit("alerts test-send: --to must be listed in ATP_ALERT_TEST_RECIPIENTS (a designated test destination). "
             "Nothing sent.")
        return EXIT_CONFIG
    msg = render_test_message(cfg, args.to, "TEST-" + uuid4().hex[:12])
    if not args.confirm_send:
        emit(f"DRY RUN (no --confirm-send): would send to {args.to}\nSubject: {msg.subject}\n\n{msg.body}")
        return EXIT_OK
    transport = (transport_factory or build_transport)(cfg)
    result = transport.send(msg)
    if result.delivered:
        emit(f"test message accepted by the transport (id {result.external_id}) for {args.to}.")
        return EXIT_OK
    emit(f"test-send FAILED ({result.outcome}): {result.error}")
    return EXIT_FAILED
