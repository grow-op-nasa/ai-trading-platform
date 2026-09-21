"""The Streamlit dashboard entrypoint -- src/dashboard/app.py (Sprint 9,
`DECISIONS.md` ADR-0042).

    streamlit run src/dashboard/app.py

A read-only Streamlit research and paper-portfolio analytics interface
over the platform's existing backtesting, experiment, strategy,
market-data, and portfolio capabilities -- **not** a production trading
terminal. Opening it, selecting an experiment, comparing experiments,
or refreshing the paper portfolio never submits an order, changes a
risk limit, or mutates any `Portfolio`/`Experiment`/`Strategy` state;
there is no button anywhere in this package that does any of those
things.

This file is deliberately thin: it owns page selection and the
experiment/comparison selectors, and delegates every actual
computation and render to `src.dashboard.views`, which in turn never
computes a metric itself -- that's `src.analytics`'s job. See that
package's module docstring for the full architecture.
"""

from __future__ import annotations

import streamlit as st

from src.dashboard import views
from src.experiments.registry import DEFAULT_DB_PATH

st.set_page_config(page_title="AI Trading Platform -- Research Dashboard", layout="wide")

DB_PATH = str(DEFAULT_DB_PATH)

st.sidebar.title("AI Trading Platform")
st.sidebar.caption(
    "Read-only research & paper-portfolio dashboard over backtests, "
    "experiments, and paper-traded strategies. Not connected to any live "
    "broker and cannot place orders."
)

PAGES = ("Overview", "Backtest / Experiment Analysis", "Strategy Comparison", "Paper Portfolio")
page = st.sidebar.radio("View", PAGES)

experiment_ids = views.list_experiment_ids(DB_PATH)

if not experiment_ids:
    st.info("No experiments found yet -- run `python scripts/run_experiment.py` to create one.")
elif page == "Overview":
    views.render_overview(DB_PATH)
elif page == "Backtest / Experiment Analysis":
    experiment_id = st.sidebar.selectbox(
        "Experiment", experiment_ids, format_func=lambda i: f"#{i}"
    )
    views.render_experiment_analysis(DB_PATH, experiment_id)
elif page == "Strategy Comparison":
    selected = st.sidebar.multiselect(
        "Experiments to compare", experiment_ids, format_func=lambda i: f"#{i}"
    )
    views.render_comparison(DB_PATH, selected)
elif page == "Paper Portfolio":
    experiment_id = st.sidebar.selectbox(
        "Experiment", experiment_ids, format_func=lambda i: f"#{i}", key="paper_portfolio_experiment"
    )
    views.render_paper_portfolio(DB_PATH, experiment_id)
