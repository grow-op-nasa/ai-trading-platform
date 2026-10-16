"""Small presentation helpers shared by every page (Sprint 19). No data access, no computation of a financial value."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from src.dashboard.formatting import format_timestamp

UNKNOWN_TEXT = "unknown"


def box(state: str, text: str) -> None:
    """Restrained status presentation. UNKNOWN / not-evaluable is never styled as success."""
    s = (state or "").upper()
    if s in ("OK", "PASS", "MATCH", "HEALTHY", "DELIVERED"):
        st.success(text)
    elif s in ("FAIL", "FAILED", "CRITICAL", "MISMATCH", "ERROR"):
        st.error(text)
    elif s in ("WARN", "WARNING", "BLOCKED", "PENDING", "RETRY_PENDING", "NOT_EVALUABLE"):
        st.warning(text)
    else:
        st.info(text)


def money(value: float | None) -> str:
    return UNKNOWN_TEXT if value is None else f"${value:,.2f}"


def qty(value: float | None) -> str:
    return UNKNOWN_TEXT if value is None else f"{value:,.4g}"


def ts(value: Any) -> str:
    return format_timestamp(value) if value else "none recorded"


def table(rows: list[dict], *, empty: str) -> None:
    if not rows:
        st.info(empty)
        return
    frame = pd.DataFrame(rows).astype(str)      # mixed int/str columns cannot be Arrow-serialised; display text only
    st.dataframe(frame, hide_index=True, width="stretch")


def json_block(value: Any) -> None:
    st.json(value if value is not None else {}, expanded=False)
