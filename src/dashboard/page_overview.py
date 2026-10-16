"""Overview (Sprint 19): compact, sourced summary of persisted state. Read-only; makes no provider request."""

from __future__ import annotations

import streamlit as st

from src.appsvc import monitoring as mon
from src.appsvc import portfolio as pf
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.dashboard import shell, ui


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Overview")
    if not paths.registry_exists():
        st.info("No experiment registry exists yet (nothing is created implicitly). Create experiments with "
                "`python scripts/run_experiment.py`, then reload. See Setup & System Status.")
    left, right = st.columns(2)
    with left:
        st.subheader("Paper account")
        _account(paths, svc, account)
    with right:
        st.subheader("Monitoring and alerts")
        _monitoring(paths)


def _account(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    if not shell.need_account(account):
        return
    stored = pf.stored_state(paths, account)
    if stored is None:
        st.warning(f"Experiment #{account} has no saved portfolio.")
        return
    c = st.columns(3)
    c[0].metric("Cash (stored)", ui.money(stored.cash))
    c[1].metric("Open positions", str(len(stored.positions)))
    c[2].metric("Realised P&L (gross of fees)", ui.money(stored.realized_pnl))
    st.caption("Source: experiment registry, a STORED snapshot (not live). Positions are at cost. Equity needs an explicit valuation "
               "(Portfolio & Execution); it is not shown here because no price has been requested.")
    info = svc.inspect(account)
    if not info["available"]:
        st.warning("Paper runtime: no paper database yet, so no account is initialised and no cycle has run. "
                   "Use Paper Trading to initialise explicitly.")
        return
    if info["account"] is None:
        st.warning("Paper runtime: this experiment has no initialised paper account.")
        return
    cycles = info["cycles"]
    if cycles:
        last = cycles[0]
        ui.box("OK" if last.status.value in ("ORDER_FILLED", "NO_ACTION", "NO_NEW_BAR", "RISK_REJECTED") else "WARN",
               f"Latest paper cycle: {last.status.value}" + (f" [{last.reason_code}]" if last.reason_code else "")
               + f" -- bar {ui.ts(last.decision_bar)}, started {ui.ts(last.started_at)}")
    else:
        st.info("No paper cycles have been recorded for this account yet.")
    if info["unresolved"]:
        st.error(f"{len(info['unresolved'])} execution intent(s) unresolved -- see Portfolio & Execution.")
    rec = info["latest_reconciliation"]
    if rec is None:
        st.info("No reconciliation recorded yet.")
    else:
        ui.box(rec.status.value, f"Reconciliation: {rec.status.value} ({len(rec.discrepancies)} discrepancy(ies)), {ui.ts(rec.created_at)}")


def _monitoring(paths: AppPaths) -> None:
    if not mon.available(paths):
        st.info("No monitoring database yet: no monitoring run has been recorded. (Monitoring does not run by itself; "
                "it runs after a paper cycle, via `monitor --once`, or the scheduler.)")
        return
    runs = mon.runs(paths, 1)
    if runs:
        r = runs[0]
        ui.box("OK" if r.status.value == "HEALTHY" else "WARN" if r.status.value != "CRITICAL" else "CRITICAL",
               f"Latest monitoring run: {r.status.value} at {ui.ts(r.finished_at)}"
               + (" (synthetic/injected failure)" if mon.is_synthetic(r) else ""))
        unknown = [c.check_id for c in r.checks if c.status.value == "NOT_EVALUABLE"]
        if unknown:
            st.warning("Not evaluable (unknown, not healthy): " + ", ".join(unknown))
    else:
        st.info("No monitoring runs recorded.")
    o = mon.open_findings(paths)
    c = st.columns(2)
    c[0].metric("Open CRITICAL findings", str(len(o["CRITICAL"])))
    c[1].metric("Open WARNING findings", str(len(o["WARNING"])))
    counts = mon.alert_counts(paths)
    if counts is None:
        st.info("Alert delivery state is unavailable.")
    else:
        st.caption(f"Alert deliveries -- pending {counts.get('PENDING', 0) + counts.get('RETRY_PENDING', 0)}, "
                   f"failed {counts.get('FAILED', 0)}, delivered {counts.get('DELIVERED', 0)}. "
                   "Delivered means a mail server accepted the message; delivery status is separate from finding severity.")
