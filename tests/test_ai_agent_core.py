"""Tests for the AI Research Agent's core building blocks -- Sprint 13
(`DECISIONS.md`, ADR-0046): domain models, the provider abstraction, the
policy, the tool framework, and the session. No network access
anywhere in this file (Sprint 13 spec, section 73).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.ai.agents.models import (
    AgentEvidence,
    AgentResearchReport,
    AgentRun,
    AgentRunStatus,
    ToolCallRecord,
    TrialResult,
    utc_now,
)
from src.ai.agents.policy import ResearchAgentPolicy
from src.ai.agents.provider import (
    FakeLLMProvider,
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    ProviderError,
)
from src.ai.agents.session import AgentSession
from src.ai.agents.tools import (
    AgentTool,
    ToolRegistry,
    ToolResult,
    validate_arguments,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _make_run(**overrides) -> AgentRun:
    defaults = dict(
        run_id="run-1",
        goal="test goal",
        provider="fake",
        model="fake-model",
        status=AgentRunStatus.COMPLETED,
        started_at=utc_now(),
        finished_at=utc_now(),
        step_count=1,
        tool_call_count=0,
        backtest_count=0,
        system_prompt_version="v1",
        tool_schema_version="hash1",
        policy_hash="hash2",
    )
    defaults.update(overrides)
    return AgentRun(**defaults)


def test_agent_run_requires_timezone_aware_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        _make_run(started_at=datetime(2026, 1, 1), finished_at=utc_now())
    with pytest.raises(ValueError, match="timezone-aware"):
        _make_run(started_at=utc_now(), finished_at=datetime(2026, 1, 1))


def test_agent_run_accepts_timezone_aware_timestamps():
    run = _make_run()
    assert run.started_at.tzinfo is not None
    assert run.finished_at.tzinfo is not None


def test_utc_now_is_timezone_aware_utc():
    now = utc_now()
    assert now.tzinfo is timezone.utc


def test_agent_research_report_has_no_trading_recommendation_fields():
    report = AgentResearchReport(
        run_id="run-1", goal="g", status=AgentRunStatus.COMPLETED, summary="s"
    )
    field_names = {f for f in report.__dataclass_fields__}
    assert "trade_recommendation" not in field_names
    assert "buy_sell_recommendation" not in field_names


def test_tool_call_record_is_compact_and_never_raw():
    record = ToolCallRecord(
        step=1, tool_name="list_experiments", arguments={}, is_error=False,
        result_summary={"count": 0},
    )
    assert record.error_code is None
    assert record.result_summary == {"count": 0}


def test_agent_run_status_values_distinguish_all_three_terminal_states():
    assert {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.BUDGET_EXHAUSTED} == {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.BUDGET_EXHAUSTED,
    }
    assert AgentRunStatus.COMPLETED != AgentRunStatus.FAILED
    assert AgentRunStatus.FAILED != AgentRunStatus.BUDGET_EXHAUSTED


def test_trial_result_carries_full_provenance():
    trial = TrialResult(
        trial_id="t1", strategy_name="ema_cross", strategy_version="v1", symbol="SPY",
        interval="1d", dataset_fingerprint="fp1", dataset_start="2024-01-01",
        dataset_end="2024-06-01", risk_config={}, execution_config={}, analytics={},
        trade_count=3,
    )
    assert trial.model_id is None
    assert trial.trade_count == 3


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_default_policy_is_research_only():
    policy = ResearchAgentPolicy()
    assert policy.allow_live_trading is False
    assert policy.allow_portfolio_mutation is False
    assert policy.allow_strategy_generation is False
    assert policy.allow_model_training is False
    assert policy.allow_historical_backtests is True
    assert policy.allow_experiment_reads is True
    assert policy.allow_model_reads is True


def test_policy_rejects_dangerous_flags_even_if_explicitly_requested():
    with pytest.raises(ValueError, match="allow_live_trading"):
        ResearchAgentPolicy(allow_live_trading=True)
    with pytest.raises(ValueError, match="allow_portfolio_mutation"):
        ResearchAgentPolicy(allow_portfolio_mutation=True)
    with pytest.raises(ValueError, match="allow_strategy_generation"):
        ResearchAgentPolicy(allow_strategy_generation=True)
    with pytest.raises(ValueError, match="allow_model_training"):
        ResearchAgentPolicy(allow_model_training=True)


def test_policy_rejects_non_positive_budgets():
    with pytest.raises(ValueError):
        ResearchAgentPolicy(max_steps=0)
    with pytest.raises(ValueError):
        ResearchAgentPolicy(max_backtests=0)


def test_policy_default_budgets_are_sensible():
    policy = ResearchAgentPolicy()
    assert policy.max_steps == 12
    assert policy.max_backtests == 4


def test_policy_config_hash_is_deterministic_and_sensitive_to_changes():
    a = ResearchAgentPolicy(max_steps=5)
    b = ResearchAgentPolicy(max_steps=5)
    c = ResearchAgentPolicy(max_steps=6)
    assert a.config_hash() == b.config_hash()
    assert a.config_hash() != c.config_hash()


# ---------------------------------------------------------------------------
# Provider (fake)
# ---------------------------------------------------------------------------


def test_fake_provider_plays_back_scripted_responses_in_order():
    r1 = LLMResponse(text=None, tool_calls=[LLMToolCall(id="1", name="foo", arguments={})])
    r2 = LLMResponse(text="done")
    provider = FakeLLMProvider([r1, r2])
    assert provider.generate(system="s", messages=[], tools=[]) is r1
    assert provider.generate(system="s", messages=[], tools=[]) is r2


def test_fake_provider_raises_provider_error_when_script_exhausted():
    provider = FakeLLMProvider([LLMResponse(text="only one")])
    provider.generate(system="s", messages=[], tools=[])
    with pytest.raises(ProviderError):
        provider.generate(system="s", messages=[], tools=[])


def test_fake_provider_supports_callables_for_dynamic_scripting():
    calls = {"n": 0}

    def make_response():
        calls["n"] += 1
        return LLMResponse(text=f"call-{calls['n']}")

    provider = FakeLLMProvider([make_response, make_response])
    r1 = provider.generate(system="s", messages=[], tools=[])
    r2 = provider.generate(system="s", messages=[], tools=[])
    assert (r1.text, r2.text) == ("call-1", "call-2")


def test_fake_provider_records_calls_for_inspection():
    provider = FakeLLMProvider([LLMResponse(text="ok")])
    provider.generate(system="sys-prompt", messages=[LLMMessage(role="user", content="hi")], tools=[])
    assert len(provider.calls) == 1
    assert provider.calls[0]["system"] == "sys-prompt"


def test_provider_error_carries_code_and_message():
    err = ProviderError("MISSING_API_KEY", "no key set")
    assert err.code == "MISSING_API_KEY"
    assert "no key set" in str(err)


# ---------------------------------------------------------------------------
# Tool framework
# ---------------------------------------------------------------------------


class _EchoTool:
    name = "echo"
    description = "echoes its input"
    required_permission = "allow_experiment_reads"

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}, "count": {"type": "integer", "minimum": 0}},
            "required": ["text"],
        }

    def execute(self, arguments: dict) -> ToolResult:
        return ToolResult.ok(self.name, {"echoed": arguments["text"]})


class _CrashingTool:
    name = "crash"
    description = "always raises"
    required_permission = "allow_experiment_reads"

    def schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, arguments: dict) -> ToolResult:
        raise RuntimeError("boom")


class _ModelReadTool:
    name = "model_read"
    description = "gated on model reads"
    required_permission = "allow_model_reads"

    def schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, arguments: dict) -> ToolResult:
        return ToolResult.ok(self.name, {})


def test_validate_arguments_requires_required_fields():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    assert validate_arguments(schema, {}) is not None
    assert validate_arguments(schema, {"a": "x"}) is None


def test_validate_arguments_rejects_unexpected_fields():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    error = validate_arguments(schema, {"b": "x"})
    assert error is not None and "unexpected" in error


def test_validate_arguments_enforces_type_and_range():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}},
        "required": [],
    }
    assert validate_arguments(schema, {"n": 3}) is None
    assert validate_arguments(schema, {"n": "not a number"}) is not None
    assert validate_arguments(schema, {"n": 0}) is not None
    assert validate_arguments(schema, {"n": 6}) is not None


def test_validate_arguments_enforces_enum():
    schema = {"type": "object", "properties": {"x": {"type": "string", "enum": ["A", "B"]}}, "required": []}
    assert validate_arguments(schema, {"x": "A"}) is None
    assert validate_arguments(schema, {"x": "C"}) is not None


def test_validate_arguments_rejects_bool_for_integer_field():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": []}
    assert validate_arguments(schema, {"n": True}) is not None


def test_tool_registry_registers_and_looks_up_by_name():
    registry = ToolRegistry([_EchoTool()])
    assert registry.get("echo") is not None
    assert registry.get("nope") is None


def test_tool_registry_rejects_duplicate_registration():
    registry = ToolRegistry([_EchoTool()])
    with pytest.raises(ValueError):
        registry.register(_EchoTool())


def test_tool_registry_execute_runs_a_valid_call():
    registry = ToolRegistry([_EchoTool()])
    policy = ResearchAgentPolicy()
    result = registry.execute("echo", {"text": "hi"}, policy)
    assert result.status == "ok"
    assert result.data == {"echoed": "hi"}


def test_tool_registry_execute_rejects_unknown_tool():
    registry = ToolRegistry([_EchoTool()])
    result = registry.execute("nonexistent", {}, ResearchAgentPolicy())
    assert result.is_error
    assert result.error_code == "UNKNOWN_TOOL"


def test_tool_registry_execute_rejects_invalid_arguments_without_calling_tool():
    calls = []

    class _SpyTool(_EchoTool):
        def execute(self, arguments):
            calls.append(arguments)
            return super().execute(arguments)

    registry = ToolRegistry([_SpyTool()])
    result = registry.execute("echo", {}, ResearchAgentPolicy())
    assert result.is_error
    assert result.error_code == "INVALID_ARGUMENTS"
    assert calls == []


def test_tool_registry_execute_catches_internal_exceptions_as_structured_errors():
    registry = ToolRegistry([_CrashingTool()])
    result = registry.execute("crash", {}, ResearchAgentPolicy())
    assert result.is_error
    assert result.error_code == "INTERNAL_ERROR"
    assert "boom" in result.error_message


def test_tool_registry_allowed_tools_filters_by_policy():
    registry = ToolRegistry([_EchoTool(), _ModelReadTool()])
    policy = ResearchAgentPolicy(allow_model_reads=False)
    allowed_names = {t.name for t in registry.allowed_tools(policy)}
    assert allowed_names == {"echo"}


def test_tool_registry_execute_rejects_a_tool_the_policy_forbids():
    registry = ToolRegistry([_ModelReadTool()])
    policy = ResearchAgentPolicy(allow_model_reads=False)
    result = registry.execute("model_read", {}, policy)
    assert result.is_error
    assert result.error_code == "POLICY_REJECTED"


def test_tool_registry_schema_hash_is_deterministic_and_policy_sensitive():
    registry = ToolRegistry([_EchoTool(), _ModelReadTool()])
    full_policy = ResearchAgentPolicy()
    restricted_policy = ResearchAgentPolicy(allow_model_reads=False)
    assert registry.schema_hash(full_policy) == registry.schema_hash(full_policy)
    assert registry.schema_hash(full_policy) != registry.schema_hash(restricted_policy)


def test_tool_registry_allowed_definitions_never_include_forbidden_tools():
    registry = ToolRegistry([_EchoTool(), _ModelReadTool()])
    policy = ResearchAgentPolicy(allow_model_reads=False)
    names = {d["name"] for d in registry.allowed_definitions(policy)}
    assert "model_read" not in names


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_agent_session_accumulates_state():
    session = AgentSession(goal="investigate something")
    session.add_message(LLMMessage(role="user", content="go"))
    session.record_tool_call(
        ToolCallRecord(step=1, tool_name="t", arguments={}, is_error=False)
    )
    session.record_evidence(AgentEvidence(kind="trial", reference="t1", description="d"))
    session.record_experiment_id("42")
    session.record_experiment_id("42")  # duplicate, should not double up

    assert len(session.messages) == 1
    assert len(session.tool_calls) == 1
    assert len(session.evidence) == 1
    assert session.experiment_ids == ["42"]
