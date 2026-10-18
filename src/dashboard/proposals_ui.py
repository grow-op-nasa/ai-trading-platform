"""Approval queue UI (Sprint 20), shared by Home (summary) and Paper Trading (review + decide).

Approve / Reject act on the EXACT proposal id shown, through `PaperService` (server-side revalidation, expiry,
invalidation, idempotency). Nothing is decided by a rerun, a refresh, a navigation or an elapsed timer: only a button
click inside this module's `if st.button` branches. There is no AI, notification or auto-approve path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from src.appsvc import explain
from src.appsvc.paper import OUTCOME_MEANING, PaperService
from src.dashboard import ui

_BOX = {"PENDING": "WARN", "APPROVING": "WARN", "EXECUTED": "OK", "REJECTED": "INFO", "EXPIRED": "INFO", "INVALIDATED": "WARN"}


def _remaining(expires_at: str) -> str:
    left = (datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).total_seconds()
    return "expired" if left <= 0 else f"{int(left // 60)} min {int(left % 60)} s left"


def pending_summary(rows: list[dict]) -> None:
    if not rows:
        st.info("No proposed trades are waiting for approval.")
        return
    ui.table([{"Symbol": r["proposal"].symbol, "Side": r["proposal"].side, "Quantity": f"{r['proposal'].approved_quantity:g}",
               "Reference price": f"{r['proposal'].reference_price:g}", "Expires": _remaining(r["proposal"].expires_at),
               "Proposal": r["proposal"].proposal_id[:12]} for r in rows], empty="")


def render_queue(svc: PaperService, account: int, *, monitoring: bool = True, key: str = "q") -> None:
    """Pending proposals with review details and Approve / Reject. Expired rows are shown as expired and cannot be approved."""
    rows = svc.proposals(account, pending_only=True)
    if not rows:
        st.info("No proposed trades are waiting for approval. A proposal appears here when a cycle finds a risk-approved action.")
        return
    for r in rows:
        p = r["proposal"]
        pid = p.proposal_id
        with st.container(border=True):
            st.markdown(f"**{p.side} {p.approved_quantity:g} {p.symbol}** (paper) -- proposal `{pid[:12]}`")
            ui.table([{"Item": k, "Value": v} for k, v in [
                ("Strategy", p.strategy_name), ("Decision bar", ui.ts(p.decision_bar)), ("Reference price", f"{p.reference_price:g}"),
                ("Stop price", "none" if p.stop_price is None else f"{p.stop_price:g}"), ("Created", ui.ts(p.created_at)),
                ("Expires", f"{ui.ts(p.expires_at)} ({_remaining(p.expires_at)})")]], empty="")
            with st.expander("Signal and risk decision"):
                ui.json_block({"signal": p.signal, "risk_decision": p.risk_decision})
            st.caption(explain.proposal("PENDING").meaning + " Approval re-checks the account, data and configuration at that moment; "
                       "it can still be refused. Expiry, rejection and silence never place an order.")
            ok = st.checkbox("I confirm: approve this exact paper trade", key=f"{key}_ok_{pid}")
            c1, c2 = st.columns(2)
            if c1.button("Approve proposal", key=f"{key}_approve_{pid}"):
                if not ok:
                    st.warning("Tick the confirmation to approve. Nothing was submitted.")
                else:
                    with st.spinner("Re-validating and submitting the paper order..."):
                        st.session_state["last_decision"] = ("approve", pid, svc.approve_proposal(pid, account, monitoring=monitoring))
                    st.cache_data.clear()
                    st.rerun()
            if c2.button("Reject proposal", key=f"{key}_reject_{pid}"):
                st.session_state["last_decision"] = ("reject", pid, svc.reject_proposal(pid, account))
                st.cache_data.clear()
                st.rerun()


def show_decision() -> None:
    """The result of the last Approve/Reject click, from the persisted record the service returned."""
    last = st.session_state.get("last_decision")
    if not last:
        return
    kind, pid, out = last
    if out.refused:
        ui.box("WARN", f"Not submitted ({out.proposal_error or 'refused'}): {out.refused}. No order was placed by this action.")
        return
    if out.error:
        st.error(f"The service raised an error: {out.error}. Check the Activity page; this is not a success.")
        return
    rec = out.record
    status = rec.status.value
    ui.box({"ORDER_FILLED": "OK", "PROPOSAL_REJECTED": "INFO"}.get(status, "WARN"),
           f"Proposal {pid[:12]} -- {status}" + (" (replayed: nothing was submitted again)" if rec.replayed else ""))
    st.write(OUTCOME_MEANING.get(status, explain.cycle(status, rec.reason_code).meaning))
