"""Opportunity Scanner (Sprint 21, ADR-0058): run ONE registered strategy over a watchlist of daily bars.

Opening or refreshing this page runs nothing and writes nothing. Watchlist edits and scans are explicit button actions.
A scan never places an order or creates an intent, cycle, proposal, fill or fee, and never changes a portfolio; the only
bridge to trading is the explicit "Review in paper workflow" button, which just prefills the Paper Trading page (whose fresh
preflight, risk check and human approval are unchanged).
"""

from __future__ import annotations

import json
import uuid

import streamlit as st

from src.appsvc import instruments, strategies
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.scanner import (MAX_WATCHLIST_SYMBOLS, ResultStatus, ScanSetupError, ScanStatus, WatchlistError, explain_result, trace_of)
from src.dashboard import shell, ui

_STATUS_LABEL = {
    ResultStatus.SIGNAL_FOUND: "Signal found", ResultStatus.NO_SIGNAL: "No signal", ResultStatus.RISK_BLOCKED: "Blocked by risk",
    ResultStatus.DATA_UNAVAILABLE: "Data unavailable", ResultStatus.NOT_EVALUABLE: "Not evaluable", ResultStatus.ERROR: "Error",
}
_SCAN_LABEL = {ScanStatus.RUNNING: "Running", ScanStatus.COMPLETED: "Completed", ScanStatus.PARTIAL: "Partial (some symbols could not be evaluated)",
               ScanStatus.FAILED: "Failed (nothing could be evaluated)", ScanStatus.INTERRUPTED: "Interrupted (stored results are real but incomplete)"}


def _handoff(h: dict) -> None:
    """on_click: prefill the Paper Trading page and navigate. Creates nothing; runs nothing."""
    st.session_state["paper_strategy"] = h["strategy"]
    sym = h["symbol"]
    if sym in instruments.symbols():
        st.session_state["paper_symbol_choice"] = sym
    else:
        st.session_state["paper_symbol_choice"] = instruments.CUSTOM
        st.session_state["_custom_kept"] = sym
        st.session_state["paper_symbol_custom"] = sym
    st.session_state["paper_params"] = json.dumps(h["params"])
    if h.get("account") is not None:
        st.session_state["paper_account"] = h["account"]
    st.session_state["_scanner_handoff"] = h
    shell.goto("Paper Trading")


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Opportunity Scanner")
    sc = shell.scanner_service(paths)
    st.warning("READ-ONLY. A scan only evaluates a strategy on completed daily bars. It places no orders and creates no paper records. "
               "Missing or invalid data is reported as such, never as 'no signal'.")
    wl = _watchlists(sc)
    _scan_form(paths, sc, wl, account)
    _results(sc)
    _history(sc)


# ---------------------------------------------------------------------------------------------------- watchlists
def _watchlists(sc):
    st.subheader("1. Watchlist")
    lists = sc.watchlists()
    if not lists:
        st.info("You have no watchlists yet. Create one below, or start from the documented starter list "
                "(QQQ, SPY, IWM, DIA, AAPL, MSFT, NVDA, AMD).")
        if st.button("Create the US Liquid Starter watchlist", key="scanner_seed"):
            made = sc.ensure_starter()
            st.success("Created 'US Liquid Starter'." if made else "No starter list was created (one was already created earlier).")
            st.rerun()
    names = {w.watchlist_id: w for w in lists}
    chosen = None
    if lists:
        chosen_id = st.selectbox("Watchlist", list(names), index=None, placeholder="Select a watchlist", key="scanner_wl",
                                 format_func=lambda i: f"{names[i].name}  ({len(names[i].symbols)} symbols)")
        chosen = names.get(chosen_id)
        if chosen is not None:
            st.write(", ".join(chosen.symbols))
    with st.expander("Create a new watchlist"):
        with st.form("scanner_create"):
            name = st.text_input("Name", key="scanner_new_name")
            syms = st.text_area(f"Symbols (separate with commas, spaces or new lines; up to {MAX_WATCHLIST_SYMBOLS})", key="scanner_new_syms")
            if st.form_submit_button("Create watchlist"):
                try:
                    w = sc.create_watchlist(name, syms)
                    st.success(f"Created '{w.name}' with {len(w.symbols)} symbols.")
                    st.rerun()
                except WatchlistError as exc:
                    st.error(str(exc))
    if chosen is not None:
        with st.expander("Rename, edit or delete the selected watchlist"):
            with st.form("scanner_edit"):
                new_name = st.text_input("Name", value=chosen.name, key=f"scanner_edit_name_{chosen.watchlist_id}")
                new_syms = st.text_area("Symbols", value=", ".join(chosen.symbols), key=f"scanner_edit_syms_{chosen.watchlist_id}")
                if st.form_submit_button("Save changes"):
                    try:
                        w = sc.update_watchlist(chosen.watchlist_id, name=new_name, symbols=new_syms)
                        st.success(f"Saved '{w.name}' ({len(w.symbols)} symbols). Past scans keep the symbols they used.")
                        st.rerun()
                    except WatchlistError as exc:
                        st.error(str(exc))
            ok = st.checkbox("I understand this deletes the watchlist (scan history is kept)", key=f"scanner_del_ok_{chosen.watchlist_id}")
            if st.button("Delete watchlist", key="scanner_delete"):
                if ok:
                    sc.delete_watchlist(chosen.watchlist_id)
                    st.session_state.pop("scanner_wl", None)
                    st.rerun()
                else:
                    st.warning("Tick the confirmation first. Nothing was deleted.")
    return chosen


