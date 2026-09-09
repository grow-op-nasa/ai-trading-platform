"""Turns deterministic `ResearchFindings` into prose.

Two renderers share one contract (`NarrativeRenderer`): a
`FallbackNarrativeRenderer` that always works (template-based, no
network, no API key needed) and an optional `ClaudeNarrativeRenderer`
that asks Claude to rephrase the SAME findings more naturally. The LLM
is explicitly constrained, via its system prompt, to rephrase only --
never to introduce a fact, number, or claim that isn't already in
`ResearchFindings` (`DECISIONS.md`, ADR-0020). `anthropic` is an
optional, lazily-imported dependency -- deliberately not added to
`requirements.txt` -- so the platform works with zero setup and quietly
upgrades its prose when a key happens to be present.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.research.models import ResearchFindings

_SYSTEM_PROMPT = (
    "You are a research assistant summarizing a completed trading strategy "
    "backtest for a reader who will decide what to do next. You will be "
    "given a list of findings as label/value pairs, and sometimes one "
    "suggested next experiment. Rephrase them into a short, clear, "
    "evidence-grounded paragraph. Rules: never state a number that was not "
    "given to you; never invent a cause, pattern, or claim that is not "
    "present in the findings; do not give investment advice or tell the "
    "reader what to do, beyond restating any given 'next experiment' in "
    "your own words; if you are unsure whether something is supported by "
    "the findings, leave it out."
)


@runtime_checkable
class NarrativeRenderer(Protocol):
    """Anything that can turn findings into prose."""

    def render(self, findings: ResearchFindings) -> str: ...


class FallbackNarrativeRenderer:
    """Deterministic, template-based prose.

    Always available -- no network, no API key, nothing to configure.
    `ResearchReporter` falls back to this whenever a
    `ClaudeNarrativeRenderer` isn't configured, or fails.
    """

    def render(self, findings: ResearchFindings) -> str:
        lines = [f"Research summary for {findings.strategy_name}:"]
        lines.append(", ".join(findings.as_lines()) + ".")
        if findings.recommendation:
            lines.append(findings.recommendation)
        return "\n".join(lines)


class ClaudeNarrativeRenderer:
    """Rephrases `ResearchFindings` via the Claude API.

    Requires `anthropic` to be installed and an API key to be supplied
    -- neither is required to use the platform. `ResearchReporter` only
    reaches for this renderer when both are available, and falls back
    silently otherwise.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-5") -> None:
        self._api_key = api_key
        self._model = model

    def render(self, findings: ResearchFindings) -> str:
        import anthropic  # optional dependency, imported lazily

        client = anthropic.Anthropic(api_key=self._api_key)
        findings_text = "\n".join(findings.as_lines())
        if findings.recommendation:
            findings_text += f"\n{findings.recommendation}"

        response = client.messages.create(
            model=self._model,
            max_tokens=300,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": findings_text}],
        )
        return "".join(
            block.text for block in response.content if hasattr(block, "text")
        )
