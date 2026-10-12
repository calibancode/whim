"""The terminal UI's view of whim, derived only from native ledger events
(docs/client_protocol.md §4). Nothing here is authoritative or stored: it is rebuilt by
replaying the ledger each time the TUI starts.

Transcript items are kept in ledger order and never removed. Widgets render items; the
model reports which items changed since the UI last asked (`take_changes`).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

from whim.tui import describe

KEEP_RUN_EVENTS = 300  # recent runs whose full event list is kept for the Activity view
HOST_EVENTS = 2000  # host-level events kept for the Activity view
ANSWER_PREFIX = "(answering your question:"


def parse_at(s: str | None) -> datetime:
    if not s:
        return datetime.now().astimezone()
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()


@dataclass(eq=False)
class Item:
    key: str
    at: datetime
    version: int = 0

    @property
    def visible(self) -> bool:
        return True


@dataclass(eq=False)
class Day(Item):
    pass


@dataclass(eq=False)
class Message(Item):
    role: str = "human"  # human | character
    text: str = ""
    origin: str | None = None  # character: what woke it, when not a conversation ("heartbeat", ...)
    question_id: str | None = None  # character: carried this question; human: answered it
    mode: str | None = None  # human: steer | next_run
    # A human message not yet seen in the ledger: sending | unsent | unconfirmed | accepted | rejected
    local: str | None = None
    note: str = ""


@dataclass(eq=False)
class Notice(Item):
    text: str = ""
    level: str = "info"  # info | warn | error


@dataclass(eq=False)
class Call:
    operation_id: str
    tool: str
    args: dict
    effect: str | None
    started: datetime
    status: str | None = None
    result: object = None
    reason: str | None = None
    finished: datetime | None = None
    recovered: bool = False
    delivered: bool = False  # it delivered a message to the human, which the transcript shows itself
    segment: "Segment | None" = None

    @property
    def description(self) -> str:
        return describe.operation(self.tool, self.args)

    @property
    def meaningful(self) -> bool:
        return describe.meaningful(self.tool, self.args, self.effect, self.status)


@dataclass(eq=False)
class Segment(Item):
    """A run's tool activity between two other transcript items."""
    run: "Run | None" = None
    calls: list[Call] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # text the character wrote but did not send

    def shown_calls(self) -> list[Call]:
        return [c for c in self.calls if not c.delivered and c.tool != "turn.finish"]

    @property
    def visible(self) -> bool:
        calls = self.shown_calls()
        if self.run is None or not self.run.autonomous:
            return bool(calls or self.notes)
        return any(c.meaningful for c in calls)


@dataclass(eq=False)
class Run:
    id: str
    origin: dict
    started: datetime
    ended: datetime | None = None
    outcome: str | None = None  # finished | paused | failed | interrupted
    reason: str | None = None
    detail: str | None = None
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    calls: dict[str, Call] = field(default_factory=dict)
    authored: list[tuple[datetime, str]] = field(default_factory=list)
    deliveries: int = 0
    events: list[dict] | None = field(default_factory=list)
    segment: Segment | None = None
    segments: int = 0

    @property
    def kind(self) -> str:
        return self.origin.get("kind", "")

    @property
    def autonomous(self) -> bool:
        return self.kind not in ("input", "control")

    @property
    def quiet(self) -> bool:
        return self.deliveries == 0 and not any(c.meaningful for c in self.calls.values())

    @property
    def troubles(self) -> int:
        return sum(c.status in describe.TROUBLE for c in self.calls.values()) + (self.outcome == "failed")


