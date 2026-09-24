"""The tool contract and registry -- src/ai/agents/tools.py.

Sprint 13 spec, sections 15, 62-63, 103: the LLM is never allowed to
call arbitrary Python. Every tool is typed, schema-validated, policy-
gated, and auditable, and every call passes through one fixed pipeline
before anything executes:

    model requests action
            |
            v
       tool exists?
            |
            v
     policy permits?
            |
            v
    arguments valid?
            |
            v
        execute

`ToolRegistry.execute()` is the only place this happens -- `agent.py`
never executes a tool directly. The registry also decides which tools a
policy even offers the model in the first place (`allowed_definitions()`)
-- a forbidden tool is not merely refused at call time, it is never
described to the model at all (Sprint 13 spec, section 62).

Concrete research tools live in `src.ai.agents.research_tools`
(Phase 5) and `src.ai.agents.backtest_tool` (Phase 7); this module only
defines the framework and `default_tool_registry()`, which wires the
concrete tools together for normal use.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.ai.agents.policy import ResearchAgentPolicy

# Every concrete tool this sprint ships is gated by one of these two
# read-only policy flags, or the historical-backtest flag -- see each
# tool's own `required_permission`.
PERMISSION_EXPERIMENT_READS = "allow_experiment_reads"
PERMISSION_MODEL_READS = "allow_model_reads"
PERMISSION_HISTORICAL_BACKTESTS = "allow_historical_backtests"


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one `AgentTool.execute()` call -- always a
    structured result, never a raised exception for an ordinary failure
    (Sprint 13 spec, section 48: "never give the model raw stack traces
    by default").

    Args:
        tool_name: which tool produced this result.
        status: `"ok"` or `"error"`.
        data: the tool's compact, JSON-safe output on success (Sprint 13
            spec, section 43: bounded lists, structured metrics, never a
            full DataFrame/trade history/equity curve). `{}` on error.
        error_code: a short machine-readable code on error (e.g.
            `"INVALID_DATE_RANGE"`, `"NOT_FOUND"`), `None` on success.
        error_message: a short human-readable message on error, `None`
            on success.
    """

    tool_name: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, tool_name: str, data: dict[str, Any]) -> "ToolResult":
        return cls(tool_name=tool_name, status="ok", data=data)

    @classmethod
    def error(cls, tool_name: str, code: str, message: str) -> "ToolResult":
        return cls(tool_name=tool_name, status="error", error_code=code, error_message=message)

    @property
    def is_error(self) -> bool:
        return self.status == "error"


