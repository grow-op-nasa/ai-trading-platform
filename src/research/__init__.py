"""The AI Research Reporter -- Sprint 3 Module 4.

Turns a completed backtest into an evidence-grounded research summary:
deterministic findings (`compile_findings`) plus one rendered narrative
(`ResearchReporter`). See `DECISIONS.md`, ADR-0020.
"""

from src.research.compiler import compile_findings
from src.research.models import Finding, ResearchFindings, ResearchReport
from src.research.renderers import (
    ClaudeNarrativeRenderer,
    FallbackNarrativeRenderer,
    NarrativeRenderer,
)
from src.research.reporter import ResearchReporter

__all__ = [
    "ResearchReporter",
    "ResearchReport",
    "ResearchFindings",
    "Finding",
    "compile_findings",
    "NarrativeRenderer",
    "FallbackNarrativeRenderer",
    "ClaudeNarrativeRenderer",
]
