"""The TUI's side of the client protocol (docs/client_protocol.md): requests, the native
event stream with reconnection, and a durable outbox. No Textual here.

The TUI keeps only client state of its own, outside the whim home: the composer draft
and messages the daemon hasn't confirmed yet (`Store`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Callable

from whim.cli import NOT_REACHED, OVERSIZED, UNREACHABLE, Client
from whim.config import Config
from whim.tui.state import Model

log = logging.getLogger("whim.tui")
REQUEST_TIMEOUT = 15.0  # a local daemon answers far sooner; past this the outcome is unknown


class Store:
    """Draft and outbox, saved atomically under $XDG_STATE_HOME/whim (owner-only)."""

    def __init__(self, home: Path):
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "whim"
        self.path = base / f"tui-{hashlib.sha256(str(home).encode()).hexdigest()[:12]}.json"
        self.data: dict = {}
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("couldn't save TUI state to %s: %s", self.path, e)


class Outbox:
    """Everything the human submits that carries a client_message_id (messages, steers,
    answers), sent in order. Each item keeps its body and id until the daemon answers:
    a lost acknowledgement is retried with the same id, which the daemon deduplicates.
    Items are saved before sending, so quitting or a crash can't lose one; a later
    session retries them. Only an HTTP refusal drops an item (reported as rejected).

    `on_update(item, state, note)` reports: unsent (whim not reachable: not received),
    unconfirmed (the outcome of an attempt is unknown), accepted, rejected."""

    def __init__(self, store: Store, on_update: Callable[[dict, str, str], None]):
        self.store = store
        self.on_update = on_update
        self.items: list[dict] = list(store.data.get("outbox", []))
        self.changed = asyncio.Event()
        if self.items:
            self.changed.set()

    def add(self, path: str, body: dict, *, text: str, mode: str | None = None,
            question_id: str | None = None) -> dict:
        cmid = str(uuid.uuid4())
        item = {"cmid": cmid, "path": path, "body": {**body, "client_message_id": cmid}, "state": "new",
                "text": text, "mode": mode, "question_id": question_id, "created": time.time()}
        self.items.append(item)
        self._save()  # durable before anything else happens to it
        self.changed.set()
        return item

    def _save(self) -> None:
        self.store.data["outbox"] = self.items
        self.store.save()

    async def run(self, client: Client) -> None:
        delay = 0.5
        while True:
            await self.changed.wait()
            self.changed.clear()
            while self.items:
                item = self.items[0]
                try:
                    status, data = await client.try_post(item["path"], item["body"])
                except NOT_REACHED:
                    if item["state"] != "unconfirmed":
                        item["state"] = "unsent"
                        self.on_update(item, "unsent", "")
                except UNREACHABLE:
                    item["state"] = "unconfirmed"
                    self.on_update(item, "unconfirmed", "")
                else:
                    retried = item["state"] != "new"
                    self.items.pop(0)
                    self._save()
                    delay = 0.5
                    # An answer whose first attempt went through is "already answered" on a retry.
                    if status == 409 and retried and "/answer" in item["path"] and "already" in str(data.get("error")):
                        status = 200
                    if status >= 400:
                        self.on_update(item, "rejected", str(data.get("error") or f"HTTP {status}"))
                    else:
                        self.on_update(item, "accepted", "")
                    continue
                self._save()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)


class Link:
    """The connection to the daemon. Replays the whole native event stream into `model`
    from seq 0, then follows it live, resuming from the last processed seq after any
    drop (daemon restart, crash, slow-client disconnect).

    Callbacks: `on_events()` after events were applied; `on_state()` when the connection
    state or the snapshot changes; `on_loaded()` once history up to the first snapshot's
    cursor has been replayed."""

    def __init__(self, cfg: Config, model: Model, *, on_events: Callable[[], None],
                 on_state: Callable[[], None], on_loaded: Callable[[], None]):
        self.cfg = cfg
        self.model = model
        self.on_events, self.on_state, self.on_loaded = on_events, on_state, on_loaded
        self.client: Client | None = None
        self.state = "connecting"  # connecting | connected | down | absent
        self.snapshot: dict = {}
        self.loaded = False
        self.problem = ""  # the last stream problem worth showing

    async def start(self) -> None:
        self.client = Client(self.cfg, read_timeout=REQUEST_TIMEOUT, interface="tui")

    async def close(self) -> None:
        if self.client is not None:
            await self.client.session.close()

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.on_state()

    async def refresh_snapshot(self) -> None:
        try:
            snap = await self.client.get("/v1/snapshot")
        except UNREACHABLE:
            return
        if isinstance(snap, dict) and "cursor" in snap:
            self.snapshot = snap
            self.on_state()

    async def follow(self) -> None:
        delay = 0.5
        while True:
            try:
                snap = await self.client.get("/v1/snapshot")
                self.snapshot = snap
                target = snap["cursor"]
                self.on_state()
                if not self.loaded and self.model.cursor >= target:
                    self._loaded()

                def opened() -> None:
                    nonlocal delay
                    delay = 0.5
                    self.problem = ""
                    self._set("connected")

                async for seq, ev in self.client.frames(self.model.cursor, follow=True, on_open=opened):
                    if ev is OVERSIZED:
                        self.model.skipped.append(seq)
                    elif ev is not None:
                        try:
                            self.model.apply(ev)
                        except Exception:  # noqa: BLE001 - one bad event must not stop the stream
                            log.exception("couldn't apply event %s", seq)
                            self.problem = f"couldn't show event {seq}"
                    if seq is not None:
                        self.model.cursor = seq  # only after handling it: never skipped silently
                    if not self.loaded and self.model.cursor >= target:
                        self._loaded()
                    elif self.loaded:
                        self.on_events()
            except asyncio.CancelledError:
                raise
            except (*UNREACHABLE, KeyError, ValueError) as e:
                if not isinstance(e, NOT_REACHED + (asyncio.TimeoutError,)):
                    self.problem = f"{type(e).__name__}: {e}"
            except Exception as e:  # noqa: BLE001 - e.g. a malformed stream: report it and reconnect
                log.exception("event stream error")
                self.problem = f"{type(e).__name__}: {e}"
            self._set("down" if self.cfg.socket_path.exists() else "absent")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)

    def _loaded(self) -> None:
        self.loaded = True
        self.on_loaded()

    async def request(self, method: str, path: str, body: dict | None = None) -> tuple[bool, str, dict]:
        """One request that acts on current state (not retried). Returns (ok, message, data);
        the message says what happened in words when not ok."""
        try:
            if method == "GET":
                async with self.client.session.get(f"http://whim{path}") as r:
                    status, data = r.status, await r.json(content_type=None)
            else:
                status, data = await self.client.try_post(path, body or {})
        except NOT_REACHED:
            return False, "not sent: whim isn't reachable", {}
        except UNREACHABLE:
            return False, "outcome unknown: whim may have received this; check before repeating it", {}
        if status >= 400:
            return False, str(data.get("error") or f"HTTP {status}"), data if isinstance(data, dict) else {}
        return True, "", data