# ---------------------------------------------------------------------------------------------------- scan form
def _scan_form(paths, sc, wl, account) -> None:
    st.subheader("2. Scan")
    names = strategies.eligible_names(paths)
    if not names:
        st.error("No strategies are registered in the production registry, so nothing can be scanned.")
        return
    strategy = st.selectbox("Strategy (production registry)", names, index=None, placeholder="Select a strategy", key="scanner_strategy")
    params_text = st.text_area("Strategy parameters (JSON object; empty uses the strategy defaults)", value="{}", key="scanner_params")
    assess = False
    if account is not None:
        assess = st.checkbox(f"Also run the read-only risk check against account #{account}'s saved portfolio", key="scanner_assess",
                             help="Each candidate is assessed on its own, not as a basket. Without this, signals show 'risk not assessed'.")
    else:
        st.caption("No paper account selected: signals will show 'risk not assessed'. The paper workflow's preflight decides.")
    st.caption("Daily bars only. Each symbol needs enough complete bars for the strategy's parameters, and the latest completed session "
               "must be present; otherwise it is reported as Not evaluable.")
    missing = [n for n, v in (("a watchlist", wl), ("a strategy", strategy)) if v is None]
    if missing:
        st.info("Select " + " and ".join(missing) + " to continue.")
        return
    try:
        params = json.loads(params_text or "{}")
        if not isinstance(params, dict):
            raise ValueError("parameters must be a JSON object")
    except ValueError as exc:
        st.error(f"Parameters are not valid: {exc}")
        return
    token = st.session_state.setdefault("_scanner_token", uuid.uuid4().hex)   # one token per form instance -> double-click safe
    if st.button(f"Scan {len(wl.symbols)} symbols", key="scanner_run", type="primary"):
        bar = st.progress(0.0, text="Starting scan")

        def progress(done: int, total: int, sym: str) -> None:
            bar.progress(done / total, text=f"Evaluated {done} of {total} (latest: {sym})")

        try:
            rec, created = sc.scan(wl.watchlist_id, strategy, params, account=account if assess else None, assess_risk=assess,
                                   request_token=token, progress=progress)
        except ScanSetupError as exc:
            bar.empty()
            st.error(f"The scan could not start: {exc}")
            return
        st.session_state["scanner_scan_id"] = rec.scan_id
        st.session_state.pop("_scanner_token", None)
        if not created:
            st.info("This scan is already running or was already submitted; showing it instead of starting a second one.")
        st.rerun()


