"""The local application entry point -- src/dashboard/app.py (Sprint 9 dashboard; Sprint 19 MVP shell, ADR-0054).

    ./scripts/start_app.sh          (or: python -m src.dashboard.launch)

A local-first, single-user Streamlit application over the platform's existing services. Pages read the same persisted
files the CLI uses (through `src.appsvc`) and never reimplement business logic. Opening or refreshing a page never
trades, backtests, sends email, calls an AI provider or mutates a portfolio; the only side effects are the explicit
buttons on Paper Trading, Portfolio & Execution (valuation = price request) and Monitoring & Alerts. Paper only.
"""

from __future__ import annotations

import streamlit as st

from src.appsvc.paths import AppPaths
from src.dashboard import (
    page_activity,
    page_home,
    page_monitoring,
    page_paper,
    page_portfolio,
    page_research,
    page_setup,
    page_strategies,
    shell,
)

st.set_page_config(page_title="AI Trading Platform (paper mode)", layout="wide")

_PAGES = {
    "Home": page_home.render,
    "Research & Experiments": page_research.render,
    "Strategies": page_strategies.render,
    "Paper Trading": page_paper.render,
    "Portfolio & Execution": page_portfolio.render,
    "Activity": page_activity.render,
    "Monitoring & Alerts": page_monitoring.render,
    "Setup & System Status": page_setup.render,
}

paths = AppPaths()
service = shell.paper_service(paths)
page, account = shell.sidebar(paths)
_PAGES[page](paths, service, account)