@runtime_checkable
class AgentTool(Protocol):
    """The contract every agent tool satisfies -- structural typing,
    matching `src.strategies.base.Strategy`'s own convention.

    Every implementation must be deterministic where the underlying
    platform capability is deterministic (a repeated `get_experiment`
    call for the same id returns the same data), bounded (never returns
    an unbounded amount of data), and auditable (its `schema()` fully
    describes what arguments it accepts, so `ToolRegistry` can validate
    before ever calling `execute()`).
    """

    @property
    def name(self) -> str:
        """This tool's unique, stable name -- what the model requests
        by, and what `ToolCallRecord.tool_name` records."""
        ...

    @property
    def description(self) -> str:
        """A short, model-facing description of what this tool does."""
        ...

    @property
    def required_permission(self) -> str:
        """Which `ResearchAgentPolicy` boolean flag must be `True` for
        this tool to be offered/executed at all."""
        ...

    def schema(self) -> dict[str, Any]:
        """A JSON-schema-shaped description of this tool's arguments --
        `{"type": "object", "properties": {...}, "required": [...]}`.
        `ToolRegistry.validate_arguments()` validates every call against
        this before `execute()` is ever invoked."""
        ...

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Run this tool against already-validated `arguments`. Must
        never raise for an ordinary, anticipated failure (a missing
        resource, an out-of-range value not already caught by schema
        validation) -- return `ToolResult.error(...)` instead. An
        unanticipated exception is still caught by `ToolRegistry.
        execute()` and turned into a structured `INTERNAL_ERROR` result,
        but a well-behaved tool should not rely on that."""
        ...


_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_arguments(schema: dict[str, Any], arguments: Any) -> str | None:
    """A small, deliberately minimal JSON-schema-subset validator
    (Sprint 13 spec, section 49) -- object/type/required/enum/minimum/
    maximum/items, exactly what this sprint's seven tool schemas
    actually use. Returns `None` when `arguments` satisfies `schema`,
    or a short human-readable reason when it doesn't.

    Deliberately hand-rolled rather than depending on the `jsonschema`
    package's full validation semantics (already a transitive project
    dependency via Jupyter, but never a direct one for this package) --
    this keeps tool-schema validation fully under this platform's own
    control and just as deterministic and testable as everything else
    in `src.ai.agents`.
    """
    if not isinstance(arguments, dict):
        return f"expected an object, got {type(arguments).__name__}"

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for field_name in required:
        if field_name not in arguments:
            return f"missing required field {field_name!r}"

    for field_name, value in arguments.items():
        if field_name not in properties:
            return f"unexpected field {field_name!r}"
        field_schema = properties[field_name]
        error = _validate_value(field_name, value, field_schema)
        if error is not None:
            return error
    return None


def _validate_value(field_name: str, value: Any, field_schema: dict[str, Any]) -> str | None:
    field_type = field_schema.get("type")
    if field_type in ("integer", "number"):
        # bool is a subclass of int in Python -- reject a bool where an
        # integer/number was requested explicitly, rather than letting
        # `isinstance(True, int)` silently pass it through.
        if isinstance(value, bool):
            return f"{field_name!r} must be a {field_type}, got boolean"
        if not isinstance(value, _TYPE_CHECKS[field_type]):
            return f"{field_name!r} must be a {field_type}, got {type(value).__name__}"
    elif field_type == "boolean":
        if not isinstance(value, bool):
            return f"{field_name!r} must be a boolean, got {type(value).__name__}"
    elif field_type is not None:
        expected = _TYPE_CHECKS.get(field_type)
        if expected is not None and not isinstance(value, expected):
            return f"{field_name!r} must be a {field_type}, got {type(value).__name__}"

    enum = field_schema.get("enum")
    if enum is not None and value not in enum:
        return f"{field_name!r} must be one of {enum!r}, got {value!r}"

    minimum = field_schema.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and value < minimum:
        return f"{field_name!r} must be >= {minimum}, got {value}"

    maximum = field_schema.get("maximum")
    if maximum is not None and isinstance(value, (int, float)) and value > maximum:
        return f"{field_name!r} must be <= {maximum}, got {value}"

    if field_type == "array":
        items_schema = field_schema.get("items")
        max_items = field_schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            return f"{field_name!r} must have at most {max_items} items, got {len(value)}"
        if items_schema is not None:
            for index, item in enumerate(value):
                error = _validate_value(f"{field_name}[{index}]", item, items_schema)
                if error is not None:
                    return error
    return None


class ToolRegistry:
    """A deterministic registry of `AgentTool` instances, filtered by a
    `ResearchAgentPolicy` (Sprint 13 spec, section 62).

    Deliberately holds no state about any particular agent run -- one
    `ToolRegistry` can be shared/reused across many `ResearchAgent.run()`
    calls; permission/budget state that *is* per-run lives on
    `AgentSession`/is checked by `ResearchAgent` itself.
    """

    def __init__(self, tools: list[AgentTool] | None = None) -> None:
        self._tools: dict[str, AgentTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        """Add `tool` to the registry.

        Raises:
            ValueError: a tool is already registered under this name.
        """
        if tool.name in self._tools:
            raise ValueError(f"a tool is already registered under {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[AgentTool]:
        return [self._tools[name] for name in sorted(self._tools)]

    def allowed_tools(self, policy: ResearchAgentPolicy) -> list[AgentTool]:
        """Every registered tool `policy` permits, sorted by name for
        determinism -- what `allowed_definitions()` describes to the
        provider, and the only names `execute()` will actually run for
        this policy."""
        return [
            tool for tool in self.all_tools() if getattr(policy, tool.required_permission)
        ]

    def allowed_definitions(self, policy: ResearchAgentPolicy) -> list[dict[str, Any]]:
        """Provider-facing tool definitions (`name`/`description`/
        `input_schema`) for exactly the tools `policy` permits -- a
        forbidden tool's definition is never sent to the model at all
        (Sprint 13 spec, section 62)."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.schema(),
            }
            for tool in self.allowed_tools(policy)
        ]

    def schema_hash(self, policy: ResearchAgentPolicy) -> str:
        """A stable SHA-256 hash of the exact tool definitions offered
        under `policy` -- `AgentRun.tool_schema_version` (Sprint 13
        spec, section 11). Changes whenever a tool is added/removed/
        changed, or when `policy` permits a different subset."""
        canonical = json.dumps(self.allowed_definitions(policy), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def execute(
        self, name: str, arguments: dict[str, Any], policy: ResearchAgentPolicy
    ) -> ToolResult:
        """Run the full validation pipeline, then execute -- the only
        entry point `ResearchAgent` uses to run a tool (Sprint 13 spec,
        section 103's pipeline, enforced here rather than trusted to
        each call site).

        Never raises: an unregistered tool, a policy rejection, a schema
        violation, or an unexpected internal exception all come back as
        a structured `ToolResult.error(...)`.
        """
        tool = self.get(name)
        if tool is None:
            return ToolResult.error(
                name, "UNKNOWN_TOOL", f"no tool registered under {name!r}"
            )
        if not getattr(policy, tool.required_permission):
            return ToolResult.error(
                name,
                "POLICY_REJECTED",
                f"tool {name!r} is not permitted by the current agent policy "
                f"(requires {tool.required_permission}=True)",
            )
        schema_error = validate_arguments(tool.schema(), arguments)
        if schema_error is not None:
            return ToolResult.error(name, "INVALID_ARGUMENTS", schema_error)
        try:
            return tool.execute(arguments)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: a tool
            # must never crash the agent loop or leak a raw traceback to
            # the model (Sprint 13 spec, section 48); detailed
            # diagnostics belong in application logs, not here.
            return ToolResult.error(name, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


def default_tool_registry(
    experiment_registry: Any = None,
    model_registry: Any = None,
    research_trial_service: Any = None,
) -> ToolRegistry:
    """Build the standard Sprint 13 tool registry -- the seven tools
    described in the Sprint 13 spec, wired to real platform services
    unless overrides are given (tests inject fakes/fixtures here).

    Args:
        experiment_registry: `src.experiments.registry.ExperimentRegistry`
            instance to back the experiment-reading tools. Defaults to
            `ExperimentRegistry()` at the platform's default database path.
        model_registry: `src.ai.registry.ModelRegistry` instance to back
            `get_model_metadata`. Defaults to `ModelRegistry()` at the
            platform's default models directory.
        research_trial_service: `src.research.trial_service.
            ResearchTrialService` instance to back
            `run_historical_backtest`. Defaults to a fresh
            `ResearchTrialService()`.
    """
    from src.ai.agents.backtest_tool import RunHistoricalBacktestTool
    from src.ai.agents.research_tools import (
        AnalyzeExperimentTool,
        CompareExperimentsTool,
        GetExperimentTool,
        GetModelMetadataTool,
        ListExperimentsTool,
        ListStrategiesTool,
    )
    from src.ai.registry import ModelRegistry
    from src.experiments.registry import ExperimentRegistry
    from src.research.trial_service import ResearchTrialService

    experiment_registry = experiment_registry or ExperimentRegistry()
    model_registry = model_registry or ModelRegistry()
    research_trial_service = research_trial_service or ResearchTrialService()

    return ToolRegistry(
        [
            ListExperimentsTool(experiment_registry),
            GetExperimentTool(experiment_registry),
            AnalyzeExperimentTool(experiment_registry),
            CompareExperimentsTool(experiment_registry),
            ListStrategiesTool(),
            GetModelMetadataTool(model_registry),
            RunHistoricalBacktestTool(research_trial_service),
        ]
    )