# ---------------------------------------------------------------------------------------------------- results
def _results(sc) -> None:
    st.subheader("3. Results")
    sid = st.session_state.get("scanner_scan_id")
    view = sc.view(sid) if sid else None
    if view is None:
        st.info("No scan selected. Run a scan above or open one from history.")
        return
    s = view.scan
    kind = {ScanStatus.COMPLETED: "OK", ScanStatus.PARTIAL: "WARN", ScanStatus.FAILED: "FAIL", ScanStatus.INTERRUPTED: "WARN"}.get(view.status, "INFO")
    ui.box(kind, f"**{_SCAN_LABEL[view.status]}** -- {s.strategy_name} over '{s.watchlist_name}' ({len(s.symbols)} symbols), started {ui.ts(s.started_at)}.")
    if s.error:
        st.caption(f"Note: {s.error}")
    cols = st.columns(len(ResultStatus))
    for c, st_ in zip(cols, ResultStatus):
        c.metric(_STATUS_LABEL[st_], s.counts.get(st_.value, sum(1 for r in view.results if r.status is st_)))
    st.caption(f"Scan {s.scan_id}. Strategy hash {s.strategy_hash[:12]}, config {s.config_hash}. Risk: "
               + ("assessed read-only against account #%s" % s.account if s.risk_mode == "ASSESSED" else "not assessed")
               + ". Results are a snapshot of this moment; markets and data change.")
    pick = st.multiselect("Show statuses", [x.value for x in ResultStatus], default=[x.value for x in ResultStatus], key="scanner_filter",
                          format_func=lambda v: _STATUS_LABEL[ResultStatus(v)])
    shown = [r for r in view.results if r.status.value in pick]
    ui.table([{"Symbol": r.symbol, "Status": _STATUS_LABEL[r.status], "Strategy output": (r.decision or {}).get("direction", "no new signal on this bar"),
               "Decision bar": (r.decision_bar or "n/a")[:10], "Risk": {"NOT_APPLICABLE": "nothing to assess", "NOT_ASSESSED": "not assessed"}.get(r.risk_state.value, r.risk_state.value.lower()),
               "Why": r.reason} for r in shown], empty="No results match the filter.")
    if not shown:
        return
    sym = st.selectbox("Symbol detail", [r.symbol for r in shown], index=None, placeholder="Select a symbol", key="scanner_detail")
    r = next((x for x in shown if x.symbol == sym), None)
    if r is None:
        return
    st.markdown(f"#### {r.symbol} -- {_STATUS_LABEL[r.status]}")
    for line in explain_result(r):
        st.write(line)
    if r.error:
        st.caption(f"Technical detail: {r.error}")
    if r.status is ResultStatus.SIGNAL_FOUND:
        h = sc.review_handoff(s, r)
        st.button("Review in paper workflow", key="scanner_review", on_click=_handoff, args=(h,),
                  help="Opens Paper Trading with this symbol and strategy selected. Nothing is created: a fresh preflight, risk check and your approval still apply.")
    elif r.status is ResultStatus.RISK_BLOCKED:
        st.caption("Blocked by the risk rules, so there is no review action.")
    with st.expander("Audit trace (data to decision)"):
        t = trace_of(r)
        if t is None:
            st.caption("No audit trace was recorded for this symbol (older scan, or evaluation stopped before the strategy ran). It is not reconstructed after the fact.")
        else:
            ui.json_block({"scan": {"id": s.scan_id, "strategy": s.strategy_name, "strategy_hash": s.strategy_hash, "params": s.strategy_params,
                                    "config_hash": s.config_hash, "interval": s.interval, "evaluated_at": r.evaluated_at},
                           "data": {k: v for k, v in (r.data or {}).items() if k != "trace"}, **t,
                           "scanner": {"status": r.status.value, "reason_code": r.reason_code, "persisted_decision": r.decision,
                                       "risk_state": r.risk_state.value}})
    with st.expander("Raw stored record"):
        ui.json_block({"status": r.status.value, "reason_code": r.reason_code, "decision": r.decision, "data": r.data,
                       "risk_state": r.risk_state.value, "evaluated_at": r.evaluated_at})


# ---------------------------------------------------------------------------------------------------- history
def _history(sc) -> None:
    st.subheader("4. Scan history")
    hist = sc.history(30)
    if not hist:
        st.info("No scans yet.")
        return
    ui.table([{"Started": ui.ts(h.scan.started_at), "Status": _SCAN_LABEL[h.status].split(" (")[0], "Strategy": h.scan.strategy_name,
               "Watchlist": h.scan.watchlist_name, "Symbols": len(h.scan.symbols),
               "Signals": h.scan.counts.get("SIGNAL_FOUND", 0), "Scan": h.scan.scan_id} for h in hist], empty="No scans yet.")
    ids = [h.scan.scan_id for h in hist]
    pick = st.selectbox("Open a past scan", ids, index=None, placeholder="Select a scan", key="scanner_history_pick")
    if st.button("Open scan", key="scanner_open") and pick:
        st.session_state["scanner_scan_id"] = pick
        st.rerun()
