"""Paper Trading (Sprint 20): guided workflow -- choose, Run (fresh preflight included), approve a proposal, inspect.

"Run paper cycle" performs a FRESH authoritative preflight automatically and then ONE bounded cycle; there is no separate
preflight click. A read-only "Preflight only" button remains. A risk-approved actionable signal ends as a persisted PROPOSAL
(no order); only an explicit Approve click on the Approvals tab -- on that exact proposal id -- can place the simulated order,
and the backend re-validates everything at that moment. Rerun, refresh, expiry, rejection and silence never trade.

Side effects happen only inside `st.button` branches. The submission token is consumed per click, the service/runtime enforce
blocking checks and idempotency regardless of the UI.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import streamlit as st

from src.appsvc import explain, freshness, instruments, prefs, strategies
from src.appsvc.paper import DEFAULT_FORM, OUTCOME_MEANING, PaperConfigError, PaperService, stages_from_record
from src.appsvc.paths import AppPaths
from src.dashboard import proposals_ui, shell, ui

_OUTCOME_STATE = {"AWAITING_APPROVAL": "WARN", "PROPOSAL_REJECTED": "INFO", "PROPOSAL_EXPIRED": "INFO", "PROPOSAL_INVALIDATED": "WARN", "ORDER_FILLED": "OK", "NO_ACTION": "INFO", "NO_NEW_BAR": "INFO", "RISK_REJECTED": "WARN", "ORDER_REJECTED": "WARN",
                  "AWAITING_EXECUTION": "WARN", "RECONCILIATION_REQUIRED": "FAIL", "BLOCKED": "WARN", "FAILED": "FAIL", "STARTED": "FAIL"}
REMEDIATION = {
    "registry": "The experiment registry file is missing. It is never created implicitly; create a paper account below or run scripts/run_experiment.py.",
    "portfolio": "This experiment has no saved portfolio. Choose another account or create a new paper account below.",
    "strategy": "The strategy is not resolvable in the production registry. Choose a listed strategy.",
    "paper_database": "No paper database yet. It is created when you initialise the paper account (button below).",
    "paper_account": "Initialise the paper account for this experiment (explicit button below), then run preflight again.",
    "unresolved_intents": "An earlier execution intent is unresolved. Running a cycle recovers it by lookup (never a blind resubmission).",
    "reconciliation": "The paper broker ledger and the portfolio are not in agreement. Open Portfolio & Execution; a MISMATCH needs human review. No new risk is taken.",
    "market_data": "Market data could not be obtained or validated (network, provider or stale data). Check your connection and run preflight again.",
    "persistence_writable": "The paper database is read-only. Fix its file permissions.",
    "duplicate_run_protection": "Another paper run holds the lock for this account. Wait for it to finish.",
    "alerting": "Alerting is enabled but its configuration is invalid (see Setup & System Status). Trading is not affected, but alerts would not work.",
    "monitoring": "No monitoring service is configured, so no post-cycle monitoring would run.",
}


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Paper Trading")
    st.warning("PAPER ONLY. This page can run exactly one simulated decision cycle at a time through the same runtime as "
               "`python -m src.cli paper run --once`. It cannot place real orders, select a live broker, or start a background loop.")
    acting = st.session_state.get("paper_account") or account       # the account chosen on this page, else the sidebar one
    pending = len(svc.proposals(acting, pending_only=True)) if acting is not None else 0
    tab_run, tab_appr, tab_hist = st.tabs(["Run", f"Approvals ({pending})", "Cycle history & trace"])
    with tab_run:
        _run_tab(paths, svc, account)
    with tab_appr:
        _approvals_tab(svc, acting)
    with tab_hist:
        _history_tab(paths, svc, account)


# --------------------------------------------------------------------------------------------- run
def _remember_symbol(paths: AppPaths) -> None:
    """on_change callback for the instrument widgets: remember a VALID symbol only. Runs nothing and touches no portfolio."""
    sym, _ = _effective_symbol()
    if sym is None:
        return
    try:
        prefs.save_symbol(paths, sym)
    except OSError:
        st.session_state["_pref_error"] = "Could not save your instrument choice; it applies to this session only."


def _effective_symbol() -> tuple[str | None, str | None]:
    choice = st.session_state.get("paper_symbol_choice")
    if choice is None:
        return None, "Select an instrument."
    if choice == instruments.CUSTOM:
        return instruments.normalize_symbol(st.session_state.get("paper_symbol_custom", st.session_state.get("_custom_kept", "")))
    return choice, None


def _symbol_selector(paths: AppPaths, col) -> str | None:
    """Preset dropdown + Custom…. Selecting changes nothing but this widget state (and, for a valid symbol, the remembered
    preference); it never runs a preflight or a cycle. Invalid custom input is reported, never replaced by a default."""
    if "paper_symbol_choice" not in st.session_state:                   # first load: restore, never guess
        saved = prefs.saved_symbol(paths)
        if saved in instruments.symbols():
            st.session_state["paper_symbol_choice"] = saved
        elif saved:
            st.session_state["paper_symbol_choice"] = instruments.CUSTOM
            st.session_state["_custom_kept"] = saved
            st.session_state["_symbol_restored"] = saved
    col.selectbox("Instrument", instruments.options(), index=None, placeholder="Select an instrument", key="paper_symbol_choice",
                  format_func=instruments.display, on_change=_remember_symbol, args=(paths,),
                  help="Presets are suggestions. Whether a symbol has usable data is only established by the preflight.")
    if st.session_state.get("paper_symbol_choice") == instruments.CUSTOM:
        col.text_input("Custom ticker symbol", value=st.session_state.get("_custom_kept", ""), key="paper_symbol_custom",
                       placeholder="e.g. TSLA", on_change=_remember_symbol, args=(paths,),
                       help="Trimmed and upper-cased. Existence, data and strategy support are checked by the preflight, not here.")
        st.session_state["_custom_kept"] = st.session_state.get("paper_symbol_custom", "")
        if st.session_state.get("_symbol_restored") and st.session_state["_custom_kept"] == st.session_state["_symbol_restored"]:
            col.caption(f"Restored your last-used symbol {st.session_state['_symbol_restored']}. It is revalidated by the next preflight/run.")
    sym, problem = _effective_symbol()
    if st.session_state.get("paper_symbol_choice") == instruments.CUSTOM and sym is None:
        if st.session_state.get("_custom_kept"):
            col.error(problem)
        else:
            col.info(problem)
    elif sym is not None and instruments.lookup(sym) is None:
        col.caption(f"{sym} is a custom symbol: support is unverified until the preflight confirms market data for it.")
    return sym


def _selection(paths: AppPaths, svc: PaperService, sidebar_account: int | None):
    """Step 1: the visible selectors. Returns the validated config, or None with an explanation already shown."""
    st.subheader("1. Choose what to run")
    names = strategies.eligible_names(paths)
    rows = [r for r in shell.experiment_rows(paths) if r.has_portfolio]
    c1, c2 = st.columns(2)
    if names:
        strategy = c1.selectbox("Strategy (production registry)", names, index=None, placeholder="Select a strategy", key="paper_strategy")
    else:
        c1.error("No strategies are registered in the production registry, so nothing can be run.")
        strategy = None
    ids = [r.experiment_id for r in rows]
    if ids:
        default = ids.index(sidebar_account) if sidebar_account in ids else None
        account = c2.selectbox("Paper account (experiment with a saved portfolio)", ids, index=default, placeholder="Select an account",
                               key="paper_account", format_func=lambda i: f"#{i}  {next(r.strategy_name for r in rows if r.experiment_id == i)}")
    else:
        account = None
        c2.warning("No paper account exists yet: no experiment has a saved portfolio. Create a new paper account below "
                   "(explicit; nothing is created for you).")
    c3, c4 = st.columns(2)
    symbol = _symbol_selector(paths, c3)
    interval = c4.selectbox("Timeframe", ["1d"], key="paper_interval", help="The paper runtime supports completed daily bars only.")

    with st.expander("Risk, stop and cost settings (review these; they are part of what preflight approves)"):
        r, sp, e = DEFAULT_FORM["risk"], DEFAULT_FORM["stop"], DEFAULT_FORM["execution"]
        k1, k2, k3 = st.columns(3)
        alloc = k1.number_input("Allocation per trade (fraction)", value=r["allocation_per_trade_pct"], min_value=0.0, max_value=1.0, step=0.01, format="%.3f", key="paper_alloc")
        expo = k2.number_input("Max portfolio exposure (fraction)", value=r["max_portfolio_exposure_pct"], min_value=0.0, max_value=1.0, step=0.05, format="%.3f", key="paper_expo")
        riskp = k3.number_input("Risk per trade (fraction)", value=r["risk_pct_per_trade"], min_value=0.0, max_value=1.0, step=0.005, format="%.4f", key="paper_riskp")
        d1, d2 = st.columns(2)
        atr_p = d1.number_input("ATR stop period", value=sp["period"], min_value=1, step=1, key="paper_atr_p")
        atr_m = d2.number_input("ATR stop multiple", value=sp["multiple"], min_value=0.1, step=0.1, key="paper_atr_m")
        f1, f2, f3 = st.columns(3)
        slip = f1.number_input("Slippage (bps)", value=e["slippage_bps"], min_value=0.0, step=1.0, key="paper_slip")
        fee = f2.number_input("Fee (bps)", value=e["fee_bps"], min_value=0.0, step=1.0, key="paper_fee")
        fixed = f3.number_input("Fixed fee ($)", value=e["fixed_fee"], min_value=0.0, step=0.1, key="paper_fixed")
        params_text = st.text_area("Strategy parameters (JSON object; empty uses the strategy defaults)", value="{}", key="paper_params")
        st.caption("Execution timing is fixed to SIGNAL_BAR_CLOSE, the only timing the paper runtime supports.")

    missing = [n for n, v in (("a strategy", strategy), ("a paper account", account), ("an instrument", symbol)) if v is None]
    if missing:
        st.info("Select " + ", ".join(missing) + " to continue. The preflight button appears as soon as the selections are complete.")
        return None
    try:
        params = json.loads(params_text or "{}")
        if not isinstance(params, dict):
            raise PaperConfigError("strategy parameters must be a JSON object")
        form = {"interval": interval, "lookback_days": DEFAULT_FORM["lookback_days"],
                "risk": {"allocation_per_trade_pct": alloc, "max_portfolio_exposure_pct": expo, "risk_pct_per_trade": riskp},
                "stop": {"type": "atr", "period": int(atr_p), "multiple": atr_m},
                "execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": slip, "fee_bps": fee, "fixed_fee": fixed}}
        return svc.build_config(experiment_id=int(account), strategy_name=strategy, symbol=symbol, strategy_params=params, form=form)
    except (PaperConfigError, json.JSONDecodeError) as exc:
        st.error(f"These selections are not a valid paper-run configuration: {exc}")
        return None


def _create_account(svc: PaperService, paths: AppPaths) -> None:
    names = strategies.eligible_names(paths)
    with st.expander("No suitable paper account? Create a new empty paper account (explicit)"):
        st.caption("Creates a NEW experiment record and a cash-only portfolio with the existing registry. It is not a backtest, changes no existing "
                   "account, and does not start paper trading: you still initialise it and run preflight below.")
        with st.form("create_account_form"):
            strat = st.selectbox("Strategy this account is for", names, index=None, placeholder="Select a strategy", key="new_acct_strategy") if names else None
            sym = st.text_input("Instrument symbol", key="new_acct_symbol")
            cash = st.number_input("Starting cash ($)", value=10000.0, min_value=100.0, max_value=1_000_000_000.0, step=100.0, key="new_acct_cash")
            label = st.text_input("Label", key="new_acct_label")
            ok = st.checkbox("I understand this creates a new paper account record", key="new_acct_ok")
            go = st.form_submit_button("Create paper account")
        if go:
            if not ok or not strat:
                st.warning("Choose a strategy and tick the confirmation. Nothing was created.")
                return
            try:
                exp = svc.create_paper_account(strategy_name=strat, symbol=sym, starting_cash=float(cash), label=label)
                st.cache_data.clear()
                st.success(f"Created paper account #{exp}. Reload the page (or change any selection) and pick it in the account selector.")
            except PaperConfigError as exc:
                st.error(f"Not created: {exc}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not create the account: {type(exc).__name__}: {str(exc)[:200]}")


def _run_tab(paths: AppPaths, svc: PaperService, sidebar_account: int | None) -> None:
    cfg = _selection(paths, svc, sidebar_account)
    _create_account(svc, paths)
    if cfg is None:
        return
    chash = cfg.identity_hash()
    info = svc.inspect(cfg.experiment_id)
    initialised = bool(info["available"] and info["account"])

    st.subheader("2. Paper account")
    if initialised:
        st.success(f"Paper account initialised for experiment #{cfg.experiment_id}: {info['account'].get('label', '')}")
    else:
        st.warning("This experiment has no initialised paper account. A cycle cannot run until you explicitly initialise one.")
        _init_form(svc, cfg)

    st.subheader("3. Run")
    st.caption(f"One click runs a fresh preflight for this exact configuration ({cfg.strategy_name}, {cfg.symbol}, {cfg.interval}, account "
               f"#{cfg.experiment_id}, paper) and then ONE bounded cycle. It makes market-data requests, never places an order by itself "
               "(a trade becomes a proposal you approve) and starts no background loop.")
    monitoring = st.checkbox("Run post-cycle monitoring (records a monitoring run; may queue alert events only if alerting is enabled)", value=True, key="paper_monitoring")
    c_run, c_pre = st.columns(2)
    other = [r["proposal"] for r in svc.proposals(cfg.experiment_id, pending_only=True) if r["proposal"].symbol != cfg.symbol]
    if not initialised:
        st.caption("Run is disabled until the paper account is initialised (explicit step above).")
    if other:
        st.warning(f"A pending proposal exists for {other[0].symbol} ({other[0].side} {other[0].approved_quantity:g}). It keeps its own symbol and "
                   f"cannot be approved as {cfg.symbol}. Decide it on the Approvals tab (or let it expire) before running {cfg.symbol}.")
    if c_run.button("Run paper cycle", key="btn_run", type="primary", disabled=not initialised or bool(other)):
        token = st.session_state.setdefault("run_token", uuid4().hex)
        with st.status("Running paper cycle...", expanded=True) as box:
            def progress(stage: str, status: str, detail: str) -> None:
                box.write(f"{stage}: {status}" + (f" -- {detail}" if detail else ""))
            outcome = svc.run_with_preflight(cfg, request_token=token, monitoring=monitoring, progress=progress)
            box.update(label="Finished" if outcome.ran else "Not run" if outcome.refused else "Error",
                       state="complete" if outcome.ran else "error")
        st.session_state["run_token"] = uuid4().hex
        st.session_state["last_outcome"] = outcome
        st.cache_data.clear()
    if c_pre.button("Preflight only (read-only)", key="btn_preflight"):
        now = datetime.now(timezone.utc)
        with st.spinner("Running preflight..."):
            try:
                st.session_state["paper_preflight"] = {"hash": chash, "at": now, "report": svc.preflight(cfg, check_data=True).to_dict()}
            except Exception as exc:  # noqa: BLE001
                st.session_state["paper_preflight"] = {"hash": chash, "at": now, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    pre = st.session_state.get("paper_preflight")
    if pre is not None and pre["hash"] == chash:
        if "error" in pre:
            st.error(f"Preflight could not be evaluated: {pre['error']}")
        else:
            rep = pre["report"]
            ui.box("PASS" if rep["ready"] else rep["overall"], f"Preflight {rep['overall']} at {pre['at'].isoformat(timespec='seconds')} (read-only; nothing was run).")
            _check_table(rep["checks"])
    elif pre is not None:
        st.caption("An earlier preflight was for different selections and is not shown.")
    last = st.session_state.get("last_outcome")
    if last is not None:
        _show_outcome(svc, last)

    with st.expander("Optional: save these settings as a profile file for the CLI (`paper run --config`)"):
        pname = st.text_input("Profile name", key="paper_profile_name")
        if st.button("Save profile", key="btn_save_profile"):
            try:
                st.success(f"Saved {svc.save_profile(pname, cfg)}")
            except PaperConfigError as exc:
                st.error(f"Not saved: {exc}")


def _check_table(checks: list[dict]) -> None:
    ui.table([{"Check": c["name"], "Status": c["status"], "Detail": c["detail"][:140]} for c in checks], empty="")
    for c in checks:
        if c["status"] != "PASS":
            st.write(f"**{c['name']}** ({c['status']}): {REMEDIATION.get(c['name'], c['detail'])}")


def _approvals_tab(svc: PaperService, account: int | None) -> None:
    if not shell.need_account(account):
        return
    st.caption("A proposal is a persisted request to place ONE simulated paper order. Nothing is ordered until you approve that exact proposal. "
               "Rejecting, letting it expire, or doing nothing places no order.")
    proposals_ui.show_decision()
    proposals_ui.render_queue(svc, account, key="appr")
    past = [r for r in svc.proposals(account) if r["effective_status"] != "PENDING"]
    if past:
        st.markdown("Decided or lapsed proposals")
        ui.table([{"Proposal": r["proposal"].proposal_id[:12], "Symbol": r["proposal"].symbol, "Side": r["proposal"].side,
                   "Status": r["effective_status"], "Meaning": explain.proposal(r["effective_status"]).meaning,
                   "Created": ui.ts(r["proposal"].created_at)} for r in past], empty="")


def _init_form(svc: PaperService, cfg) -> None:
    with st.expander("Initialise paper account (explicit; records the current saved portfolio as the opening baseline)", expanded=True):
        with st.form("init_form"):
            label = st.text_input("Label for this account baseline", key="init_label")
            ok = st.checkbox("I understand this records the baseline once for this experiment", key="init_ok")
            go = st.form_submit_button("Initialise paper account")
        if go:
            if not (label.strip() and ok):
                st.warning("Enter a label and tick the confirmation. Nothing was changed.")
                return
            try:
                info = svc.initialize_account(cfg, label)
                st.success(f"Initialised: baseline cash {info['baseline_cash']:.2f}, {len(info['baseline_positions'])} open position(s). "
                           "Press Run preflight to continue.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not initialise: {type(exc).__name__}: {str(exc)[:200]}")


def _show_outcome(svc: PaperService, out) -> None:
    st.subheader("Result of the last submission" + (f" ({out.record.symbol})" if out.record is not None and out.record.symbol else ""))
    if out.reused_token:
        st.info("This submission had already been executed; the earlier result is shown. Nothing was run again.")
    if out.refused:
        st.error(f"Not run: {out.refused}")
        if out.refused_checks:
            st.write("Blocking checks (nothing was executed; the runtime enforces these independently of this page):")
            _check_table(out.refused_checks)
        if out.locked:
            st.caption("Another paper run holds the lock for this account.")
        return
    if out.error:
        st.error(f"The service raised an error: {out.error}. Durable records may exist -- check Cycle history & trace. This is not a success.")
        return
    tr = svc.trace(out.record.cycle_id)
    rec = tr["cycle"] if tr.get("available") else out.record      # prefer the PERSISTED record
    status = rec.status.value
    ui.box(_OUTCOME_STATE.get(status, "INFO"), f"{status}" + (f" [{rec.reason_code}]" if rec.reason_code else "")
           + (" -- replayed an existing cycle (no new execution)" if out.record.replayed else ""))
    ex = explain.cycle(status, rec.reason_code)
    st.write(OUTCOME_MEANING.get(status, ex.meaning))
    if ex.next_step:
        st.caption("Next: " + ex.next_step)
    if rec.reason:
        st.caption(rec.reason)
    if status == "AWAITING_APPROVAL":
        st.warning("A trade is proposed but NOT placed. Open the Approvals tab to approve or reject this exact proposal.")
    ui.table(stages_from_record(rec), empty="")
    ui.table([{"Item": k, "Value": v} for k, v in [
        ("Cycle id", rec.cycle_id), ("Status", status), ("Started", ui.ts(rec.started_at)), ("Finished", ui.ts(rec.finished_at)),
        ("Decision bar", ui.ts(rec.decision_bar)), ("Reconciliation", rec.reconciliation_status or "not run"),
        ("Intent / client order", f"{rec.intent_id} / {rec.client_order_id}" if rec.client_order_id else "none (no order was proposed)")]], empty="")
    for title, value in (("Signal at the decision bar", rec.signal), ("Risk decision", rec.risk_decision), ("Paper fill (simulated)", rec.fill)):
        with st.expander(title):
            if value:
                ui.json_block(value)
            else:
                st.info("None for this cycle (for example, no signal means no risk decision and no order).")
    if tr.get("available"):
        _monitoring_section(svc, tr, key="result")


_MON_BOX = {"COMPLETED": "OK", "FAILED": "FAIL", "PENDING": "WARN", "SKIPPED": "INFO", "NOT_CONFIGURED": "INFO",
            "NOT_RECORDED": "WARN", "LINKED_RUN_MISSING": "FAIL", "UNKNOWN": "WARN"}


def _monitoring_section(svc: PaperService, tr: dict, *, key: str) -> None:
    """Monitoring status from the PERSISTED cycle record; links the run and its findings; offers the explicit monitoring-only op."""
    c, ms = tr["cycle"], tr["monitoring_state"]
    with st.expander("Monitoring", expanded=True):
        ui.box(_MON_BOX.get(ms["state"], "INFO"), f"{ms['state']}: {ms['message']}")
        run = tr.get("monitoring_run")
        if run is not None:
            ui.table([{"Item": k, "Value": v} for k, v in [
                ("Monitoring run id", run.run_id), ("Run status", getattr(run.status, "value", str(run.status))),
                ("Run started", ui.ts(run.started_at)), ("Recorded on cycle", ui.ts((c.monitoring or {}).get("at"))),
                ("Ran after the cycle finished (explicit monitoring-only run)", "yes" if (c.monitoring or {}).get("late") else "no"),
                ("Findings in run", len(tr.get("findings") or [])), ("Alert events for run", len(tr.get("alert_events") or []))]], empty="")
            for f in tr.get("findings") or []:
                st.write(f"- {f.severity.value}: {f.title}")
        if c.monitoring:
            st.caption("Stored monitoring record on the cycle:")
            ui.json_block(c.monitoring)
        if ms["can_run"]:
            st.caption("Run monitoring only for this cycle: evaluates the CURRENT persisted portfolio/operational state with the deterministic "
                       "monitoring service. It generates no signal, submits no order, creates no cycle and changes no positions or cash. "
                       "The result is recorded on this cycle and marked as run after the fact.")
            with st.form(f"monitor_form_{key}"):
                ok = st.checkbox("I confirm: run monitoring only for this cycle", key=f"monitor_ok_{key}")
                go = st.form_submit_button("Run monitoring for this cycle")
            if go:
                if not ok:
                    st.warning("Tick the confirmation to run. Nothing was executed.")
                else:
                    try:
                        cfg = svc.build_config(experiment_id=c.experiment_id, strategy_name=c.strategy_name, symbol=c.symbol)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Cannot build a configuration for this cycle: {exc}")
                        return
                    out = svc.monitor_cycle(cfg, c.cycle_id)
                    if out.refused or out.error:
                        st.error(f"Monitoring was not run: {out.refused or out.error}")
                    else:
                        st.cache_data.clear()
                        st.success("Monitoring recorded. Reload the cycle (change the selection or refresh) to see it.")



# --------------------------------------------------------------------------------------------- history
def _history_tab(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    if not shell.need_account(account):
        return
    info = svc.inspect(account)
    if not info["available"]:
        st.info(info["reason"] + " -- no cycle has been recorded.")
        return
    cycles = info["cycles"]
    if not cycles:
        st.info("No paper cycles have been recorded for this account yet.")
        return
    ui.table([{"Cycle": c.cycle_id[:12], "Status": c.status.value, "Reason": c.reason_code or "", "Decision bar": ui.ts(c.decision_bar),
               "Started": ui.ts(c.started_at), "Replayed record": c.replayed} for c in cycles], empty="")
    cid = st.selectbox("Inspect a cycle", [c.cycle_id for c in cycles], index=None, placeholder="Select a cycle", key="trace_cycle",
                       format_func=lambda i: f"{i[:12]}  {next(c.status.value for c in cycles if c.cycle_id == i)}")
    if cid is None:
        return
    tr = svc.trace(cid)
    if not tr.get("available"):
        st.warning(tr.get("reason", "not available"))
        return
    c = tr["cycle"]
    st.markdown(f"**Cycle {c.cycle_id}** -- {c.status.value}" + (f" [{c.reason_code}]" if c.reason_code else ""))
    for title, value in (("Market-data provenance", c.dataset), ("Strategy identity", {"strategy": c.strategy_name, "strategy_hash": c.strategy_hash,
                         "config_hash": c.config_hash, "symbol": c.symbol, "interval": c.interval, "idempotency_key": c.idempotency_key}),
                         ("Signal at the decision bar", c.signal), ("Risk decision", c.risk_decision),
                         ("Execution intent", tr["intent"].to_dict() if tr["intent"] else None),
                         ("Paper fill (simulated)", tr["fill"].to_dict() if tr["fill"] else None),
                         ("Reconciliation", tr["reconciliation"].to_dict() if tr["reconciliation"] else None)):
        with st.expander(title):
            if value:
                ui.json_block(value)
            else:
                st.info("Not recorded for this cycle.")
    _monitoring_section(svc, tr, key="hist")
    for lim in tr["limitations"]:
        st.caption("Limitation: " + lim)
