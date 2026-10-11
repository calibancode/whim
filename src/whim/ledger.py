"""The host-owned SQLite store: append-only event ledger plus the state tables that
recovery and context reconstruction depend on.

Every durable state change happens inside `Ledger.tx()`. Events appended inside a
transaction are published to live subscribers only after COMMIT, so subscribers never
see a fact that could still roll back.

Nesting: a `tx()` opened inside another *joins* it. It is not a separate transaction;
it commits or rolls back with the outermost one. If code inside the outer transaction
catches an exception from an inner `tx()` block, whatever that block wrote (rows and
events) stays part of the outer transaction. Use `savepoint()` when part of a
transaction must be undone on its own: it rolls back its rows and discards the
events appended inside it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from whim import events
from whim.timebase import Timebase

SCHEMA_VERSION = 1
# Events one live subscriber may have waiting. A subscriber that falls this far behind
# (a stalled client) is dropped rather than buffered without end; it resumes from its
# cursor, which the ledger can always replay.
SUBSCRIBER_QUEUE_LIMIT = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  at TEXT NOT NULL,
  type TEXT NOT NULL,
  run_id TEXT,
  cause_id TEXT,
  actor TEXT NOT NULL,
  origin TEXT,
  audience TEXT NOT NULL,
  data TEXT NOT NULL
);
-- Accepted inputs. Deduplicated by (source_kind, source_message_id).
CREATE TABLE IF NOT EXISTS inputs(
  id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_message_id TEXT,
  principal_id TEXT,
  route TEXT NOT NULL,
  text TEXT NOT NULL,
  received_at TEXT NOT NULL,
  status TEXT NOT NULL,              -- pending | admitted
  run_id TEXT,
  UNIQUE(source_kind, source_message_id)
);
-- Non-input wake requests (host/manual, recovery).
CREATE TABLE IF NOT EXISTS wakes(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                -- host | recovery
  status TEXT NOT NULL,              -- queued | admitted | cancelled
  created_at TEXT NOT NULL,
  data TEXT NOT NULL,
  run_id TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,              -- active | finished | interrupted | paused | failed
  origin TEXT NOT NULL,
  wake_id TEXT,
  resumes_run_id TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  reason TEXT,
  requests INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0
);
-- The working model context, in provider message format, including private
-- provider continuation state (reasoning_content). Never projected to frontends.
CREATE TABLE IF NOT EXISTS context(
  pos INTEGER PRIMARY KEY AUTOINCREMENT,
  epoch INTEGER NOT NULL,
  run_id TEXT,
  role TEXT NOT NULL,
  message TEXT NOT NULL
);
-- One row per tool call attempt; the recovery source of truth for effects.
CREATE TABLE IF NOT EXISTS operations(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  context_pos INTEGER NOT NULL,      -- assistant message that requested it
  call_index INTEGER NOT NULL,
  call_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  impl TEXT NOT NULL,
  effect TEXT NOT NULL,
  replay_safe INTEGER NOT NULL,
  args TEXT NOT NULL,
  status TEXT NOT NULL,              -- intent | dispatched | ok | error | not_dispatched | uncertain | interrupted
  result TEXT,
  UNIQUE(context_pos, call_index)
);
CREATE TABLE IF NOT EXISTS deliveries(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  operation_id TEXT,
  destination TEXT NOT NULL,
  content TEXT NOT NULL,
  reply_to TEXT,
  status TEXT NOT NULL,              -- requested | sent | failed | uncertain
  receipt TEXT
);
-- Host notes waiting to be shown to the character on its next admitted wake.
CREATE TABLE IF NOT EXISTS notes(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL,              -- pending | delivered
  run_id TEXT,
  created_at TEXT NOT NULL
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Ledger:
    def __init__(self, path: Path, *, character_id: str, thread_id: str, time: Timebase | None = None):
        self.path = path
        self.time = time or Timebase()
        self.character_id = character_id
        self.thread_id = thread_id
        self._depth = 0
        self._unpublished: list[dict] = []
        self._subscribers: dict[asyncio.Queue, Callable[[], None] | None] = {}
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        # Additive migrations for homes created by earlier versions.
        self.ensure_column("inputs", "mode", "TEXT NOT NULL DEFAULT 'normal'")  # normal | steer | next_run
        self.ensure_column("inputs", "question_id", "TEXT")

    def ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.db.close()

    # -- transactions --------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Reentrant write transaction. A nested tx() joins its parent (no independent
        rollback; see savepoint()). Events publish only after the outermost COMMIT."""
        if self._depth == 0:
            self.db.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self.db
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.db.execute("ROLLBACK")
                self._unpublished.clear()
            raise
        self._depth -= 1
        if self._depth == 0:
            self.db.execute("COMMIT")
            published, self._unpublished = self._unpublished, []
            for ev in published:
                for q, on_overflow in list(self._subscribers.items()):
                    try:
                        q.put_nowait(ev)
                    except asyncio.QueueFull:
                        del self._subscribers[q]  # it can only miss events now: cut it off
                        if on_overflow is not None:
                            on_overflow()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """A nested, independently rolled-back part of the current transaction. Events
        appended inside it are discarded with it, so a rolled-back fact never reaches
        subscribers (and never reuses a sequence number that a later event commits)."""
        assert self._depth > 0, "savepoints live inside Ledger.tx()"
        self._savepoints = getattr(self, "_savepoints", 0) + 1
        name = f"sp_{self._savepoints}"
        mark = len(self._unpublished)
        self.db.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.db.execute(f"ROLLBACK TO {name}")
            self.db.execute(f"RELEASE {name}")
            del self._unpublished[mark:]
            raise
        self.db.execute(f"RELEASE {name}")

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, *args: Any) -> sqlite3.Cursor:
        assert self._depth > 0, "writes must happen inside Ledger.tx()"
        return self.db.execute(sql, args)

    # -- meta ----------------------------------------------------------------

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.q1("SELECT value FROM meta WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.tx():
            self.x(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                key,
                dumps(value),
            )

    # -- events --------------------------------------------------------------

    def append(
        self,
        type: str,
        data: dict | None = None,
        *,
        run_id: str | None = None,
        cause_id: str | None = None,
        actor: str = "host",
        origin: dict | None = None,
        audience: str = "operator",
    ) -> dict:
        """Append one durable semantic fact. Must be called inside tx()."""
        if type not in events.DURABLE:
            raise ValueError(f"unknown durable event type {type!r}")
        if audience not in events.AUDIENCES:
            raise ValueError(f"unknown audience {audience!r}")
        ev_id = new_id("ev")
        at = self.time.iso()
        cur = self.x(
            "INSERT INTO events(id, at, type, run_id, cause_id, actor, origin, audience, data) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ev_id,
            at,
            type,
            run_id,
            cause_id,
            actor,
            dumps(origin) if origin is not None else None,
            audience,
            dumps(data or {}),
        )
        ev = self._envelope(
            seq=cur.lastrowid,
            id=ev_id,
            at=at,
            type=type,
            run_id=run_id,
            cause_id=cause_id,
            actor=actor,
            origin=origin,
            audience=audience,
            data=data or {},
        )
        self._unpublished.append(ev)
        return ev

    def _envelope(self, **f: Any) -> dict:
        ev = {
            "v": 1,
            "seq": f["seq"],
            "id": f["id"],
            "at": f["at"],
            "type": f["type"],
            "character_id": self.character_id,
            "thread_id": self.thread_id,
            "run_id": f["run_id"],
            "cause_id": f["cause_id"],
            "actor": f["actor"],
            "origin": f["origin"],
            "audience": f["audience"],
            "data": f["data"],
        }
        return {k: v for k, v in ev.items() if v is not None}

    def _row_event(self, r: sqlite3.Row) -> dict:
        return self._envelope(
            seq=r["seq"],
            id=r["id"],
            at=r["at"],
            type=r["type"],
            run_id=r["run_id"],
            cause_id=r["cause_id"],
            actor=r["actor"],
            origin=json.loads(r["origin"]) if r["origin"] else None,
            audience=r["audience"],
            data=json.loads(r["data"]),
        )

    def events_after(self, after: int, limit: int = 1000) -> list[dict]:
        rows = self.q("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", after, limit)
        return [self._row_event(r) for r in rows]

    def last_seq(self) -> int:
        row = self.q1("SELECT MAX(seq) AS s FROM events")
        return row["s"] or 0

    def subscribe(self, *, limit: int | None = None, on_overflow: Callable[[], None] | None = None) -> asyncio.Queue:
        """A live feed of committed events. If it ever holds `limit` undelivered events
        it is unsubscribed (receiving nothing more) and `on_overflow` is called once; the
        consumer must then end and let its client resume from the last event it got."""
        q: asyncio.Queue = asyncio.Queue(maxsize=limit or SUBSCRIBER_QUEUE_LIMIT)
        self._subscribers[q] = on_overflow
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)
