"""Model-callable tool registry.

A tool has a canonical dotted name (`message.send`) and a provider wire name
(`message_send`), because OpenAI-compatible function names cannot contain dots.
Effect class and replay safety are host-owned properties bound to the concrete
implementation identity (`impl`), never to the name a tool chooses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from whim.run import RunState

Effect = Literal["control", "read", "write", "send", "plugin"]


class ArgumentError(Exception):
    """The model's arguments don't match the tool's schema."""


class JsonArgs(dict):
    """Arguments validated against a plain JSON Schema (plugin tools)."""

    def model_dump(self) -> dict:
        return dict(self)


@dataclass
class ToolResult:
    ok: bool
    data: dict
    # Host-only: character files this call wrote, as {name: sha256 of the bytes written}.
    # Recorded as already seen so the next boundary doesn't echo the character's own
    # write back to it. Never sent to the model.
    file_acks: dict[str, str] | None = None
    # Host-only: work to do after the result's transaction commits (e.g. deleting a file).
    after_commit: Callable[[], None] | None = None

    def to_model(self) -> str:
        import json

        return json.dumps({"ok": self.ok, **self.data}, ensure_ascii=False)


Handler = Callable[["RunState", Any], Awaitable[ToolResult]]
# Atomic handlers are synchronous and run inside the transaction that records the call
# and its result, so host-state tools can never be left half-applied by a crash.
AtomicHandler = Callable[["RunState", Any, str], ToolResult]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: type[BaseModel] | None
    effect: Effect
    impl: str
    handler: Handler | AtomicHandler | None = None  # None for control tools handled by the run loop
    json_schema: dict | None = None  # plugin tools: a validated JSON Schema instead of `params`
    help: str | None = None  # longer guidance, shown only by tool_describe
    replay_safe: bool = False
    atomic: bool = False
    # Offered to the model only while this returns True (checked per provider request).
    available: Callable[[Any], bool] | None = None
    # Decides the outcome of a call whose result was lost in a crash, by inspecting the
    # effect itself: (harness, operation row) -> (status, model-facing result JSON).
    reconcile: Callable[[Any, Any], tuple[str, str]] | None = None

    @property
    def wire_name(self) -> str:
        return self.name.replace(".", "_")

    def parameters(self) -> dict:
        if self.json_schema is not None:
            return self.json_schema
        return _strip_titles(_inline_refs(self.params.model_json_schema()))

    def schema(self) -> dict:
        params = self.parameters()
        return {
            "type": "function",
            "function": {"name": self.wire_name, "description": self.description, "parameters": params},
        }

    def parse(self, raw: str):
        if self.json_schema is not None:
            import json

            from whim import jsonschemas

            try:
                value = json.loads(raw or "{}")
            except json.JSONDecodeError as e:
                raise ArgumentError(f"arguments are not valid JSON: {e}") from None
            try:
                errors = sorted(jsonschemas.validator(self.json_schema).iter_errors(value), key=lambda e: list(e.path))
            except Exception:  # noqa: BLE001 - e.g. an unresolvable reference: say nothing about it
                raise ArgumentError("this tool's parameter schema can't be used (unsupported reference)") from None
            if errors:
                raise ArgumentError("; ".join(f"{'/'.join(map(str, e.path)) or 'arguments'}: {e.message}"
                                              for e in errors[:5]))
            return JsonArgs(value)
        try:
            return self.params.model_validate_json(raw or "{}")
        except ValidationError as e:
            raise ArgumentError(str(e.errors(include_url=False))) from None


def _inline_refs(schema: dict) -> dict:
    """Replace local $refs with their definitions; not every provider resolves $defs."""
    defs = schema.pop("$defs", {})

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            if "$ref" in v:
                return walk(defs[v["$ref"].rsplit("/", 1)[-1]])
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(schema)


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


class Registry:
    # Replay safety may only be asserted by audited first-party implementations.
    TRUSTED_IMPL_PREFIX = "whim.builtin."

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.replay_safe and not tool.impl.startswith(self.TRUSTED_IMPL_PREFIX):
            raise ValueError(f"{tool.impl} may not declare itself replay-safe")
        if tool.wire_name in {t.wire_name for t in self._tools.values()}:
            raise ValueError(f"tool {tool.name} already registered")
        self._tools[tool.name] = tool

    def by_wire(self, wire_name: str) -> Tool | None:
        return next((t for t in self._tools.values() if t.wire_name == wire_name), None)

    def by_impl(self, impl: str) -> Tool | None:
        return next((t for t in self._tools.values() if t.impl == impl), None)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def offered(self, harness: Any = None) -> list[Tool]:
        """Built-in tools available right now."""
        return [t for n in sorted(self._tools) if (t := self._tools[n]).available is None or t.available(harness)]

    def schemas(self, harness: Any = None) -> list[dict]:
        """Built-in tool schemas in a stable order (plugin tools come from plugins.snapshot)."""
        return [t.schema() for t in self.offered(harness)]


__all__ = ["ArgumentError", "Effect", "JsonArgs", "Registry", "Tool", "ToolResult", "ValidationError"]
