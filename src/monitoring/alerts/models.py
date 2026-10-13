"""Typed alert records -- src/monitoring/alerts/models.py (ADR-0052)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.monitoring.models import FindingSeverity, FindingStatus

# Canonical severity ordering is the monitoring system's own (WARNING < CRITICAL).
SEVERITY_RANK: dict[FindingSeverity, int] = {FindingSeverity.WARNING: 1, FindingSeverity.CRITICAL: 2}


def is_escalation(previous: FindingSeverity, current: FindingSeverity) -> bool:
    return SEVERITY_RANK[current] > SEVERITY_RANK[previous]


class AlertEventType(str, Enum):
    NEW = "NEW"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"


class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DELIVERED = "DELIVERED"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"  # permanent failure or retries exhausted; inspectable, manually requeueable


NON_TERMINAL = (DeliveryStatus.PENDING, DeliveryStatus.IN_PROGRESS, DeliveryStatus.RETRY_PENDING)


def event_key(event_type: AlertEventType, finding_id: str, sequence: int | None = None) -> str:
    """Deterministic idempotency key for one logical finding transition.

    NEW and RESOLVED happen at most once per finding id (a condition that reappears after
    resolution is a NEW finding with a new id -- ADR-0050). ESCALATED is keyed by the finding's
    durable `occurrence_count` at the transition, so a repeated discovery pass of the same
    persisted state yields the same key while a later, distinct escalation yields a new one."""
    if event_type == AlertEventType.ESCALATED:
        if sequence is None:
            raise ValueError("ESCALATED events require the finding's occurrence_count as a sequence")
        return f"{event_type.value}:{finding_id}:{sequence}"
    return f"{event_type.value}:{finding_id}"


@dataclass(frozen=True)
class AlertEventSettings:
    """What the deterministic service needs in order to record alert events (no transport details)."""

    enabled: bool = False
    notify_existing: bool = False  # False = baseline policy: pre-existing open findings are not announced
    channels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.enabled and not self.channels:
            raise ValueError("alert events enabled but no delivery channel configured")


@dataclass(frozen=True)
class AlertEvent:
    event_id: str
    idempotency_key: str
    event_type: AlertEventType
    finding_id: str
    fingerprint: str
    rule_id: str
    severity: FindingSeverity
    previous_severity: FindingSeverity | None
    finding_status: FindingStatus
    title: str
    run_id: str
    sequence: int
    occurred_at: datetime
    created_at: datetime
    snapshot: dict[str, Any] = field(default_factory=dict)
    reason: str = "transition"  # "transition" | "existing_at_enablement"

    def __post_init__(self) -> None:
        for name in ("occurred_at", "created_at"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware (UTC)")

    def payload_json(self) -> str:
        return json.dumps({"snapshot": self.snapshot, "reason": self.reason}, default=str, sort_keys=True)


@dataclass(frozen=True)
class AlertDelivery:
    delivery_id: str
    event_id: str
    channel: str
    status: DeliveryStatus
    attempt_count: int  # attempts in the current retry cycle
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    last_error: str | None
    external_id: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None = None


@dataclass(frozen=True)
class AlertAttempt:
    attempt_id: str
    delivery_id: str
    event_id: str
    channel: str
    attempt_no: int
    started_at: datetime
    finished_at: datetime | None
    outcome: str  # IN_PROGRESS | DELIVERED | RETRYABLE_FAILURE | PERMANENT_FAILURE | ABANDONED
    error: str | None = None
    external_id: str | None = None
    next_retry_at: datetime | None = None


@dataclass(frozen=True)
class TransportResult:
    outcome: str  # "delivered" | "retryable" | "permanent"
    external_id: str | None = None
    error: str | None = None
    detail: str | None = None

    @property
    def delivered(self) -> bool:
        return self.outcome == "delivered"

    @classmethod
    def ok(cls, external_id: str | None = None, detail: str | None = None) -> "TransportResult":
        return cls("delivered", external_id=external_id, detail=detail)

    @classmethod
    def retryable(cls, error: str) -> "TransportResult":
        return cls("retryable", error=error)

    @classmethod
    def permanent(cls, error: str) -> "TransportResult":
        return cls("permanent", error=error)


@dataclass(frozen=True)
class AlertMessage:
    subject: str
    body: str
    event_id: str
    recipients: tuple[str, ...] = ()
    is_test: bool = False
