"""Alert events, durable outbox, dispatcher, SMTP transport, CLI (Sprint 17, ADR-0052).

Network-free: SMTP is exercised against a loopback `SmtpSink` or an injected fake client; time comes
from fake clocks; no real mail server, API key or broker is ever contacted. A DELIVERED status in these
tests means "accepted by the sink / fake transport", never "accepted by a remote provider".
"""

from __future__ import annotations

import ast
import contextlib
import email
import io
import json
import os
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.monitoring.store as store_module
from src.cli.alerts import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_PENDING, run_alerts
from src.cli.monitor import run_monitor
from src.monitoring import (
    FindingSeverity,
    FindingStatus,
    MonitoringService,
    MonitoringStore,
    PlatformState,
    StaticStateSource,
)
from src.monitoring.alerts.config import AlertConfig, AlertConfigError
from src.monitoring.alerts.dispatcher import AlertDispatcher
from src.monitoring.alerts.format import render_message
from src.monitoring.alerts.models import (
    AlertEvent,
    AlertEventSettings,
    AlertEventType,
    AlertMessage,
    DeliveryStatus,
    TransportResult,
)
from src.monitoring.alerts.sink import SmtpSink
from src.monitoring.alerts.transports import ConsoleTransport, SmtpTransport, build_transport
from src.monitoring.models import CheckResult, CheckStatus, FindingCandidate

REPO = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
EMAIL_SETTINGS = AlertEventSettings(enabled=True, channels=("email",))


# ------------------------------------------------------------------ helpers
class Clock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class SevCheck:
    """mode: 'warning' | 'critical' | 'good' | 'unknown'"""

    check_id = "scripted"

    def __init__(self, mode="critical", symbol="QQQ"):
        self.mode, self.symbol = mode, symbol

    def run(self, state, config, ctx):
        if self.mode == "unknown":
            return CheckResult.not_evaluable(self.check_id, "no data this time")
        res = CheckResult(self.check_id, CheckStatus.HEALTHY)
        res.mark_checked("scripted.rule", symbol=self.symbol)
        if self.mode in ("warning", "critical"):
            sev = FindingSeverity.CRITICAL if self.mode == "critical" else FindingSeverity.WARNING
            res.candidates.append(FindingCandidate("scripted.rule", "test", sev, "Scripted problem", "desc",
                                                   observed_value=1, expected_value=0,
                                                   entities={"symbol": self.symbol}))
        res.finalize_status()
        return res


def service(check=None, store=None, settings=EMAIL_SETTINGS, clock=None):
    chk = check or SevCheck()
    store = store or MonitoringStore(":memory:")
    clock = clock or Clock()
    svc = MonitoringService(StaticStateSource(PlatformState(as_of=T0, source="experiment_registry", experiment_id=7)),
                            store, checks=[chk], clock=_Ticking(clock), alerts=settings)
    return svc, chk, store, clock


class _Ticking:
    """Each call moves 1s so started/finished differ; shares the base clock's offset."""

    def __init__(self, base: Clock):
        self.base = base

    def __call__(self):
        self.base.advance(1)
        return self.base.t


def events(store):
    return list(reversed(store.alerts.list_events(limit=500)))


def cfg_smtp(sink_port=2525, **kw):
    base = dict(enabled=True, transport="smtp", smtp_host="127.0.0.1", smtp_port=sink_port, smtp_security="none",
                sender="atp@example.com", recipients=("ops@example.com",), max_attempts=3,
                backoff_base_seconds=60.0, backoff_max_seconds=300.0, batch_limit=10, lease_seconds=300.0)
    base.update(kw)
    return AlertConfig(**base)


class FakeTransport:
    name, channel = "smtp", "email"

    def __init__(self, results=None):
        self.results = list(results or [])
        self.sent: list[AlertMessage] = []

    def validate(self):
        return None

    def send(self, message):
        self.sent.append(message)
        r = self.results.pop(0) if self.results else TransportResult.ok("<ok@test>")
        if isinstance(r, Exception):
            raise r
        return r


def dispatcher(store, transport=None, clock=None, **cfg_kw):
    clock = clock or Clock()
    return AlertDispatcher(store.alerts, transport or FakeTransport(), cfg_smtp(**cfg_kw), clock=clock), clock


# =====================================================================================
# A. event lifecycle
# =====================================================================================
def test_new_finding_creates_one_new_event_and_a_pending_delivery_atomically():
    svc, _, store, _ = service()
    run = svc.run_once()
    (e,) = events(store)
    assert e.event_type == AlertEventType.NEW and e.severity == FindingSeverity.CRITICAL and e.run_id == run.run_id
    assert e.finding_id == run.finding_ids[0] and e.created_at.tzinfo is not None
    (d,) = store.alerts.list_deliveries()
    assert d.event_id == e.event_id and d.status == DeliveryStatus.PENDING and d.channel == "email"
    assert e.snapshot["is_paper_snapshot"] is True and e.snapshot["source"]["experiment_id"] == 7


def test_unchanged_open_finding_never_creates_another_event():
    svc, _, store, _ = service()
    for _ in range(4):
        svc.run_once()
    assert [e.event_type for e in events(store)] == [AlertEventType.NEW]
    assert store.alerts.counts()["events"] == 1


def test_escalation_creates_an_event_only_for_a_real_rise_and_each_rise_is_distinct():
    chk = SevCheck("warning")
    svc, _, store, _ = service(chk)
    svc.run_once()                                   # NEW (WARNING)
    svc.run_once()                                   # unchanged
    chk.mode = "critical"; svc.run_once()            # ESCALATED #1
    svc.run_once()                                   # unchanged CRITICAL: no event
    chk.mode = "warning"; svc.run_once()             # de-escalation: no event
    chk.mode = "critical"; svc.run_once()            # ESCALATED #2 (a genuinely new rise)
    kinds = [(e.event_type.value, e.severity.value) for e in events(store)]
    assert kinds == [("NEW", "WARNING"), ("ESCALATED", "CRITICAL"), ("ESCALATED", "CRITICAL")]
    esc = [e for e in events(store) if e.event_type == AlertEventType.ESCALATED]
    assert esc[0].previous_severity == FindingSeverity.WARNING
    assert esc[0].idempotency_key != esc[1].idempotency_key


