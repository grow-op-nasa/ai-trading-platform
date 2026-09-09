"""Data shapes produced by the AI Research Reporter.

Plain dataclasses, no behavior beyond simple derived rendering -- the
`compile_findings()` function (`compiler.py`) does the actual extraction,
and a `NarrativeRenderer` (`renderers.py`) does the actual prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Finding:
    """One evidence-grounded fact the compiler extracted.

    This is the atomic unit a renderer is allowed to talk about --
    nothing in a `ResearchReport`'s narrative should say anything a
    `Finding` doesn't already say (`DECISIONS.md`, ADR-0020).
    """

    label: str
    value: str


@dataclass
class ResearchFindings:
    """Everything `compile_findings()` extracted from a `BacktestResult`
    and an `AttributionReport` -- purely deterministic, no prose yet."""

    strategy_name: str
    findings: list[Finding] = field(default_factory=list)
    recommendation: str | None = None

    def as_lines(self) -> list[str]:
        """Each finding as a "Label: value" string, in extraction order."""
        return [f"{f.label}: {f.value}" for f in self.findings]


@dataclass
class ResearchReport:
    """The final output of the AI Research Reporter: the deterministic
    findings plus one rendered narrative -- a research recommendation
    for a person to weigh, never a trading decision (ADR-0017)."""

    findings: ResearchFindings
    narrative: str
    rendered_by: str  # "fallback" or "claude"
