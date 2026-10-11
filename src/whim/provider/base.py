from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str  # provider wire name
    arguments: str  # raw JSON text as produced by the model


@dataclass
class Completion:
    content: str | None = None
    # Private provider continuation state. DeepSeek thinking mode requires it to be
    # replayed on every later tool-enabled request; it is never shown to frontends.
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    model: str | None = None
    usage: dict = field(default_factory=dict)

    def assistant_message(self) -> dict:
        """Provider-format assistant message, exactly as it will be replayed later."""
        msg: dict = {"role": "assistant", "content": self.content}
        if self.reasoning_content is not None:
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        return msg


@dataclass
class Request:
    messages: list[dict]
    tools: list[dict]
    max_tokens: int | None = None


class ProviderError(Exception):
    pass


class Provider(Protocol):
    name: str

    async def complete(self, request: Request) -> Completion: ...

    async def close(self) -> None: ...
