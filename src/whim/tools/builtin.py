"""First-party tools for Milestone 1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from whim import (
    authz,
    bootstrap,
    cron,
    heartbeat,
    history,
    journal,
    plugins,
    questions,
    safefs,
)
from whim.config import CHARACTER_FILES
from whim.delivery import Deliveries
from whim.tools.registry import Registry, Tool, ToolResult

if TYPE_CHECKING:
    from whim.run import RunState

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 256 * 1024


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SendArgs(_Args):
    destination: str = Field(
        description='Where to deliver. "human" is your human\'s direct chat; "reply_to_source" '
        "replies wherever the latest message in this wake came from."
    )
    content: str = Field(description="Exactly the text the recipient will receive.")
    reply_to: str | None = Field(default=None, description="Optional id of the message you are replying to.")


class FinishArgs(_Args):
    pass


class BootstrapCompleteArgs(_Args):
    pass


class HeartbeatManageArgs(_Args):
    operations: list[heartbeat.CueOp] = Field(
        default_factory=list,
        description="Changes to apply together, all or none. Leave empty to just view your cues.",
    )
    expected_revision: int | None = Field(
        default=None, description="Optional: the cue revision you last saw; the edit is refused if it changed."
    )


class ReadArgs(_Args):
    path: str = Field(description='Path relative to your workspace, e.g. MEMORY.md or notes/ideas.md. '
                                  'A folder (e.g. "journal", or "." for the top) returns its listing.')


class WriteArgs(_Args):
    path: str = Field(description="Path relative to your workspace. Parent folders are created.")
    content: str = Field(description="The complete new file content (replaces the file).")


def workspace_path(workspace: Path, rel: str) -> Path:
    """Lexical check only (for messages and identity); actual file access goes through
    whim.safefs, which refuses links at the moment of opening."""
    safefs.parts(rel)
    return workspace / rel


async def _send(state: "RunState", args: SendArgs) -> ToolResult:
    if not args.content.strip():
        return ToolResult(False, {"error": "content is empty"})
    try:
        receipt = await state.harness.deliveries.send(
            state, state.operation_id, args.destination, args.content, args.reply_to
        )
    except LookupError as e:
        return ToolResult(False, {"error": str(e)})
    return ToolResult(receipt["status"] == "sent", receipt)


async def _read(state: "RunState", args: ReadArgs) -> ToolResult:
    ws = state.harness.cfg.workspace
    try:
        rel = "/".join(safefs.parts(args.path))
        if not rel or safefs.kind(ws, rel) == "dir":
            entries = [n + ("/" if k == "dir" else "") for n, k in safefs.listdir(ws, rel) if k != "other"]
            return ToolResult(True, {"path": args.path, "entries": entries})
        data = safefs.read_bytes(ws, rel, limit=MAX_READ_BYTES)
    except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError) as e:
        return ToolResult(False, {"error": _fs_error(args.path, e)})
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    return ToolResult(True, {"path": args.path, "content": text, "truncated": truncated})


def _fs_error(path: str, e: Exception) -> str:
    if isinstance(e, FileNotFoundError):
        return f"{path} doesn't exist"
    if isinstance(e, safefs.UnsafePath):
        return f"{path}: {e} (only regular files and folders inside your workspace can be used)"
    return f"{path}: {type(e).__name__}"


async def _write(state: "RunState", args: WriteArgs) -> ToolResult:
    data = args.content.encode()
    if len(data) > MAX_WRITE_BYTES:
        return ToolResult(False, {"error": f"content exceeds {MAX_WRITE_BYTES} bytes"})
    ws = state.harness.cfg.workspace
    try:
        p = safefs.parts(args.path)
        if p == [bootstrap.FILE]:
            return ToolResult(False, {"error": f"{bootstrap.FILE} is managed by the host"})
        if p and p[0] == journal.DIR:
            return ToolResult(False, {"error": "journal files are append-only; use journal_write"})
        safefs.write_atomic(ws, "/".join(p), data)
    except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError) as e:
        return ToolResult(False, {"error": _fs_error(args.path, e)})
    acks = {name: hashlib.sha256(data).hexdigest() for name in CHARACTER_FILES if p == [name]}
    return ToolResult(True, {"path": args.path, "bytes": len(data)}, file_acks=acks or None)


def _bootstrap_complete(state: "RunState", args: BootstrapCompleteArgs, operation_id: str) -> ToolResult:
    h = state.harness
    ok, data = bootstrap.complete(h, state.run_id, operation_id)
    return ToolResult(ok, data, after_commit=(lambda: bootstrap.retire_file(h)) if ok else None)


def _heartbeat_manage(state: "RunState", args: HeartbeatManageArgs, operation_id: str) -> ToolResult:
    led = state.harness.ledger
    try:
        with led.savepoint():
            rev, applied = heartbeat.apply_ops(
                led, args.operations, actor="character", run_id=state.run_id,
                expected_revision=args.expected_revision, now=state.harness.time.iso(),
            )
    except heartbeat.CueError as e:
        return ToolResult(False, {"error": str(e), "revision": heartbeat.revision(led)})
    current = [
        {"id": c["id"], "instruction": c["instruction"], "status": c["status"], "priority": c["priority"]}
        for c in heartbeat.cues(led)
    ]
    return ToolResult(True, {"revision": rev, "applied": applied, "cues": current})


class CronManageArgs(_Args):
    operations: list[cron.JobOp] = Field(
        default_factory=list,
        description="Changes to apply together, all or none. Leave empty to just list your jobs.",
    )
    expected_revision: int | None = Field(
        default=None, description="Optional: the job revision you last saw; the edit is refused if it changed."
    )


def _cron_manage(state: "RunState", args: CronManageArgs, operation_id: str) -> ToolResult:
    h = state.harness
    led = h.ledger
    try:
        with led.savepoint():
            rev, applied = cron.apply_ops(h, args.operations, actor="character", run_id=state.run_id,
                                          expected_revision=args.expected_revision)
    except cron.ScheduleError as e:
        return ToolResult(False, {"error": str(e), "revision": cron.revision(led)})
    return ToolResult(True, {"revision": rev, "applied": applied, "jobs": cron.listing(h)})


class PluginArgs(_Args):
    op: Literal["install", "activate", "remove", "list"]
    path: str | None = Field(default=None, description="install: the plugin folder in your workspace.")
    name: str | None = Field(default=None, description="activate / remove: the plugin's name.")


def _plugin_view(h, row) -> dict:
    out = {"name": row["name"], "version": row["impl"].split("@")[1], "status": row["status"],
           "tools": [f"{row['name']}_{t['name']}" for t in json.loads(row["tools"])],
           "permissions": json.loads(row["manifest"])["permissions"]}
    if row["error"]:
        out["error"] = row["error"]
    pending = h.ledger.q1("SELECT id FROM approvals WHERE subject = ? AND status = 'pending'", row["impl"])
    if pending:
        out["waiting_for_approval"] = pending["id"]
    return out


async def _plugin_manage(state: "RunState", args: PluginArgs) -> ToolResult:
    h = state.harness
    led = h.ledger
    if args.op == "list":
        rows = led.q("SELECT * FROM plugins WHERE status NOT IN ('superseded', 'removed') ORDER BY name, created_at")
        return ToolResult(True, {"plugins": [_plugin_view(h, r) for r in rows]})
    if args.op == "install":
        if not args.path:
            return ToolResult(False, {"error": "install needs path"})
        try:
            src = workspace_path(h.cfg.workspace, args.path)
            if safefs.kind(h.cfg.workspace, "/".join(safefs.parts(args.path))) != "dir":
                return ToolResult(False, {"error": f"{args.path} is not a folder"})
            row = await plugins.install(h, src, source="character")
        except (PermissionError, plugins.PluginRejected) as e:
            return ToolResult(False, {"error": str(e)})
        view = _plugin_view(h, row)
        if row["status"] == "failed":
            return ToolResult(False, {"error": "the plugin didn't pass validation", **view})
        return ToolResult(True, {**view, "next": "plugin_manage activate when you want to use it"})
    if not args.name:
        return ToolResult(False, {"error": f"{args.op} needs name"})
    row = plugins.latest(h, args.name)
    if row is None:
        return ToolResult(False, {"error": f"no plugin named {args.name!r}"})
    if args.op == "remove":
        if row["status"] != "active":
            return ToolResult(False, {"error": f"{args.name} isn't active", **_plugin_view(h, row)})
        with led.tx():
            plugins.deactivate(h, row["impl"], "removed", "removed by the character")
        return ToolResult(True, {"removed": args.name})
    # activate
    if row["status"] == "active":
        return ToolResult(True, {**_plugin_view(h, row), "note": "already active"})
    if row["status"] != "validated":
        return ToolResult(False, {"error": f"{args.name} can't be activated while {row['status']}",
                                  **_plugin_view(h, row)})
    needed = authz.missing(h, row["impl"], plugins.required_scopes(row))
    with led.tx():
        if not needed:
            plugins.activate(h, row["impl"], f"activated {row['impl']}")
            return ToolResult(True, {**_plugin_view(h, led.q1("SELECT * FROM plugins WHERE impl = ?", row["impl"])),
                                     "note": "active: its tools are available from your next step"})
        manifest = json.loads(row["manifest"])
        summary = (f"Plugin {row['name']} ({row['impl'].split('@')[1]}), tools "
                   f"{', '.join(_plugin_view(h, row)['tools'])}: needs {', '.join(needed)}. "
                   f"{manifest['description']}").strip()
        req = authz.request(h, row["impl"], needed, summary, state.run_id)
    return ToolResult(True, {"status": "waiting_for_approval", "approval_id": req["id"], "needs": needed,
                             "note": "Your human has to approve this in their whim control interface; you'll be "
                                     "told when they decide, and the tools appear then, even mid-wake."})


class ToolSearchArgs(_Args):
    query: str = Field(description="Words describing what you want to do.")


class ToolDescribeArgs(_Args):
    name: str = Field(description="A tool name as you'd call it, e.g. file_read or wordcount_count.")


def _all_known(h) -> list[tuple[object, str]]:
    """(tool, status) for every tool the host knows: offered now, or in a plugin not active."""
    _, offered = plugins.snapshot(h)
    out = [(t, "available") for t in offered.values()]
    for row in h.ledger.q("SELECT * FROM plugins WHERE status IN ('validated', 'denied', 'failed', 'active')"):
        status = {"validated": "needs activation", "denied": "denied", "failed": "failed validation",
                  "active": "awaiting authorization"}[row["status"]]
        for t in plugins._plugin_tools(h, row):
            if t.wire_name not in offered:
                out.append((t, status))
    return out


async def _tool_search(state: "RunState", args: ToolSearchArgs) -> ToolResult:
    terms = [t for t in args.query.casefold().split() if t]
    scored = []
    for tool, status in _all_known(state.harness):
        text = f"{tool.wire_name} {tool.description} {tool.help or ''}".casefold()
        score = sum(text.count(t) for t in terms) + sum(3 for t in terms if t in tool.wire_name)
        if score:
            first = tool.description if len(tool.description) <= 200 else tool.description[:200] + "…"
            params = sorted(tool.parameters().get("properties", {}))
            scored.append((score, {"name": tool.wire_name, "summary": first, "params": params, "status": status}))
    scored.sort(key=lambda s: -s[0])
    return ToolResult(True, {"results": [s[1] for s in scored[:8]],
                             "note": "tool_describe gives the full description and parameters."})


async def _tool_describe(state: "RunState", args: ToolDescribeArgs) -> ToolResult:
    for tool, status in _all_known(state.harness):
        if tool.wire_name == args.name:
            origin = "built in" if tool.impl.startswith("whim.") else f"plugin ({tool.impl})"
            out = {"name": tool.wire_name, "status": status, "origin": origin, "effect": tool.effect,
                   "description": tool.description, "parameters": tool.parameters()}
            if tool.help:
                out["help"] = tool.help
            return ToolResult(True, out)
    return ToolResult(False, {"error": f"no tool named {args.name!r}; try tool_search"})


def register_builtins(reg: Registry, deliveries: Deliveries, timezone: str = "UTC") -> None:
    dests = ", ".join(f'"{n}"' for n in deliveries.names())
    reg.register(
        Tool(
            name="message.send",
            description=(
                "Deliver a message to someone. This is the only way anything you write reaches "
                f"anyone. Available destinations: {dests}. Returns a delivery receipt. Does not end "
                "your turn."
            ),
            params=SendArgs,
            effect="send",
            impl="whim.builtin.message_send@1",
            handler=_send,
        )
    )
    reg.register(
        Tool(
            name="turn.finish",
            description=(
                "End this wake. Call it alone, with no other tool calls in the same response. "
                "Finishing without sending anything is fine."
            ),
            params=FinishArgs,
            effect="control",
            impl="whim.builtin.turn_finish@1",
        )
    )
    reg.register(
        Tool(
            name="file.read",
            description="Read a text file from your workspace, or list a folder.",
            params=ReadArgs,
            effect="read",
            impl="whim.builtin.file_read@1",
            handler=_read,
            replay_safe=True,
        )
    )
    reg.register(
        Tool(
            name="file.write",
            description="Create or replace a text file in your workspace.",
            params=WriteArgs,
            effect="write",
            impl="whim.builtin.file_write@1",
            handler=_write,
        )
    )
    reg.register(
        Tool(
            name="bootstrap.complete",
            description=(
                "Close your first-meeting period. Call it once you feel your first meeting with "
                "your human has done its job; the host then retires BOOTSTRAP.md for good. It "
                "does not end your turn or send anything."
            ),
            params=BootstrapCompleteArgs,
            effect="control",
            impl="whim.builtin.bootstrap_complete@1",
            handler=_bootstrap_complete,
            atomic=True,
            available=lambda h: bootstrap.state(h)["status"] == bootstrap.ACTIVE,
        )
    )
    reg.register(
        Tool(
            name="heartbeat.manage",
            description=(
                "View or edit your standing cues: short notes you leave for your future self, shown "
                "to you at each heartbeat (your recurring free time). Keep one concern per cue, and "
                "point to a file for details. Operations: add (instruction, optional priority "
                "low|normal|high), update (id, new instruction and/or priority), pause, resume, "
                "remove (id). Paused cues aren't shown at heartbeats. Changes take effect at the "
                "next heartbeat. With no operations, returns your current cues."
            ),
            params=HeartbeatManageArgs,
            effect="write",
            impl="whim.builtin.heartbeat_manage@1",
            handler=_heartbeat_manage,
            atomic=True,
        )
    )
    reg.register(
        Tool(
            name="journal.write",
            description=(
                "Add an entry to your journal. The host files it under today's date "
                "(journal/YYYY-MM-DD.md) with the time; entries are only ever appended. Read past "
                "days with file_read (file_read on \"journal\" lists them). The journal is a running "
                "record; MEMORY.md and USER.md stay the notes you curate."
            ),
            params=journal.JournalArgs,
            effect="write",
            impl="whim.builtin.journal_write@1",
            handler=journal.write,
            reconcile=journal.reconcile,
        )
    )
    reg.register(
        Tool(
            name="cron.manage",
            description=(
                "Schedule wakes for specific times: one-shot (at a local date/time, or in N minutes) "
                f"or recurring (5-field cron in your local time zone, {timezone}). When a job comes "
                "due you get a wake with its instruction; what you do then is up to you. Operations: "
                "add (instruction plus one of at, in_minutes, cron), update (id plus new instruction "
                "and/or timing), pause, resume, remove. With no operations, lists your jobs and how "
                "their last runs went. Heartbeat cues are for things to revisit in free time; jobs "
                "are for particular times."
            ),
            params=CronManageArgs,
            effect="write",
            impl="whim.builtin.cron_manage@1",
            handler=_cron_manage,
            atomic=True,
        )
    )
    reg.register(
        Tool(
            name="history.search",
            description=(
                "Look back through your own past: messages people sent you, messages you sent, your "
                "private text, and your journal. Give words to search for, or around=<ref> to see "
                "what happened near an earlier result. Useful for anything older than what you "
                "currently remember in detail."
            ),
            params=history.HistoryArgs,
            effect="read",
            impl="whim.builtin.history_search@1",
            handler=history.search,
            replay_safe=True,
        )
    )
    reg.register(
        Tool(
            name="plugin.manage",
            description=(
                "Make a reusable tool of your own as a small sandboxed plugin, or manage your "
                "plugins: install a plugin folder from your workspace, activate it, remove it, or "
                "list them. tool_describe on plugin_manage explains how to write one."
            ),
            params=PluginArgs,
            effect="control",
            impl="whim.builtin.plugin_manage@1",
            handler=_plugin_manage,
            help=plugins.HELP,
        )
    )
    reg.register(
        Tool(
            name="tool.search",
            description="Find tools by what they do, including plugin tools that aren't active yet.",
            params=ToolSearchArgs,
            effect="read",
            impl="whim.builtin.tool_search@1",
            handler=_tool_search,
            replay_safe=True,
        )
    )
    reg.register(
        Tool(
            name="tool.describe",
            description="Full description, parameters and status of one tool.",
            params=ToolDescribeArgs,
            effect="read",
            impl="whim.builtin.tool_describe@1",
            handler=_tool_describe,
            replay_safe=True,
        )
    )
    reg.register(
        Tool(
            name="human.ask",
            description=(
                "Ask your human a question, optionally with 2-5 choices (they can always answer in "
                "their own words). It is sent to them now and returns at once with a question id; "
                "their answer arrives later as a message from them. You don't have to wait for it. "
                "For an ordinary conversational question, message_send is enough; use this when a "
                "clear, trackable answer matters."
            ),
            params=questions.AskArgs,
            effect="send",
            impl="whim.builtin.human_ask@1",
            handler=questions.ask,
        )
    )
    reg.register(
        Tool(
            name="human.cancel_question",
            description="Withdraw a question you asked that no longer needs an answer.",
            params=questions.CancelArgs,
            effect="write",
            impl="whim.builtin.human_cancel_question@1",
            handler=questions.cancel,
        )
    )

