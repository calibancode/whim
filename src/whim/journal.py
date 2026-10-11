"""The character's journal: host-dated, append-only daily files under `journal/`.

The host picks the file (`journal/YYYY-MM-DD.md`, in the configured time zone) and the
time heading; the character supplies only the text. Entries are appended, never
rewritten, and `file_write` cannot modify journal files.

Crash safety: before writing, the host records the exact append it is about to make
(file, offset, length, hash). If the harness dies before the result is recorded,
startup reconciliation re-reads the file and can tell "fully written" (confirmed),
"nothing written" (safe to write again) and "partially or differently written"
(uncertain). It never re-appends on its own.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from whim import safefs
from whim.tools.registry import ToolResult

if TYPE_CHECKING:
    from whim.harness import Harness
    from whim.run import RunState

DIR = "journal"
MAX_ENTRY_BYTES = 64 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_appends(
  operation_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  offset INTEGER NOT NULL,
  length INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  planned_at TEXT NOT NULL
);
"""


class JournalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry: str = Field(description="What to write. The host adds the date and time.")


def ensure_schema(led) -> None:
    led.db.executescript(SCHEMA)


def _chunk(now, text: str, exists: bool) -> bytes:
    head = "" if exists else f"# {now:%A, %d %B %Y}\n"
    return f"{head}\n## {now:%H:%M}\n\n{text}\n".encode()


async def write(state: "RunState", args: JournalArgs) -> ToolResult:
    h = state.harness
    text = args.entry.strip()
    if not text:
        return ToolResult(False, {"error": "entry is empty"})
    if len(text.encode()) > MAX_ENTRY_BYTES:
        return ToolResult(False, {"error": f"entry exceeds {MAX_ENTRY_BYTES} bytes"})
    now = h.time.local()  # one reading: date, file and heading always agree
    rel = f"{DIR}/{now:%Y-%m-%d}.md"
    try:
        offset = safefs.size(h.cfg.workspace, rel) or 0
    except (safefs.UnsafePath, NotADirectoryError, IsADirectoryError) as e:
        return ToolResult(False, {"error": f"{rel}: {e}; the journal must be regular files in a journal folder"})
    data = _chunk(now, text, offset > 0)
    with h.ledger.tx():
        h.ledger.x(
            "INSERT INTO journal_appends(operation_id, path, offset, length, sha256, planned_at) VALUES(?, ?, ?, ?, ?, ?)",
            state.operation_id, rel, offset, len(data), hashlib.sha256(data).hexdigest(), h.time.iso(),
        )
    await h.fault("journal.before_append")
    start, _ = safefs.append(h.cfg.workspace, rel, data)  # refuses links at every path component
    if start != offset:  # someone else wrote in between: record where it actually went
        with h.ledger.tx():
            h.ledger.x("UPDATE journal_appends SET offset = ? WHERE operation_id = ?", start, state.operation_id)
    await h.fault("journal.after_append")
    return ToolResult(True, {"path": rel, "time": f"{now:%H:%M}"})


def reconcile(h: "Harness", op) -> tuple[str, str]:
    """Outcome of a journal append whose result was never recorded."""
    plan = h.ledger.q1("SELECT * FROM journal_appends WHERE operation_id = ?", op["id"])

    def result(ok: bool, **data) -> str:
        return json.dumps({"ok": ok, **data}, ensure_ascii=False)

    if plan is None:
        return "not_dispatched", result(False, outcome="not_dispatched",
                                        error="Not written: the harness stopped before saving this entry. "
                                              "Writing it again is safe.")
    try:
        data = safefs.read_bytes(h.cfg.workspace, plan["path"])
    except FileNotFoundError:
        data = b""
    except (safefs.UnsafePath, NotADirectoryError, IsADirectoryError):
        return "uncertain", result(False, outcome="uncertain", path=plan["path"],
                                   error="The journal file was replaced by something that isn't a regular file; "
                                         "whether this entry was saved can't be checked.")
    written = data[plan["offset"]:plan["offset"] + plan["length"]]
    if len(written) == plan["length"] and hashlib.sha256(written).hexdigest() == plan["sha256"]:
        return "ok", result(True, path=plan["path"], note="Entry confirmed during recovery.")
    if len(data) <= plan["offset"]:
        return "not_dispatched", result(False, outcome="not_dispatched", path=plan["path"],
                                        error="Not written: the harness stopped before saving this entry. "
                                              "Writing it again is safe.")
    return "uncertain", result(False, outcome="uncertain", path=plan["path"],
                               error="The harness stopped while saving this entry; it may be partly written. "
                                     "Check the file before writing it again.")
