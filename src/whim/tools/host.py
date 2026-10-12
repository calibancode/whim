"""Self-management tools: access requests, configuration, restart, and the guide.

Each is backed by the same implementation as the human's `whim access`, `whim config`
and `whim restart` commands. Anything that widens what the character can reach, or
changes cost, trust or what runs, becomes an approval request for the designated human;
looking is never gated.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from whim import access, authz, restart, settings
from whim.config import ConfigError
from whim.tools.registry import Registry, Tool, ToolResult

if TYPE_CHECKING:
    from whim.run import RunState


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessArgs(_Args):
    op: Literal["list", "request"] = "list"
    target: str | None = Field(default=None, description='request: an absolute folder path, "self" (your own '
                               'implementation), "shell" (running commands) or "network" (network for commands).')
    write: bool = Field(default=False, description="request on a folder: read-write instead of read-only.")
    tool: bool = Field(default=False, description="request on a folder of installed programs (e.g. ~/.cargo/bin): "
                       "also put it on PATH for your shell commands.")
    reason: str = Field(default="", description="request: why you need it, in a sentence; your human sees this.")


def _access(state: "RunState", args: AccessArgs, operation_id: str) -> ToolResult:
    h = state.harness
    if args.op == "list":
        return ToolResult(True, access.describe(h))
    if not args.target:
        return ToolResult(False, {"error": "request needs target"})
    path = None
    if args.target == "shell":
        subj, scopes = access.SHELL, [access.SHELL_RUN]
    elif args.target == "network":
        subj, scopes = access.SHELL, [access.SHELL_NET]
    else:
        try:
            path = access.implementation_root() if args.target == "self" else access.check_grantable(h, args.target)
        except access.AccessError as e:
            return ToolResult(False, {"error": str(e)})
        subj = access.subject(path)
        scopes = [access.READ, access.WRITE] if args.write else [access.READ]
        if args.tool:
            scopes.append(access.TOOL)
        if path == access.implementation_root() and not args.write:
            return ToolResult(True, {"note": f"{path} is already readable for you"})
    needed = authz.missing(h, subj, scopes)
    if not needed:
        return ToolResult(True, {"note": "you already have this", **access.describe(h)})
    req = authz.request(h, subj, needed, access.request_summary(path, needed, args.reason, h), state.run_id)
    return ToolResult(True, {"status": "waiting_for_approval", "approval_id": req["id"],
                             "note": "Your human decides this in their whim control interface; you'll be told "
                                     "when they do."})


class ConfigArgs(_Args):
    op: Literal["get", "set"] = "get"
    key: str | None = Field(default=None, description='get: one setting ("heartbeat.interval_minutes") or a '
                                                     'section ("compaction"). Omit for everything.')
    changes: dict[str, Any] | None = Field(default=None, description='set: {"key": value, ...}, applied together '
                                                                     'or not at all.')


def _config(state: "RunState", args: ConfigArgs, operation_id: str) -> ToolResult:
    h = state.harness
    try:
        saved = settings.load_saved(h)
    except ConfigError as e:
        return ToolResult(False, {"error": f"the saved configuration doesn't load: {e}"})
    if args.op == "get":
        if args.key and "." in args.key:
            try:
                return ToolResult(True, settings.describe(saved, args.key, for_character=True))
            except ConfigError as e:
                return ToolResult(False, {"error": str(e)})
        values = settings.effective(saved, for_character=True)
        if args.key:
            values = {k: v for k, v in values.items() if k.split(".")[0] == args.key}
            if not values:
                return ToolResult(False, {"error": f"no section {args.key!r}"})
        out = {"settings": values,
               "note": "get with a full key shows its type, default, when it applies and whether changing it "
                       "needs your human's approval."}
        pending = settings.pending_restart(h.cfg, saved)
        if pending:
            out["saved_but_waiting_for_restart"] = pending
        return ToolResult(True, out)
    if not args.changes:
        return ToolResult(False, {"error": "set needs changes"})
    try:
        settings.with_changes(saved, args.changes)  # validate before asking anyone
    except ConfigError as e:
        return ToolResult(False, {"error": str(e)})
    if any(settings.policy(k).hidden for k in args.changes):
        return ToolResult(False, {"error": "unknown setting"})
    sensitive = sorted(k for k in args.changes if settings.policy(k).sensitive)
    if sensitive:
        summary = "Settings change: " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in args.changes.items())
        req = authz.request(h, settings.subject(args.changes), [settings.SCOPE], summary, state.run_id)
        return ToolResult(True, {"status": "waiting_for_approval", "approval_id": req["id"], "needs_approval": sensitive,
                                 "note": "Saved and applied only if your human approves; you'll be told."})
    out = h.apply_config(args.changes, actor="character", run_id=state.run_id)
    return ToolResult(True, out)


class RestartArgs(_Args):
    reason: str = Field(description="Why, in a sentence (shown to your human).")
    note: str = Field(default="", description="Optional note to yourself, shown when you wake after the restart.")


def _restart(state: "RunState", args: RestartArgs, operation_id: str) -> ToolResult:
    h = state.harness
    try:
        intent = h.request_restart(by="character", reason=args.reason, note=args.note, run_id=state.run_id)
    except restart.RestartUnavailable as e:
        return ToolResult(False, {"error": str(e)})
    return ToolResult(True, {"restart_id": intent["id"], "status": "scheduled",
                             "note": "It happens once this wake ends (call turn_finish when ready). The new "
                                     "process is checked first; if the check fails nothing restarts and you're "
                                     "told why. Afterwards you get a wake describing the outcome."})


class GuideArgs(_Args):
    topic: str | None = Field(default=None, description="A topic from the index; omit for the index.")


def guide_text(topic: str | None) -> str | None:
    name = (topic or "index").strip().lower().removesuffix(".md")
    if not name.replace("-", "").isalnum():
        return None
    f = resources.files("whim") / "guide" / f"{name}.md"
    return f.read_text() if f.is_file() else None


def _guide(state: "RunState", args: GuideArgs) -> ToolResult:
    text = guide_text(args.topic)
    if text is None:
        return ToolResult(False, {"error": f"no topic {args.topic!r}", "index": guide_text(None)})
    impl = access.implementation_root()
    return ToolResult(True, {"topic": args.topic or "index", "text": text, "implementation": str(impl),
                             "package": str(Path(__file__).resolve().parents[1])})


async def _guide_async(state: "RunState", args: GuideArgs) -> ToolResult:
    return _guide(state, args)


def register(reg: Registry) -> None:
    reg.register(Tool(
        name="host.guide",
        description=("How you and the harness you run in work, and how they're administered: an index of short "
                     "topics (memory and compaction, configuration, access, shell, restarts, plugins...) with "
                     "pointers into the source, which you can read."),
        params=GuideArgs, effect="read", impl="whim.builtin.host_guide@1", handler=_guide_async, replay_safe=True))
    reg.register(Tool(
        name="host.access",
        description=("List the folders you can use (beyond your workspace) and whether you may run shell commands; "
                     "or ask your human for access to a folder, to running commands, or to the network."),
        params=AccessArgs, effect="control", impl="whim.builtin.host_access@1", handler=_access, atomic=True))
    reg.register(Tool(
        name="host.config",
        description=("Read the harness's settings, or change them. Validated before saving; some apply at once, "
                     "others after a restart; some need your human's approval. Never holds secrets."),
        params=ConfigArgs, effect="write", impl="whim.builtin.host_config@1", handler=_config, atomic=True))
    reg.register(Tool(
        name="host.restart",
        description=("Restart the harness (e.g. to apply saved settings or changed source) after this wake ends. "
                     "Checked before it happens; you're told the outcome afterwards."),
        params=RestartArgs, effect="control", impl="whim.builtin.host_restart@1", handler=_restart, atomic=True))



