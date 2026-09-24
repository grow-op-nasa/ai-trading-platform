"""The Anthropic provider adapter -- src/ai/agents/anthropic_provider.py.

Sprint 13 spec, sections 6-8, 10 (Phase 10): the first concrete
`LLMProvider` implementation. Mirrors the optional-dependency
convention `src.research.renderers.ClaudeNarrativeRenderer` already
established for this codebase (`DECISIONS.md`, ADR-0020): `anthropic`
is imported lazily, inside `generate()`, never at module import time --
`anthropic` is deliberately **not** added to `requirements.txt`, so the
base platform (including every other test in this suite) continues to
work with zero setup whether or not the package or an API key is
present. This module is never imported by `src.ai.agents.agent`,
`src.ai.agents.tools`, or any other agent-core module -- only by
whatever constructs a `ResearchAgent` for real use (the CLI).

Configuration lives entirely outside source (Sprint 13 spec, section 6):
`AI_AGENT_MODEL` (falls back to `DEFAULT_MODEL` below) and
`ANTHROPIC_API_KEY`, both read from the environment at construction
time, not hard-coded.
"""

from __future__ import annotations

import os
from typing import Any

from src.ai.agents.provider import LLMMessage, LLMResponse, LLMToolCall, ProviderError

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 2048


class AnthropicLLMProvider:
    """`LLMProvider` backed by the Anthropic Messages API's tool-calling
    mechanism.

    Args:
        api_key: overrides `ANTHROPIC_API_KEY` from the environment.
        model: overrides `AI_AGENT_MODEL` from the environment, which
            itself overrides `DEFAULT_MODEL`. Never hard-coded into
            `src.ai.agents.agent` (Sprint 13 spec, section 6).
        max_tokens: forwarded to the Anthropic SDK's own `max_tokens`.

    Raises:
        ProviderError: (code `MISSING_API_KEY`) no API key is available
            from either the argument or the environment. Raised at
            construction time -- a `ResearchAgent` should never be
            handed a provider that can't possibly succeed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ProviderError(
                "MISSING_API_KEY",
                "ANTHROPIC_API_KEY is not set -- the research agent's Anthropic "
                "provider requires an API key, configured via the ANTHROPIC_API_KEY "
                "environment variable (never hard-coded).",
            )
        self._api_key = resolved_key
        self._model = model or os.environ.get("AI_AGENT_MODEL") or DEFAULT_MODEL
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return "anthropic"

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
        try:
            import anthropic  # optional dependency, imported lazily
        except ImportError as exc:
            raise ProviderError(
                "PROVIDER_DEPENDENCY_MISSING",
                "the 'anthropic' package is not installed -- install it to use "
                "AnthropicLLMProvider (the base platform does not require it)",
            ) from exc

        anthropic_messages = [_to_anthropic_message(m) for m in messages]
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            }
            for t in tools
        ]

        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            response = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=anthropic_messages,
                tools=anthropic_tools or anthropic.NOT_GIVEN,
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderError("TIMEOUT", str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("NETWORK_ERROR", str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError("PROVIDER_ERROR", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 -- any other SDK-raised failure
            # still must become a structured ProviderError, never propagate
            # as a raw, provider-specific exception type into the agent
            # loop (Sprint 13 spec, section 8).
            raise ProviderError("PROVIDER_ERROR", f"{type(exc).__name__}: {exc}") from exc

        return _from_anthropic_response(response)


def _to_anthropic_message(message: LLMMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            ],
        }
    if message.role == "assistant" and message.tool_calls:
        content: list[dict[str, Any]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        return {"role": "assistant", "content": content}
    return {"role": message.role, "content": message.content}


def _from_anthropic_response(response: Any) -> LLMResponse:
    text_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(block.text)
        elif block_type == "tool_use":
            tool_calls.append(
                LLMToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            )

    usage = getattr(response, "usage", None)
    metadata = {
        "model": getattr(response, "model", None),
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
    }
    return LLMResponse(
        text="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls,
        stop_reason=getattr(response, "stop_reason", "end_turn") or "end_turn",
        model_metadata=metadata,
    )
