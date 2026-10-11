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


def _default_service(experiment_id: int | None, db_path: str | None) -> MonitoringService:
    from src.data.service import MarketDataService
    from src.experiments.registry import ExperimentRegistry
    from src.monitoring.model_lookup import registry_model_lookup

    source = RegistryStateSource(
        ExperimentRegistry(), experiment_id=experiment_id, model_lookup=registry_model_lookup()
    )
    store = MonitoringStore(db_path) if db_path else MonitoringStore()
    return MonitoringService(source, store, market_data=MarketDataService())


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

    print(json.dumps(asdict(report), indent=2, default=str) if args.json else format_report(report))
    if run.persistence_error:
        return EXIT_ERROR
    if report.status in ("CRITICAL", "WARNING"):
        return EXIT_ATTENTION
    return EXIT_UNKNOWN if report.status == "UNKNOWN" else EXIT_OK
