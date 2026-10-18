"""Home (Sprint 20): the default landing page. READ-ONLY -- no preflight, no market data, no account creation, no
scheduler, no email, no AI. It summarises stored state, shows a guided first-run checklist (required vs optional) and
ONE contextual primary action that only navigates."""

from __future__ import annotations

import streamlit as st

from src.appsvc import explain, freshness, home
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.dashboard import proposals_ui, shell, ui

_ITEM_ICON = {"DONE": "OK", "TODO": "TODO", "PROBLEM": "PROBLEM", "OPTIONAL_ON": "on", "OPTIONAL_OFF": "off (optional)"}


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Home")
    h = home.build(paths, svc, account, st.session_state.get("_acct_res"))
    st.warning("PAPER ONLY -- simulated fills. This page reads stored records; it requests no market data and starts nothing.")

    p = h.primary
    ui.box({"FAIL": "FAIL", "WARN": "WARN"}.get(p.kind, "INFO"), f"**Next step:** {p.label} -- {p.reason}")
    if p.page != "Home":
        st.button(p.label, key="home_primary", on_click=shell.goto, args=(p.page,), type="primary")

    left, right = st.columns(2)
    with left:
        st.subheader("Paper account")
        _account(h)
    with right:
        st.subheader("Needs attention")
        _attention(h)
    st.subheader("Approval queue")
    if h.account is None:
        st.info("Select a paper account to see its proposals.")
    else:
        proposals_ui.pending_summary(list(h.pending_proposals))
        if h.pending_proposals:
            st.button("Review in Paper Trading", key="home_review", on_click=shell.goto, args=("Paper Trading",))
    st.subheader("Monitoring and alerts")
    _monitoring(h)
    st.subheader("Setup checklist")
    _checklist(h)


def _account(h: home.HomeState) -> None:
    if h.account is None:
        ui.box("WARN", h.resolution.message or "No paper account is selected. Choose one in the sidebar.")
        return
    st.markdown(f"**{h.account_label}** -- PAPER account")
    if h.cash is None:
        st.warning("No saved portfolio for this experiment.")
        return
    c = st.columns(3)
    c[0].metric("Cash (stored)", ui.money(h.cash))
    c[1].metric("Open positions", str(h.open_positions))
    c[2].metric("Realised P&L (gross of fees)", ui.money(h.realized_pnl))
    st.caption(h.stored_snapshot_note + " Equity needs an explicit valuation on Portfolio & Execution.")
    if h.initialised is False:
        st.warning("The paper account is not initialised yet. Nothing has run.")
        return
    if h.latest_cycle is None:
        st.info("No paper cycle has been recorded for this account yet.")
        return
    last, ex = h.latest_cycle, h.latest_cycle_explanation
    ui.box(ex.kind, f"Latest cycle: {last.status.value}" + (f" [{last.reason_code}]" if last.reason_code else "")
           + f" -- bar {ui.ts(last.decision_bar)}")
    st.write(ex.meaning)
    if ex.next_step:
        st.caption("Next: " + ex.next_step)
    f = freshness.for_cycle(last.decision_bar)
    ui.box({"CURRENT": "OK", "NEWER_SESSION": "WARN"}.get(f.state, "INFO"), "Data freshness (calendar, last recorded): " + f.message)


def _attention(h: home.HomeState) -> None:
    items = 0
    if h.unresolved_intents:
        st.error(f"{h.unresolved_intents} execution intent(s) unresolved -- Portfolio & Execution.")
        items += 1
    rec = h.reconciliation
    if rec is not None:
        ex = explain.reconciliation(rec.status.value)
        if rec.status.value != "MATCH":
            ui.box(ex.kind, f"Reconciliation {rec.status.value}: {ex.meaning} {ex.next_step}")
            items += 1
    if h.pending_proposals:
        st.warning(f"{len(h.pending_proposals)} proposed trade(s) await your decision.")
        items += 1
    if h.open_critical:
        st.error(f"{h.open_critical} open CRITICAL monitoring finding(s).")
        items += 1
    if h.open_warning:
        st.warning(f"{h.open_warning} open WARNING monitoring finding(s).")
        items += 1
    if h.not_evaluable:
        st.warning("Not evaluable (unknown, not healthy): " + ", ".join(h.not_evaluable))
        items += 1
    if not items:
        st.success("Nothing needs attention in the stored records." if h.account is not None else "Select an account to see its status.")


def _monitoring(h: home.HomeState) -> None:
    if not h.monitoring_available:
        st.info("No monitoring run has been recorded. Monitoring is optional and runs after a paper cycle, via `monitor --once`, or the scheduler.")
        return
    if h.latest_monitoring_status:
        ui.box("OK" if h.latest_monitoring_status == "HEALTHY" else "CRITICAL" if h.latest_monitoring_status == "CRITICAL" else "WARN",
               f"Latest monitoring run: {h.latest_monitoring_status}")
    c = st.columns(2)
    c[0].metric("Open CRITICAL findings", str(h.open_critical))
    c[1].metric("Open WARNING findings", str(h.open_warning))
    if h.alert_counts is None:
        st.caption("Alert delivery state is unavailable.")
    else:
        n = h.alert_counts
        st.caption(f"Alert deliveries -- pending {n.get('PENDING', 0) + n.get('RETRY_PENDING', 0)}, failed {n.get('FAILED', 0)}, "
                   f"delivered {n.get('DELIVERED', 0)}. Delivered means a mail server accepted the message; it is separate from severity.")


def _checklist(h: home.HomeState) -> None:
    req = [c for c in h.checklist if c.required]
    opt = [c for c in h.checklist if not c.required]
    done = sum(c.state == "DONE" for c in req)
    st.caption(f"Required: {done} of {len(req)} complete. Optional items never make the app unhealthy.")
    ui.table([{"Required": c.label, "Status": _ITEM_ICON.get(c.state, c.state), "Detail": c.detail, "Next step": c.next_step} for c in req], empty="")
    ui.table([{"Optional": c.label, "Status": _ITEM_ICON.get(c.state, c.state), "Detail": c.detail, "Next step": c.next_step} for c in opt], empty="")
