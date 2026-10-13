"""Alert dispatcher -- src/monitoring/alerts/dispatcher.py (ADR-0052).

Processes the durable outbox. It never evaluates a rule, never touches findings or runs, never
calls an LLM, broker or portfolio code. It is only ever started explicitly (the `alerts dispatch`
CLI or the scheduler's opt-in `--dispatch-alerts`).

Delivery semantics (honest, at-least-once):
* an event is DELIVERED only after the transport confirms success;
* a retryable failure schedules RETRY_PENDING with bounded exponential backoff (no jitter, so the
  schedule is deterministic and testable); permanent failures and exhausted retries become FAILED
  and stay inspectable and manually requeueable;
* a crash between "transport accepted" and "DELIVERED recorded" leaves a leased IN_PROGRESS row that
  is re-sent after the lease expires, so a duplicate external message is possible at that boundary;
* a retry always reuses the original event id (the same `alert_events` row).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.monitoring.alerts.config import AlertConfig
from src.monitoring.alerts.format import render_message
from src.monitoring.alerts.models import DeliveryStatus
from src.monitoring.alerts.store import AlertStore
from src.monitoring.alerts.transports import AlertTransport, redact


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DispatchSummary:
    attempted: int = 0
    delivered: int = 0
    retry_scheduled: int = 0
    failed: int = 0
    stopped_reason: str = "idle"  # idle | batch_limit | time_budget | stop_requested
    lines: list[str] = field(default_factory=list)

    def one_line(self) -> str:
        return (f"alerts dispatch: attempted={self.attempted} delivered={self.delivered} "
                f"retry_scheduled={self.retry_scheduled} failed={self.failed} stopped={self.stopped_reason}")


class AlertDispatcher:
    def __init__(self, store: AlertStore, transport: AlertTransport, config: AlertConfig, *,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        self._store, self._transport, self._cfg, self._clock = store, transport, config, clock

    def backoff_seconds(self, attempt_no: int) -> float:
        """Delay before the next try after the `attempt_no`-th failure (1-based): base * 2^(n-1), capped."""
        return min(self._cfg.backoff_base_seconds * (2 ** max(attempt_no - 1, 0)), self._cfg.backoff_max_seconds)

    def dispatch(self, *, limit: int | None = None, budget_seconds: float | None = None,
                 event_id: str | None = None, should_stop: Callable[[], bool] | None = None) -> DispatchSummary:
        """Send eligible deliveries, oldest event first, up to `limit` attempts / `budget_seconds`.

        Raises `AlertConfigError` (before claiming anything) if the transport is not usable, so a
        misconfiguration never burns retry attempts."""
        self._transport.validate()
        limit = self._cfg.batch_limit if limit is None else limit
        summary = DispatchSummary()
        started = self._clock()
        while True:
            if should_stop is not None and should_stop():
                summary.stopped_reason = "stop_requested"
                break
            if summary.attempted >= limit:
                summary.stopped_reason = "batch_limit"
                break
            if budget_seconds is not None and (self._clock() - started).total_seconds() >= budget_seconds:
                summary.stopped_reason = "time_budget"
                break
            now = self._clock()
            claimed = self._store.claim_next(self._transport.channel, now, self._cfg.lease_seconds, event_id=event_id)
            if claimed is None:
                summary.stopped_reason = "idle"
                break
            delivery, event, attempt = claimed
            summary.attempted += 1
            try:
                result = self._transport.send(render_message(event, self._cfg))
            except Exception as exc:  # noqa: BLE001 -- a transport bug is a retryable failure, never a crash
                from src.monitoring.alerts.models import TransportResult
                result = TransportResult.retryable(redact(f"transport raised {type(exc).__name__}",
                                                          [self._cfg.smtp_password, self._cfg.smtp_username]))
            done = self._clock()
            if result.delivered:
                self._store.complete_delivered(delivery.delivery_id, attempt.attempt_id, done, result.external_id)
                summary.delivered += 1
                summary.lines.append(f"delivered event {event.event_id} ({event.event_type.value}) via {delivery.channel}")
                continue
            error = redact(result.error or "unknown failure", [self._cfg.smtp_password, self._cfg.smtp_username])
            permanent = result.outcome == "permanent"
            exhausted = delivery.attempt_count >= self._cfg.max_attempts
            if permanent or exhausted:
                self._store.complete_failed(delivery.delivery_id, attempt.attempt_id, done, error,
                                            retry_at=None, permanent=permanent)
                summary.failed += 1
                why = "permanent failure" if permanent else f"retries exhausted ({delivery.attempt_count})"
                summary.lines.append(f"FAILED event {event.event_id} ({event.event_type.value}): {why}: {error}")
            else:
                retry_at = done + timedelta(seconds=self.backoff_seconds(delivery.attempt_count))
                status = self._store.complete_failed(delivery.delivery_id, attempt.attempt_id, done, error,
                                                     retry_at=retry_at, permanent=False)
                assert status == DeliveryStatus.RETRY_PENDING
                summary.retry_scheduled += 1
                summary.lines.append(f"retry scheduled for event {event.event_id} at "
                                     f"{retry_at.strftime('%Y-%m-%dT%H:%M:%SZ')}: {error}")
        return summary
