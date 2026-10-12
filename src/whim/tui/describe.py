"""Short human descriptions of the character's tool calls, for transcript and activity lines."""

from __future__ import annotations

import json

STATUS_ICON = {None: "◌", "ok": "✓", "error": "✗", "not_dispatched": "⊘", "interrupted": "⏸", "uncertain": "?"}
STATUS_WORD = {None: "running", "ok": "done", "error": "failed", "not_dispatched": "not run",
               "interrupted": "cut off", "uncertain": "outcome uncertain"}
TROUBLE = ("error", "uncertain", "interrupted")


def _q(s: object, n: int = 40) -> str:
    s = " ".join(str(s or "").split())
    return f"“{s[:n]}…”" if len(s) > n else f"“{s}”"


def _ops(args: dict) -> list:
    return args.get("operations") or []


def operation(tool: str, args: dict | None) -> str:
    """What a call does, in a few words (past tense; the status icon says how it went)."""
    a = args if isinstance(args, dict) else {}
    match tool:
        case "file.read":
            return f"read {a.get('path', 'a file')}"
        case "file.write":
            return f"wrote {a.get('path', 'a file')}"
        case "file.edit":
            return f"edited {a.get('path', 'a file')}"
        case "journal.write":
            return "wrote in the journal"
        case "heartbeat.manage":
            n = len(_ops(a))
            return f"updated heartbeat cues ({n} change{'s' * (n != 1)})" if n else "looked over heartbeat cues"
        case "cron.manage":
            n = len(_ops(a))
            return f"updated scheduled jobs ({n} change{'s' * (n != 1)})" if n else "looked over scheduled jobs"
        case "message.send":
            return f"sent a message to {a.get('destination', '?')}"
        case "human.ask":
            return "asked you a question"
        case "human.cancel_question":
            return "withdrew a question"
        case "history.search":
            return f"searched history for {_q(a['query'])}" if a.get("query") else "looked back through history"
        case "host.guide":
            return f"read the guide ({a['topic']})" if a.get("topic") else "read the guide"
        case "host.access":
            if a.get("op") == "request":
                what = a.get("target") or "?"
                return f"asked for {'write ' if a.get('write') else ''}access to {what}"
            return "checked its access"
        case "host.config":
            if a.get("op") == "set":
                return "asked to change " + ", ".join(sorted((a.get("changes") or {}).keys()) or ["settings"])
            return f"checked settings{' (' + a['key'] + ')' if a.get('key') else ''}"
        case "host.restart":
            return "asked for a restart"
        case "shell.exec":
            return f"ran `{' '.join(str(a.get('command', '')).split())[:60]}`"
        case "shell.write":
            return "typed into a shell session"
        case "shell.kill":
            return "stopped a shell session"
        case "shell.list":
            return "listed shell sessions"
        case "plugin.manage":
            op = a.get("op", "")
            return f"{op} plugin {a.get('name') or a.get('path') or ''}".strip() if op != "list" else "listed plugins"
        case "tool.search":
            return f"looked for tools: {_q(a.get('query'))}"
        case "tool.describe":
            return f"looked up {a.get('name', 'a tool')}"
        case "bootstrap.complete":
            return "finished the first meeting"
        case "turn.finish":
            return "finished"
    return tool


def meaningful(tool: str, args: dict | None, effect: str | None, status: str | None) -> bool:
    """Worth surfacing from an autonomous wake: anything that changed or sent something,
    or went wrong. Looking around (reads, listings, empty manage calls) is not."""
    if status in TROUBLE:
        return True
    a = args if isinstance(args, dict) else {}
    if effect == "read" or tool in ("turn.finish", "shell.list"):
        return False
    if tool in ("heartbeat.manage", "cron.manage") and not _ops(a):
        return False
    if tool == "host.access" and a.get("op", "list") == "list":
        return False
    if tool == "host.config" and a.get("op", "get") == "get":
        return False
    if tool == "plugin.manage" and a.get("op") == "list":
        return False
    return True


ORIGINS = {  # what woke a run: (label, how a message from it is tagged)
    "heartbeat": ("heartbeat", "during a heartbeat"),
    "cron": ("scheduled job", "during a scheduled job"),
    "recovery": ("after a restart", "after a restart"),
    "maintenance": ("memory upkeep", "during memory upkeep"),
    "host": ("unprompted", "unprompted"),
    "input": ("conversation", ""),
    "control": ("conversation", ""),
}


def origin_label(origin: dict | None) -> str:
    kind = (origin or {}).get("kind", "")
    return ORIGINS.get(kind, (kind or "run", ""))[0]


def origin_phrase(origin: dict | None) -> str:
    kind = (origin or {}).get("kind", "")
    return ORIGINS.get(kind, ("", f"during {kind}"))[1]


def fields(value: object, *, indent: int = 4, style: str = "dim", limit: int = 600):
    """A dict as `key: value` lines (values compact, long strings abridged); anything
    else as one compact line. Returns a Rich Text."""
    from rich.text import Text

    pad = " " * indent
    t = Text(style=style)

    def compact(v: object) -> str:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if len(s) > limit:
            s = s[:limit] + f"… ({len(s) - limit} more)"
        return s.replace("\n", "\n" + pad + "  ")

    items = value.items() if isinstance(value, dict) else [(None, value)]
    for i, (k, v) in enumerate(items):
        if i:
            t.append("\n")
        t.append(pad)
        if k is not None:
            t.append(f"{k}: ", style="bold")
        t.append(compact(v))
    return t
