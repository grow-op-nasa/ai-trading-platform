"""Small, shared display-formatting helpers.

Promoted here from `src/attribution/models.py` once a second module
(`src/research`) needed the same duration formatting -- the same
reasoning as `DECISIONS.md` ADR-0008's `CacheManager` extraction: don't
let a second consumer reinvent the first one's logic.
"""

from __future__ import annotations

import pandas as pd


def format_timedelta(delta: pd.Timedelta) -> str:
    """Render a duration in whichever unit reads most naturally.

    e.g. 23 minutes -> "23 min", 5 hours -> "5.0h", 3 days -> "3.0 days".
    """
    total_minutes = delta.total_seconds() / 60
    if total_minutes < 90:
        return f"{total_minutes:.0f} min"
    total_hours = total_minutes / 60
    if total_hours < 48:
        return f"{total_hours:.1f}h"
    return f"{total_hours / 24:.1f} days"
