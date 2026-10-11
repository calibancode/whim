"""Deterministic providers for tests and offline runs. No network, no model."""

from __future__ import annotations

import asyncio
import copy
import inspect
import itertools
import json
from pathlib import Path
from typing import Any, Callable

from whim.provider.base import Completion, ProviderError, Request, ToolCall

_ids = itertools.count(1)


def tool_call(tool: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids):04d}", name=tool, arguments=json.dumps(arguments))


def say(text: str, *, reasoning: str | None = "thinking...") -> Completion:
    return Completion(content=text, reasoning_content=reasoning, finish_reason="stop")


def call(tool: str, /, *, authored: str | None = None, reasoning: str | None = "thinking...",
         **args: Any) -> Completion:
    """One tool call. `authored` is plain text alongside it; kwargs are the tool arguments."""
    return Completion(
        content=authored,
        reasoning_content=reasoning,
        tool_calls=[tool_call(tool, **args)],
        finish_reason="tool_calls",
    )


def calls(*tcs: ToolCall, authored: str | None = None, reasoning: str | None = "thinking...") -> Completion:
    return Completion(
        content=authored, reasoning_content=reasoning, tool_calls=list(tcs), finish_reason="tool_calls"
    )


def finish(**kw: Any) -> Completion:
    return call("turn_finish", **kw)


Step = Completion | Callable[[Request], Any]


class ScriptedProvider:
    """Returns scripted completions in order and records every request it received.

    A step may be a Completion or a (sync or async) callable taking the Request, which
    lets tests block mid-request or raise to simulate crashes at exact points.
    """

    name = "scripted"

    def __init__(self, steps: list[Step] | None = None):
        self.steps: list[Step] = list(steps or [])
        self.requests: list[Request] = []

    def extend(self, *steps: Step) -> None:
        self.steps.extend(steps)

    async def complete(self, request: Request) -> Completion:
        self.requests.append(copy.deepcopy(request))
        if not self.steps:
            raise ProviderError("scripted provider has no more steps")
        step = self.steps.pop(0)
        if isinstance(step, Completion):
            return copy.deepcopy(step)
        out = step(request)
        if inspect.isawaitable(out):
            out = await out
        return out

    async def close(self) -> None:
        pass


class RuleScriptProvider:
    """Rule-driven offline provider for `provider.kind = "script"` (CLI demos, process tests).

    Script JSON: {"rules": [{"match": "substring", "delay": 0.0, "content": "...",
    "tool_calls": [{"name": "message_send", "arguments": {...}}]}], "default": {...}}.
    The first rule whose `match` occurs in the last message's text wins. `{last}` in
    string values is replaced by that text. Stateless, so it behaves the same after restarts.
    """

    name = "script"

    def __init__(self, path: str | Path):
        self.script = json.loads(Path(path).read_text())

    async def complete(self, request: Request) -> Completion:
        last = request.messages[-1].get("content") or ""
        rule = next(
            (r for r in self.script.get("rules", []) if r.get("match", "") in last),
            self.script.get("default"),
        )
        if rule is None:
            raise ProviderError("no matching script rule")
        if rule.get("delay"):
            await asyncio.sleep(rule["delay"])

        def sub(v: Any) -> Any:
            if isinstance(v, str):
                return v.replace("{last}", last)
            if isinstance(v, dict):
                return {k: sub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [sub(x) for x in v]
            return v

        tcs = [tool_call(t["name"], **sub(t.get("arguments", {}))) for t in rule.get("tool_calls", [])]
        return Completion(
            content=sub(rule.get("content")),
            reasoning_content=rule.get("reasoning"),
            tool_calls=tcs,
            finish_reason="tool_calls" if tcs else "stop",
            model="script",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )

    async def close(self) -> None:
        pass
