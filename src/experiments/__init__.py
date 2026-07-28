"""The Experiment Registry.

Every backtest run becomes a permanent, queryable record -- what
changed, what happened to the metrics, what was decided. Part of the
Sprint 2 research engine (`DECISIONS.md`, ADR-0009). Public entry
points: `ExperimentRegistry`, `Experiment`.
"""

from src.experiments.models import Experiment
from src.experiments.registry import ExperimentRegistry

__all__ = ["ExperimentRegistry", "Experiment"]
