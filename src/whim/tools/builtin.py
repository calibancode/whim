"""First-party tools for Milestone 1."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from whim import (
    access,
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
MAX_SLICED_FILE_BYTES = 64 * 1024 * 1024  # files read by line range
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
    path: str = Field(description='A path relative to your workspace (e.g. MEMORY.md, notes/ideas.md), or an '
                                  'absolute path inside a folder you have access to (host_access lists them). '
                                  'A folder (e.g. "journal", or "." for the top) returns its listing.')
    offset: int | None = Field(default=None, ge=1, description="Optional: first line to return (1-based).")
    limit: int | None = Field(default=None, ge=1, description="Optional: how many lines to return.")


class WriteArgs(_Args):
    path: str = Field(description="Path relative to your workspace, or an absolute path in a folder you may "
                                  "write. Parent folders are created.")
    content: str = Field(description="The complete new file content (replaces the file).")


class EditArgs(_Args):
    path: str = Field(description="Same paths as file_write.")
    old: str = Field(description="Exact text to replace; it must occur exactly once unless all is true.")
    new: str = Field(description="Replacement text.")
    all: bool = Field(default=False, description="Replace every occurrence.")


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


def _locate(h, raw: str, *, write: bool = False) -> tuple[access.Root, str]:
    root, rel = access.resolve(h, raw, write=write)
    return root, "/".join(safefs.parts(rel))


async def _read(state: "RunState", args: ReadArgs) -> ToolResult:
    try:
        root, rel = _locate(state.harness, args.path)
        if not rel or safefs.kind(root.path, rel) == "dir":
            entries = [n + ("/" if k == "dir" else "") for n, k in safefs.listdir(root.path, rel) if k != "other"]
            if len(entries) > 1000:
                return ToolResult(True, {"path": args.path, "entries": entries[:1000], "truncated": True})
            return ToolResult(True, {"path": args.path, "entries": entries})
        sliced = args.offset is not None or args.limit is not None
        data = safefs.read_bytes(root.path, rel, limit=MAX_SLICED_FILE_BYTES if sliced else MAX_READ_BYTES)
    except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError) as e:
        return ToolResult(False, {"error": _fs_error(args.path, e)})
    if sliced:
        if len(data) > MAX_SLICED_FILE_BYTES:
            return ToolResult(False, {"error": f"{args.path} is too large to read by lines; use a shell command"})
        lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
        start = (args.offset or 1) - 1
        chunk = lines[start:start + (args.limit or len(lines))]
        text = "".join(chunk)
        truncated = len(text.encode()) > MAX_READ_BYTES
        out = {"path": args.path, "content": text[:MAX_READ_BYTES], "first_line": start + 1,
               "lines": len(chunk), "total_lines": len(lines)}
        return ToolResult(True, {**out, "truncated": True} if truncated else out)
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    out = {"path": args.path, "content": text, "truncated": truncated}
    if truncated:
        out["note"] = "use offset and limit (lines) to read the rest"
    return ToolResult(True, out)


def _fs_error(path: str, e: Exception) -> str:
    if isinstance(e, FileNotFoundError):
        return f"{path} doesn't exist"
    if isinstance(e, access.AccessError):
        return str(e)
    if isinstance(e, safefs.UnsafePath):
        return f"{path}: {e} (only regular files and folders, not symbolic links, can be used)"
    return f"{path}: {type(e).__name__}"


def _write_file(h, raw: str, data: bytes) -> tuple[ToolResult | None, list[str], access.Root | None]:
    """Shared checks and the write itself; returns (error result or None, parts, root)."""
    if len(data) > MAX_WRITE_BYTES:
        return ToolResult(False, {"error": f"content exceeds {MAX_WRITE_BYTES} bytes"}), [], None
    try:
        root, rel = _locate(h, raw, write=True)
        p = safefs.parts(rel)
        if root.kind == "workspace":
            if p == [bootstrap.FILE]:
                return ToolResult(False, {"error": f"{bootstrap.FILE} is managed by the host"}), p, root
            if p and p[0] == journal.DIR:
                return ToolResult(False, {"error": "journal files are append-only; use journal_write"}), p, root
        safefs.write_atomic(root.path, "/".join(p), data)
    except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError) as e:
        return ToolResult(False, {"error": _fs_error(raw, e)}), [], None
    return None, p, root


def _acks(root, p: list[str], data: bytes) -> dict | None:
    if root.kind != "workspace":
        return None
    return {name: hashlib.sha256(data).hexdigest() for name in CHARACTER_FILES if p == [name]} or None


async def _write(state: "RunState", args: WriteArgs) -> ToolResult:
    data = args.content.encode()
    err, p, root = _write_file(state.harness, args.path, data)
    if err is not None:
        return err
    return ToolResult(True, {"path": args.path, "bytes": len(data)}, file_acks=_acks(root, p, data))


async def _edit(state: "RunState", args: EditArgs) -> ToolResult:
    h = state.harness
    try:
        root, rel = _locate(h, args.path, write=True)
        raw = safefs.read_bytes(root.path, rel, limit=MAX_WRITE_BYTES)
    except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError) as e:
        return ToolResult(False, {"error": _fs_error(args.path, e)})
    if len(raw) > MAX_WRITE_BYTES:
        return ToolResult(False, {"error": f"{args.path} is larger than {MAX_WRITE_BYTES} bytes; edit it with a "
                                           "shell command instead"})
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ToolResult(False, {"error": f"{args.path} is not UTF-8 text"})
    n = text.count(args.old) if args.old else 0
    if n == 0:
        return ToolResult(False, {"error": "old text not found (it must match exactly, including whitespace)"})
    if n > 1 and not args.all:
        return ToolResult(False, {"error": f"old text occurs {n} times; include more context or set all"})
    data = text.replace(args.old, args.new).encode()
    err, p, root = _write_file(h, args.path, data)
    if err is not None:
        return err
    return ToolResult(True, {"path": args.path, "replaced": n, "bytes": len(data)}, file_acks=_acks(root, p, data))


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
            description="Read a text file (whole, or a range of lines), or list a folder.",
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
            description="Create or replace a text file.",
            params=WriteArgs,
            effect="write",
            impl="whim.builtin.file_write@1",
            handler=_write,
        )
    )
    reg.register(
        Tool(
            name="file.edit",
            description="Replace exact text in a file (safer than rewriting a whole file).",
            params=EditArgs,
            effect="write",
            impl="whim.builtin.file_edit@1",
            handler=_edit,
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

