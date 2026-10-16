"""Strategies (Sprint 19): production registry only is paper-eligible; candidates are shown for context. Read-only."""

from __future__ import annotations

import streamlit as st

from src.appsvc import strategies
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.dashboard import shell, ui
from src.dashboard.formatting import format_hash


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Strategies")
    infos = strategies.registered_strategies(paths)
    st.subheader("Registered production strategies")
    if not infos:
        st.info("No strategies are registered.")
    else:
        ui.table([{"Strategy": s.name, "Origin": s.origin, "Version (source hash)": format_hash(s.version_hash) if s.version_hash else "unknown",
                   "Parameters (defaults)": ", ".join(f"{k}={v}" for k, v in s.parameters.items()) or "none",
                   "Experiments": len(s.experiments), "Paper eligible": "yes" if s.paper_eligible else "no"} for s in infos], empty="")
        for s in infos:
            with st.expander(f"{s.name} -- details"):
                st.write(s.summary)
                st.write(f"Implementation: `{s.class_path}`  \nFull source hash: `{s.version_hash or 'unavailable'}`")
                if s.experiments:
                    ui.table([{"Experiment": i, "Decision": d} for i, d in s.experiments], empty="")
                else:
                    st.info("No saved experiments use this strategy, so there is no research evidence for it in this registry.")
                for note in s.limitations:
                    st.caption("Limitation: " + note)
    st.subheader("Development candidates (context only)")
    st.caption("Candidates written by the strategy-development workflow. They can never be selected for a paper run. Only a strategy that has "
               "completed the human approval and registry promotion workflow appears in the list above.")
    cands = strategies.candidates(paths)
    ui.table([{"Candidate": c.candidate_id[:12], "Name": c.name, "Lifecycle status": c.status, "Meaning": c.meaning,
               "Paper eligible": "no"} for c in cands], empty="No development candidates found.")
