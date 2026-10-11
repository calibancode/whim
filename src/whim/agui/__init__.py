"""AG-UI 1.0 read projection of the ledger.

The SQLite ledger stays the source of truth; this module only translates its durable
events into AG-UI events for frontends. Schema: `schema-1.0.json`, pinned from
ag-ui-protocol/ag-ui@fdbca490dc90fd8fbce2b342df79383bd70559af (docs/spec/1.0/schema.json).

Views (each a superset of the previous):
  conversation  run lifecycle, the human's messages, messages actually delivered to the
                human (`message_send` to human), and approval requests addressed to them.
  activity      + the character's private authored text (marked as not delivered),
                tool calls and results, other deliveries, scheduling, cue/job changes,
                first-meeting events.
  inspect       + per-request usage, host context notes and remaining run-scoped facts.

Rules:
- AG-UI requires every stream to open with RUN_STARTED and forbids events outside a
  run, so only run-scoped ledger events are projected. Host-level facts between runs
  (wake queued, schedule due, host started...) remain in the native /v1/events stream.
- Audience filtering happens before projection, on replay and live alike. Run
  lifecycle is structural and is shown to any viewer, with details only for operators.
- A run paused by a host limit closes with RUN_FINISHED + cancelled outcome; a failed
  or crash-interrupted run closes with RUN_ERROR.
- Private text is never presented as a reply: authored text appears only in activity
  and inspect views, marked `metadata.whim.kind = "authored"`.
- No REASONING_* events: stored provider reasoning is never projected.
- A user message carries the host-verified sender in `metadata.whim.sender` (role,
  verified_by, label; an external sender's platform id, name and channel) and where a
  reply goes in `metadata.whim.reply_destination`; an external sender's label is also
  the AG-UI `name`. External input is operator-audience, so only entitled viewers see it.
- Cursor = ledger seq. All AG-UI events from one ledger event share it, and the SSE id
  is sent once the whole group is out, so a reconnect never splits or repeats a
  completed group. A stream resuming inside an open run first restates RUN_STARTED,
  plus a complete MESSAGES_SNAPSHOT for the view if a tool call is still open there
  (AG-UI restates history only through snapshots, which reconcile by id).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from whim.timebase import parse

if TYPE_CHECKING:
    from whim.harness import Harness

PROTOCOL_VERSION = "1.0"
VIEWS = ("conversation", "activity", "inspect")
TERMINAL = ("run.finished", "run.paused", "run.failed", "run.interrupted")
LIFECYCLE = ("run.started", *TERMINAL)
SCHEMA_PATH = Path(__file__).with_name("schema-1.0.json")
# AG-UI TokenUsage field <- DeepSeek usage field (prompt tokens already include cache hits).
FIELDS = {"inputTokens": "prompt_tokens", "outputTokens": "completion_tokens",
          "cachedInputTokens": "cache_hit_tokens", "reasoningTokens": "reasoning_tokens"}


@lru_cache(maxsize=1)
def _validator():
    import jsonschema

    return jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def validate(event: dict) -> None:
    """Raise jsonschema.ValidationError if `event` isn't a valid AG-UI 1.0 event."""
    _validator().validate(event)


def _ms(at: str) -> int:
    return int(parse(at).timestamp() * 1000)


def _text(message_id: str, role: str, text: str, ts: int, meta: dict) -> list[dict]:
    start = {"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": role, "timestamp": ts}
    if meta:
        start["metadata"] = {"whim": meta}
    return [start,
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": text, "timestamp": ts},
            {"type": "TEXT_MESSAGE_END", "messageId": message_id, "timestamp": ts}]


def _custom(name: str, value, ts: int) -> list[dict]:
    return [{"type": "CUSTOM", "name": f"whim.{name}", "value": value, "timestamp": ts}]