def test_resolution_creates_exactly_one_resolved_event():
    chk = SevCheck()
    svc, _, store, _ = service(chk)
    svc.run_once()
    chk.mode = "good"; svc.run_once(); svc.run_once(); svc.run_once()
    assert [e.event_type.value for e in events(store)] == ["NEW", "RESOLVED"]
    res = events(store)[1]
    assert res.finding_status == FindingStatus.RESOLVED and "no longer detected" in render_message(res, cfg_smtp()).body


def test_unevaluable_run_never_creates_a_false_resolution_event():
    chk = SevCheck()
    svc, _, store, _ = service(chk)
    svc.run_once()
    chk.mode = "unknown"; svc.run_once(); svc.run_once()
    assert [e.event_type.value for e in events(store)] == ["NEW"]
    assert store.list_findings(status="OPEN")                     # still open
    chk.mode = "good"; svc.run_once()
    assert [e.event_type.value for e in events(store)] == ["NEW", "RESOLVED"]


def test_reappearance_after_resolution_is_a_new_finding_with_its_own_new_event():
    chk = SevCheck()
    svc, _, store, _ = service(chk)
    svc.run_once()
    chk.mode = "good"; svc.run_once()
    chk.mode = "critical"; svc.run_once()
    ev = events(store)
    assert [e.event_type.value for e in ev] == ["NEW", "RESOLVED", "NEW"]
    assert ev[0].finding_id != ev[2].finding_id and ev[0].idempotency_key != ev[2].idempotency_key
    assert ev[0].fingerprint == ev[2].fingerprint       # same condition, new lifecycle instance


def test_disabled_alerting_records_nothing_and_keeps_the_original_apply_call_shape():
    class Strict(MonitoringStore):
        def apply(self, run, findings):                  # no alert kwargs: old signature must still work
            return super().apply(run, findings)

    svc, _, store, _ = service(store=Strict(":memory:"), settings=AlertEventSettings())
    run = svc.run_once()
    assert run.persisted and store.alerts.counts()["events"] == 0 and not store.alerts.is_initialized()


def test_baseline_policy_does_not_announce_findings_that_were_already_open():
    chk = SevCheck("warning")
    store = MonitoringStore(":memory:")
    svc_off, _, _, clock = service(chk, store, AlertEventSettings())
    svc_off.run_once()                                       # opened while alerting was off
    svc_on, _, _, _ = service(chk, store, EMAIL_SETTINGS, clock)
    svc_on.run_once(); svc_on.run_once()
    assert events(store) == [] and store.alerts.is_initialized()
    assert store.alerts.initialized_info()["initial_policy"] == "baseline"
    chk.mode = "critical"; svc_on.run_once()                 # but later changes ARE announced
    chk.mode = "good"; svc_on.run_once()
    assert [e.event_type.value for e in events(store)] == ["ESCALATED", "RESOLVED"]


def test_notify_policy_announces_existing_findings_exactly_once():
    chk = SevCheck("critical")
    store = MonitoringStore(":memory:")
    svc_off, _, _, clock = service(chk, store, AlertEventSettings())
    svc_off.run_once()
    svc_on, _, _, _ = service(chk, store, AlertEventSettings(True, True, ("email",)), clock)
    for _ in range(3):
        svc_on.run_once()
    (e,) = events(store)
    assert e.event_type == AlertEventType.NEW and e.reason == "existing_at_enablement"
    assert "already open when alerting was enabled" in render_message(e, cfg_smtp()).body


def test_settings_require_a_channel_when_enabled():
    with pytest.raises(ValueError):
        AlertEventSettings(enabled=True, channels=())


def test_event_and_transition_roll_back_together_when_persistence_fails():
    original = store_module.insert_events

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")

    store_module.insert_events = boom
    try:
        svc, _, store, _ = service()
        run = svc.run_once()
    finally:
        store_module.insert_events = original
    assert run.persisted is False and "disk full" in run.persistence_error
    assert store.list_findings() == [] and store.list_runs() == [] and store.alerts.counts()["events"] == 0


def test_event_capture_failure_never_breaks_monitoring():
    svc, _, store, _ = service()
    svc._alert_events = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture bug"))
    run = svc.run_once()
    assert run.persisted and any("alert event capture failed" in e for e in run.errors)
    assert len(store.list_findings()) == 1 and store.alerts.counts()["events"] == 0


def test_idempotency_key_is_unique_at_the_database_level():
    svc, _, store, _ = service()
    svc.run_once()
    (e,) = events(store)
    conn = sqlite3.connect(":memory:")
    from src.monitoring.alerts.store import ALERT_SCHEMA, insert_events

    conn.executescript(ALERT_SCHEMA)
    assert insert_events(conn, [e], ("email",), T0) == 1
    assert insert_events(conn, [e], ("email",), T0) == 0          # reprocessing the same transition
    clone = AlertEvent(**{**e.__dict__, "event_id": "other-id"})  # different id, same logical transition
    assert insert_events(conn, [clone], ("email",), T0) == 0
    assert conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM alert_deliveries").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO alert_events (event_id, idempotency_key, event_type, finding_id, fingerprint, "
                     "rule_id, severity, finding_status, title, run_id, sequence, occurred_at, created_at, payload) "
                     "VALUES ('x', ?, 'NEW','f','p','r','CRITICAL','OPEN','t','r',1,'a','b','{}')",
                     (e.idempotency_key,))


def test_events_and_flags_survive_a_process_restart(tmp_path):
    db = tmp_path / "m.db"
    chk = SevCheck()
    svc, _, store, clock = service(chk, MonitoringStore(db))
    svc.run_once()
    ev_id = events(store)[0].event_id
    reopened = MonitoringStore(db)                            # "restart"
    assert reopened.alerts.is_initialized() and [e.event_id for e in events(reopened)] == [ev_id]
    svc2, _, _, _ = service(chk, reopened, EMAIL_SETTINGS, clock)
    svc2.run_once()
    assert reopened.alerts.counts()["events"] == 1             # still no duplicate NEW after restart


