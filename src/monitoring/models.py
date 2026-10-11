"""Monitoring domain models -- src/monitoring/models.py (ADR-0050).

Deterministic, typed records for the Sprint 15 monitoring layer. Nothing
here knows about LLMs, Streamlit or any vendor. Every operational
timestamp is timezone-aware UTC.

Status vocabulary (kept deliberately small and honest):

    HEALTHY        -- the check had sufficient evidence and found nothing
    WARNING        -- a defined warning condition is present
    CRITICAL       -- a defined critical condition is present
    NOT_EVALUABLE  -- the check lacked the evidence to decide ("UNKNOWN");
                      *never* reported as healthy, and never allowed to
                      resolve an existing finding

A finding's identity is a **fingerprint** derived from the rule id and
the stable entity references (symbol, interval, ...) -- never from
volatile values or timestamps -- so the same underlying condition
deduplicates across runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class CheckStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class MonitoringStatus(str, Enum):
    """Run-level status: the worst check outcome; `UNKNOWN` when nothing
    is wrong but at least one check could not be evaluated (a run is
    `HEALTHY` only if every check was actually evaluated and clean)."""

    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class FindingSeverity(str, Enum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class FindingStatus(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


def fingerprint(rule_id: str, entities: dict[str, Any] | None = None) -> str:
    """Stable finding identity: rule id + sorted entity references."""
    payload = json.dumps({"rule": rule_id, "entities": entities or {}}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class MonitoringEvidence:
    """One piece of supporting evidence, always attributable.

    `reference` is a short stable pointer (e.g. `market_data:QQQ:1d`,
    `portfolio:experiment-7`) an agent or human can cite."""

    reference: str
    kind: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class FindingCandidate:
    """A condition a check detected *this run*, before deduplication."""

    rule_id: str
    category: str
    severity: FindingSeverity
    title: str
    description: str
    observed_value: Any = None
    expected_value: Any = None
    entities: dict[str, Any] = field(default_factory=dict)
    evidence: list[MonitoringEvidence] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.rule_id, self.entities)


@dataclass
class MonitorFinding:
    """A persisted, deduplicated finding with lifecycle."""

    finding_id: str
    fingerprint: str
    rule_id: str
    category: str
    severity: FindingSeverity
    status: FindingStatus
    title: str
    description: str
    observed_value: Any
    expected_value: Any
    entities: dict[str, Any]
    first_seen: datetime
    last_seen: datetime
    occurrence_count: int
    evidence: list[MonitoringEvidence]
    rule_version: str
    last_run_id: str
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("first_seen", "last_seen"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    @classmethod
    def from_candidate(
        cls, candidate: FindingCandidate, *, now: datetime, run_id: str, rule_version: str
    ) -> "MonitorFinding":
        return cls(
            finding_id=new_id(),
            fingerprint=candidate.fingerprint,
            rule_id=candidate.rule_id,
            category=candidate.category,
            severity=candidate.severity,
            status=FindingStatus.OPEN,
            title=candidate.title,
            description=candidate.description,
            observed_value=candidate.observed_value,
            expected_value=candidate.expected_value,
            entities=dict(candidate.entities),
            first_seen=now,
            last_seen=now,
            occurrence_count=1,
            evidence=list(candidate.evidence),
            rule_version=rule_version,
            last_run_id=run_id,
        )

    def observe_again(self, candidate: FindingCandidate, *, now: datetime, run_id: str) -> None:
        """Same condition seen again: advance `last_seen`/count, refresh
        the values and evidence, adopt the current severity."""
        self.last_seen = now
        self.occurrence_count += 1
        self.severity = candidate.severity
        self.title = candidate.title
        self.description = candidate.description
        self.observed_value = candidate.observed_value
        self.expected_value = candidate.expected_value
        self.evidence = list(candidate.evidence)
        self.last_run_id = run_id

    def resolve(self, *, now: datetime, run_id: str) -> None:
        self.status = FindingStatus.RESOLVED
        self.resolved_at = now
        self.last_run_id = run_id


@dataclass
class CheckResult:
    """What one monitor concluded this run.

    `checked` lists the fingerprints of every (rule, entity) condition
    this check actually evaluated -- present or not. Only a finding
    whose fingerprint is in `checked` (and not re-detected) may be
    resolved; anything the check could not look at stays open."""

    check_id: str
    status: CheckStatus
    candidates: list[FindingCandidate] = field(default_factory=list)
    checked: set[str] = field(default_factory=set)
    reason: str | None = None  # why NOT_EVALUABLE (or a short note)
    summary: str = ""
    notes: list[str] = field(default_factory=list)

    @classmethod
    def not_evaluable(cls, check_id: str, reason: str) -> "CheckResult":
        return cls(check_id=check_id, status=CheckStatus.NOT_EVALUABLE, reason=reason, summary=reason)

    def mark_checked(self, rule_id: str, **entities: Any) -> None:
        self.checked.add(fingerprint(rule_id, entities))

    def finalize_status(self) -> None:
        """Derive HEALTHY/WARNING/CRITICAL from the candidates (leaves a
        NOT_EVALUABLE result alone)."""
        if self.status == CheckStatus.NOT_EVALUABLE:
            return
        if any(c.severity == FindingSeverity.CRITICAL for c in self.candidates):
            self.status = CheckStatus.CRITICAL
        elif self.candidates:
            self.status = CheckStatus.WARNING
        else:
            self.status = CheckStatus.HEALTHY


@dataclass(frozen=True)
class CheckSummary:
    check_id: str
    status: CheckStatus
    reason: str | None
    summary: str
    finding_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class MonitoringRun:
    run_id: str
    started_at: datetime
    finished_at: datetime
    status: MonitoringStatus
    checks: list[CheckSummary]
    config_version: str
    config_snapshot: dict[str, Any]
    source: dict[str, Any]
    finding_ids: list[str] = field(default_factory=list)
    resolved_finding_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    persisted: bool = False
    persistence_error: str | None = None

    @property
    def checks_run(self) -> int:
        return len(self.checks)

    @property
    def checks_passed(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.HEALTHY)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARNING)

    @property
    def critical_findings(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.CRITICAL)

    @property
    def unknown_checks(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.NOT_EVALUABLE)


@dataclass(frozen=True)
class MonitoringReport:
    """Compact, serializable view of a run + its active findings, for
    humans and for the agent tools."""

    run_id: str
    status: str
    started_at: str
    finished_at: str
    persisted: bool
    counts: dict[str, int]
    checks: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    errors: list[str]
    source: dict[str, Any]
    persistence_error: str | None = None


def finding_to_dict(f: MonitorFinding, *, include_evidence: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "finding_id": f.finding_id,
        "fingerprint": f.fingerprint,
        "rule_id": f.rule_id,
        "category": f.category,
        "severity": f.severity.value,
        "status": f.status.value,
        "title": f.title,
        "description": f.description,
        "observed_value": f.observed_value,
        "expected_value": f.expected_value,
        "entities": f.entities,
        "first_seen": f.first_seen.isoformat(),
        "last_seen": f.last_seen.isoformat(),
        "occurrence_count": f.occurrence_count,
        "rule_version": f.rule_version,
        "last_run_id": f.last_run_id,
        "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
    }
    if include_evidence:
        out["evidence"] = [
            {"reference": e.reference, "kind": e.kind, "summary": e.summary, "data": e.data}
            for e in f.evidence
        ]
    else:
        out["evidence_references"] = [e.reference for e in f.evidence]
    return out


def run_to_report(run: MonitoringRun, findings: list[MonitorFinding]) -> MonitoringReport:
    return MonitoringReport(
        run_id=run.run_id,
        status=run.status.value,
        started_at=run.started_at.isoformat(),
        finished_at=run.finished_at.isoformat(),
        persisted=run.persisted,
        counts={
            "checks_run": run.checks_run,
            "checks_passed": run.checks_passed,
            "warnings": run.warnings,
            "critical": run.critical_findings,
            "unknown_or_not_evaluable": run.unknown_checks,
            "findings": len(findings),
        },
        checks=[
            {
                "check_id": c.check_id,
                "status": c.status.value,
                "reason": c.reason,
                "summary": c.summary,
                "finding_ids": list(c.finding_ids),
                "notes": list(c.notes),
            }
            for c in run.checks
        ],
        findings=[finding_to_dict(f, include_evidence=False) for f in findings],
        errors=list(run.errors),
        source=dict(run.source),
        persistence_error=run.persistence_error,
    )
