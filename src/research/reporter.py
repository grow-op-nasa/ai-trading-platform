"""The AI Research Reporter -- Sprint 3 Module 4.

    from src.research import ResearchReporter

    reporter = ResearchReporter()
    report = reporter.run(result, attribution)
    print(report.narrative)

Deliberately two-stage (`DECISIONS.md`, ADR-0020): `compile_findings()`
extracts evidence-grounded facts (fully deterministic, fully testable
without any network access), then a `NarrativeRenderer` turns those
facts into prose. `ResearchReporter` picks a `ClaudeNarrativeRenderer`
when `ANTHROPIC_API_KEY` is set and `anthropic` is installed, and a
`FallbackNarrativeRenderer` otherwise -- the platform never depends on
an API key to produce a research report, only to make its prose read a
little more naturally. A research report is a recommendation for a
person to weigh, never a trading decision (ADR-0017).

A `ClaudeNarrativeRenderer` failure still falls back to the
deterministic renderer -- `run()` never raises just because the
optional AI prose failed -- but the failure is recorded, not hidden:
`ResearchReport.renderer_error` is set whenever the fallback happened
*because of* a real failure, and stays `None` when it's simply the
normal, unconfigured-Claude path (`DECISIONS.md`, ADR-0034).
"""

from __future__ import annotations

import os

from src.attribution.models import AttributionReport
from src.backtesting.models import BacktestResult
from src.research.compiler import compile_findings
from src.research.models import ResearchReport
from src.research.renderers import FallbackNarrativeRenderer, NarrativeRenderer


class ResearchReporter:
    """Turns a completed backtest + attribution report into a `ResearchReport`."""

    def __init__(self, renderer: NarrativeRenderer | None = None) -> None:
        """Args:
        renderer: override the auto-selected renderer (mainly for
            tests). Left `None` in normal use so `run()` picks Claude
            when available and the deterministic fallback otherwise.
        """
        self._renderer = renderer

    def run(
        self, result: BacktestResult, attribution: AttributionReport
    ) -> ResearchReport:
        findings = compile_findings(result, attribution)
        renderer = self._renderer or self._default_renderer()
        renderer_error: str | None = None

        try:
            narrative = renderer.render(findings)
            rendered_by = "fallback" if isinstance(renderer, FallbackNarrativeRenderer) else "claude"
        except Exception as exc:
            # A renderer failure (missing package, network error, bad
            # key, API outage, malformed response, timeout) falls back
            # rather than losing the report entirely -- the deterministic
            # findings always matter more than the prose describing them.
            # The failure is not swallowed, though (DECISIONS.md,
            # ADR-0034): `renderer_error` records what went wrong, so an
            # operator can tell "fallback because no API key was
            # configured" apart from "fallback because Claude actually
            # failed" -- both currently look identical from `rendered_by`
            # alone.
            narrative = FallbackNarrativeRenderer().render(findings)
            rendered_by = "fallback"
            renderer_error = str(exc)

        return ResearchReport(
            findings=findings,
            narrative=narrative,
            rendered_by=rendered_by,
            renderer_error=renderer_error,
        )

    def _default_renderer(self) -> NarrativeRenderer:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return FallbackNarrativeRenderer()
        try:
            import anthropic  # noqa: F401  (only checking availability)
        except ImportError:
            return FallbackNarrativeRenderer()

        from src.research.renderers import ClaudeNarrativeRenderer

        return ClaudeNarrativeRenderer(api_key=api_key)
