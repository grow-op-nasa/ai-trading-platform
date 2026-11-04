"""Strategy Research (Sprint 23, ADR-0061): the strategy catalogue, historical experiments, scorecards and comparison.

Opening or refreshing this page runs nothing. A research run happens only when you press "Run research", and it only
reads market data and writes an immutable research record. It never places orders or creates intents, proposals, cycles
or fills, never promotes a strategy, and never changes a watchlist. Historical research is separate from the live scanner.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import streamlit as st

from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.strategy_research import (
    MAX_BATCH_SYMBOLS,
    POINT_IN_TIME_NOTE,
    StrategyResearchService,
    default_assumptions,
)
from src.dashboard import shell, ui

_METRICS = [
    ("closed_trades", "Closed trades", "int"), ("open_trades_at_end", "Open at end of window", "int"),
    ("win_rate", "Win rate", "pct"), ("average_win", "Average winning trade ($, net)", "usd"),
    ("average_loss", "Average losing trade ($, net)", "usd"), ("payoff_ratio", "Average risk/reward (win / loss)", "num"),
    ("expectancy", "Expectancy ($ per trade, net)", "usd"), ("profit_factor", "Profit factor (net)", "num"),
    ("gross_return", "Gross return (no costs)", "pct"), ("net_return", "Net return (after simulated costs)", "pct"),
    ("cost_drag", "Cost drag (gross - net)", "pct"), ("net_return_at_stress_costs", "Net return at 3x costs", "pct"),
    ("total_costs", "Total transaction costs ($)", "usd"), ("annualised_return", "Annualised return", "pct"),
    ("annualised_volatility", "Annualised volatility", "pct"), ("sharpe", "Sharpe ratio", "num"), ("sortino", "Sortino ratio", "num"),
    ("max_drawdown", "Maximum drawdown", "pct"), ("average_holding_days", "Average holding period (days)", "num"),
    ("turnover", "Turnover (annual traded notional / equity)", "num"), ("exposure_time", "Time invested", "pct"),
    ("benchmark_return", "Buy-and-hold, same instrument and window", "pct"), ("excess_return", "Net return minus buy-and-hold", "pct"),
]


def _pct(v: float | None) -> str:
    return "N/A" if v is None else f"{v:.2%}"


def _num(v: float | None) -> str:
    return "N/A" if v is None else f"{v:.2f}"


def _fmt(m: dict | None, kind: str) -> str:
    if m is None:
        return "N/A"
    if isinstance(m, (int, float)):
        return str(m)
    if m.get("status") != "ok":
        return f"N/A ({m.get('reason') or 'undefined'})"
    v = m["value"]
    return {"pct": f"{v:.2%}", "usd": f"${v:,.2f}", "num": f"{v:.2f}", "int": str(int(v))}[kind]


def scorecard_rows(card: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for key, label, kind in _METRICS:
        value = card.get(key)
        rows.append({"Metric": label, "Value": str(value) if key in ("closed_trades", "open_trades_at_end") else _fmt(value, kind)})
    return rows


def _show_card(card: dict[str, Any], title: str) -> None:
    st.markdown(f"**{title}** -- {card['first_bar'][:10]} to {card['last_bar'][:10]} ({card['bars']} bars), sample: {card['sample']}")
    ui.table(scorecard_rows(card), empty="")
    for note in card.get("notes", []):
        st.caption("Note: " + note)


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Strategy Research")
    rs = shell.research_service(paths)
    st.warning("HISTORICAL RESEARCH. Nothing on this page places orders or creates paper records, and no result promotes a strategy. "
               "Results describe the past under simulated costs; they are not predictions.")
    tab_cat, tab_run, tab_res, tab_cmp = st.tabs(["Strategy catalogue", "Run research", "Results & scorecards", "Compare runs"])
    with tab_cat:
        _catalogue(rs)
    with tab_run:
        _run_form(rs)
    with tab_res:
        _results(rs)
    with tab_cmp:
        _compare(rs)


# ---------------------------------------------------------------------------------------------------- catalogue
def _catalogue(rs: StrategyResearchService) -> None:
    view = rs.catalogue()
    st.caption("Strategies are grouped by the hypothesis they test. Two strategies using different indicators are not independent "
               "if they test the same idea (a variant is labelled).")
    ui.table([{"Strategy": e.strategy_id, "Name": e.name, "Family": e.family.value,
               "Variant of": e.metadata.variant_of or "", "Direction": e.metadata.directionality.value,
               "Intervals": ", ".join(e.metadata.supported_intervals) or "none declared",
               "Minimum history (bars)": (e.history_requirement or {}).get("min_bars", "n/a"),
               "Implementation": e.maturity.value, "Research state": view.research_states.get(e.strategy_id, "EXPERIMENTAL")}
              for e in view.entries], empty="No strategies are registered.")
    covered = {f.value: ids for f, ids in view.families.items() if ids}
    missing = [f.value for f, ids in view.families.items() if not ids]
    st.caption("Families represented: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in covered.items()))
    st.caption("Not yet represented: " + (", ".join(missing) or "none"))
    for e in view.entries:
        with st.expander(f"{e.strategy_id} -- details"):
            m = e.metadata
            st.write(f"**Hypothesis.** {m.hypothesis}")
            st.write(f"**Entry.** {m.entry}")
            st.write(f"**Exit.** {m.exit}")
            st.write(f"**Regime assumptions.** {m.regime_assumptions}")
            for w in m.known_weaknesses:
                st.caption("Known weakness: " + w)
            st.write("**Parameters (defaults).** " + (", ".join(f"{p.name}={p.default}" for p in e.parameters) or "none"))
            st.write("**Declared history requirement.** " + (e.history_note or json.dumps((e.history_requirement or {}).get("min_bars"))))
            st.caption(f"Implementation `{e.class_path}`, source hash `{(e.source_hash or 'unavailable')[:12]}`.")
            if not m.researchable:
                st.info("Not runnable here: " + m.researchable_note)


# ---------------------------------------------------------------------------------------------------- run
def _run_form(rs: StrategyResearchService) -> None:
    view = rs.catalogue()
    runnable = [e for e in view.entries if e.metadata.researchable]
    if not runnable:
        st.info("No researchable strategies are registered.")
        return
    st.subheader("Experiment")
    by_id = {e.strategy_id: e for e in runnable}
    strategy = st.selectbox("Strategy", list(by_id), key="sr_strategy")
    entry = by_id[strategy]
    st.caption(entry.metadata.hypothesis)
    universe = rs.universe()
    lists = rs.watchlists()
    source = st.radio("Instruments", ["Type symbols", "A saved watchlist (read-only)", "Curated research universe"], key="sr_source", horizontal=True)
    if source == "Type symbols":
        text = st.text_input("Symbols (comma separated)", "SPY", key="sr_symbols")
        symbols = [s.strip().upper() for s in text.split(",") if s.strip()]
    elif source == "A saved watchlist (read-only)":
        if not lists:
            st.info("You have no saved watchlists. Research never creates or changes one.")
            symbols = []
        else:
            name = st.selectbox("Watchlist", list(lists), key="sr_wl")
            symbols = list(lists[name])
    else:
        group = st.multiselect("Groups", list(universe["groups"]), default=["Major index ETFs"], key="sr_groups")
        symbols = [s for g in group for s in universe["groups"][g]]
        st.caption(universe["note"])
    st.caption(f"{len(symbols)} instrument(s) selected; at most {MAX_BATCH_SYMBOLS} per batch, evaluated one at a time. "
               "Each is an independent evaluation -- there is no combined portfolio result.")
    c1, c2 = st.columns(2)
    start = c1.date_input("Start date", date.today() - timedelta(days=365 * 8), key="sr_start")
    end = c2.date_input("End date", date.today() - timedelta(days=1), key="sr_end")
    st.markdown("**Configuration(s)** -- pre-specified before any result is seen (at most 5; each one is recorded).")
    default_params = json.dumps({p.name: p.default for p in entry.parameters if p.name != "confidence"})
    raw = st.text_area("Parameter sets (one JSON object per line; blank line = strategy defaults)", default_params, key=f"sr_cfg_{strategy}")
    with st.expander("Execution and cost assumptions (identical for every strategy)"):
        a = default_assumptions()
        st.write(a.describe_costs())
        st.json(a.to_dict(), expanded=False)
        st.caption("Chronological split: 50% development, 25% validation, 25% holdout of the evaluable bars. Only development and "
                   "validation bars are used to choose between configurations; the holdout is evaluated once, afterwards.")
    if entry.history_requirement:
        st.caption("Declared history requirement: " + str((entry.history_requirement or {}).get("min_bars")) + " complete bars.")
    if st.button("Run research", key="sr_go", type="primary", disabled=not symbols):
        try:
            configs = [json.loads(line) for line in raw.splitlines() if line.strip()] or [{}]
            if not all(isinstance(c, dict) for c in configs):
                raise ValueError("each line must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            st.error(f"Could not read the parameter sets: {exc}")
            return
        bar = st.progress(0.0, text="Starting...")
        def progress(i: int, n: int, sym: str) -> None:
            bar.progress(min(i / max(n, 1), 1.0), text=f"Evaluating {sym}..." if sym else "Done")
        try:
            outcomes, cov = rs.run_batch(strategy, symbols, start, end, configs, progress=progress)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.session_state["sr_last_batch"] = [o.run_id for o in outcomes.values()]
        st.success(f"{len(outcomes)} evaluation(s) recorded. Open 'Results & scorecards' to inspect them.")
        ui.table([{"Symbol": r.symbol, "Coverage": r.status, "Detail": r.detail, "Run": r.run_id} for r in cov], empty="")
        st.caption(POINT_IN_TIME_NOTE)


# ---------------------------------------------------------------------------------------------------- results
def _results(rs: StrategyResearchService) -> None:
    runs = rs.runs()
    if not runs:
        st.info("No research runs yet. Use 'Run research' to create one.")
        return
    ui.table([{"Run": r["run_id"], "Kind": r["kind"], "Strategy": r["strategy_name"], "Symbol": r["symbol"], "Status": r["status"],
               "Research state": r["state"], "Created": ui.ts(r["created_at"])} for r in runs], empty="")
    chosen = st.selectbox("Inspect a run", [r["run_id"] for r in runs], key="sr_inspect")
    detail = rs.run_detail(chosen)
    if detail is None:
        return
    rec = detail["payload"]
    if rec["status"] != "COMPLETED":
        ui.box("NOT_EVALUABLE", f"Not evaluable ({rec['reason_code']}): {rec['reason']}")
        st.caption("This is an explanation, not a result: no scorecard exists for a run that could not be evaluated.")
        return
    if detail["kind"] == "WALK_FORWARD":
        _walk_forward_detail(rec)
        return
    ui.box("INFO", f"Research state: {detail['state']} -- {detail['state_reason']}.")
    sel = rec["selection"]
    st.write(f"{sel['configurations_attempted']} configuration(s) attempted. {sel['reason']}. Selection rule: {sel['rule']}.")
    chosen_attempt = rec["attempts"][sel["selected_index"]]
    st.write(f"Selected configuration: `{json.dumps(rec['selected']['params'])}` (hash `{rec['selected']['params_hash']}`)")
    _show_card(chosen_attempt["scorecards"]["development"], "Development")
    _show_card(chosen_attempt["scorecards"]["validation"], "Validation")
    ho = detail["holdout"]
    if ho is None:
        st.info("The holdout (the most recent 25% of evaluable bars) is locked. It can be evaluated once, on the configuration "
                "already selected above.")
        if st.button("Evaluate the holdout (one time only)", key=f"sr_ho_{chosen}"):
            try:
                rs.evaluate_holdout(chosen)
                st.rerun()
            except Exception as exc:  # noqa: BLE001 - shown to the user, never swallowed
                st.error(f"{type(exc).__name__}: {exc}")
    else:
        _show_card(ho["payload"]["scorecard"], "Holdout (out of sample)")
        for line in ho["payload"]["assessment"]:
            st.caption("Assessment: " + line)
    with st.expander("Performance by calendar year and regime (development + validation)"):
        ui.table([{"Year": y["year"], "Bars": y["bars"], "Net return": _fmt(y["net_return"], "pct"),
                   "Buy-and-hold": _fmt(y["benchmark_return"], "pct")} for y in rec["selected"]["yearly"]], empty="")
        for axis, rows in rec["selected"]["regimes"].items():
            st.caption(axis.replace("_", " "))
            ui.table([{"Regime": r["regime"], "Bars": r["bars"], "Net return": f"{r['net_return']:.2%}"} for r in rows], empty="")
        st.caption("Regimes use the existing trend (trending/ranging) and volatility axes. A bar uses the regime known at the previous close.")
    with st.expander("Provenance and limitations"):
        st.json({k: rec[k] for k in ("strategy", "provenance", "history", "boundaries", "assumptions", "cost_note")}, expanded=False)
        for lim in rec["limitations"]:
            st.caption("Limitation: " + lim)
    if st.button("Replay from the stored snapshot (offline)", key=f"sr_rep_{chosen}"):
        st.json(rs.reproduce(chosen), expanded=True)


def _walk_forward_detail(rec: dict[str, Any]) -> None:
    ag = rec["aggregate"]
    st.write(f"Walk-forward: {ag['folds']} folds, {ag['out_of_sample_closed_trades']} out-of-sample closed trades, chained "
             f"out-of-sample net return {ag['out_of_sample_net_return_chained']:.2%}; {ag['configurations_attempted']} configuration(s) attempted.")
    st.caption(rec["method"])
    ui.table([{"Fold": f["fold"], "Train": f"{f['train_first_bar'][:10]} to {f['train_last_bar'][:10]}",
               "Test": f"{f['test_first_bar'][:10]} to {f['test_last_bar'][:10]}", "Selected": json.dumps(f["selected_params"]),
               "Test trades": f["test_scorecard"]["closed_trades"], "Test net return": _fmt(f["test_scorecard"]["net_return"], "pct")}
              for f in rec["folds"]], empty="")


# ---------------------------------------------------------------------------------------------------- compare
def _compare(rs: StrategyResearchService) -> None:
    runs = [r for r in rs.runs("SINGLE") if r["status"] == "COMPLETED"]
    if not runs:
        st.info("Complete at least one research run to compare runs.")
        return
    label = {r["run_id"]: f"{r['run_id']}  {r['strategy_name']} {r['symbol']}" for r in runs}
    picked = st.multiselect("Runs to compare", list(label), format_func=lambda i: label[i], key="sr_cmp")
    if not picked:
        return
    rows, warnings = rs.compare(picked)
    for w in warnings:
        st.warning(w)
    sort_by = st.selectbox("Sort by", ["Run order", "Net return", "Expectancy", "Profit factor", "Holdout net return", "Trades"], key="sr_sort")
    keys = {"Net return": "net_return", "Expectancy": "expectancy", "Profit factor": "profit_factor",
            "Holdout net return": "holdout_net_return", "Trades": "closed_trades"}
    if sort_by in keys:
        rows = sorted(rows, key=lambda r: (getattr(r, keys[sort_by]) is None, -(getattr(r, keys[sort_by]) or 0)))
    ui.table([{"Run": r.run_id, "Strategy": r.strategy, "Family": r.family, "Instrument": r.symbol, "Parameters": json.dumps(r.params),
               "Period (dev + validation)": r.period, "Closed trades": r.closed_trades, "Win rate": _pct(r.win_rate),
               "Expectancy ($)": _num(r.expectancy), "Profit factor": _num(r.profit_factor), "Net return": _pct(r.net_return),
               "Max drawdown": _pct(r.max_drawdown), "Holdout net return": _pct(r.holdout_net_return),
               "Holdout trades": r.holdout_trades if r.holdout_trades is not None else "not evaluated",
               "Attempts": r.selection_attempts, "Research state": r.state} for r in rows], empty="")
    st.caption("No composite score or ranking is computed: sort by any one column, and read the sample sizes.")
