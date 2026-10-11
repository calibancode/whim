"""Heartbeats: a recurring opportunity for the character to act on its own, plus the
character's structured standing cues. The schedule itself runs in `whim.scheduler`.

Schedule (host state in `meta.heartbeat`, all changes transactional and in the ledger):
- Nothing is armed until the first meeting is complete; then the first heartbeat is
  due one interval later (`schedule.armed`).
- When due, one heartbeat wake is queued (`schedule.due`). It waits in the ordinary
  wake queue: human input always goes first, and an active run is never interrupted.
- Further intervals missed while it waits (busy, or the host was down) coalesce into
  that same wake; there is never a backlog.
- Admission is the fire (`schedule.fired`, in the run's opening transaction). The next
  heartbeat is due one interval after that actual admission. Nothing else, such as a
  human conversation, moves the schedule.

Cues (tables `cues` / `cue_history`): host-issued ids, plain-text instruction,
active|paused, optional priority. Only active cues are shown, in the heartbeat's
opening message, read at admission time. Edits are atomic, revisioned and audited;
removed cues keep their history.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from whim.ledger import Ledger, dumps
from whim.timebase import iso, parse  # noqa: F401  (re-exported for callers)

MAX_CUE_BYTES = 256 * 1024  # safety ceiling on all stored (active + paused) cue text
PRIORITIES = ("low", "normal", "high")

CUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cues(
  id TEXT PRIMARY KEY,
  instruction TEXT NOT NULL,
  status TEXT NOT NULL,              -- active | paused | removed
  priority TEXT NOT NULL DEFAULT 'normal',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  created_rev INTEGER NOT NULL,
  updated_rev INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cue_history(
  rev INTEGER NOT NULL,
  cue_id TEXT NOT NULL,
  op TEXT NOT NULL,
  before TEXT,
  after TEXT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  run_id TEXT
);
"""

INSTRUCTION = (
    "Scheduled heartbeat: time of your own; nobody has messaged you. Act as you choose, or "
    "let it pass. Update or retire your standing cues with heartbeat_manage when useful."
)


def ensure_schema(led: Ledger) -> None:
    led.db.executescript(CUE_SCHEMA)


# -- cues ----------------------------------------------------------------------


def revision(led: Ledger) -> int:
    return led.get_meta("cues_revision", 0)


def cues(led: Ledger, *, statuses: tuple[str, ...] = ("active", "paused")) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    rows = led.q(
        f"SELECT * FROM cues WHERE status IN ({marks}) "
        "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_rev",
        *statuses,
    )
    return [dict(r) for r in rows]


def render_cues(led: Ledger) -> str:
    active = cues(led, statuses=("active",))
    if not active:
        return "You have no standing cues."
    lines = [f"Standing cues (revision {revision(led)}):"]
    for c in active:
        tag = "" if c["priority"] == "normal" else f" [{c['priority']}]"
        lines.append(f"- {c['id']}{tag}: {c['instruction']}")
    return "\n".join(lines)


def cue_digest(led: Ledger) -> str:
    active = [(c["id"], c["instruction"], c["priority"]) for c in cues(led, statuses=("active",))]
    return hashlib.sha256(dumps(active).encode()).hexdigest()[:16]


class CueOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add", "update", "pause", "resume", "remove"]
    id: str | None = Field(default=None, description="Cue id; required for everything except add.")
    instruction: str | None = Field(default=None, description="Cue text, for add or update.")
    priority: Literal["low", "normal", "high"] | None = Field(default=None, description="For add or update.")


class CueError(Exception):
    pass


def apply_ops(led: Ledger, ops: list[CueOp], *, actor: str, run_id: str | None,
              expected_revision: int | None, now: str) -> tuple[int, list[dict]]:
    """Apply all operations or none. Call inside a transaction."""
    rev = revision(led)
    if expected_revision is not None and expected_revision != rev:
        raise CueError(f"conflict: cues are at revision {rev}, not {expected_revision}; review and retry")
    if not ops:
        return rev, []
    new_rev = rev + 1
    results = []
    for i, o in enumerate(ops, 1):
        where = f"operation {i} ({o.op})"
        if o.op == "add":
            text = (o.instruction or "").strip()
            if not text:
                raise CueError(f"{where}: instruction is required")
            dup = led.q1("SELECT id FROM cues WHERE status != 'removed' AND instruction = ?", text)
            if dup:
                raise CueError(f"{where}: same text as {dup['id']}")
            n = led.get_meta("cues_next_id", 1)
            cue_id = f"hb_{n}"
            led.x("INSERT OR REPLACE INTO meta(key, value) VALUES('cues_next_id', ?)", dumps(n + 1))
            after = {"instruction": text, "status": "active", "priority": o.priority or "normal"}
            led.x("INSERT INTO cues(id, instruction, status, priority, created_at, updated_at, created_rev, updated_rev) "
                  "VALUES(?, ?, 'active', ?, ?, ?, ?, ?)", cue_id, text, after["priority"], now, now, new_rev, new_rev)
            before = None
        else:
            if not o.id:
                raise CueError(f"{where}: id is required")
            row = led.q1("SELECT * FROM cues WHERE id = ? AND status != 'removed'", o.id)
            if row is None:
                raise CueError(f"{where}: no cue {o.id!r}")
            cue_id = o.id
            before = {"instruction": row["instruction"], "status": row["status"], "priority": row["priority"]}
            after = dict(before)
            if o.op == "update":
                if o.instruction is None and o.priority is None:
                    raise CueError(f"{where}: give a new instruction and/or priority")
                if o.instruction is not None:
                    text = o.instruction.strip()
                    if not text:
                        raise CueError(f"{where}: instruction is empty; use remove instead")
                    dup = led.q1("SELECT id FROM cues WHERE status != 'removed' AND instruction = ? AND id != ?",
                                 text, cue_id)
                    if dup:
                        raise CueError(f"{where}: same text as {dup['id']}")
                    after["instruction"] = text
                if o.priority is not None:
                    after["priority"] = o.priority
            elif o.op == "pause":
                after["status"] = "paused"
            elif o.op == "resume":
                after["status"] = "active"
            elif o.op == "remove":
                after["status"] = "removed"
            led.x("UPDATE cues SET instruction = ?, status = ?, priority = ?, updated_at = ?, updated_rev = ? WHERE id = ?",
                  after["instruction"], after["status"], after["priority"], now, new_rev, cue_id)
        led.x("INSERT INTO cue_history(rev, cue_id, op, before, after, at, actor, run_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
              new_rev, cue_id, o.op, dumps(before) if before else None, dumps(after), now, actor, run_id)
        results.append({"op": o.op, "id": cue_id})
    total = led.q1("SELECT COALESCE(SUM(LENGTH(CAST(instruction AS BLOB))), 0) AS n FROM cues WHERE status != 'removed'")["n"]
    if total > MAX_CUE_BYTES:
        raise CueError(f"cues would total {total} bytes, over the {MAX_CUE_BYTES}-byte limit; "
                       "keep each cue short and put details in a file")
    led.x("INSERT OR REPLACE INTO meta(key, value) VALUES('cues_revision', ?)", dumps(new_rev))
    active = cues(led, statuses=("active",))
    led.append("cues.changed", {
        "revision": new_rev, "actor": actor, "operations": results,
        "active_count": len(active), "active_bytes": sum(len(c["instruction"].encode()) for c in active),
    }, run_id=run_id, actor=actor)
    return new_rev, results
