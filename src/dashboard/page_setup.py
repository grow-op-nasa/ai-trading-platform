"""Setup & System Status (Sprint 19): readiness and operating state, without secrets."""

from __future__ import annotations

import streamlit as st

from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.appsvc.system import collect_status
from src.dashboard import shell, ui


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Setup & System Status")
    items = collect_status(paths)
    for i in items:
        ui.box(i.state, f"**{i.name}** -- {i.detail}" + (f"  \n{i.help}" if i.help else ""))
    st.subheader("Where things are stored")
    ui.table([{"Item": k, "Location": str(v)} for k, v in [("Experiment registry", paths.registry), ("Paper runtime database", paths.paper_db),
              ("Monitoring database", paths.monitoring_db), ("Paper-run profiles", paths.profiles_dir), ("Logs", "logs/")]], empty="")
    st.subheader("What is running")
    st.markdown(
        "- **This app** only reads stored records until you press an action button.\n"
        "- **Paper trading is not continuous.** A cycle runs only when you run one (here or with `paper run --once`).\n"
        "- **Monitoring is not running** unless a scheduler is listed above as RUNNING; closing this app does not stop or start one.\n"
        "- **Market data is not live here.** Prices are requested only by an explicit preflight or valuation.\n"
        "- **Alert delivery** works only if alerting is enabled, valid, and a real delivery has been confirmed.\n"
        "- **No live broker and no AI call** is reachable from this app.")
    with st.expander("Troubleshooting"):
        st.markdown(
            "- *Dependencies missing*: run `scripts/setup_app.sh`.\n"
            "- *Registry or paper database not found*: the app never creates them. Create experiments with `scripts/run_experiment.py`; "
            "initialise a paper account on the Paper Trading page.\n"
            "- *Alert configuration invalid*: set the `ATP_ALERT_*` variables in your `.env` (see README).\n"
            "- *Preflight fails*: read the failing check on the Paper Trading page; it names the problem.")
