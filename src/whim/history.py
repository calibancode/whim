"""history_search: the character's way back into its own past after compaction.

Searches what people said to it, what it sent, its private authored text, and its
journal; or shows the events around one point. Read-only, straight from the ledger
and journal files: nothing is loaded into context unless the character asks.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from whim import context, journal, safefs
from whim.tools.registry import ToolResult

if TYPE_CHECKING:
    from whim.run import RunState

SNIPPET = 400
KINDS = {
    "input.received": ("message to me", "text"),
    "delivery.settled": ("I sent", "content"),
    "message.completed": ("my private text", "content"),
}


class HistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(default=None, description="Words to look for (all must appear, any case).")
    around: int | None = Field(default=None, description="A result's ref number: show what happened around it.")
    limit: int = Field(default=8, ge=1, le=30, description="Most results to return, newest first.")


def _snippet(text: str, terms: list[str]) -> str:
    if len(text) <= SNIPPET:
        return text
    low = text.casefold()
    hit = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, hit - SNIPPET // 3)
    return ("…" if start else "") + text[start:start + SNIPPET] + ("…" if start + SNIPPET < len(text) else "")


def _event_item(h, ev: dict, terms: list[str]) -> dict | None:
    kind, field = KINDS.get(ev["type"], (None, None))
    if kind is None:
        return None
    d = ev["data"]
    text = d.get(field)
    if not text:
        return None
    if ev["type"] == "delivery.settled":
        kind = f"I sent to {d.get('destination')}"
    elif ev["type"] == "input.received":
        kind = f"message to me via {d.get('via') or 'direct'}"
        if d.get("interface"):
            kind += f" · {context.interface_name(d['interface'])}"
    return {"ref": ev["seq"], "when": h.time.stamp(ev["at"]), "kind": kind, "text": _snippet(text, terms)}


def _journal_items(h, terms: list[str]) -> list[dict]:
    out = []
    try:
        names = [n for n, k in safefs.listdir(h.cfg.workspace, journal.DIR) if k == "file" and n.endswith(".md")]
    except (FileNotFoundError, NotADirectoryError, safefs.UnsafePath):
        return out
    for name in names:
        day = name[:-3]
        text = safefs.read_text(h.cfg.workspace, f"{journal.DIR}/{name}") or ""
        for chunk in re.split(r"\n(?=## \d\d:\d\d\n)", text):
            m = re.match(r"## (\d\d:\d\d)\n\n?(.*)", chunk, re.S)
            if not m:
                continue
            body = m.group(2).strip()
            if all(t in body.casefold() for t in terms):
                out.append({"ref": f"{journal.DIR}/{name}", "when": f"{day} {m.group(1)}", "kind": "journal",
                            "text": _snippet(body, terms)})
    return out


async def search(state: "RunState", args: HistoryArgs) -> ToolResult:
    h = state.harness
    led = h.ledger
    types = tuple(KINDS)
    marks = ",".join("?" * len(types))
    if args.around is not None:
        rows = led.q(f"SELECT * FROM events WHERE type IN ({marks}) AND seq <= ? ORDER BY seq DESC LIMIT 6",
                     *types, args.around)
        rows = list(reversed(rows)) + led.q(
            f"SELECT * FROM events WHERE type IN ({marks}) AND seq > ? ORDER BY seq LIMIT 5", *types, args.around)
        items = [it for r in rows if (it := _event_item(h, led._row_event(r), [])) is not None]
        return ToolResult(True, {"around": args.around, "items": items})
    terms = [t for t in (args.query or "").casefold().split() if t]
    if not terms:
        return ToolResult(False, {"error": "give a query, or around=<ref> from an earlier result"})
    like = " AND ".join(["LOWER(data) LIKE ?"] * len(terms))
    rows = led.q(f"SELECT * FROM events WHERE type IN ({marks}) AND {like} ORDER BY seq DESC LIMIT 200",
                 *types, *[f"%{t}%" for t in terms])
    items = []
    for r in rows:
        ev = led._row_event(r)
        it = _event_item(h, ev, terms)
        if it and all(t in json.dumps(ev["data"], ensure_ascii=False).casefold() for t in terms):
            items.append(it)
    items = items[: args.limit]
    found_journal = _journal_items(h, terms)[::-1][: args.limit]
    merged = sorted(items + found_journal, key=lambda it: it["when"], reverse=True)[: args.limit]
    return ToolResult(True, {"query": args.query, "items": merged,
                             "note": "Use around=<ref> with a number ref to see what happened near it."})
