"""Portfolio & Execution (Sprint 19): stored state, EXPLICIT valuation, paper intents/fills/fees, reconciliation."""

from __future__ import annotations

import streamlit as st

from src.appsvc import portfolio as pf
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.dashboard import shell, ui
from src.dashboard.formatting import format_metric


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Portfolio & Execution")
    if not shell.need_account(account):
        return
    stored = pf.stored_state(paths, account)
    if stored is None:
        st.warning(f"Experiment #{account} has no saved portfolio.")
        return
    _stored(stored)
    _valuation(paths, account)
    _execution(svc, account)


def _stored(stored: pf.StoredPortfolio) -> None:
    st.subheader("Stored portfolio (experiment registry snapshot)")
    c = st.columns(3)
    c[0].metric("Cash", ui.money(stored.cash))
    c[1].metric("Realised P&L (gross of fees)", ui.money(stored.realized_pnl))
    c[2].metric("Closed positions", str(stored.closed_count))
    st.caption("A stored snapshot, not live state. Realised P&L is computed per position from entry and exit price and excludes fees "
               "(fees are debited from cash). Positions below are at cost; no market price is attached.")
    ui.table([{"Symbol": p.symbol, "Side": p.side, "Quantity": ui.qty(p.quantity), "Entry price": ui.money(p.entry_price),
               "Entry time": ui.ts(p.entry_timestamp), "Cost basis": ui.money(p.cost_basis),
               "Stop": ui.money(p.stop_price) if p.stop_price is not None else "none"} for p in stored.positions],
             empty="No open positions in the stored portfolio.")


def _valuation(paths: AppPaths, account: int) -> None:
    st.subheader("Valuation at the latest market price")
    key = f"valuation::{account}"
    if st.button("Value open positions now (requests one price per open symbol)", key="btn_value"):
        with st.spinner("Valuing..."):
            try:
                snap, at = pf.value_now(paths, account)
                st.session_state[key] = (snap, at, None)
            except Exception as exc:  # noqa: BLE001
                st.session_state[key] = (None, "", f"{type(exc).__name__}: {str(exc)[:200]}")
    res = st.session_state.get(key)
    if res is None:
        st.info("Not valued. Equity is unknown until you request a valuation (this page never requests prices on its own).")
        return
    snap, at, err = res
    if err:
        st.error(f"Valuation failed: {err}. Equity is unknown.")
        return
    c = st.columns(4)
    c[0].metric("Cash", ui.money(snap.cash))
    c[1].metric("Equity", format_metric(snap.equity, kind="dollar"))
    c[2].metric("Unrealised P&L", format_metric(snap.unrealized_pnl, kind="dollar"))
    c[3].metric("Market value", format_metric(snap.market_value, kind="dollar"))
    st.caption(f"Valued at {at} UTC via the market-data service, a one-time snapshot of the moment you clicked (it goes stale).")
    for w in snap.warnings:
        st.warning(w)
    ui.table([{"Symbol": p.symbol, "Quantity": ui.qty(p.quantity), "Entry": ui.money(p.entry_price),
               "Market price": ui.money(p.market_price) if p.price_available else "unavailable",
               "Unrealised P&L": ui.money(p.unrealized_pnl) if p.price_available else "unknown"} for p in snap.positions],
             empty="No open positions, so no price was requested.")


def _execution(svc: PaperService, account: int) -> None:
    st.subheader("Paper execution records")
    info = svc.inspect(account, limit=200)
    if not info["available"]:
        st.info(info["reason"] + ": no intents, fills or reconciliations exist.")
        return
    if info["unresolved"]:
        st.error(f"{len(info['unresolved'])} unresolved execution intent(s): " + ", ".join(i.client_order_id for i in info["unresolved"])
                 + ". Recovery happens on the next run; nothing is resubmitted blindly.")
    states = sorted({i.state.value for i in info["intents"]})
    pick = st.multiselect("Filter intents by state", states, default=states, key="pf_state")
    ui.table([{"Client order": i.client_order_id, "State": i.state.value, "Symbol": i.symbol, "Side": i.side,
               "Quantity": ui.qty(i.quantity), "Approved qty": ui.qty(i.approved_quantity), "Reference price": ui.money(i.reference_price),
               "Decision bar": ui.ts(i.decision_bar), "Reason": i.reason or ""} for i in info["intents"] if i.state.value in pick],
             empty="No execution intents recorded (an intent exists only when a trade was proposed and approved).")
    fills = info["fills"]
    syms = sorted({f.symbol for f in fills})
    fsel = st.multiselect("Filter fills by symbol", syms, default=syms, key="pf_sym")
    shown = [f for f in fills if f.symbol in fsel]
    ui.table([{"Client order": f.client_order_id, "Symbol": f.symbol, "Side": f.side, "Quantity": ui.qty(f.quantity),
               "Fill price (simulated)": ui.money(f.fill_price), "Reference": ui.money(f.reference_price), "Slippage": ui.money(f.slippage_amount),
               "Fee": ui.money(f.fee), "Cash delta": ui.money(f.cash_delta), "Filled at": ui.ts(f.fill_timestamp)} for f in shown],
             empty="No paper fills recorded.")
    if shown:
        st.caption(f"Sum of recorded fees in the rows shown: {ui.money(sum(f.fee for f in shown))}. Fills are simulated by the paper broker; "
                   "a fill is not evidence of a profitable trade.")
    st.markdown("**Reconciliation**")
    recs = info["reconciliations"]
    if not recs:
        st.info("No reconciliation recorded yet.")
    for r in recs[:10]:
        with st.expander(f"#{r.recon_id} {r.status.value} -- {ui.ts(r.created_at)} -- {len(r.discrepancies)} discrepancy(ies)"):
            ui.box(r.status.value, f"Status {r.status.value}. Reconciliation observes and records; it never corrects the portfolio.")
            if r.discrepancies:
                ui.json_block(r.discrepancies)
            ui.json_block(r.summary)
