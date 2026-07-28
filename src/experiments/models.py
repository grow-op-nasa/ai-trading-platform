"""The Experiment record.

One row of permanent, queryable knowledge: what changed, what happened
to the metrics, and what was decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Experiment:
    """One logged experiment.

    Args:
        id: database id. `None` for an experiment not yet inserted.
        created_at: ISO 8601 UTC timestamp.
        strategy_name: which strategy this experiment was run against,
            if applicable.
        changed: what was different about this run, as
            `{"parameter.path": [before, after]}`, e.g.
            `{"RSI.period": [14, 10]}`. Deliberately a plain dict rather
            than a fixed schema, since what can change (an indicator
            parameter today, a whole strategy swap tomorrow) will vary.
        metrics_before: metric name -> value, prior to the change.
        metrics_after: metric name -> value, after the change.
        decision: "KEEP", "DISCARD", or "INCONCLUSIVE".
        notes: free-text, optional.
    """

    id: int | None
    created_at: str
    strategy_name: str | None
    changed: dict
    metrics_before: dict
    metrics_after: dict
    decision: str
    notes: str = field(default="")

    def summary(self) -> str:
        """A short, human-readable rendering -- the "Experiment #18" view."""
        lines = [f"Experiment #{self.id}" if self.id is not None else "Experiment (unsaved)"]
        if self.strategy_name:
            lines.append(f"Strategy: {self.strategy_name}")

        lines.append("Changed:")
        for key, value in self.changed.items():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                lines.append(f"  {key}: {value[0]} -> {value[1]}")
            else:
                lines.append(f"  {key}: {value}")

        lines.append("Result:")
        for key in sorted(set(self.metrics_before) | set(self.metrics_after)):
            before = self.metrics_before.get(key)
            after = self.metrics_after.get(key)
            lines.append(f"  {key}: {before} -> {after}")

        lines.append(f"Decision: {self.decision}")
        return "\n".join(lines)
