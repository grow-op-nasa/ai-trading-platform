"""Monitoring & Alerts (Sprint 19): persisted monitoring runs, findings and alert deliveries.

Viewing is read-only. The two operator actions call the existing alert subsystem once, behind an explicit confirmation:
dispatch (may send email through the configured server) and requeue (moves FAILED deliveries to retry; sends nothing).
Neither runs monitoring or a paper cycle. No AI agent is invoked here.
"""

from __future__ import annotations

import streamlit as st

from src.appsvc import monitoring as mon
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.system import collect_status
from src.dashboard import shell, ui
from src.appsvc.monitoring import AlertConfigError


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Monitoring & Alerts")
    if not mon.available(paths):
        st.info("No monitoring database exists yet, so there are no runs, findings or alerts. Monitoring does not run by itself: it runs after a paper "
                "cycle (if selected), with `python -m src.cli monitor --once`, or under `monitor-scheduler`.")
        _scheduler(paths)
        return
    t_runs, t_find, t_alerts, t_sched = st.tabs(["Runs & checks", "Findings", "Alert delivery", "Scheduler"])
    with t_runs:
        _runs(paths)
    with t_find:
        _findings(paths)
    with t_alerts:
        _alerts(paths)
    with t_sched:
        _scheduler(paths)


def _runs(paths: AppPaths) -> None:
    runs = mon.runs(paths, 30)
    ui.table([{"Run": r.run_id[:12], "Status": r.status.value, "Finished": ui.ts(r.finished_at), "Findings": len(r.finding_ids),
               "Resolved": len(r.resolved_finding_ids), "Errors": len(r.errors), "Synthetic/injected": "yes" if mon.is_synthetic(r) else "not indicated"}
              for r in runs], empty="No monitoring runs recorded.")
    if not runs:
        return
    rid = st.selectbox("Run detail", [r.run_id for r in runs], index=None, placeholder="Select a run", key="mon_run",
                       format_func=lambda i: f"{i[:12]}  {next(r.status.value for r in runs if r.run_id == i)}")
    if rid is None:
        return
    r = mon.get_run(paths, rid)
    if r is None:
        st.warning("Run not found.")
        return
    ui.box("OK" if r.status.value == "HEALTHY" else r.status.value, f"Run status: {r.status.value}. Persisted: {r.persisted}.")
    if mon.is_synthetic(r):
        st.warning("This run's own record indicates a synthetic or injected failure; it is not a real outage.")
    ui.table([{"Check": c.check_id, "Status": c.status.value, "Why not evaluated": c.reason or "", "Summary": c.summary[:120]} for c in r.checks], empty="")
    if any(c.status.value == "NOT_EVALUABLE" for c in r.checks):
        st.warning("NOT_EVALUABLE checks are UNKNOWN. They are not healthy and they cannot resolve findings.")
    for e in r.errors:
        st.error(e[:300])
    st.caption("Source: " + str(r.source))


def _findings(paths: AppPaths) -> None:
    c1, c2 = st.columns(2)
    status = c1.selectbox("Status", ["(all)", "OPEN", "RESOLVED"], key="mon_f_status")
    sev = c2.selectbox("Severity", ["(all)", "CRITICAL", "WARNING"], key="mon_f_sev")
    fs = mon.findings(paths, status=None if status == "(all)" else status, severity=None if sev == "(all)" else sev)
    ui.table([{"Finding": f.finding_id[:12], "Severity": f.severity.value, "Status": f.status.value, "Rule": f.rule_id, "Title": f.title,
               "First seen": ui.ts(f.first_seen), "Last seen": ui.ts(f.last_seen), "Occurrences": f.occurrence_count} for f in fs],
             empty="No findings match. (No findings means none were recorded -- check the run's check statuses for unknowns.)")
    if not fs:
        return
    fid = st.selectbox("Finding detail", [f.finding_id for f in fs], index=None, placeholder="Select a finding", key="mon_finding",
                       format_func=lambda i: i[:12] + "  " + next(f.title for f in fs if f.finding_id == i))
    if fid is None:
        return
    f = next(x for x in fs if x.finding_id == fid)
    st.write(f"**{f.title}** ({f.severity.value}, {f.status.value})")
    st.write(f.description)
    ui.table([{"Field": k, "Value": str(v)} for k, v in [("Finding id", f.finding_id), ("Fingerprint", f.fingerprint), ("Observed", f.observed_value),
              ("Expected", f.expected_value), ("Entities", f.entities), ("Rule version", f.rule_version), ("Last run", f.last_run_id),
              ("Resolved at", ui.ts(f.resolved_at))]], empty="")
    ui.table([{"Evidence": e.reference, "Kind": e.kind, "Summary": e.summary} for e in f.evidence], empty="No evidence references stored.")


