"""The Claude client: one narrow interface, implemented with the Anthropic SDK and by test fakes.

Claude receives only retrieved snippets, may call read-only tools, and must answer
with JSON that matches the verdict schema (structured output). Anything that keeps
Claude from answering in time is reported as `ClaudeUnavailable`, and the hits stay
as the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("factual.ai")


class ClaudeUnavailable(RuntimeError):
    """Claude could not be reached or did not answer usefully."""


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass(frozen=True)
class ModelReply:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)


class ClaudeClient(Protocol):
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        output_schema: dict,
        max_tokens: int,
    ) -> ModelReply: ...


class AnthropicClaude:
    """The real thing: `claude-sonnet-5-5` by default (CLAUDE_MODEL)."""

    def __init__(self, api_key: str, model: str, *, timeout: float = 30.0) -> None:
        import anthropic

        self.model = model
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)

    async def complete(self, *, system, messages, tools, output_schema, max_tokens) -> ModelReply:
        anthropic = self._anthropic
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                output_config={"format": {"type": "json_schema", "schema": output_schema}},
            )
        except anthropic.RateLimitError as exc:
            raise ClaudeUnavailable(f"rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeUnavailable(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise ClaudeUnavailable(f"connection error: {type(exc).__name__}") from exc
        content: list = []
        for block in response.content:
            if block.type == "text":
                content.append(TextBlock(block.text))
            elif block.type == "tool_use":
                content.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input)))
        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", None),
            "output_tokens": getattr(response.usage, "output_tokens", None),
        }
        return ModelReply(
            content=content, stop_reason=response.stop_reason or "end_turn", usage=usage
        )
