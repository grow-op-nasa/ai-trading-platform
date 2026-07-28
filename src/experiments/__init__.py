"""The Experiment Registry.

Every backtest run becomes a permanent, queryable record -- what
changed, what happened to the metrics, what was decided. Part of the
Sprint 2 research engine (`DECISIONS.md`, ADR-0009). Public entry
points: `ExperimentRegistry`, `Experiment`.

Also owns `Signal` storage (`DECISIONS.md`, ADR-0016):
`save_signals()` / `get_signals()` / `get_signal()` persist and look up
the `Signal` objects (`src/signals/`) behind a `Trade`'s
`entry_signal_id` / `exit_signal_id`, so Performance Attribution and
the AI Research Reporter can trace a trade back to the evidence that
opened it.
"""

from src.experiments.models import Experiment
from src.experiments.registry import ExperimentRegistry

__all__ = ["ExperimentRegistry", "Experiment"]
