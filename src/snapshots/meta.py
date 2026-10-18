"""Metadata recorded with every snapshot (Sprint 22, ADR-0059): dataset description and a software-environment fingerprint.

Plain functions over plain data; no provider, broker, execution, AI or UI imports."""

from __future__ import annotations

import platform
from typing import Any

import numpy as np
import pandas as pd


def environment_fingerprint() -> dict[str, str]:
    """Library versions that can change numerical results. Recorded, compared on replay, never silently ignored."""
    return {"python": platform.python_version(), "pandas": pd.__version__, "numpy": np.__version__}


def snapshot_meta(ds: Any, symbol: str, interval: str) -> dict[str, Any]:
    """JSON metadata stored with the snapshot. Contains no per-run values (no clock), so identical data dedupes."""
    rep = getattr(ds, "validation_report", None)
    gaps = getattr(rep, "gaps", None)
    return {
        "symbol": symbol,
        "interval": str(getattr(getattr(ds, "interval", None), "value", interval)),
        "provider": getattr(ds, "provider", None),
        "content_hash": getattr(ds, "content_hash", None),
        "session_policy": str(getattr(getattr(ds, "session_policy", None), "value", getattr(ds, "session_policy", ""))),
        "session_timezone": getattr(ds, "session_timezone", None),
        "timezone": getattr(ds, "timezone", "UTC"),
        "requested_start": str(getattr(ds, "requested_start", "")),
        "requested_end": str(getattr(ds, "requested_end", "")),
        "dataset_start": str(getattr(ds, "dataset_start", "")),
        "dataset_end": str(getattr(ds, "dataset_end", "")),
        "validation": {"clean": bool(rep.is_clean) if rep is not None else None,
                       "gap_count": int(getattr(gaps, "count", 0)) if gaps is not None else None,
                       "suspicious": list(getattr(rep, "suspicious", ()) or ())[:20] if rep is not None else []},
    }
