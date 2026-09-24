"""The LLM provider abstraction -- src/ai/agents/provider.py.

Sprint 13 spec, sections 5, 88: `ResearchAgent` (`agent.py`) must not
directly depend on Anthropic, OpenAI, or Google -- it depends on
`LLMProvider` alone. The concrete Anthropic adapter lives in
`src.ai.agents.anthropic_provider` (Phase 10) and is never imported by
this module or by `agent.py`; a caller wires a concrete provider in at
construction time (dependency injection), the same way
`src.research.reporter.ResearchReporter` accepts an optional
`NarrativeRenderer`.

`FakeLLMProvider` is what makes the entire agent loop testable without
network access (Sprint 13 spec, section 73: "use a fake/mock LLM
provider. No network.") -- it plays back a scripted sequence of
`LLMResponse` objects, one per `generate()` call, so a test can prove
the agent genuinely branches on provider output rather than following a
single hard-coded path (section 83).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


class ProviderError(Exception):
    """A structured, provider-level failure (Sprint 13 spec, section 8):
    missing API key, missing provider dependency, network failure,
    invalid response shape, or a provider timeout. `ResearchAgent`
    catches this and turns it into `AgentRunStatus.FAILED` with `str(exc)`
    as the recorded error -- it is never allowed to propagate out of
    `ResearchAgent.run()` uncaught, and the agent never silently
    pretends research completed when this is raised.

    Args:
        code: a short machine-readable code (e.g. `"MISSING_API_KEY"`,
            `"PROVIDER_DEPENDENCY_MISSING"`, `"NETWORK_ERROR"`,
            `"INVALID_RESPONSE"`, `"TIMEOUT"`).
        message: a human-readable description. Never includes a secret
            (an API key or credential) -- see
            `tests/test_ai_agent_security.py`.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class LLMToolCall:
    """One tool-call request from the model -- provider-neutral shape,
    translated from whatever the concrete SDK's own tool-use
    representation looks like.

    Args:
        id: the provider's own identifier for this tool call (needed to
            correlate a tool result back to the request in most
            providers' multi-turn tool-use protocols).
        name: which tool the model wants to invoke.
        arguments: the model's requested arguments, as a plain dict --
            not yet validated (`src.ai.agents.tools.ToolRegistry` does
            that); may be malformed, and `ResearchAgent` must handle
            that structurally (Sprint 13 spec, section 49).
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMMessage:
    """One turn in the conversation sent to/received from a provider --
    provider-neutral, so `ResearchAgent`'s own conversation history
    never depends on a specific SDK's message shape.

    Args:
        role: `"user"`, `"assistant"`, or `"tool"`.
        content: the text content of this turn (empty string when a
            message is purely a tool call/tool result).
        tool_calls: populated on an `"assistant"` message that requested
            one or more tool calls; `None` otherwise.
        tool_call_id: populated on a `"tool"` message -- which
            `LLMToolCall.id` this result answers; `None` otherwise.
    """

    role: str
    content: str = ""
    tool_calls: list[LLMToolCall] | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """One `LLMProvider.generate()` result -- provider-neutral.

    Args:
        text: the model's final text, when it produced one (may be
            non-empty alongside `tool_calls` for some providers, e.g. a
            short "I'll check the experiment registry" preamble
            alongside a tool call -- `ResearchAgent` only treats a
            response as a *final* answer when `tool_calls` is empty).
        tool_calls: zero or more tool calls the model requested this
            turn. Sprint 13 spec, section 26 caps *executed* concurrent
            tool calls to one; a provider may still request several in
            one response, and `ResearchAgent` processes them
            sequentially, checking budget/policy before each.
        stop_reason: the provider's own reason the turn ended (e.g.
            `"tool_use"`, `"end_turn"`, `"max_tokens"`) -- informational,
            not branched on by the agent loop itself (which branches on
            whether `tool_calls` is empty).
        model_metadata: provider/model bookkeeping -- e.g. `model`,
            `input_tokens`, `output_tokens` -- kept generic (Sprint 13
            spec, section 85: "keep cost reporting optional... do not
            make cost calculation provider-specific in the core agent").
    """

    text: str | None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    model_metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """The contract every LLM provider adapter satisfies -- structural
    typing, matching `src.strategies.base.Strategy`'s own convention.

    `ResearchAgent` depends on this and nothing more concrete (Sprint 13
    spec, section 5). A provider receives only `system`/`messages`/
    `tools` -- never API keys, database connection secrets, or
    environment variables beyond what its own constructor was given
    (Sprint 13 spec, section 51); secrets belong to the concrete
    provider adapter alone.
    """

    @property
    def name(self) -> str:
        """A short provider identifier (e.g. `"anthropic"`, `"fake"`) --
        recorded on `AgentRun.provider`."""
        ...

    @property
    def model(self) -> str:
        """The underlying model identifier this provider instance uses
        (e.g. `"claude-sonnet-5"`) -- recorded on `AgentRun.model`.
        Never hard-coded by `ResearchAgent` itself (Sprint 13 spec,
        section 6)."""
        ...

    def generate(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        """Send `messages` (with `system` as the system prompt and
        `tools` as the available tool schemas) and return the model's
        response.

        Raises:
            ProviderError: on any failure -- missing configuration,
                network failure, invalid response, or timeout. Never
                returns a fabricated/default response on failure.
        """
        ...


class FakeLLMProvider:
    """A scripted, deterministic `LLMProvider` for tests (Sprint 13
    spec, section 73) -- no network, ever.

    Args:
        responses: a sequence of `LLMResponse` objects (or
            zero-argument callables returning one, for a scripted
            response that needs to inspect nothing -- kept simple: this
            provider is intentionally dumb, the test author decides the
            whole script up front) played back one per `generate()`
            call, in order.
        model: the fake model identifier to report.

    Raises:
        ProviderError: `generate()` is called more times than
            `responses` has entries -- a test's script ran out, which
            usually means the agent looped more than the test expected
            (a real bug to catch, not something to paper over with an
            infinite default response).
    """

    def __init__(
        self,
        responses: list[LLMResponse | Callable[[], LLMResponse]],
        model: str = "fake-model-v1",
    ) -> None:
        self._responses = list(responses)
        self._model = model
        self._call_count = 0
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        if self._call_count >= len(self._responses):
            raise ProviderError(
                "SCRIPT_EXHAUSTED",
                f"FakeLLMProvider.generate() called {self._call_count + 1} times "
                f"but only {len(self._responses)} scripted responses were given",
            )
        entry = self._responses[self._call_count]
        self._call_count += 1
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        return entry() if callable(entry) else entry
