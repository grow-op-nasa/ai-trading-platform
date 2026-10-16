"""Paper Trading (Sprint 19 follow-up): ONE visible workflow -- choose, preflight, run one cycle, inspect.

Everything needed is on the page with no prerequisite object (no profile): strategy, paper account, instrument and timeframe
selectors; settings; a Run preflight button; the preflight result with remediation; the run control (enabled only when the latest
preflight PASSED for exactly the current selections and is recent); and the persisted result of the cycle.

Side effects happen only inside `st.button` / `st.form_submit_button` branches. A rerun, refresh or reconnection cannot repeat a
preflight or a cycle: (1) the branch runs only on the rerun triggered by the click, (2) the submission token is consumed per click,
(3) the service re-checks the approved configuration hash and the hard preflight failures at the boundary, and (4) the runtime
enforces idempotency (cycle key, intent and ledger UNIQUE constraints) regardless of the UI.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import streamlit as st

from src.appsvc import strategies
from src.appsvc.paper import DEFAULT_FORM, OUTCOME_MEANING, PaperConfigError, PaperService
from src.appsvc.paths import AppPaths
from src.dashboard import shell, ui

PREFLIGHT_MAX_AGE_SECONDS = 1800
_OUTCOME_STATE = {"ORDER_FILLED": "OK", "NO_ACTION": "INFO", "NO_NEW_BAR": "INFO", "RISK_REJECTED": "WARN", "ORDER_REJECTED": "WARN",
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
    tab_run, tab_hist = st.tabs(["Run", "Cycle history & trace"])
    with tab_run:
        _run_tab(paths, svc, account)
    with tab_hist:
        _history_tab(paths, svc, account)


# --------------------------------------------------------------------------------------------- run
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
    symbols = sorted({r.symbol for r in rows if r.symbol})
    c3, c4 = st.columns(2)
    picked = c3.selectbox("Instrument (from saved experiments)", symbols, index=None, placeholder="Select or type below", key="paper_symbol") if symbols else None
    typed = c3.text_input("Or type an instrument symbol", key="paper_symbol_text", help="Used if filled in; otherwise the selection above.")
    symbol = (typed.strip().upper() if typed and typed.strip() else picked) or None
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

    st.subheader("3. Preflight (read-only)")
    st.caption(f"Checks this exact configuration ({cfg.strategy_name}, {cfg.symbol}, {cfg.interval}, account #{cfg.experiment_id}) and makes one "
               "market-data request. It changes nothing. It never runs by itself: press the button.")
    if st.button("Run preflight", key="btn_preflight"):
        now = datetime.now(timezone.utc)
        with st.spinner("Running preflight..."):
            try:
                rep = svc.preflight(cfg, check_data=True)
                st.session_state["paper_preflight"] = {"hash": chash, "at": now, "report": rep.to_dict()}
            except Exception as exc:  # noqa: BLE001
                st.session_state["paper_preflight"] = {"hash": chash, "at": now, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    pre = st.session_state.get("paper_preflight")
    ready = False
    if pre is None:
        st.info("No preflight has been run in this session. The run control stays disabled until one passes.")
    elif pre["hash"] != chash:
        st.warning("The selections or settings changed since the last preflight, so that result no longer applies. Run preflight again.")
    else:
        age = (datetime.now(timezone.utc) - pre["at"]).total_seconds()
        if "error" in pre:
            st.error(f"Preflight could not be evaluated: {pre['error']}")
        else:
            rep = pre["report"]
            stale = age > PREFLIGHT_MAX_AGE_SECONDS
            ready = bool(rep["ready"]) and not stale and initialised
            ui.box("PASS" if rep["ready"] else rep["overall"],
                   f"Preflight {rep['overall']} at {pre['at'].isoformat(timespec='seconds')} for this configuration."
                   + (" It is older than 30 minutes and no longer counts; run it again." if stale else ""))
            ui.table([{"Check": c["name"], "Status": c["status"], "Detail": c["detail"][:140]} for c in rep["checks"]], empty="")
            for c in rep["checks"]:
                if c["status"] != "PASS":
                    st.write(f"**{c['name']}** ({c['status']}): {REMEDIATION.get(c['name'], c['detail'])}")

    st.subheader("4. Run ONE paper cycle")
    with st.form("run_cycle_form"):
        st.write(f"Will run one decision cycle: strategy **{cfg.strategy_name}**, instrument **{cfg.symbol}**, timeframe **{cfg.interval}**, "
                 f"paper account **#{cfg.experiment_id}**, mode **paper**. A cycle may end with no signal, a risk rejection, or a simulated fill.")
        monitoring = st.checkbox("Run post-cycle monitoring (records a monitoring run; may queue alert events only if alerting is enabled)", value=True, key="paper_monitoring")
        confirm = st.checkbox("I confirm: run one paper-only cycle now", key="paper_confirm")
        submitted = st.form_submit_button("Run one paper cycle", disabled=not ready)
    if not ready:
        st.caption("Disabled until the account is initialised and a recent preflight PASSED for exactly these selections.")
    if submitted:
        if not confirm:
            st.warning("Tick the confirmation to run. Nothing was executed.")
        else:
            token = st.session_state.setdefault("run_token", uuid4().hex)
            with st.spinner("Running one paper cycle..."):
                outcome = svc.run_cycle(cfg, request_token=token, monitoring=monitoring, approved_config_hash=pre["hash"] if pre else None)
            st.session_state["run_token"] = uuid4().hex
            st.session_state["last_outcome"] = outcome
            st.cache_data.clear()
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
    st.subheader("Result of the last submission")
    if out.reused_token:
        st.info("This submission had already been executed; the earlier result is shown. Nothing was run again.")
    if out.refused:
        st.error(f"Not run: {out.refused}")
        for c in out.refused_checks:
            st.write(f"- {c['name']}: {c['detail']}")
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
    st.write(OUTCOME_MEANING.get(status, ""))
    if rec.reason:
        st.caption(rec.reason)
    ui.table([{"Item": k, "Value": v} for k, v in [
        ("Cycle id", rec.cycle_id), ("Status", status), ("Started", ui.ts(rec.started_at)), ("Finished", ui.ts(rec.finished_at)),
        ("Decision bar", ui.ts(rec.decision_bar)), ("Reconciliation", rec.reconciliation_status or "not run"),
        ("Intent / client order", f"{rec.intent_id} / {rec.client_order_id}" if rec.client_order_id else "none (no order was proposed)")]], empty="")
    for title, value in (("Signal at the decision bar", rec.signal), ("Risk decision", rec.risk_decision), ("Paper fill (simulated)", rec.fill),
                         ("Monitoring", rec.monitoring)):
        with st.expander(title):
            if value:
                ui.json_block(value)
            else:
                st.info("None for this cycle (for example, no signal means no risk decision and no order).")


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
                         ("Reconciliation", tr["reconciliation"].to_dict() if tr["reconciliation"] else None),
                         ("Monitoring", c.monitoring)):
        with st.expander(title):
            if value:
                ui.json_block(value)
            else:
                st.info("Not recorded for this cycle.")
    if tr["findings"]:
        st.write("Findings in the linked monitoring run: " + ", ".join(f"{f.severity.value}: {f.title}" for f in tr["findings"]))
    for lim in tr["limitations"]:
        st.caption("Limitation: " + lim)