# =====================================================================================
# B. dispatcher, retries, claims, ordering
# =====================================================================================
def test_delivered_only_after_the_transport_confirms_and_never_resent():
    svc, _, store, _ = service(); svc.run_once()
    d, clock = dispatcher(store)
    s = d.dispatch()
    assert (s.attempted, s.delivered) == (1, 1)
    (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.DELIVERED and dv.delivered_at and dv.external_id == "<ok@test>"
    assert d.dispatch().attempted == 0 and len(d._transport.sent) == 1


def test_retryable_failure_keeps_the_same_event_and_backs_off_then_recovers():
    svc, _, store, _ = service(); svc.run_once()
    ev_id = events(store)[0].event_id
    t = FakeTransport([TransportResult.retryable("timeout"), TransportResult.retryable("timeout")])
    d, clock = dispatcher(store, t)
    s = d.dispatch()
    assert (s.attempted, s.retry_scheduled) == (1, 1)
    (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.RETRY_PENDING and dv.attempt_count == 1 and dv.last_error == "timeout"
    assert dv.next_attempt_at == clock.t + timedelta(seconds=60)
    assert d.dispatch().attempted == 0                      # not eligible yet: no flooding of a down transport
    clock.advance(61); d.dispatch()                          # attempt 2 fails: backoff doubles
    (dv,) = store.alerts.list_deliveries()
    assert dv.next_attempt_at == clock.t + timedelta(seconds=120)
    clock.advance(121); d.dispatch()                         # transport recovered
    (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.DELIVERED and dv.event_id == ev_id
    assert store.alerts.counts()["events"] == 1             # retries never create logical events
    outcomes = [a.outcome for a in store.alerts.attempts_for(dv.delivery_id)]
    assert outcomes == ["RETRYABLE_FAILURE", "RETRYABLE_FAILURE", "DELIVERED"]
    assert all(m.event_id == ev_id for m in t.sent)


def test_backoff_is_capped_and_exhaustion_becomes_failed_but_inspectable():
    d, _ = dispatcher(MonitoringStore(":memory:"), backoff_base_seconds=100.0, backoff_max_seconds=250.0)
    assert [d.backoff_seconds(n) for n in (1, 2, 3, 4)] == [100, 200, 250, 250]
    svc, _, store, _ = service(); svc.run_once()
    t = FakeTransport([TransportResult.retryable("down")] * 5)
    d, clock = dispatcher(store, t, max_attempts=3)
    for _ in range(6):
        d.dispatch(); clock.advance(400)
    (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.FAILED and len(t.sent) == 3 and "down" in dv.last_error
    assert store.alerts.counts()["events"] == 1 and store.list_findings()      # finding untouched


def test_permanent_failure_is_not_retried_and_manual_requeue_reuses_the_event():
    svc, _, store, _ = service(); svc.run_once()
    ev_id = events(store)[0].event_id
    t = FakeTransport([TransportResult.permanent("auth failed")])
    d, clock = dispatcher(store, t)
    d.dispatch(); clock.advance(10_000)
    assert d.dispatch().attempted == 0
    (dv,) = store.alerts.list_deliveries(); assert dv.status == DeliveryStatus.FAILED
    assert store.alerts.requeue_failed(clock.t, delivery_id=dv.delivery_id) == 1
    d.dispatch()
    (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.DELIVERED and dv.event_id == ev_id
    assert [a.attempt_no for a in store.alerts.attempts_for(dv.delivery_id)] == [1, 2]   # history kept
    assert store.alerts.counts()["events"] == 1


def test_a_transport_that_raises_is_a_redacted_retryable_failure_not_a_crash():
    svc, _, store, _ = service(); svc.run_once()
    d, _ = dispatcher(store, FakeTransport([RuntimeError("boom hunter2")]), smtp_password="hunter2")
    s = d.dispatch()
    assert s.retry_scheduled == 1
    (dv,) = store.alerts.list_deliveries()
    assert "hunter2" not in (dv.last_error or "")


def test_unusable_transport_config_fails_before_any_attempt_is_consumed():
    svc, _, store, _ = service(); svc.run_once()
    bad = AlertConfig(enabled=True, transport="smtp")            # no host/sender/recipients
    with pytest.raises(AlertConfigError):
        AlertDispatcher(store.alerts, SmtpTransport(bad), bad).dispatch()
    (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.PENDING and dv.attempt_count == 0 and store.alerts.attempts_for(dv.delivery_id) == []


def test_two_dispatchers_cannot_both_claim_one_delivery_and_expired_leases_are_recoverable():
    svc, _, store, _ = service(); svc.run_once()
    clock = Clock()
    first = store.alerts.claim_next("email", clock.t, 300.0)
    assert first is not None and first[0].status == DeliveryStatus.IN_PROGRESS
    assert store.alerts.claim_next("email", clock.t, 300.0) is None       # lease held
    clock.advance(301)                                                       # holder "crashed"
    second = store.alerts.claim_next("email", clock.t, 300.0)
    assert second is not None and second[0].delivery_id == first[0].delivery_id
    outcomes = [a.outcome for a in store.alerts.attempts_for(first[0].delivery_id)]
    assert outcomes == ["ABANDONED", "IN_PROGRESS"]


def test_crash_after_claim_resends_after_lease_expiry_documented_at_least_once(tmp_path):
    db = tmp_path / "m.db"
    svc, _, store, clock = service(store=MonitoringStore(db)); svc.run_once()
    claimed = store.alerts.claim_next("email", clock.t, 300.0)           # process dies right after the claim
    assert claimed is not None
    restarted = MonitoringStore(db)
    t = FakeTransport(); d = AlertDispatcher(restarted.alerts, t, cfg_smtp(), clock=Clock(clock.t + timedelta(seconds=400)))
    assert d.dispatch().delivered == 1 and len(t.sent) == 1


def test_events_for_one_fingerprint_are_delivered_in_order():
    chk = SevCheck("warning")
    svc, _, store, _ = service(chk); svc.run_once()
    chk.mode = "critical"; svc.run_once()
    t = FakeTransport([TransportResult.retryable("temporary")])
    d, clock = dispatcher(store, t)
    d.dispatch()                                                          # NEW fails -> RETRY_PENDING (future)
    assert len(t.sent) == 1 and t.sent[0].subject.split(" ")[1] == "NEW"  # ESCALATED did not overtake it
    clock.advance(61); d.dispatch()
    types = [m.subject.split(" ")[1] for m in t.sent]
    assert types == ["NEW", "NEW", "ESCALATED"]


def test_a_permanently_failed_earlier_event_does_not_block_later_ones():
    chk = SevCheck("warning")
    svc, _, store, _ = service(chk); svc.run_once()
    chk.mode = "critical"; svc.run_once()
    t = FakeTransport([TransportResult.permanent("rejected")])
    d, _ = dispatcher(store, t)
    s = d.dispatch()
    assert (s.failed, s.delivered) == (1, 1)


def test_batch_limit_and_time_budget_bound_one_dispatch_pass():
    store = MonitoringStore(":memory:")
    for sym in "ABCDE":
        svc, _, _, _ = service(SevCheck("critical", sym), store); svc.run_once()
    d, clock = dispatcher(store)
    s = d.dispatch(limit=2)
    assert (s.attempted, s.stopped_reason) == (2, "batch_limit")
    assert store.alerts.counts()["PENDING"] == 3

    class Slow(FakeTransport):
        def send(self, m):
            clock.advance(10)
            return super().send(m)

    d2 = AlertDispatcher(store.alerts, Slow(), cfg_smtp(), clock=clock)
    s2 = d2.dispatch(budget_seconds=15)
    assert s2.stopped_reason == "time_budget" and s2.attempted == 2
    assert d2.dispatch(should_stop=lambda: True).stopped_reason == "stop_requested"


def test_delivery_outcome_cannot_change_monitoring_results():
    svc, chk, store, _ = service()
    run = svc.run_once()
    before = (store.get_run(run.run_id).status, store.list_findings()[0].severity)
    d, _ = dispatcher(store, FakeTransport([TransportResult.ok("<1>")]))
    d.dispatch()
    assert (store.get_run(run.run_id).status, store.list_findings()[0].severity) == before
    assert before[0].value == "CRITICAL"                    # a delivered email does not make it healthy
    d2, _ = dispatcher(store, FakeTransport([TransportResult.permanent("x")])); 
    run2 = svc.run_once()
    assert run2.status.value == "CRITICAL"                  # and a failed one does not either


# =====================================================================================
# C. configuration & message formatting
# =====================================================================================
GOOD_ENV = {"ATP_ALERTS_ENABLED": "true", "ATP_ALERT_SMTP_HOST": "smtp.example.com", "ATP_ALERT_FROM": "atp@example.com",
            "ATP_ALERT_TO": "a@example.com, b@example.com", "ATP_ALERT_SMTP_USERNAME": "user",
            "ATP_ALERT_SMTP_PASSWORD": "s3cretPass!"}


def test_alerting_is_off_unless_explicitly_enabled():
    cfg = AlertConfig.from_env({})
    assert cfg.enabled is False and cfg.problems() == [] and cfg.event_settings().enabled is False
    assert AlertConfig.from_env({"ATP_ALERT_SMTP_HOST": "x"}).enabled is False


def test_valid_environment_parses_and_hides_the_secret():
    cfg = AlertConfig.from_env(GOOD_ENV).validate()
    assert cfg.recipients == ("a@example.com", "b@example.com") and cfg.smtp_security == "starttls"
    assert cfg.event_settings() == AlertEventSettings(True, False, ("email",))
    assert "s3cretPass" not in repr(cfg) and "s3cretPass" not in json.dumps(cfg.describe())


@pytest.mark.parametrize("patch,needle", [
    ({"ATP_ALERT_SMTP_HOST": ""}, "SMTP_HOST"),
    ({"ATP_ALERT_FROM": ""}, "ATP_ALERT_FROM"),
    ({"ATP_ALERT_TO": ""}, "ATP_ALERT_TO"),
    ({"ATP_ALERT_TO": "good@example.com,bad address"}, "invalid recipient"),
    ({"ATP_ALERT_TO": "x@example.com\r\nBcc: evil@example.com"}, "invalid recipient"),
    ({"ATP_ALERT_SMTP_PASSWORD": ""}, "together"),
    ({"ATP_ALERT_SMTP_SECURITY": "plain"}, "SECURITY"),
    ({"ATP_ALERT_SMTP_SECURITY": "none"}, "loopback"),
    ({"ATP_ALERT_TRANSPORT": "carrier-pigeon"}, "TRANSPORT"),
    ({"ATP_ALERT_EXISTING_FINDINGS": "maybe"}, "EXISTING_FINDINGS"),
])
def test_invalid_enabled_configuration_is_reported_clearly(patch, needle):
    cfg = AlertConfig.from_env({**GOOD_ENV, **patch})
    with pytest.raises(AlertConfigError, match=needle):
        cfg.validate()
    with pytest.raises(AlertConfigError):
        cfg.event_settings()


@pytest.mark.parametrize("patch", [{"ATP_ALERT_SMTP_PORT": "99999"}, {"ATP_ALERT_SMTP_PORT": "abc"},
                                   {"ATP_ALERT_MAX_ATTEMPTS": "0"}, {"ATP_ALERT_SMTP_TIMEOUT_SECONDS": "nan"},
                                   {"ATP_ALERTS_ENABLED": "perhaps"}, {"ATP_ALERT_SUBJECT_PREFIX": "a\nb"}])
def test_malformed_numbers_and_flags_are_rejected(patch):
    with pytest.raises(AlertConfigError):
        AlertConfig.from_env({**GOOD_ENV, **patch})


def test_plaintext_is_only_allowed_on_loopback_without_credentials():
    ok = AlertConfig.from_env({**GOOD_ENV, "ATP_ALERT_SMTP_HOST": "127.0.0.1", "ATP_ALERT_SMTP_SECURITY": "none",
                               "ATP_ALERT_SMTP_USERNAME": "", "ATP_ALERT_SMTP_PASSWORD": ""})
    assert ok.problems() == []
    with_creds = AlertConfig.from_env({**GOOD_ENV, "ATP_ALERT_SMTP_HOST": "127.0.0.1", "ATP_ALERT_SMTP_SECURITY": "none"})
    assert any("plaintext" in p for p in with_creds.problems())


def _event(kind=AlertEventType.NEW, title="Gross exposure above ceiling"):
    return AlertEvent(
        event_id="e1", idempotency_key="k", event_type=kind, finding_id="f" * 32, fingerprint="p" * 32,
        rule_id="risk.portfolio_exposure", severity=FindingSeverity.CRITICAL,
        previous_severity=FindingSeverity.WARNING if kind == AlertEventType.ESCALATED else None,
        finding_status=FindingStatus.RESOLVED if kind == AlertEventType.RESOLVED else FindingStatus.OPEN,
        title=title, run_id="r" * 32, sequence=1, occurred_at=T0, created_at=T0,
        snapshot={"category": "risk", "description": "d", "observed_value": 1.6, "expected_value": "< 1.0",
                  "first_seen": T0.isoformat(), "evidence": [{"reference": "portfolio:1", "kind": "p", "summary": "s"}],
                  "source": {"source": "experiment_registry", "experiment_id": 1}, "is_paper_snapshot": True})


def test_message_rendering_is_deterministic_informational_and_link_free():
    cfg = cfg_smtp(subject_prefix="ATP", environment_label="validation")
    m1, m2 = render_message(_event(), cfg), render_message(_event(), cfg)
    assert m1 == m2
    assert m1.subject == "[ATP validation] NEW (CRITICAL): Gross exposure above ceiling"
    for needle in ("f" * 32, "risk.portfolio_exposure", "stored paper-portfolio snapshot, not a live trading session",
                   "Transition recorded (UTC): 2026-10-07T12:00:00Z", "Alert event ID: e1", "does not recommend, place or change"):
        assert needle in m1.body
    assert "http" not in m1.body.lower()
    esc = render_message(_event(AlertEventType.ESCALATED), cfg)
    assert "rose from WARNING to CRITICAL" in esc.body
    res = render_message(_event(AlertEventType.RESOLVED), cfg)
    assert "does not mean the portfolio or market data is otherwise healthy" in res.body


def test_header_injection_through_a_finding_title_is_neutralised():
    m = render_message(_event(title="x\r\nBcc: evil@example.com\r\nSubject: pwn"), cfg_smtp())
    assert "\n" not in m.subject and "\r" not in m.subject


# =====================================================================================
# D. SMTP transport (loopback sink / injected fake client)
# =====================================================================================
def deliver_via(sink, store, **cfg_kw):
    cfg = cfg_smtp(sink.port, **cfg_kw)
    clock = Clock()
    return AlertDispatcher(store.alerts, SmtpTransport(cfg), cfg, clock=clock), clock


def test_full_message_reaches_the_local_sink_with_correct_headers_and_body():
    svc, _, store, _ = service(); svc.run_once()
    with SmtpSink() as sink:
        d, _ = deliver_via(sink, store, recipients=("ops@example.com", "oncall@example.com"))
        assert d.dispatch().delivered == 1
        (m,) = sink.messages
    parsed = email.message_from_string(m.raw)
    assert m.mail_from == "atp@example.com" and m.rcpt_to == ["ops@example.com", "oncall@example.com"]
    assert parsed["Subject"].startswith("[ATP] NEW (CRITICAL)")
    ev = events(store)[0]
    assert parsed["X-ATP-Alert-Event-ID"] == ev.event_id and parsed["Message-ID"].endswith("@atp-alert.local>")
    body = parsed.get_payload(decode=True).decode()
    assert ev.finding_id in body and "stored paper-portfolio snapshot" in body
    (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.DELIVERED and dv.external_id == parsed["Message-ID"]


def test_sink_failures_are_classified_and_a_recovered_server_receives_the_same_event():
    svc, _, store, _ = service(); svc.run_once()
    ev_id = events(store)[0].event_id
    with SmtpSink() as sink:
        d, clock = deliver_via(sink, store)
        sink.fail_next("temp")
        d.dispatch(); (dv,) = store.alerts.list_deliveries()
        assert dv.status == DeliveryStatus.RETRY_PENDING and "451" in dv.last_error or "SMTP 4" in dv.last_error
        clock.advance(61); sink.fail_next("drop")
        d.dispatch(); (dv,) = store.alerts.list_deliveries()
        assert dv.status == DeliveryStatus.RETRY_PENDING and dv.attempt_count == 2
        clock.advance(121)                                   # server recovered
        d.dispatch(); (dv,) = store.alerts.list_deliveries()
        assert dv.status == DeliveryStatus.DELIVERED and len(sink.messages) == 1
    assert dv.event_id == ev_id and store.alerts.counts()["events"] == 1


def test_permanent_server_rejection_marks_failed_without_retrying():
    svc, _, store, _ = service(); svc.run_once()
    with SmtpSink() as sink:
        d, clock = deliver_via(sink, store)
        sink.fail_next("reject_rcpt")
        d.dispatch(); clock.advance(10_000); d.dispatch()
        (dv,) = store.alerts.list_deliveries()
    assert dv.status == DeliveryStatus.FAILED and dv.attempt_count == 1 and sink.messages == []


def test_connection_refused_is_retryable():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()   # nothing listens
    t = SmtpTransport(cfg_smtp(port))
    r = t.send(AlertMessage("s", "b", "e1"))
    assert r.outcome == "retryable"


class FakeSmtp:
    log: list = []
    behaviour: dict = {}

    def __init__(self, host, port, timeout, security, context):
        FakeSmtp.log.append(("connect", host, port, timeout, security, context))
        if FakeSmtp.behaviour.get("connect_error"):
            raise FakeSmtp.behaviour["connect_error"]

    def ehlo(self):
        FakeSmtp.log.append(("ehlo",))

    def starttls(self, context=None):
        FakeSmtp.log.append(("starttls", context))
        if FakeSmtp.behaviour.get("no_starttls"):
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

    def login(self, user, password):
        FakeSmtp.log.append(("login", user))
        if FakeSmtp.behaviour.get("bad_auth"):
            raise smtplib.SMTPAuthenticationError(535, f"bad credentials for {user}/{password}".encode())

    def send_message(self, msg, from_addr=None, to_addrs=None):
        FakeSmtp.log.append(("send",))
        return {}

    def quit(self):
        FakeSmtp.log.append(("quit",))


def fake_transport(**kw):
    FakeSmtp.log, FakeSmtp.behaviour = [], kw.pop("behaviour", {})
    cfg = AlertConfig.from_env({**GOOD_ENV, **kw})
    return SmtpTransport(cfg, smtp_factory=FakeSmtp), cfg


def test_starttls_uses_a_verifying_context_authenticates_and_never_downgrades():
    t, cfg = fake_transport()
    assert t.send(AlertMessage("s", "b", "e1")).delivered
    steps = [x[0] for x in FakeSmtp.log]
    assert steps[:5] == ["connect", "ehlo", "starttls", "ehlo", "login"] and steps[-2:] == ["send", "quit"]
    ctx = [x for x in FakeSmtp.log if x[0] == "starttls"][0][1]
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    t2, _ = fake_transport(behaviour={"no_starttls": True})
    r = t2.send(AlertMessage("s", "b", "e1"))
    assert r.outcome == "permanent"
    assert "login" not in [x[0] for x in FakeSmtp.log] and "send" not in [x[0] for x in FakeSmtp.log]   # no plaintext fallback


def test_implicit_tls_mode_passes_a_verifying_context_to_the_factory():
    t, _ = fake_transport(ATP_ALERT_SMTP_SECURITY="ssl", ATP_ALERT_SMTP_PORT="465")
    assert t.send(AlertMessage("s", "b", "e1")).delivered
    connect = FakeSmtp.log[0]
    assert connect[4] == "ssl" and connect[5].verify_mode == ssl.CERT_REQUIRED and connect[3] == 10.0


def test_authentication_failure_is_permanent_and_the_password_never_leaks():
    t, _ = fake_transport(behaviour={"bad_auth": True})
    r = t.send(AlertMessage("s", "b", "e1"))
    assert r.outcome == "permanent" and "s3cretPass" not in (r.error or "") and "user" not in (r.error or "")


@pytest.mark.parametrize("exc,kind", [
    (socket.timeout("timed out"), "retryable"), (ConnectionResetError("reset s3cretPass!"), "retryable"),
    (smtplib.SMTPServerDisconnected("gone"), "retryable"), (OSError("network unreachable"), "retryable"),
    (ssl.SSLCertVerificationError(1, "certificate verify failed"), "permanent"),
])
def test_transport_exceptions_are_classified_and_redacted(exc, kind):
    t, _ = fake_transport(behaviour={"connect_error": exc})
    r = t.send(AlertMessage("s", "b", "e1"))
    assert r.outcome == kind and "s3cretPass" not in (r.error or "")


def test_the_transport_never_disables_certificate_verification():
    src = (REPO / "src" / "monitoring" / "alerts" / "transports.py").read_text()
    assert "CERT_NONE" not in src and "_create_unverified_context" not in src and "check_hostname = False" not in src


# =====================================================================================
# E. CLI
# =====================================================================================
def cli(argv, env=None, tmp=None, **kw):
    out: list[str] = []
    rc = run_alerts(argv, env=env if env is not None else {}, emit=out.append, **kw)
    return rc, "\n".join(out)


def populated_db(tmp_path, n_symbols=1):
    db = tmp_path / "m.db"
    store = MonitoringStore(db)
    for sym in "ABC"[:n_symbols]:
        svc, _, _, _ = service(SevCheck("critical", sym), store); svc.run_once()
    return db, store


def test_config_command_never_sends_and_never_prints_the_secret():
    rc, out = cli(["config"], env=GOOD_ENV)
    assert rc == EXIT_OK and "valid" in out and "s3cretPass" not in out
    rc, out = cli(["config"], env={})
    assert rc == EXIT_OK and "DISABLED" in out
    rc, out = cli(["config"], env={**GOOD_ENV, "ATP_ALERT_TO": ""})
    assert rc == EXIT_CONFIG and "PROBLEM" in out


def test_preview_renders_without_touching_any_transport(tmp_path):
    def forbidden(cfg):
        raise AssertionError("preview must not build a transport")

    rc, out = cli(["preview", "--sample", "ESCALATED"], transport_factory=forbidden)
    assert rc == EXIT_OK and "SAMPLE" in out and "nothing was sent" in out
    db, store = populated_db(tmp_path)
    ev = events(store)[0]
    rc, out = cli(["--db", str(db), "preview", "--event-id", ev.event_id], transport_factory=forbidden)
    assert rc == EXIT_OK and ev.finding_id in out
    rc, _ = cli(["--db", str(db), "preview", "--latest"], transport_factory=forbidden)
    assert rc == EXIT_OK


def test_status_reports_exit_codes_for_clean_pending_and_failed_states(tmp_path):
    db, store = populated_db(tmp_path)
    rc, out = cli(["--db", str(db), "status"])
    assert rc == EXIT_PENDING and "pending=1" in out
    d, clock = dispatcher(store, FakeTransport([TransportResult.permanent("nope")]))
    d.dispatch()
    rc, out = cli(["--db", str(db), "status", "--json"])
    data = json.loads(out)
    assert rc == EXIT_FAILED and data["counts"]["FAILED"] == 1 and data["deliveries"][0]["last_error"] == "nope"
    store.alerts.requeue_failed(clock.t); d.dispatch()
    rc, out = cli(["--db", str(db), "status"])
    assert rc == EXIT_OK and "delivered=1" in out


def test_status_can_filter_by_finding_and_event_id(tmp_path):
    db, store = populated_db(tmp_path, 2)
    ev = events(store)
    rc, out = cli(["--db", str(db), "status", "--finding-id", ev[0].finding_id, "--json"])
    assert [r["finding_id"] for r in json.loads(out)["deliveries"]] == [ev[0].finding_id]
    rc, out = cli(["--db", str(db), "status", "--event-id", ev[1].event_id, "--json"])
    assert [r["event_id"] for r in json.loads(out)["deliveries"]] == [ev[1].event_id]


def test_dispatch_requires_once_enablement_and_valid_config(tmp_path):
    db, _ = populated_db(tmp_path)
    assert cli(["--db", str(db), "dispatch"], env=GOOD_ENV)[0] == EXIT_CONFIG
    rc, out = cli(["--db", str(db), "dispatch", "--once"], env={})
    assert rc == EXIT_CONFIG and "not enabled" in out
    rc, out = cli(["--db", str(db), "dispatch", "--once"], env={**GOOD_ENV, "ATP_ALERT_TO": ""})
    assert rc == EXIT_CONFIG


def test_dispatch_delivers_through_the_injected_transport_and_reports_the_outbox(tmp_path):
    db, store = populated_db(tmp_path, 2)
    t = FakeTransport()
    rc, out = cli(["--db", str(db), "dispatch", "--once"], env=GOOD_ENV, transport_factory=lambda cfg: t)
    assert rc == EXIT_OK and len(t.sent) == 2 and "delivered=2" in out and "outbox:" in out
    rc, out = cli(["--db", str(db), "dispatch", "--once"], env=GOOD_ENV, transport_factory=lambda cfg: t)
    assert rc == EXIT_OK and len(t.sent) == 2                       # nothing re-sent


def test_dispatch_exit_codes_distinguish_pending_retry_from_permanent_failure(tmp_path):
    db, _ = populated_db(tmp_path)
    t = FakeTransport([TransportResult.retryable("timeout")])
    rc, _ = cli(["--db", str(db), "dispatch", "--once"], env=GOOD_ENV, transport_factory=lambda cfg: t)
    assert rc == EXIT_PENDING
    db2, _ = populated_db(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
    t2 = FakeTransport([TransportResult.permanent("bad")])
    rc, _ = cli(["--db", str(db2), "dispatch", "--once"], env=GOOD_ENV, transport_factory=lambda cfg: t2)
    assert rc in (EXIT_FAILED, EXIT_PENDING)


def test_retry_requeues_failed_deliveries_without_new_events(tmp_path):
    db, store = populated_db(tmp_path)
    d, clock = dispatcher(store, FakeTransport([TransportResult.permanent("bad")])); d.dispatch()
    rc, out = cli(["--db", str(db), "retry", "--all-failed"])
    assert rc == EXIT_OK and "requeued 1" in out
    assert store.alerts.counts()["events"] == 1 and store.alerts.counts()["RETRY_PENDING"] == 1


def test_a_missing_database_is_reported_and_never_created(tmp_path):
    missing = tmp_path / "nope" / "m.db"
    for argv in (["status"], ["retry", "--all-failed"], ["dispatch", "--once"], ["preview", "--latest"]):
        rc, out = cli(["--db", str(missing), *argv], env=GOOD_ENV)
        assert rc == EXIT_CONFIG and "not found" in out
    assert not missing.exists() and not missing.parent.exists()


def test_test_send_is_guarded_by_enablement_allowlist_and_explicit_confirmation():
    sent = []

    class T(FakeTransport):
        def send(self, m):
            sent.append(m)
            return super().send(m)

    env = {**GOOD_ENV, "ATP_ALERT_TEST_RECIPIENTS": "sandbox@example.com"}
    factory = lambda cfg: T()  # noqa: E731
    assert cli(["test-send", "--to", "sandbox@example.com", "--confirm-send"], env={}, transport_factory=factory)[0] == EXIT_CONFIG
    assert cli(["test-send", "--to", "a@example.com", "--confirm-send"], env=env, transport_factory=factory)[0] == EXIT_CONFIG
    rc, out = cli(["test-send", "--to", "sandbox@example.com"], env=env, transport_factory=factory)
    assert rc == EXIT_OK and "DRY RUN" in out and sent == []
    rc, out = cli(["test-send", "--to", "sandbox@example.com", "--confirm-send"], env=env, transport_factory=factory)
    assert rc == EXIT_OK and len(sent) == 1 and sent[0].is_test and sent[0].recipients == ("sandbox@example.com",)
    assert "TEST" in sent[0].subject


def test_test_send_failure_is_reported_as_a_failure():
    env = {**GOOD_ENV, "ATP_ALERT_TEST_RECIPIENTS": "sandbox@example.com"}
    t = FakeTransport([TransportResult.permanent("auth failed")])
    rc, out = cli(["test-send", "--to", "sandbox@example.com", "--confirm-send"], env=env, transport_factory=lambda c: t)
    assert rc == EXIT_FAILED and "FAILED" in out


def test_monitor_once_reports_queued_alerts_without_delivering_them(tmp_path):
    svc, _, store, _ = service()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_monitor(["--once"], service_factory=lambda e, d: svc)
    assert "Alerts: 1 event(s) recorded for this run (queued, NOT delivered)" in buf.getvalue()
    assert store.alerts.counts()["PENDING"] == 1
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_monitor(["--once", "--json"], service_factory=lambda e, d: svc)
    payload = json.loads(buf.getvalue())
    assert payload["alerts"]["queued_events"] == 0 and "run_id" in payload      # second run: nothing new


def test_monitor_json_has_no_alerts_key_when_alerting_is_off():
    svc, _, _, _ = service(settings=AlertEventSettings())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_monitor(["--once", "--json"], service_factory=lambda e, d: svc)
    assert "alerts" not in json.loads(buf.getvalue())


def test_monitor_once_fails_clearly_when_alerting_is_enabled_but_invalid():
    old = {k: os.environ.get(k) for k in ("ATP_ALERTS_ENABLED", "ATP_ALERT_SMTP_HOST")}
    os.environ["ATP_ALERTS_ENABLED"] = "true"; os.environ.pop("ATP_ALERT_SMTP_HOST", None)
    try:
        from src.cli.monitor import _default_service
        with pytest.raises(AlertConfigError):
            _default_service(None, ":memory:")
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


# =====================================================================================
# F. scheduler integration
# =====================================================================================
def test_scheduled_runs_produce_one_event_and_the_dispatch_hook_runs_each_tick_without_repeats():
    from src.monitoring.scheduler import MonitoringScheduler, ScheduleStore, SchedulerConfig

    svc, _, store, clock = service()
    d, dclock = dispatcher(store)
    lines: list[str] = []
    sched = MonitoringScheduler(svc, ScheduleStore(":memory:"), SchedulerConfig("s1", 10, max_runs=3), clock=clock,
                                wait=lambda s: (clock.advance(s), False)[1], emit=lines.append,
                                after_run=lambda run: d.dispatch().one_line())
    sched.run_forever()
    assert store.alerts.counts()["events"] == 1                       # NEW once, not per tick
    assert len(d._transport.sent) == 1
    assert sum("alerts dispatch:" in l for l in lines) == 3


def test_a_failing_dispatch_hook_never_changes_the_scheduler_outcome():
    from src.monitoring.scheduler import MonitoringScheduler, ScheduleStore, SchedulerConfig

    svc, _, store, clock = service()
    lines: list[str] = []

    def boom(run):
        raise RuntimeError("smtp exploded")

    ss = ScheduleStore(":memory:")
    sched = MonitoringScheduler(svc, ss, SchedulerConfig("s1", 10, max_runs=2), clock=clock,
                                wait=lambda s: (clock.advance(s), False)[1], emit=lines.append, after_run=boom)
    summary = sched.run_forever()
    assert summary.completed == 2 and summary.errors == 0 and summary.infrastructure_errors == 0
    assert any("ALERT-DISPATCH-ERROR" in l and "unaffected" in l for l in lines)
    assert all(a.outcome == "COMPLETED" for a in ss.list_attempts("s1"))


def test_scheduler_cli_refuses_dispatch_alerts_when_alerting_is_not_usable(tmp_path):
    from src.cli.monitor_scheduler import run_monitor_scheduler

    out: list[str] = []
    old = os.environ.pop("ATP_ALERTS_ENABLED", None)
    try:
        rc = run_monitor_scheduler(["--experiment-id", "1", "--interval-seconds", "5", "--dispatch-alerts",
                                    "--db", str(tmp_path / "m.db")],
                                   service_factory=lambda *a: object(), emit=out.append, install_signals=False)
    finally:
        if old is not None:
            os.environ["ATP_ALERTS_ENABLED"] = old
    assert rc == 2 and "--dispatch-alerts is not usable" in out[0]
    assert not (tmp_path / "m.db").exists()


# =====================================================================================
# G. security / architecture
# =====================================================================================
FORBIDDEN = ("anthropic", "src.ai", "src.broker", "src.execution", "src.promotion", "src.portfolio", "src.risk",
             "src.strategies", "src.experiments")


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_alert_modules_import_no_llm_broker_execution_portfolio_or_strategy_code():
    files = list((REPO / "src" / "monitoring" / "alerts").glob("*.py")) + [REPO / "src" / "cli" / "alerts.py"]
    assert len(files) >= 8
    for f in files:
        for m in _imports(f):
            assert not m.startswith(FORBIDDEN), (f.name, m)


def test_importing_alert_code_starts_nothing_and_the_cli_loads_it_lazily():
    code = (
        "import sys, socket\n"
        "calls = []\n"
        "orig = socket.socket.connect\n"
        "socket.socket.connect = lambda self, *a, **k: calls.append(a) or orig(self, *a, **k)\n"
        "import src.monitoring.alerts.transports, src.monitoring.alerts.dispatcher, src.monitoring.alerts.sink\n"
        "import src.monitoring.alerts.format, src.monitoring.alerts.config, src.monitoring.alerts.store\n"
        "bad=[x for x in sys.modules if x=='anthropic' or x.startswith('src.ai') or x.startswith('src.promotion')]\n"
        "assert not bad, bad\n"
        "assert not calls, calls\n"
        "import src.cli.__main__ as m\n"
        "assert 'src.cli.alerts' not in sys.modules, 'alerts CLI must be lazy'\n"
        "assert 'alerts' in m.ARGV_COMMANDS\n"
        "assert not calls, calls\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), os.environ.get("PYTHONPATH", "")])})
    assert r.returncode == 0, r.stderr


def test_the_agent_gains_no_alerting_or_sending_capability():
    from src.ai.agents.monitoring.tools import default_monitoring_tool_registry

    svc, _, _, _ = service(settings=AlertEventSettings())
    names = {t["name"] for t in default_monitoring_tool_registry(svc).allowed_definitions(
        __import__("src.ai.agents.monitoring.policy", fromlist=["x"]).MarketMonitoringAgentPolicy())}
    assert names == {"run_monitoring_checks", "list_findings", "get_finding", "get_monitoring_run",
                     "get_paper_portfolio_snapshot", "get_execution_summary", "get_market_data_health"}
    assert not any(w in n for n in names for w in ("alert", "dispatch", "send", "email", "notify"))


def test_alert_creation_and_dispatch_never_modify_findings_or_runs():
    svc, _, store, _ = service(); run = svc.run_once()
    snap = [(f.finding_id, f.status, f.severity, f.occurrence_count) for f in store.list_findings()]
    d, _ = dispatcher(store); d.dispatch()
    assert snap == [(f.finding_id, f.status, f.severity, f.occurrence_count) for f in store.list_findings()]
    assert store.get_run(run.run_id).persisted


def test_no_credential_like_literals_in_alert_sources():
    for f in list((REPO / "src" / "monitoring" / "alerts").glob("*.py")) + [REPO / "src" / "cli" / "alerts.py"]:
        txt = f.read_text()
        assert "sk-ant-" not in txt
        import re

        assert not re.search(r"password\w*\s*=\s*[\"'][^\"']+[\"']", txt, re.I), f.name