class Projector:
    def __init__(self, h: "Harness", view: str = "conversation",
                 entitlements: Iterable[str] = ("operator", "human")):
        if view not in VIEWS:
            raise ValueError(f"unknown view {view!r}")
        self.h = h
        self.view = view
        self.level = VIEWS.index(view)
        self.entitlements = frozenset(entitlements)
        self.operator = "operator" in self.entitlements
        self.thread_id = h.cfg.thread_id

    # -- stream assembly -------------------------------------------------------

    def resume_prefix(self, after: int) -> list[dict]:
        """Events a stream resuming after `after` must start with: RUN_STARTED restated
        for a run that was open at that point."""
        led = self.h.ledger
        marks = ",".join("?" * len(TERMINAL))
        row = led.q1(
            f"SELECT seq FROM events WHERE type = 'run.started' AND seq <= ? AND run_id NOT IN "
            f"(SELECT run_id FROM events WHERE type IN ({marks}) AND seq <= ? AND run_id IS NOT NULL) "
            "ORDER BY seq DESC LIMIT 1",
            after, *TERMINAL, after,
        )
        if row is None:
            return []
        started = led.events_after(row["seq"] - 1, 1)[0]
        ev = self.project(started)[0]  # exactly as it was sent live, marked as restated
        ev.setdefault("metadata", {}).setdefault("whim", {})["restated"] = True
        if not self._open_tool_call(started["run_id"], after):
            return [ev]
        # A tool call opened before the cursor will be answered in this stream. AG-UI
        # restates history only as a snapshot, which must be complete for the view.
        return [ev, {"type": "MESSAGES_SNAPSHOT", "messages": self.messages(after)}]

    def _open_tool_call(self, run_id: str, after: int) -> bool:
        led = self.h.ledger
        started = {json.loads(r["data"])["operation_id"] for r in led.q(
            "SELECT data FROM events WHERE run_id = ? AND type = 'tool.started' AND seq <= ?", run_id, after)}
        finished = {json.loads(r["data"])["operation_id"] for r in led.q(
            "SELECT data FROM events WHERE run_id = ? AND type = 'tool.finished' AND seq <= ?", run_id, after)}
        return self.level >= 1 and bool(started - finished)

    def messages(self, through: int) -> list[dict]:
        """The view's complete message list as of ledger seq `through` (for MESSAGES_SNAPSHOT)."""
        msgs: dict[str, dict] = {}
        args: dict[str, str] = {}
        last = 0
        while batch := self.h.ledger.events_after(last, 1000):
            for ev in batch:
                if ev["seq"] > through:
                    return list(msgs.values())
                self._accumulate(ev, msgs, args)
            last = batch[-1]["seq"]
        return list(msgs.values())

    def _accumulate(self, ev: dict, msgs: dict, args: dict) -> None:
        for e in self.project(ev):
            t = e["type"]
            if t == "TEXT_MESSAGE_START":
                # Keep what the live stream said about the message (authored vs delivered,
                # sender, question): a snapshot must mean the same as the events it restates.
                msgs[e["messageId"]] = {"id": e["messageId"], "role": e["role"], "content": "",
                                        **{k: e[k] for k in ("name", "metadata") if k in e}}
            elif t == "TEXT_MESSAGE_CONTENT":
                msgs[e["messageId"]]["content"] += e["delta"]
            elif t == "TOOL_CALL_START":
                args[e["toolCallId"]] = ""
                msgs[e["parentMessageId"]] = {"id": e["parentMessageId"], "role": "assistant", "toolCalls": [
                    {"id": e["toolCallId"], "type": "function",
                     "function": {"name": e["toolCallName"], "arguments": ""}}]}
            elif t == "TOOL_CALL_ARGS":
                args[e["toolCallId"]] += e["delta"]
            elif t == "TOOL_CALL_END":
                msgs[f"{e['toolCallId']}.call"]["toolCalls"][0]["function"]["arguments"] = args[e["toolCallId"]]
            elif t == "TOOL_CALL_RESULT":
                msgs[e["messageId"]] = {"id": e["messageId"], "role": "tool", "content": e["content"],
                                        "toolCallId": e["toolCallId"]}

    def visible(self, ev: dict) -> bool:
        return ev.get("run_id") is not None and (ev["type"] in LIFECYCLE or ev["audience"] in self.entitlements)

    def project(self, ev: dict) -> list[dict]:
        """AG-UI events for one ledger event (possibly none)."""
        if not self.visible(ev):
            return []
        handler = getattr(self, "_" + ev["type"].replace(".", "_"), None)
        out = handler(ev, ev.get("data", {}), _ms(ev["at"])) if handler else None
        if out is None:
            out = self._fallback(ev, _ms(ev["at"]))
        return out

    # -- lifecycle -------------------------------------------------------------

    def _run_started(self, ev, d, ts):
        out = {"type": "RUN_STARTED", "threadId": self.thread_id, "runId": ev["run_id"],
               "protocolVersion": PROTOCOL_VERSION, "timestamp": ts}
        if self.operator:
            meta = {"origin": (ev.get("origin") or {}).get("kind")}
            if d.get("resumes_run_id"):
                meta["resumes_run_id"] = d["resumes_run_id"]
            out["metadata"] = {"whim": meta}
        return [out]

    def _usage(self, run_id: str) -> list[dict]:
        totals: dict[tuple, dict] = {}
        for r in self.h.ledger.q("SELECT data FROM events WHERE run_id = ? AND type = 'provider.request.finished'",
                                 run_id):
            d = json.loads(r["data"])
            u = d.get("usage") or {}
            key = (d.get("provider") or "unknown", d.get("model") or "unknown")
            t = totals.setdefault(key, {k: [] for k in FIELDS})
            for k, src in FIELDS.items():
                t[k].append(u.get(src))
        entries = []
        for (provider, model), t in totals.items():
            e = {"provider": provider, "model": model}
            # A count appears only if every request reported it: absent never means zero.
            for k, values in t.items():
                if all(v is not None for v in values):
                    e[k] = sum(values)
            if "inputTokens" in e and "outputTokens" in e:
                e["totalTokens"] = e["inputTokens"] + e["outputTokens"]
            entries.append(e)
        return entries

    def _close(self, ev, ts, base: dict) -> list[dict]:
        base.update(timestamp=ts)
        if self.operator:
            usage = self._usage(ev["run_id"])
            if usage:
                base["usage"] = usage
        return [base]

    def _run_finished(self, ev, d, ts):
        return self._close(ev, ts, {"type": "RUN_FINISHED", "threadId": self.thread_id, "runId": ev["run_id"]})

    def _run_paused(self, ev, d, ts):
        out = {"type": "RUN_FINISHED", "threadId": self.thread_id, "runId": ev["run_id"],
               "outcome": {"type": "cancelled"}}
        if self.operator:
            out["metadata"] = {"whim": {"stopped_by": "host_limit", "reason": d.get("reason")}}
        return self._close(ev, ts, out)

    def _run_failed(self, ev, d, ts):
        msg = f"run failed: {d.get('reason')}" if self.operator else "run failed"
        return self._close(ev, ts, {"type": "RUN_ERROR", "message": msg, "code": d.get("reason") or "failed"})

    def _run_interrupted(self, ev, d, ts):
        return self._close(ev, ts, {"type": "RUN_ERROR", "message": "the host stopped unexpectedly during this run",
                                    "code": "interrupted"})

    # -- messages ----------------------------------------------------------------

    def _input_admitted(self, ev, d, ts):
        row = self.h.ledger.q1("SELECT * FROM inputs WHERE id = ?", d["input_id"])
        if row is None or (self.level == 0 and row["source_kind"] != "direct"):
            return []
        from whim.harness import attribution

        route = json.loads(row["route"])
        sender = attribution(row["source_kind"], route)
        meta = {"kind": "input", "via": route.get("via"), "sender": sender,
                "reply_destination": route.get("destination")}
        if row["question_id"]:
            meta["question_id"] = row["question_id"]
        if row["mode"] != "normal":
            meta["mode"] = row["mode"]
        out = _text(row["id"], "user", row["text"], ts, meta)
        if sender["role"] == "external":
            out[0]["name"] = sender["label"]
        return out

    def _delivery_settled(self, ev, d, ts):
        if d.get("destination") == "human" and d.get("status") == "sent":
            meta = {"kind": "delivery", "destination": "human"}
            if d.get("question_id"):
                meta["question_id"] = d["question_id"]
            return _text(d["delivery_id"], "assistant", d.get("content", ""), ts, meta)
        if self.level < 1:
            return []
        return _custom("delivery", {k: d.get(k) for k in ("delivery_id", "destination", "status")}, ts)

    def _message_completed(self, ev, d, ts):
        if self.level < 1:
            return []  # private authored text is never part of the conversation
        return _text(d["message_id"], "assistant", d["content"], ts, {"kind": "authored", "delivered": False})

    # -- tools ---------------------------------------------------------------------

    def _tool_started(self, ev, d, ts):
        if self.level < 1:
            return []
        call_id = d["operation_id"]
        args = d.get("args")
        delta = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
        start = {"type": "TOOL_CALL_START", "toolCallId": call_id, "toolCallName": d["tool"],
                 "parentMessageId": f"{call_id}.call", "timestamp": ts}
        return [start,
                {"type": "TOOL_CALL_ARGS", "toolCallId": call_id, "delta": delta, "timestamp": ts},
                {"type": "TOOL_CALL_END", "toolCallId": call_id, "timestamp": ts}]

    def _tool_finished(self, ev, d, ts):
        if self.level < 1:
            return []
        content = json.dumps({"status": d.get("status"), **({"result": d["result"]} if "result" in d else {})},
                             ensure_ascii=False)
        return [{"type": "TOOL_CALL_RESULT", "messageId": f"{d['operation_id']}.result",
                 "toolCallId": d["operation_id"], "content": content, "role": "tool", "timestamp": ts}]

    # -- everything else -------------------------------------------------------

    ACTIVITY_CUSTOM = ("schedule.fired", "cues.changed", "jobs.changed", "bootstrap.started", "bootstrap.completed",
                       "memory.checkpointed", "plugin.activated", "plugin.deactivated", "catalog.changed")

    def _approval_requested(self, ev, d, ts):
        # Addressed to the designated human, so even the conversation view carries it
        # (a frontend can render inline approve/deny controls; deciding goes through the
        # authenticated control endpoint, never through AG-UI).
        return _custom("approval.requested", {k: d.get(k) for k in ("approval_id", "scopes", "summary")}, ts)

    def _question_created(self, ev, d, ts):
        # Rendered structurally for the human (choices); the text itself also arrives as
        # the delivery that carried it.
        return _custom("question.created", {k: d.get(k) for k in ("question_id", "text", "choices", "expires_at")}, ts)

    def _question_cancelled(self, ev, d, ts):
        return _custom("question.cancelled", {"question_id": d.get("question_id")}, ts)

    def _fallback(self, ev, ts):
        if ev["type"] in self.ACTIVITY_CUSTOM and self.level >= 1:
            return _custom(ev["type"], ev.get("data", {}), ts)
        if self.level >= 2 and self.operator:
            return _custom(ev["type"], ev.get("data", {}), ts)
        return []
