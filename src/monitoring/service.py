"""MonitoringService -- src/monitoring/service.py (ADR-0050).

Gathers platform state, runs the deterministic checks, applies the
finding lifecycle and persists the result. Independent of Anthropic and
Streamlit; fully useful with no API key.

Lifecycle rules (the only way a finding changes state):
  * a condition detected again          -> same finding: last_seen,
                                           occurrence_count advance
  * a condition detected for the first time -> new OPEN finding
  * a previously-open condition that a check *actually evaluated* and no
    longer detects                      -> RESOLVED
  * a check that was NOT_EVALUABLE, or did not evaluate that exact
    (rule, entity)                      -> finding is left untouched
Nothing else -- not an LLM, not a tool -- can resolve or suppress one.

Read-only toward trading state: the service never calls a Portfolio,
PaperBroker, risk or execution mutator.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from src.calendar.base import TradingCalendar
from src.calendar.nyse import NYSECalendar
from src.data.service import MarketDataService
from src.monitoring.alerts.models import (
    AlertEvent,
    AlertEventSettings,
    AlertEventType,
    event_key,
    is_escalation,
)
from src.monitoring.alerts.store import new_id as new_event_id
from src.monitoring.checks import Check, CheckContext, default_checks
from src.monitoring.config import CONFIG_VERSION, MonitoringConfig
from src.monitoring.models import (
    CheckResult,
    CheckStatus,
    CheckSummary,
    MonitoringReport,
    MonitoringRun,
    MonitoringStatus,
    MonitorFinding,
    new_id,
    run_to_report,
    utc_now,
)
from src.monitoring.state import PlatformState, StateSource, observe_market_data
from src.monitoring.store import MonitoringStore


class MonitoringService:
    def __init__(
        self,
        source: StateSource,
        store: MonitoringStore,
        *,
        market_data: MarketDataService | None = None,
        config: MonitoringConfig | None = None,
        checks: list[Check] | None = None,
        calendar: TradingCalendar | None = None,
        clock: Callable[[], datetime] = utc_now,
        alerts: AlertEventSettings | None = None,
    ) -> None:
        self._source = source
        self._store = store
        self._market_data = market_data
        self._config = config or MonitoringConfig()
        self._checks = checks if checks is not None else default_checks()
        self._calendar = calendar or NYSECalendar()
        self._clock = clock
        self._alerts = alerts or AlertEventSettings()

    @property
    def state_source(self) -> StateSource:
        return self._source

    @property
    def alert_settings(self) -> AlertEventSettings:
        return self._alerts

    @property
    def store(self) -> MonitoringStore:
        return self._store

    @property
    def config(self) -> MonitoringConfig:
        return self._config

    def run_once(self) -> MonitoringRun:
        started = self._clock()
        run_id = new_id()
        errors: list[str] = []

        try:
            state = self._source.load(started)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"state source failed: {type(exc).__name__}: {str(exc)[:200]}")
            state = PlatformState(as_of=started, source="unavailable", notes=["state source failed"])

        self._observe_market_data(state, started)
        ctx = CheckContext(now=started, calendar=self._calendar)

        results: list[CheckResult] = []
        for check in self._checks:
            try:
                res = check.run(state, self._config, ctx)
            except Exception as exc:  # noqa: BLE001 -- one broken check must not stop the others
                res = CheckResult.not_evaluable(check.check_id, f"check raised {type(exc).__name__}")
                errors.append(f"{check.check_id}: {type(exc).__name__}: {str(exc)[:200]}")
            results.append(res)

        # ---- lifecycle ---------------------------------------------------
        now = self._clock()
        try:
            open_fp = self._store.open_by_fingerprint()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"could not read open findings: {type(exc).__name__}")
            open_fp = {}
        start_severity = {fp: f.severity for fp, f in open_fp.items()}  # before observe_again mutates them
        changed: dict[str, MonitorFinding] = {}
        active_fp: set[str] = set()
        per_check_ids: dict[str, list[str]] = {}
        for res in results:
            ids = per_check_ids.setdefault(res.check_id, [])
            for cand in res.candidates:
                fp = cand.fingerprint
                active_fp.add(fp)
                finding = changed.get(fp) or open_fp.get(fp)
                if finding is None:
                    finding = MonitorFinding.from_candidate(
                        cand, now=now, run_id=run_id, rule_version=CONFIG_VERSION
                    )
                else:
                    finding.observe_again(cand, now=now, run_id=run_id)
                changed[fp] = finding
                ids.append(finding.finding_id)
        checked_fp = set().union(*(r.checked for r in results if r.status != CheckStatus.NOT_EVALUABLE)) if results else set()
        resolved_ids: list[str] = []
        for fp, finding in open_fp.items():
            if fp in checked_fp and fp not in active_fp:
                finding.resolve(now=now, run_id=run_id)
                changed[fp] = finding
                resolved_ids.append(finding.finding_id)

        summaries = [
            CheckSummary(
                r.check_id, r.status, r.reason, r.summary or (r.reason or ""),
                tuple(per_check_ids.get(r.check_id, [])), tuple(r.notes),
            )
            for r in results
        ]
        run = MonitoringRun(
            run_id=run_id, started_at=started, finished_at=now, status=self._status(results),
            checks=summaries, config_version=CONFIG_VERSION, config_snapshot=self._config.to_dict(),
            source=state.describe(),
            finding_ids=[f.finding_id for fp, f in changed.items() if fp in active_fp],
            resolved_finding_ids=resolved_ids, errors=errors,
        )
        events: list[AlertEvent] = []
        init_policy: str | None = None
        if self._alerts.enabled:
            try:
                events, init_policy = self._alert_events(
                    state, run_id, now, open_fp, start_severity, changed, active_fp, resolved_ids
                )
            except Exception as exc:  # noqa: BLE001 -- alert capture must never break monitoring
                errors.append(f"alert event capture failed: {type(exc).__name__}: {str(exc)[:200]}")
                run.errors = errors
        try:
            if events or init_policy is not None:
                self._store.apply(
                    run, list(changed.values()), alert_events=events,
                    alert_channels=self._alerts.channels, alert_initialize_policy=init_policy,
                )
            else:  # unchanged call shape when alerting is off or idle (keeps store subclasses compatible)
                self._store.apply(run, list(changed.values()))
            run.persisted = True
        except Exception as exc:  # noqa: BLE001 -- surface, never claim persisted
            run.persisted = False
            run.persistence_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            run.errors.append(f"persistence failed: {run.persistence_error}")
        self._last_changed = changed
        return run

    # ------------------------------------------------------------------ alert events (ADR-0052)
    def _alert_events(self, state, run_id, now, open_fp, start_severity, changed, active_fp, resolved_ids):
        """Derive logical alert events from THIS run's persisted-to-be finding transitions. Pure and
        deterministic: NEW (finding created), ESCALATED (severity strictly rose vs. the stored one),
        RESOLVED (the lifecycle above resolved it). Nothing here evaluates a rule."""
        initialized = self._store.alerts.is_initialized()
        events: list[AlertEvent] = []
        resolved = set(resolved_ids)

        def make(etype, f, *, previous=None, reason="transition", sequence=None):
            seq = f.occurrence_count if sequence is None else sequence
            return AlertEvent(
                event_id=new_event_id(), idempotency_key=event_key(etype, f.finding_id, seq),
                event_type=etype, finding_id=f.finding_id, fingerprint=f.fingerprint, rule_id=f.rule_id,
                severity=f.severity, previous_severity=previous, finding_status=f.status, title=f.title,
                run_id=run_id, sequence=seq, occurred_at=now, created_at=now,
                snapshot=_alert_snapshot(f, state), reason=reason,
            )

        for fp in sorted(active_fp):  # deterministic order
            f = changed[fp]
            if fp not in open_fp:
                events.append(make(AlertEventType.NEW, f))
            elif is_escalation(start_severity[fp], f.severity):
                events.append(make(AlertEventType.ESCALATED, f, previous=start_severity[fp]))
        for fp, f in sorted(open_fp.items()):
            if f.finding_id in resolved:
                events.append(make(AlertEventType.RESOLVED, f))
        init_policy = None
        if not initialized:
            init_policy = "notify" if self._alerts.notify_existing else "baseline"
            if self._alerts.notify_existing:
                for fp, f in sorted(open_fp.items()):
                    if f.finding_id not in resolved:
                        events.append(make(AlertEventType.NEW, f, reason="existing_at_enablement", sequence=1))
        return events, init_policy

    def report(self, run: MonitoringRun) -> MonitoringReport:
        findings = [f for f in (self._store.get_finding(i) for i in run.finding_ids) if f is not None]
        if not run.persisted:  # store unavailable: fall back to this run's in-memory findings
            findings = [f for f in getattr(self, "_last_changed", {}).values() if f.finding_id in run.finding_ids]
        return run_to_report(run, findings)

    # ------------------------------------------------------------------
    def _observe_market_data(self, state: PlatformState, now: datetime) -> None:
        if not state.market_data_targets:
            return
        if self._market_data is None:
            state.market_data_unavailable_reason = "no MarketDataService configured"
            return
        for target in state.market_data_targets:
            state.market_data[target.key] = observe_market_data(
                self._market_data, target, now, self._config.market_data_lookback_days
            )

    @staticmethod
    def _status(results: list[CheckResult]) -> MonitoringStatus:
        statuses = {r.status for r in results}
        if CheckStatus.CRITICAL in statuses:
            return MonitoringStatus.CRITICAL
        if CheckStatus.WARNING in statuses:
            return MonitoringStatus.WARNING
        if not results or CheckStatus.NOT_EVALUABLE in statuses:
            return MonitoringStatus.UNKNOWN
        return MonitoringStatus.HEALTHY


def _alert_snapshot(f: MonitorFinding, state: PlatformState) -> dict:
    """Facts frozen at the transition, so a delayed email describes the transition accurately."""
    desc = state.describe()
    return {
        "category": f.category,
        "description": f.description,
        "observed_value": f.observed_value,
        "expected_value": f.expected_value,
        "entities": dict(f.entities),
        "first_seen": f.first_seen.isoformat(),
        "last_seen": f.last_seen.isoformat(),
        "occurrence_count": f.occurrence_count,
        "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
        "evidence": [{"reference": e.reference, "kind": e.kind, "summary": e.summary} for e in f.evidence],
        "source": {"source": desc.get("source"), "experiment_id": desc.get("experiment_id")},
        "is_paper_snapshot": desc.get("source") == "experiment_registry",
    }