def _alerts(paths: AppPaths) -> None:
    counts = mon.alert_counts(paths)
    if counts is None:
        st.info("Alert state is unavailable (no alert tables in this database).")
        return
    c = st.columns(5)
    for col, k in zip(c, ("PENDING", "RETRY_PENDING", "IN_PROGRESS", "FAILED", "DELIVERED")):
        col.metric(k.replace("_", " ").title(), str(counts.get(k, 0)))
    st.caption("DELIVERED means a mail server accepted the message, not that a person read it. Delivery status is independent of finding severity.")
    pick = st.selectbox("Delivery status", ["(all)", "PENDING", "RETRY_PENDING", "IN_PROGRESS", "FAILED", "DELIVERED"], key="mon_d_status")
    ds = mon.deliveries(paths, None if pick == "(all)" else pick)
    ui.table([{"Delivery": d.delivery_id[:12], "Event": d.event_id[:12], "Channel": d.channel, "Status": d.status.value, "Attempts": d.attempt_count,
               "Next attempt": ui.ts(d.next_attempt_at), "Last error": (d.last_error or "")[:100], "Delivered": ui.ts(d.delivered_at)} for d in ds],
             empty="No deliveries match.")
    if ds:
        did = st.selectbox("Attempt history", [d.delivery_id for d in ds], index=None, placeholder="Select a delivery", key="mon_attempt",
                           format_func=lambda i: i[:12])
        if did:
            ui.table([{"Attempt": a.attempt_no, "Outcome": a.outcome, "Started": ui.ts(a.started_at), "Error": (a.error or "")[:100],
                       "Next retry": ui.ts(a.next_retry_at)} for a in mon.attempts(paths, did)], empty="No attempts recorded.")
    ui.table([{"Event": e.event_id[:12], "Type": e.event_type.value, "Severity": e.severity.value, "Title": e.title, "Occurred": ui.ts(e.occurred_at)}
              for e in mon.alert_events(paths, 30)], empty="No alert events recorded.")
    st.markdown("**Operator actions** (explicit; neither runs monitoring or a paper cycle)")
    with st.form("alert_actions"):
        ok = st.checkbox("I understand dispatching may send real email through the configured server")
        d = st.form_submit_button("Dispatch pending alerts now")
        r = st.form_submit_button("Requeue FAILED deliveries for retry (sends nothing)")
    if d:
        if not ok:
            st.warning("Tick the confirmation to dispatch. Nothing was sent.")
        else:
            try:
                s = mon.dispatch_pending(paths)
                st.success(s.one_line())
            except AlertConfigError as exc:
                st.error(f"Not dispatched: {exc}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Dispatch error: {type(exc).__name__}: {str(exc)[:200]}")
    if r:
        n = mon.retry_failed(paths)
        st.info(f"Requeued {n} failed delivery(ies); original event ids preserved. They are delivered only when you dispatch.")


def _scheduler(paths: AppPaths) -> None:
    st.subheader("Scheduler")
    sched = [i for i in collect_status(paths) if i.name.startswith("Monitoring scheduler")]
    for i in sched:
        ui.box(i.state, f"{i.name}: {i.detail}")
    st.caption("The app never starts or stops a scheduler. Start one in a terminal with `python -m src.cli monitor-scheduler ...`; it stops with the terminal.")
    rows = mon.scheduler_attempts(paths)
    ui.table([{"Schedule": a.schedule_id, "Started": ui.ts(a.started_at), "Outcome": a.outcome, "Seconds": round(a.duration_seconds, 2)} for a in rows],
             empty="No scheduler attempts recorded.")
