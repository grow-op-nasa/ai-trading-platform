"""Paper Trading (Sprint 19): preflight, ONE explicit paper cycle, inspection. Every action calls PaperService once.

Side effects happen only inside `st.button` / `st.form_submit_button` branches. A Streamlit rerun cannot repeat a cycle:
(1) the branch runs only on the rerun triggered by the click, (2) the submission token is consumed per click, and
(3) the runtime enforces idempotency (cycle key, intent and ledger UNIQUE constraints) regardless of the UI.
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

_OUTCOME_STATE = {"ORDER_FILLED": "OK", "NO_ACTION": "INFO", "NO_NEW_BAR": "INFO", "RISK_REJECTED": "WARN", "ORDER_REJECTED": "WARN",
                  "AWAITING_EXECUTION": "WARN", "RECONCILIATION_REQUIRED": "FAIL", "BLOCKED": "WARN", "FAILED": "FAIL", "STARTED": "FAIL"}


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Paper Trading")
    st.warning("PAPER ONLY. This page can run exactly one simulated decision cycle at a time through the same runtime as "
               "`python -m src.cli paper run --once`. It cannot place real orders, select a live broker, or start a background loop.")
    tab_run, tab_hist, tab_new = st.tabs(["Run", "Cycle history & trace", "Create profile"])
    with tab_run:
        _run_tab(paths, svc)
    with tab_hist:
        _history_tab(paths, svc, account)
    with tab_new:
        _profile_tab(paths, svc)


# --------------------------------------------------------------------------------------------- run
def _run_tab(paths: AppPaths, svc: PaperService) -> None:
    profiles = svc.list_profiles()
    for p in (p for p in profiles if p.config is None):
        st.warning(f"Profile '{p.name}' is invalid and cannot be used: {p.error}")
    valid = {p.name: p for p in profiles if p.config is not None}
    if not valid:
        st.info("No paper-run profiles yet. Create one in the 'Create profile' tab (a validated JSON file under data/app/paper_profiles). "
                "Nothing is created for you.")
        return
    name = st.selectbox("Profile", list(valid), index=None, placeholder="Select a profile", key="paper_profile")
    if name is None:
        st.info("Select a profile to see its configuration and run a preflight.")
        return
    cfg = valid[name].config
    st.subheader("Selected run configuration")
    ui.table([{"Setting": k, "Value": v} for k, v in [
        ("Execution mode", "paper (fixed; no other mode exists)"), ("Paper account (experiment)", f"#{cfg.experiment_id}"),
        ("Strategy", cfg.strategy_name), ("Strategy parameters", json.dumps(cfg.strategy_params) or "{}"), ("Instrument", cfg.symbol),
        ("Timeframe", cfg.interval), ("Lookback days", cfg.lookback_days), ("Risk limits", json.dumps(cfg.risk)),
        ("Stop policy", json.dumps(cfg.stop)), ("Execution costs", json.dumps(cfg.execution)),
        ("Market-data source", "yfinance (public provider); health shown by preflight"), ("Config identity", cfg.identity_hash()[:16])]], empty="")
    info = svc.inspect(cfg.experiment_id)
    initialised = bool(info["available"] and info["account"])
    if not initialised:
        st.warning("This experiment has no initialised paper account. A cycle cannot run until you explicitly initialise one.")
        _init_form(svc, cfg)
    else:
        st.caption(f"Paper account initialised: {info['account'].get('label', '')}")

    st.subheader("1. Preflight (read-only)")
    key = f"preflight::{name}::{cfg.identity_hash()}"
    if st.button("Run preflight (makes one market-data request; changes nothing)", key="btn_preflight"):
        with st.spinner("Running preflight..."):
            try:
                rep = svc.preflight(cfg, check_data=True)
                st.session_state[key] = {"report": rep.to_dict(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            except Exception as exc:  # noqa: BLE001
                st.session_state[key] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                                         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    pre = st.session_state.get(key)
    ready = False
    if pre is None:
        st.info("No preflight has been run for this configuration in this session. The run control stays disabled until one passes.")
    elif "error" in pre:
        st.error(f"Preflight could not be evaluated: {pre['error']}")
    else:
        rep = pre["report"]
        ready = rep["ready"]
        ui.box("PASS" if ready else rep["overall"], f"Preflight {rep['overall']} at {pre['at']} UTC. Not a guarantee for later: re-run if time has passed.")
        ui.table([{"Check": c["name"], "Status": c["status"], "Detail": c["detail"][:140]} for c in rep["checks"]], empty="")
    can_run = ready and initialised

    st.subheader("2. Run ONE paper cycle")
    with st.form("run_cycle_form"):
        st.write(f"Will run one decision cycle: strategy **{cfg.strategy_name}**, instrument **{cfg.symbol}**, timeframe **{cfg.interval}**, "
                 f"paper account **#{cfg.experiment_id}**, mode **paper**. A cycle may end with no signal, a risk rejection, or a simulated fill.")
        monitoring = st.checkbox("Run post-cycle monitoring (records a monitoring run; may queue alert events only if alerting is enabled)", value=True)
        confirm = st.checkbox("I confirm: run one paper-only cycle now")
        submitted = st.form_submit_button("Run one paper cycle", disabled=not can_run)
    if not can_run:
        st.caption("Disabled until the account is initialised and the latest preflight PASSES.")
    if submitted:
        if not confirm:
            st.warning("Tick the confirmation to run. Nothing was executed.")
        else:
            token = st.session_state.setdefault("run_token", uuid4().hex)
            with st.spinner("Running one paper cycle..."):
                outcome = svc.run_cycle(cfg, request_token=token, monitoring=monitoring)
            st.session_state["run_token"] = uuid4().hex
            st.session_state["last_outcome"] = (name, outcome)
            st.cache_data.clear()
    last = st.session_state.get("last_outcome")
    if last and last[0] == name:
        _show_outcome(last[1])


def _init_form(svc: PaperService, cfg) -> None:
    with st.expander("Initialise paper account (explicit, records the current saved portfolio as the opening baseline)"):
        with st.form("init_form"):
            label = st.text_input("Label for this account baseline")
            ok = st.checkbox("I understand this records the baseline once for this experiment")
            go = st.form_submit_button("Initialise paper account")
        if go:
            if not (label.strip() and ok):
                st.warning("Enter a label and tick the confirmation. Nothing was changed.")
                return
            try:
                info = svc.initialize_account(cfg, label)
                st.success(f"Initialised: baseline cash {info['baseline_cash']:.2f}, {len(info['baseline_positions'])} open position(s). Reload to continue.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not initialise: {type(exc).__name__}: {str(exc)[:200]}")


def _show_outcome(out) -> None:
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
    rec = out.record
    status = rec.status.value
    ui.box(_OUTCOME_STATE.get(status, "INFO"), f"{status}" + (f" [{rec.reason_code}]" if rec.reason_code else "") + (" -- replayed existing cycle" if rec.replayed else ""))
    st.write(OUTCOME_MEANING.get(status, ""))
    if rec.reason:
        st.caption(rec.reason)
    cols = st.columns(3)
    cols[0].write(f"Cycle: `{rec.cycle_id}`")
    cols[1].write(f"Decision bar: {ui.ts(rec.decision_bar)}")
    cols[2].write(f"Reconciliation: {rec.reconciliation_status or 'not run'}")
    if rec.client_order_id:
        st.write(f"Intent `{rec.intent_id}`, client order `{rec.client_order_id}`")
    if rec.fill:
        st.write("Simulated paper fill: " + ", ".join(f"{k}={v}" for k, v in rec.fill.items() if k in ("quantity", "fill_price", "fee", "slippage_amount", "side")))
    if rec.monitoring:
        st.write(f"Monitoring: {rec.monitoring}")


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


# --------------------------------------------------------------------------------------------- profile
def _profile_tab(paths: AppPaths, svc: PaperService) -> None:
    st.caption("A profile is a validated configuration file (the single source of truth, also usable by the CLI). Saving writes a NEW file "
               "and never overwrites. Review every value: nothing here is a recommendation.")
    exps = [r for r in shell.experiment_rows(paths) if r.has_portfolio]
    names = strategies.eligible_names(paths)
    if not exps or not names:
        st.info("A profile needs at least one experiment with a saved portfolio and at least one registered strategy.")
        return
    with st.form("profile_form"):
        pname = st.text_input("Profile name (letters, digits, - _ .)")
        exp = st.selectbox("Paper account (experiment)", [r.experiment_id for r in exps], format_func=lambda i: f"#{i}")
        strat = st.selectbox("Registered strategy", names)
        symbol = st.text_input("Instrument (symbol)")
        interval = st.selectbox("Timeframe", ["1d"], help="The paper runtime supports daily bars only.")
        params_text = st.text_area("Strategy parameters (JSON object)", value="{}")
        r = DEFAULT_FORM["risk"]; s = DEFAULT_FORM["stop"]; e = DEFAULT_FORM["execution"]
        c1, c2, c3 = st.columns(3)
        alloc = c1.number_input("Allocation per trade (fraction)", value=r["allocation_per_trade_pct"], min_value=0.0, max_value=1.0, step=0.01, format="%.3f")
        expo = c2.number_input("Max portfolio exposure (fraction)", value=r["max_portfolio_exposure_pct"], min_value=0.0, max_value=1.0, step=0.05, format="%.3f")
        riskp = c3.number_input("Risk per trade (fraction)", value=r["risk_pct_per_trade"], min_value=0.0, max_value=1.0, step=0.005, format="%.4f")
        d1, d2 = st.columns(2)
        atr_p = d1.number_input("ATR stop period", value=s["period"], min_value=1, step=1)
        atr_m = d2.number_input("ATR stop multiple", value=s["multiple"], min_value=0.1, step=0.1)
        f1, f2, f3 = st.columns(3)
        slip = f1.number_input("Slippage (bps)", value=e["slippage_bps"], min_value=0.0, step=1.0)
        fee = f2.number_input("Fee (bps)", value=e["fee_bps"], min_value=0.0, step=1.0)
        fixed = f3.number_input("Fixed fee ($)", value=e["fixed_fee"], min_value=0.0, step=0.1)
        st.caption("Execution timing is fixed to SIGNAL_BAR_CLOSE (the only timing the paper runtime supports).")
        submit = st.form_submit_button("Validate and save profile")
    if submit:
        try:
            params = json.loads(params_text or "{}")
            if not isinstance(params, dict):
                raise PaperConfigError("strategy parameters must be a JSON object")
            form = {"interval": interval, "lookback_days": DEFAULT_FORM["lookback_days"],
                    "risk": {"allocation_per_trade_pct": alloc, "max_portfolio_exposure_pct": expo, "risk_pct_per_trade": riskp},
                    "stop": {"type": "atr", "period": int(atr_p), "multiple": atr_m},
                    "execution": {"timing": "SIGNAL_BAR_CLOSE", "slippage_bps": slip, "fee_bps": fee, "fixed_fee": fixed}}
            cfg = svc.build_config(experiment_id=int(exp), strategy_name=strat, symbol=symbol, strategy_params=params, form=form)
            path = svc.save_profile(pname, cfg)
            st.success(f"Saved {path}. Select it in the Run tab.")
        except (PaperConfigError, json.JSONDecodeError) as exc:
            st.error(f"Not saved: {exc}")
