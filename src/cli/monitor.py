"""Deterministic monitoring CLI -- src/cli/monitor.py (ADR-0050).

    python -m src.cli monitor --once [--experiment-id N] [--json]

Runs every deterministic check once against the platform's real
persisted paper state and prints the result. Needs no LLM provider or
API key. Starts no background process and schedules nothing: monitoring
is on-demand.

With no saved paper portfolio it says so explicitly (every check that
needs one reports NOT_EVALUABLE) -- it never invents a sample portfolio.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Sequence

from src.monitoring import MonitoringService, MonitoringStore, RegistryStateSource
from src.monitoring.models import MonitoringReport

EXIT_OK, EXIT_ATTENTION, EXIT_UNKNOWN, EXIT_ERROR = 0, 1, 3, 2


def _default_service(
    experiment_id: int | None, db_path: str | None, registry_path: str | None = None
) -> MonitoringService:
    from src.data.service import MarketDataService
    from src.experiments.registry import ExperimentRegistry
    from src.monitoring.model_lookup import registry_model_lookup

    registry = ExperimentRegistry(registry_path) if registry_path else ExperimentRegistry()
    source = RegistryStateSource(registry, experiment_id=experiment_id, model_lookup=registry_model_lookup())
    store = MonitoringStore(db_path) if db_path else MonitoringStore()
    from src.monitoring.alerts.config import AlertConfig

    # Alert EVENTS are recorded only when alerting is explicitly enabled and valid (invalid => raises).
    alerts = AlertConfig.from_env().event_settings()
    return MonitoringService(source, store, market_data=MarketDataService(), alerts=alerts)


def format_report(report: MonitoringReport) -> str:
    lines = [
        f"Monitoring run: {report.run_id}",
        f"Run status: {report.status}   (persisted: {report.persisted})",
        f"Source: {report.source.get('source')}  experiment_id={report.source.get('experiment_id')}",
        f"Checks evaluated: {report.counts['checks_run'] - report.counts['unknown_or_not_evaluable']}"
        f" / {report.counts['checks_run']}",
        f"Healthy: {report.counts['checks_passed']}  Warnings: {report.counts['warnings']}  "
        f"Critical: {report.counts['critical']}  Unknown/not evaluable: {report.counts['unknown_or_not_evaluable']}",
        "",
        "Checks:",
    ]
    for c in report.checks:
        extra = f" -- {c['reason']}" if c["reason"] else (f" -- {c['summary']}" if c["summary"] else "")
        lines.append(f"  [{c['status']}] {c['check_id']}{extra}")
        for note in c["notes"]:
            lines.append(f"      note: {note}")
    if report.findings:
        lines += ["", "Findings:"]
        for f in report.findings:
            refs = ", ".join(f.get("evidence_references", [])) or "-"
            lines.append(
                f"  {f['finding_id']}  [{f['severity']}] {f['title']}  "
                f"(rule {f['rule_id']}, seen {f['occurrence_count']}x)  evidence: {refs}"
            )
    else:
        lines += ["", "Findings: none"]
    for note in report.source.get("notes", []):
        lines.append(f"\nNote: {note}")
    for err in report.errors:
        lines.append(f"Error: {err}")
    if report.status == "UNKNOWN":
        lines.append(
            "\nStatus UNKNOWN means one or more checks lacked the evidence to decide; "
            "it does not mean the system is healthy."
        )
    return "\n".join(lines)


def _alert_info(service, run) -> dict | None:
    """Queued alert events for this run, or None when alerting is off (or the service has no alert store)."""
    settings = getattr(service, "alert_settings", None)
    if settings is None or not getattr(settings, "enabled", False) or not run.persisted:
        return None
    try:
        events = service.store.alerts.events_for_run(run.run_id)
    except Exception:  # noqa: BLE001 -- reporting must never fail the command
        return None
    return {"queued_events": len(events), "event_ids": [e.event_id for e in events],
            "delivery": "queued only; run `alerts dispatch --once` to deliver"}


def run_monitor(argv: Sequence[str] | None = None, *, service_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli monitor")
    parser.add_argument("--once", action="store_true", help="run all checks once (required; no daemon exists)")
    parser.add_argument("--experiment-id", type=int, default=None, help="monitor this saved paper portfolio")
    parser.add_argument("--db", default=None, help="monitoring database path (default data/monitoring/monitoring.db)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.once:
        print("monitor: pass --once. Monitoring is on-demand; no background service or scheduler is provided.")
        return EXIT_ERROR
    try:
        service = (
            service_factory(args.experiment_id, args.db)
            if service_factory
            else _default_service(args.experiment_id, args.db)
        )
        run = service.run_once()
        report = service.report(run)
    except Exception as exc:  # noqa: BLE001
        print(f"monitor: failed to run ({type(exc).__name__}: {str(exc)[:200]})")
        return EXIT_ERROR

    alert_info = _alert_info(service, run)
    if args.json:
        payload = asdict(report)
        if alert_info is not None:
            payload["alerts"] = alert_info
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_report(report))
        if alert_info is not None:
            print(f"\nAlerts: {alert_info['queued_events']} event(s) recorded for this run "
                  "(queued, NOT delivered). Deliver with: python -m src.cli alerts dispatch --once")
    if run.persistence_error:
        return EXIT_ERROR
    if run.errors and report.status == "UNKNOWN":
        # the monitor itself failed (state source / check execution), so "unknown" is not a
        # data judgement -- surface it as an error, distinct from legitimately missing evidence
        return EXIT_ERROR
    if report.status in ("CRITICAL", "WARNING"):
        return EXIT_ATTENTION
    return EXIT_UNKNOWN if report.status == "UNKNOWN" else EXIT_OK
