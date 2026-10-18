"""Activity (Sprint 20): one chronological, read-only view over stored records. Relationships are shown only where a
stored id says so; filters only hide rows."""

from __future__ import annotations

import streamlit as st

from src.appsvc import activity
from src.appsvc.paper import PaperService
from src.appsvc.paths import AppPaths
from src.dashboard import shell, ui


def render(paths: AppPaths, svc: PaperService, account: int | None) -> None:
    shell.header("Activity")
    st.caption("Paper events are for the selected account; monitoring and alert events are platform-wide. Nothing here changes anything.")
    c1, c2 = st.columns(2)
    cats = c1.multiselect("Event types", activity.CATEGORIES, key="act_cats", placeholder="All")
    all_events = activity.timeline(paths, account, categories=tuple(cats) or None)
    statuses = sorted({e.status for e in all_events})
    picked = c2.multiselect("Status", statuses, key="act_status", placeholder="All")
    events = [e for e in all_events if not picked or e.status in picked]
    if account is None:
        st.info("No paper account is selected, so paper cycles, proposals, orders and fills are not shown.")
    if not events:
        st.info("No recorded activity matches.")
        return
    ui.table([{"When": ui.ts(e.when), "Type": e.category, "Event": e.title, "Status": e.status, "Severity": e.severity,
               "Linked ids": ", ".join(f"{k}={str(v)[:12]}" for k, v in e.refs.items())} for e in events], empty="")
    idx = st.selectbox("Drill into an event", range(len(events)), index=None, placeholder="Select an event", key="act_pick",
                       format_func=lambda i: f"{ui.ts(events[i].when)}  {events[i].category}: {events[i].title}")
    if idx is None:
        return
    e = events[idx]
    st.markdown(f"**{e.title}**")
    if e.detail:
        st.write(e.detail)
    st.caption(f"Source: {e.source}. Status as stored: {e.status}.")
    ui.json_block({k: v for k, v in e.refs.items()})
    related = [x for x in events if x is not e and any(x.refs.get(k) == v for k, v in e.refs.items())]
    if related:
        st.markdown("Records sharing a stored id with this event:")
        ui.table([{"When": ui.ts(x.when), "Type": x.category, "Event": x.title, "Status": x.status} for x in related], empty="")
    else:
        st.caption("No other recorded event shares a stored id with this one.")
