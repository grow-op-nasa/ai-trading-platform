"""Research & Experiments (Sprint 19): wraps the existing Sprint 9 views. Historical backtests only; read-only."""

from __future__ import annotations

import streamlit as st

from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.dashboard import shell, ui, views


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Research & Experiments")
    st.caption("These are HISTORICAL BACKTEST results saved by experiment runs -- not paper-trading results and not current monitoring state. "
               "Opening this page never runs a backtest.")
    rows = shell.experiment_rows(paths)
    if not rows:
        st.info("No experiments found yet -- run `python scripts/run_experiment.py` to create one.")
        return
    tab_list, tab_one, tab_cmp = st.tabs(["Experiments", "Analysis", "Compare"])
    db = str(paths.registry)
    ids = [r.experiment_id for r in reversed(rows)]
    with tab_list:
        ui.table([{"Experiment": r.experiment_id, "Strategy": r.strategy_name, "Created": r.created_at, "Decision": r.decision,
                   "Identity recorded": "yes" if r.has_spec else "no", "Equity curve": "yes" if r.has_equity_curve else "no",
                   "Saved portfolio": "yes" if r.has_portfolio else "no", "Notes": r.notes[:80]} for r in reversed(rows)],
                 empty="No experiments.")
        st.caption("Experiments record a decision and notes; they carry no separate failure status. A missing identity or equity curve is shown as 'no', never filled in.")
    with tab_one:
        chosen = st.selectbox("Experiment", ids, format_func=lambda i: f"#{i}", key="research_experiment")
        views.render_experiment_analysis(db, chosen)
    with tab_cmp:
        selected = st.multiselect("Experiments to compare", ids, format_func=lambda i: f"#{i}", key="research_compare")
        views.render_comparison(db, selected)
