"""Tests for the AI Research Reporter (src/research).

`compile_findings()` is tested against `AttributionReport`/`BacktestResult`
objects built directly (not a real backtest) since it only needs their
shape, and the whole point of this module is that its output is
deterministic and doesn't require a real strategy run to verify.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.attribution.models import AttributionReport, RegimeStats
from src.backtesting.models import BacktestResult
from src.research.compiler import compile_findings
from src.research.models import Finding, ResearchFindings, ResearchReport
from src.research.renderers import ClaudeNarrativeRenderer, FallbackNarrativeRenderer
from src.research.reporter import ResearchReporter


def make_result(strategy_name: str = "ema_cross", **metrics) -> BacktestResult:
    return BacktestResult(
        strategy_name=strategy_name,
        trades=[],
        equity_curve=pd.Series(dtype=float),
        metrics=metrics,
    )


def make_attribution(**kwargs) -> AttributionReport:
    defaults = dict(
        total_trades=0,
        winning_trades=0,
        win_rate=None,
        average_hold=None,
        regime_breakdown={},
        best_regime=None,
        worst_regime=None,
    )
    defaults.update(kwargs)
    return AttributionReport(**defaults)


# ---------------------------------------------------------------------------
# compile_findings
# ---------------------------------------------------------------------------


def test_compile_findings_includes_strategy_and_trade_count():
    result = make_result(strategy_name="ema_cross")
    attribution = make_attribution(total_trades=12)

    findings = compile_findings(result, attribution)

    assert findings.strategy_name == "ema_cross"
    assert Finding("Strategy", "ema_cross") in findings.findings
    assert Finding("Trades", "12") in findings.findings


def test_compile_findings_omits_absent_metrics():
    result = make_result()  # no sharpe, no max_drawdown_pct
    attribution = make_attribution(total_trades=3, win_rate=None, average_hold=None)

    findings = compile_findings(result, attribution)

    labels = [f.label for f in findings.findings]
    assert "Win Rate" not in labels
    assert "Sharpe" not in labels
    assert "Max Drawdown" not in labels
    assert "Average Hold" not in labels


def test_compile_findings_includes_present_metrics_with_expected_formatting():
    result = make_result(sharpe=1.428, max_drawdown_pct=-0.123)
    attribution = make_attribution(
        total_trades=10,
        win_rate=0.6,
        average_hold=pd.Timedelta(hours=2),
        best_regime="Trending + Low Volatility",
        worst_regime="Ranging + Volatile",
        regime_breakdown={
            "trending_low_volatility": RegimeStats(
                "Trending + Low Volatility", 6, 0.83, 0.02
            ),
            "ranging_volatile": RegimeStats("Ranging + Volatile", 4, 0.25, -0.03),
        },
    )

    findings = compile_findings(result, attribution)
    lines = findings.as_lines()

    assert "Win Rate: 60.0%" in lines
    assert "Sharpe: 1.43" in lines
    assert "Max Drawdown: -12.30%" in lines
    assert "Average Hold: 2.0h" in lines
    assert "Best Regime: Trending + Low Volatility" in lines
    assert "Worst Regime: Ranging + Volatile" in lines


def test_compile_findings_recommends_next_experiment_when_worst_regime_is_a_loser():
    result = make_result()
    attribution = make_attribution(
        total_trades=10,
        best_regime="Trending + Low Volatility",
        worst_regime="Ranging + Volatile",
        regime_breakdown={
            "trending_low_volatility": RegimeStats(
                "Trending + Low Volatility", 6, 0.83, 0.02
            ),
            "ranging_volatile": RegimeStats("Ranging + Volatile", 4, 0.25, -0.03),
        },
    )

    findings = compile_findings(result, attribution)

    assert findings.recommendation is not None
    assert "Ranging + Volatile" in findings.recommendation
    assert "4 trade(s)" in findings.recommendation
    assert "-3.00%" in findings.recommendation


def test_compile_findings_no_recommendation_when_no_regime_breakdown():
    result = make_result()
    attribution = make_attribution(total_trades=5)

    findings = compile_findings(result, attribution)

    assert findings.recommendation is None


def test_compile_findings_no_recommendation_when_worst_regime_is_profitable():
    result = make_result()
    attribution = make_attribution(
        total_trades=10,
        best_regime="A",
        worst_regime="B",
        regime_breakdown={
            "a": RegimeStats("A", 6, 0.9, 0.05),
            "b": RegimeStats("B", 4, 0.6, 0.01),  # worst of the two, but still positive
        },
    )

    findings = compile_findings(result, attribution)

    assert findings.recommendation is None


def test_compile_findings_never_mentions_session_of_day():
    # Session-of-day attribution is deliberately unbuilt (ADR-0006 /
    # ADR-0019) -- the compiler must never fabricate a claim about it.
    result = make_result()
    attribution = make_attribution(
        total_trades=10,
        best_regime="A",
        worst_regime="B",
        regime_breakdown={
            "a": RegimeStats("A", 6, 0.9, 0.05),
            "b": RegimeStats("B", 4, 0.2, -0.05),
        },
    )

    findings = compile_findings(result, attribution)
    full_text = " ".join(findings.as_lines()) + " " + (findings.recommendation or "")

    for forbidden in ("09:50", "market open", "morning", "session"):
        assert forbidden not in full_text.lower()


# ---------------------------------------------------------------------------
# FallbackNarrativeRenderer
# ---------------------------------------------------------------------------


def test_fallback_renderer_includes_strategy_name_and_findings():
    findings = ResearchFindings(
        strategy_name="ema_cross",
        findings=[Finding("Trades", "10"), Finding("Win Rate", "60.0%")],
    )

    narrative = FallbackNarrativeRenderer().render(findings)

    assert "ema_cross" in narrative
    assert "Trades: 10" in narrative
    assert "Win Rate: 60.0%" in narrative


def test_fallback_renderer_includes_recommendation_when_present():
    findings = ResearchFindings(
        strategy_name="ema_cross",
        findings=[Finding("Trades", "10")],
        recommendation="Next experiment: do the thing.",
    )

    narrative = FallbackNarrativeRenderer().render(findings)

    assert "Next experiment: do the thing." in narrative


# ---------------------------------------------------------------------------
# ClaudeNarrativeRenderer -- mocked, no real network call
# ---------------------------------------------------------------------------


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessages:
    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.last_call: dict | None = None

    def create(self, **kwargs):
        self.last_call = kwargs

        class _Response:
            pass

        response = _Response()
        response.content = [_FakeTextBlock(self._response_text)]
        return response


class _FakeAnthropicClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.messages = _FakeMessages("A rephrased research summary.")


class _FakeAnthropicModule:
    Anthropic = _FakeAnthropicClient


def test_claude_renderer_returns_response_text(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", _FakeAnthropicModule())

    findings = ResearchFindings(
        strategy_name="ema_cross", findings=[Finding("Trades", "10")]
    )
    renderer = ClaudeNarrativeRenderer(api_key="sk-fake")

    narrative = renderer.render(findings)

    assert narrative == "A rephrased research summary."


def test_claude_renderer_call_contains_findings_text(monkeypatch):
    import sys

    captured = {}

    class _CapturingMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Response:
                content = [_FakeTextBlock("ok")]

            return _Response()

    class _CapturingClient:
        def __init__(self, api_key: str) -> None:
            self.messages = _CapturingMessages()

    class _CapturingModule:
        Anthropic = _CapturingClient

    monkeypatch.setitem(sys.modules, "anthropic", _CapturingModule())

    findings = ResearchFindings(
        strategy_name="ema_cross",
        findings=[Finding("Trades", "10")],
        recommendation="Next experiment: do the thing.",
    )
    ClaudeNarrativeRenderer(api_key="sk-fake").render(findings)

    assert "Trades: 10" in captured["messages"][0]["content"]
    assert "Next experiment: do the thing." in captured["messages"][0]["content"]
    assert "system" in captured
    assert "never invent" in captured["system"]


# ---------------------------------------------------------------------------
# ResearchReporter
# ---------------------------------------------------------------------------


def test_reporter_uses_fallback_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = make_result()
    attribution = make_attribution(total_trades=1)

    report = ResearchReporter().run(result, attribution)

    assert isinstance(report, ResearchReport)
    assert report.rendered_by == "fallback"
    # No renderer was ever attempted here -- this is the normal,
    # unconfigured-Claude path, not a failure (DECISIONS.md, ADR-0034).
    assert report.renderer_error is None


def test_reporter_uses_injected_renderer_when_given():
    class _StubRenderer:
        def render(self, findings):
            return "stubbed narrative"

    result = make_result()
    attribution = make_attribution(total_trades=1)

    report = ResearchReporter(renderer=_StubRenderer()).run(result, attribution)

    assert report.narrative == "stubbed narrative"
    assert report.rendered_by == "claude"
    assert report.renderer_error is None


def test_reporter_uses_claude_renderer_successfully_end_to_end(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", _FakeAnthropicModule())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    result = make_result()
    attribution = make_attribution(total_trades=1)

    report = ResearchReporter().run(result, attribution)

    assert report.rendered_by == "claude"
    assert report.renderer_error is None
    assert report.narrative == "A rephrased research summary."


def test_reporter_falls_back_when_renderer_raises():
    class _BrokenRenderer:
        def render(self, findings):
            raise RuntimeError("network is down")

    result = make_result()
    attribution = make_attribution(total_trades=1)

    report = ResearchReporter(renderer=_BrokenRenderer()).run(result, attribution)

    assert report.rendered_by == "fallback"
    assert "ema_cross" in report.narrative or report.findings.strategy_name in report.narrative
    # The failure is observable, not silently concealed (DECISIONS.md,
    # ADR-0034) -- an operator can tell this apart from the normal,
    # no-key fallback path.
    assert report.renderer_error == "network is down"


def test_reporter_deterministic_findings_survive_a_renderer_failure():
    class _BrokenRenderer:
        def render(self, findings):
            raise RuntimeError("network is down")

    result = make_result(strategy_name="orb", sharpe=2.0)
    attribution = make_attribution(total_trades=7, win_rate=0.7)

    working_report = ResearchReporter().run(result, attribution)
    broken_report = ResearchReporter(renderer=_BrokenRenderer()).run(result, attribution)

    # The deterministic findings are identical regardless of whether the
    # prose renderer succeeded or failed -- only rendered_by/
    # renderer_error/narrative differ. The LLM never gets a chance to
    # alter what facts exist, only how they're phrased.
    assert broken_report.findings == working_report.findings
    assert broken_report.rendered_by == "fallback"
    assert broken_report.renderer_error == "network is down"


def test_reporter_findings_are_always_populated_regardless_of_renderer():
    result = make_result(strategy_name="orb", sharpe=2.0)
    attribution = make_attribution(total_trades=7, win_rate=0.7)

    report = ResearchReporter().run(result, attribution)

    assert report.findings.strategy_name == "orb"
    labels = [f.label for f in report.findings.findings]
    assert "Sharpe" in labels
    assert "Win Rate" in labels