class Model:
    def __init__(self) -> None:
        self.items: list[Item] = []
        self.keys: dict[str, Item] = {}
        self.runs: dict[str, Run] = {}
        self.local: dict[str, Message] = {}  # client_message_id -> message not yet in the ledger
        self.seen_ids: set[str] = set()  # client_message_ids the ledger has shown
        self.questions: dict[str, dict] = {}  # pending: question_id -> {text, choices, expires_at}
        self.approvals: dict[str, dict] = {}  # pending: approval_id -> approval.requested data
        self.host_events: deque[dict] = deque(maxlen=HOST_EVENTS)
        self.cues: list[dict] | None = None  # as of the last heartbeat.manage result
        self.cues_at: datetime | None = None
        self.active_run: str | None = None
        self.activity = ""  # what the active run did last
        self.cursor = 0
        self.skipped: list[int] = []  # seqs of frames too large to read
        self._kept: deque[str] = deque()  # runs holding their event lists, oldest first
        self._last_day: date | None = None
        self._changed: dict[str, Item] = {}
        self._flags: set[str] = set()

    # -- reporting changes ----------------------------------------------------

    def take_changes(self) -> tuple[list[Item], set[str]]:
        items, flags = list(self._changed.values()), self._flags
        self._changed, self._flags = {}, set()
        return items, flags

    def _touch(self, item: Item | None) -> None:
        if item is not None:
            item.version += 1
            self._changed[item.key] = item

    def _append(self, item: Item) -> None:
        self.items.append(item)
        self.keys[item.key] = item
        self._touch(item)

    def _append_message(self, msg: Message) -> None:
        day = msg.at.date()
        if day != self._last_day and f"day:{day}" not in self.keys:
            self._append(Day(key=f"day:{day}", at=msg.at))
        self._last_day = day
        self._append(msg)

    # -- the human's own messages ----------------------------------------------

    def add_local(self, cmid: str, text: str, *, mode: str | None = None, question_id: str | None = None,
                  state: str = "sending") -> Message | None:
        if cmid in self.seen_ids:  # restored from a previous session, and already in the ledger
            return None
        msg = Message(key=f"local:{cmid}", at=datetime.now().astimezone(), role="human", text=text,
                      mode=mode, question_id=question_id, local=state)
        self.local[cmid] = msg
        self._append_message(msg)
        return msg

    def local_state(self, cmid: str, state: str, note: str = "") -> None:
        msg = self.local.get(cmid)
        if msg is not None:
            msg.local, msg.note = state, note
            self._touch(msg)
            if state == "rejected":
                self.local.pop(cmid, None)

    # -- events ----------------------------------------------------------------

    def apply(self, ev: dict) -> None:
        t, d, rid = ev.get("type", ""), ev.get("data") or {}, ev.get("run_id")
        at = parse_at(ev.get("at"))
        run = self._run(rid, at) if rid else None
        if run is not None:
            if run.events is not None:
                run.events.append(ev)
            self._flags.add("activity")
        elif t not in ("input.received",) or (d.get("sender") or {}).get("role") != "designated_human":
            self.host_events.append(ev)
            self._flags.add("activity")
        handler = getattr(self, "_on_" + t.replace(".", "_"), None)
        if handler is not None:
            handler(ev, d, at, run)
        if t.startswith(("schedule.", "jobs.", "cues.", "compaction.", "context.compacted", "config.",
                         "restart.", "run.", "host.", "grant.", "approval.")):
            self._flags.add("snapshot")

    def _run(self, rid: str, at: datetime) -> Run:
        run = self.runs.get(rid)
        if run is None:
            run = self.runs[rid] = Run(id=rid, origin={}, started=at)
            self._kept.append(rid)
            while len(self._kept) > KEEP_RUN_EVENTS:
                old = self.runs.get(self._kept.popleft())
                if old is not None:
                    old.events = None
        return run

    def _segment(self, run: Run, at: datetime) -> Segment:
        seg = run.segment
        if seg is None or not self.items or self.items[-1] is not seg:
            run.segments += 1
            seg = Segment(key=f"seg:{run.id}:{run.segments}", at=at, run=run)
            run.segment = seg
            self._append(seg)
        return seg

    def _notice(self, key: str, text: str, at: datetime, level: str = "info") -> None:
        if key not in self.keys:
            self._append(Notice(key=key, at=at, text=text, level=level))

    def _on_input_received(self, ev, d, at, run) -> None:
        if (d.get("sender") or {}).get("role") != "designated_human":
            return  # someone else, through an adapter: Activity only
        cmid, iid = d.get("source_message_id"), d.get("input_id")
        if cmid:
            self.seen_ids.add(cmid)
        text = d.get("text", "")
        if d.get("question_id") and text.startswith(ANSWER_PREFIX):
            text = text.partition("\n")[2] or text
        msg = self.local.pop(cmid, None) if cmid else None
        if msg is not None:
            msg.local, msg.note = None, ""
            self.keys[f"in:{iid}"] = msg
            self._touch(msg)
        elif f"in:{iid}" not in self.keys:
            self._append_message(Message(key=f"in:{iid}", at=at, role="human", text=text, mode=d.get("mode"),
                                         question_id=d.get("question_id")))

    def _on_run_started(self, ev, d, at, run) -> None:
        run.origin, run.started = ev.get("origin") or {}, at
        self.active_run, self.activity = run.id, ""
        self._flags.add("status")

    def _end(self, run: Run, at: datetime, outcome: str, d: dict) -> None:
        run.ended, run.outcome, run.reason, run.detail = at, outcome, d.get("reason"), d.get("detail")
        if self.active_run == run.id:
            self.active_run, self.activity = None, ""
        self._flags.add("status")
        if run.segment is not None:
            self._touch(run.segment)

    def _on_run_finished(self, ev, d, at, run) -> None:
        self._end(run, at, "finished", d)

    def _on_run_paused(self, ev, d, at, run) -> None:
        self._end(run, at, "paused", d)
        if not run.autonomous:
            self._notice(f"end:{run.id}", f"paused ({str(d.get('reason', '')).replace('_', ' ')})", at)

    def _on_run_failed(self, ev, d, at, run) -> None:
        self._end(run, at, "failed", d)
        if not run.autonomous or not run.quiet:
            detail = f": {d['detail']}" if d.get("detail") else ""
            self._notice(f"end:{run.id}", f"couldn't carry on ({d.get('reason', 'error')}{detail})", at, "error")

    def _on_run_interrupted(self, ev, d, at, run) -> None:
        self._end(run, at, "interrupted", d)
        text = {"operator_stop": "stopped", "operator_interrupt": "interrupted",
                "unclean_shutdown": "cut off: whim stopped unexpectedly"}.get(d.get("reason"), "interrupted")
        if not run.autonomous or not run.quiet:
            self._notice(f"end:{run.id}", text, at, "warn" if d.get("reason") == "unclean_shutdown" else "info")

    def _on_provider_request_finished(self, ev, d, at, run) -> None:
        u = d.get("usage") or {}
        run.requests += 1
        run.prompt_tokens += u.get("prompt_tokens") or 0
        run.completion_tokens += u.get("completion_tokens") or 0
        run.cache_hit_tokens += u.get("cache_hit_tokens") or 0

    def _on_message_completed(self, ev, d, at, run) -> None:
        text = (d.get("content") or "").strip()
        if not text:
            return
        run.authored.append((at, text))
        seg = self._segment(run, at) if not run.autonomous else run.segment
        if seg is not None and seg is (self.items[-1] if self.items else None):
            seg.notes.append(text)
            self._touch(seg)

    def _on_tool_started(self, ev, d, at, run) -> None:
        op = d.get("operation_id", "")
        args = d.get("args") if isinstance(d.get("args"), dict) else {}
        tool = d.get("tool", "?")
        call = run.calls.get(op)
        if call is None:
            call = run.calls[op] = Call(operation_id=op, tool=tool, args=args, effect=d.get("effect"), started=at,
                                        recovered=bool(d.get("recovered")))
            # Provisional until delivery.requested says where it went: the message itself is shown.
            call.delivered = tool == "human.ask" or (
                tool == "message.send" and args.get("destination") in ("human", "reply_to_source"))
            call.segment = self._segment(run, at)
            call.segment.calls.append(call)
        self._touch(call.segment)
        if self.active_run == run.id:
            self.activity = call.description
            self._flags.add("status")

    def _on_tool_finished(self, ev, d, at, run) -> None:
        op = d.get("operation_id", "")
        call = run.calls.get(op)
        if call is None:  # e.g. a frame we couldn't read; keep what we know
            self._on_tool_started(ev, {**d, "args": {}}, at, run)
            call = run.calls[op]
        call.status, call.result, call.reason, call.finished = d.get("status"), d.get("result"), d.get("reason"), at
        call.recovered = call.recovered or bool(d.get("recovered"))
        if call.status != "ok":
            call.delivered = False
        if call.tool == "heartbeat.manage" and call.status == "ok" and isinstance(call.result, dict) \
                and isinstance(call.result.get("cues"), list):
            self.cues, self.cues_at = call.result["cues"], at
            self._flags.add("snapshot")
        self._touch(call.segment)

    def _on_delivery_requested(self, ev, d, at, run) -> None:
        call = run.calls.get(d.get("operation_id", ""))
        if call is not None:
            call.delivered = d.get("destination") == "human"
            self._touch(call.segment)

    def _on_delivery_settled(self, ev, d, at, run) -> None:
        if d.get("destination") != "human" or d.get("status") != "sent":
            return
        run.deliveries += 1
        if f"dl:{d['delivery_id']}" in self.keys:
            return
        origin = describe.origin_phrase(run.origin) if run.autonomous and run.origin else None
        self._append_message(Message(key=f"dl:{d['delivery_id']}", at=at, role="character",
                                     text=d.get("content", ""), origin=origin, question_id=d.get("question_id")))

    def _on_question_created(self, ev, d, at, run) -> None:
        self.questions[d["question_id"]] = {"id": d["question_id"], "text": d.get("text", ""),
                                            "choices": d.get("choices") or [], "expires_at": d.get("expires_at")}
        self._flags.add("attention")

    def _on_question_answered(self, ev, d, at, run) -> None:  # also cancelled and expired
        self.questions.pop(d.get("question_id"), None)
        self._flags.add("attention")
        for item in reversed(self.items):  # the message that asked it no longer offers an answer
            if isinstance(item, Message) and item.role == "character" and item.question_id == d.get("question_id"):
                self._touch(item)
                break

    _on_question_cancelled = _on_question_expired = _on_question_answered

    def _on_approval_requested(self, ev, d, at, run) -> None:
        aid = d["approval_id"]
        self.approvals[aid] = d
        first = (d.get("summary") or "").splitlines()[0] if d.get("summary") else d.get("subject", "")
        self._notice(f"apr:{aid}", f"asked for your approval: {first}", at, "warn")
        self._flags.add("attention")

    def _on_approval_decided(self, ev, d, at, run) -> None:
        aid = d.get("approval_id")
        self.approvals.pop(aid, None)
        note = self.keys.get(f"apr:{aid}")
        if isinstance(note, Notice):
            note.text += f" — {'approved' if d.get('decision') in ('approve', 'approved') else 'denied'}"
            note.level = "info"
            self._touch(note)
        self._flags.add("attention")

    def _on_host_warning(self, ev, d, at, run) -> None:
        if d.get("kind") == "context_full":
            self._notice(f"ev:{ev.get('id')}", "working memory is full; new messages wait until it can be "
                                               "compacted", at, "warn")
        elif d.get("kind") == "context_full_cleared":
            self._notice(f"ev:{ev.get('id')}", "working memory has room again", at)

    def _on_restart_completed(self, ev, d, at, run) -> None:
        self._notice(f"ev:{ev.get('id')}", "whim restarted", at)
