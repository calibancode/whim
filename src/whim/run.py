"""One character run: many provider requests, ended only by an accepted `turn.finish`
or by the host (limits, failure, crash).

Durability rule: every fact the next step depends on is committed before that step.
- assistant message (incl. private reasoning_content) is committed before tools run
- each tool call commits `intent`, then `dispatched`, then its result + context row
- pending human input is drained into context exactly once, at a request boundary
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from whim import bootstrap, compaction, cron, plugins, recovery
from whim.context import CONTINUATION_CUE, block, input_block
from whim.ledger import dumps, new_id
from whim.provider.base import Completion, ProviderError, Request, ToolCall
from whim.tools.registry import ArgumentError, ToolResult

if TYPE_CHECKING:
    from whim.harness import Harness

FINISH_WIRE = "turn_finish"
PREVIEW_CHARS = 400


class RunEnded(Exception):
    """Internal: the run reached a terminal state that has already been committed."""


@dataclass
class RunState:
    harness: "Harness"
    run_id: str
    origin: dict
    reply_destination: str | None = None
    started: float = field(default_factory=time.monotonic)
    requests: int = 0
    tool_calls: int = 0
    text_streak: int = 0
    operation_id: str | None = None  # set while a tool handler runs
    direct_input: bool = False  # a message from the designated human was admitted to this run
    finish_nudged: bool = False
    stop_requested: bool = False  # the human asked this run to stop at its next safe point
    steered: bool = False  # a steering message cancelled the model request in flight


def _preview(v: object) -> object:
    if isinstance(v, str) and len(v) > PREVIEW_CHARS:
        return v[:PREVIEW_CHARS] + f"… ({len(v)} chars)"
    if isinstance(v, dict):
        return {k: _preview(x) for k, x in v.items()}
    return v


def drain_inputs(h: "Harness", state: RunState, *, at_start: bool = False) -> list[str]:
    """Admit pending input to this run, preceded by the first-meeting guide if this is the
    first direct contact. Mid-run, input queued for the next run keeps waiting.
    Call inside a transaction."""
    led = h.ledger
    if compaction.is_maintenance(state.origin):
        return []  # input waits until the compaction is committed
    rows = led.q("SELECT * FROM inputs WHERE status = 'pending' ORDER BY received_at, rowid")
    if not at_start:
        rows = [r for r in rows if r["mode"] != "next_run"]
    blocks = bootstrap.opening_blocks(h, state.run_id, rows) if rows else []
    for r in rows:
        led.x("UPDATE inputs SET status = 'admitted', run_id = ? WHERE id = ?", state.run_id, r["id"])
        led.append("input.admitted", {"input_id": r["id"]}, run_id=state.run_id,
                   audience="human" if r["source_kind"] == "direct" else "operator")
        blocks.append(input_block(r, h.time))
        if r["source_kind"] == "direct":
            state.direct_input = True
        state.reply_destination = json.loads(r["route"]).get("destination")
    return blocks


def end_run(h: "Harness", state: RunState, status: str, reason: str, **extra) -> None:
    led = h.ledger
    with led.tx():
        # Whatever ended the run, every tool call it made gets a recorded outcome.
        recovery.settle_dangling(h, state.run_id, f"run_{status}")
        led.x(
            "UPDATE runs SET status = ?, ended_at = ?, reason = ?, requests = ?, tool_calls = ? WHERE id = ?",
            status, h.time.iso(), reason, state.requests, state.tool_calls, state.run_id,
        )
        compaction.on_run_end(h, state.origin, state.run_id, status, reason)
        led.append(f"run.{status}", {"reason": reason, **extra}, run_id=state.run_id)
        cron.record_outcome(h, state.origin, state.run_id, status, reason)
        if status == "finished":
            led.x("INSERT OR REPLACE INTO meta(key, value) VALUES('recovery_attempts', '0')")


async def execute(h: "Harness", state: RunState) -> None:
    try:
        await _loop(h, state)
    except RunEnded:
        pass
    except ProviderError as e:
        end_run(h, state, "failed", "provider_error", detail=str(e))
    except Exception as e:  # host bug: never leave the run hanging 'active'
        h.log.exception("run %s failed", state.run_id)
        end_run(h, state, "failed", "host_error", detail=f"{type(e).__name__}: {e}")


async def _loop(h: "Harness", state: RunState) -> None:
    lim = h.cfg.limits
    while True:
        if state.stop_requested:
            _stop(h, state)
        if state.requests >= lim.max_requests_per_run:
            end_run(h, state, "paused", "request_limit")
            raise RunEnded
        if time.monotonic() - state.started > lim.max_run_seconds:
            end_run(h, state, "paused", "time_limit")
            raise RunEnded
        est = compaction.estimate(h)
        if compaction.is_maintenance(state.origin):
            if est >= compaction.maintenance_limit(h):
                end_run(h, state, "paused", "context_full")  # the compaction proceeds without it
                raise RunEnded
        elif compaction.may_force(h) and est >= h.cfg.compaction.force_tokens:
            _pause_for_compaction(h, state)
            raise RunEnded
        elif est >= compaction.hard_limit(h):
            _pause_for_compaction(h, state, reason="context_full")  # never send an oversized request
            raise RunEnded

        # One catalog generation per request: the tools it offers are the ones its calls run.
        gen, tools = plugins.snapshot(h)
        request = Request(messages=h.context.messages(), tools=[t.schema() for t in tools.values()])
        await h.fault("provider.before_request")
        h.current_step = {"kind": "model", "request": state.requests + 1, "since": h.time.iso()}
        call = asyncio.ensure_future(h.provider.complete(request))
        h.provider_call = call
        try:
            completion = await call
        except asyncio.CancelledError:
            if state.steered and asyncio.current_task().cancelling() == 0:
                state.steered = False  # a steering message replaced this request: nothing committed
                with h.ledger.tx():
                    h.ledger.append("context.note", {"kind": "steer_discarded_request"}, run_id=state.run_id)
                _boundary(h, state, cue_if_empty=False)
                continue
            raise
        finally:
            h.provider_call = None
            h.current_step = None
        await h.fault("provider.after_response")
        pos = _commit_response(h, state, completion, gen)
        await h.fault("provider.after_commit")

        if not completion.tool_calls:
            state.text_streak += 1
            if state.text_streak >= lim.max_text_only_streak:
                end_run(h, state, "paused", "stalled", text_only_responses=state.text_streak)
                raise RunEnded
            _boundary(h, state, cue_if_empty=True)
            continue

        state.text_streak = 0
        calls = completion.tool_calls
        finishing = len(calls) == 1 and calls[0].name == FINISH_WIRE
        # A stop lets an ordinary run's own finish through. Not maintenance: finishing it
        # would let the compaction the human just stopped go ahead.
        if state.stop_requested and (not finishing or compaction.is_maintenance(state.origin)):
            _stop(h, state, pos=pos, calls=calls, tools=tools)
        if finishing:
            if _unsent_reply(h, state, completion):
                state.finish_nudged = True
                _decline_finish(h, state, pos, calls[0], MISSED_REPLY)
                _boundary(h, state, cue_if_empty=False)
                continue
            _finish(h, state, pos, calls[0])
            raise RunEnded
        for idx, tc in enumerate(calls):
            if state.stop_requested:
                _stop(h, state, pos=pos, calls=calls[idx:], tools=tools, first_index=idx)
            if state.tool_calls >= lim.max_tool_calls_per_run:  # checked before every dispatch
                with h.ledger.tx():
                    _skip_calls(h, state, pos, calls[idx:], tools, idx,
                                f"not run: this wake reached its limit of {lim.max_tool_calls_per_run} tool calls")
                end_run(h, state, "paused", "tool_limit")
                raise RunEnded
            h.current_step = {"kind": "tool", "tool": tc.name, "since": h.time.iso()}
            try:
                await _execute_tool(h, state, pos, idx, tc, tools)
            finally:
                h.current_step = None
        if state.tool_calls >= lim.max_tool_calls_per_run:
            end_run(h, state, "paused", "tool_limit")
            raise RunEnded
        _boundary(h, state, cue_if_empty=False)


def _skip_calls(h: "Harness", state: RunState, pos, calls, tools, first_index: int, reason: str) -> None:
    """Record requested calls that will not run, so each still has a result. Inside a tx."""
    led = h.ledger
    for i, tc in enumerate(calls, first_index):
        tool = (tools or {}).get(tc.name)
        name = tool.name if tool else tc.name
        op_id = _record_op(h, state, pos, i, tc, tool, tc.arguments or "", "not_dispatched")
        led.append("tool.started", {"operation_id": op_id, "call_id": tc.id, "tool": name,
                                    "args": _preview(tc.arguments or "")}, run_id=state.run_id, actor="character")
        _settle_op(h, state, op_id, tc, name, "not_dispatched",
                   ToolResult(False, {"error": reason, "outcome": "not_dispatched"}))


def _stop(h: "Harness", state: RunState, *, pos: int | None = None, calls=(), tools=None,
          first_index: int = 0) -> None:
    """End the run at a safe point because the human asked: calls not yet started are
    recorded as not run, and the character is told on its next wake. Never a turn_finish."""
    led = h.ledger
    with led.tx():
        _skip_calls(h, state, pos, calls, tools, first_index, "not run: your human stopped this wake")
        led.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'control', ?, 'pending', ?)",
              new_id("note"), block(f"host · wake stopped · {h.time.stamp()}",
                                    f"Your human stopped your wake {state.run_id} at a safe point"
                                    + (f"; {len(calls)} tool call(s) you had just asked for were not run" if calls else "")
                                    + ". Nothing was left half-done. What you do about it is up to you."),
              h.time.iso())
        end_run(h, state, "interrupted", "operator_stop")
    raise RunEnded


def _pause_for_compaction(h: "Harness", state: RunState, reason: str = "context_limit") -> None:
    with h.ledger.tx():
        h.ledger.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'context', ?, 'pending', ?)",
                   new_id("note"),
                   block(f"host · wake paused · {h.time.stamp()}",
                         f"Your wake {state.run_id} was paused because your working memory was full; it has "
                         "been condensed since. Pick up anything unfinished as you see fit."),
                   h.time.iso())
        end_run(h, state, "paused", reason)
        # Come back to it once the context has been condensed (input it admitted is in there).
        if not h.ledger.q1("SELECT 1 FROM wakes WHERE status = 'queued' AND kind = 'host' "
                           "AND json_extract(data, '$.reason') = 'resume_after_compaction'"):
            wake_id = new_id("wake")
            h.ledger.x("INSERT INTO wakes(id, kind, status, created_at, data) VALUES(?, 'host', 'queued', ?, ?)",
                       wake_id, h.time.iso(), dumps({"reason": "resume_after_compaction", "text": ""}))
            h.ledger.append("wake.queued", {"wake_id": wake_id, "kind": "host", "reason": "resume_after_compaction"})


def _commit_response(h: "Harness", state: RunState, c: Completion, gen: int) -> int:
    led = h.ledger
    with led.tx():
        pos = h.context.append(state.run_id, c.assistant_message())
        compaction.record_usage(h, pos, c.usage)
        state.requests += 1
        led.x("UPDATE runs SET requests = ? WHERE id = ?", state.requests, state.run_id)
        led.append(
            "provider.request.finished",
            {
                "provider": h.provider.name,
                "model": c.model,
                "finish_reason": c.finish_reason,
                "request_index": state.requests,
                "tool_calls": len(c.tool_calls),
                "catalog_generation": gen,
                "usage": c.usage,
            },
            run_id=state.run_id,
        )
        if c.content:
            # Authored character text: canonical history, not a delivery.
            led.append(
                "message.completed",
                {"message_id": f"msg_{pos}", "content": c.content},
                run_id=state.run_id,
                actor="character",
            )
    return pos


MISSED_REPLY = (
    "turn_finish was not applied: you wrote text in this response, but nothing has been sent during "
    "this wake, so nobody has received it. If it was meant for someone, send it with message_send; "
    "if not, call turn_finish again."
)


def _unsent_reply(h: "Harness", state: RunState, c: Completion) -> bool:
    """The bounded missing-reply check: plain text written alongside turn_finish, in a wake
    where the human spoke and nothing was sent. Declined once; never sent on its behalf."""
    if state.finish_nudged or not state.direct_input or not (c.content or "").strip():
        return False
    return h.ledger.q1("SELECT 1 FROM deliveries WHERE run_id = ? LIMIT 1", state.run_id) is None


def _decline_finish(h: "Harness", state: RunState, pos: int, tc: ToolCall, reason: str) -> None:
    led = h.ledger
    tool = h.registry.by_wire(FINISH_WIRE)
    with led.tx():
        op_id = _record_op(h, state, pos, 0, tc, tool, tc.arguments or "{}", "intent")
        led.append("tool.started", {"operation_id": op_id, "call_id": tc.id, "tool": tool.name, "args": {}},
                   run_id=state.run_id, actor="character")
        _settle_op(h, state, op_id, tc, tool.name, "error", ToolResult(False, {"error": reason}))
        led.append("context.note", {"kind": "missed_reply_check"}, run_id=state.run_id)


def _boundary(h: "Harness", state: RunState, *, cue_if_empty: bool) -> None:
    """Safe inference boundary: admit pending input and file changes, or cue continuation."""
    led = h.ledger
    with led.tx():
        blocks = drain_inputs(h, state) + _deliver_notes(h, state, kinds=("approval",))
        files = h.context.file_change_blocks()
        if files:
            led.append("context.note", {"kind": "file_changed", "count": len(files)}, run_id=state.run_id)
        blocks += files
        if not blocks and cue_if_empty:
            blocks = [CONTINUATION_CUE]
            led.append("context.note", {"kind": "continuation"}, run_id=state.run_id)
        if blocks:
            h.context.append(state.run_id, {"role": "user", "content": "\n\n".join(blocks)})


def _deliver_notes(h: "Harness", state: RunState, kinds: tuple[str, ...] | None = None) -> list[str]:
    """Pending host notes for this run (all kinds, or only `kinds`). Inside a transaction."""
    led = h.ledger
    if compaction.is_maintenance(state.origin):
        return []  # not on memory maintenance, so they survive into the condensed context
    rows = led.q("SELECT * FROM notes WHERE status = 'pending' ORDER BY created_at")
    rows = [n for n in rows if kinds is None or n["kind"] in kinds]
    for n in rows:
        led.x("UPDATE notes SET status = 'delivered', run_id = ? WHERE id = ?", state.run_id, n["id"])
    if rows:
        led.append("context.note", {"kind": "host_notes", "note_ids": [n["id"] for n in rows]}, run_id=state.run_id)
    return [n["text"] for n in rows]


def _record_op(h: "Harness", state: RunState, pos: int, idx: int, tc: ToolCall, tool, args_json: str, status: str) -> str:
    op_id = new_id("op")
    h.ledger.x(
        "INSERT INTO operations(id, run_id, context_pos, call_index, call_id, tool, impl, effect, replay_safe, args, status) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        op_id, state.run_id, pos, idx, tc.id,
        tool.name if tool else tc.name,
        tool.impl if tool else "unknown",
        tool.effect if tool else "control",
        int(bool(tool and tool.replay_safe)),
        args_json, status,
    )
    return op_id


def _tool_message(tc: ToolCall, result: ToolResult) -> dict:
    return {"role": "tool", "tool_call_id": tc.id, "content": result.to_model()}


def _settle_op(h: "Harness", state: RunState, op_id: str, tc: ToolCall, tool_name: str, status: str, result: ToolResult) -> None:
    led = h.ledger
    led.x("UPDATE operations SET status = ?, result = ? WHERE id = ?", status, result.to_model(), op_id)
    led.append(
        "tool.finished",
        {"operation_id": op_id, "call_id": tc.id, "tool": tool_name, "status": status, "result": _preview(result.data)},
        run_id=state.run_id,
    )
    h.context.append(state.run_id, _tool_message(tc, result))
    if result.ok and result.file_acks:
        h.context.acknowledge_files(result.file_acks)
    state.tool_calls += 1
    led.x("UPDATE runs SET tool_calls = ? WHERE id = ?", state.tool_calls, state.run_id)


def _finish(h: "Harness", state: RunState, pos: int, tc: ToolCall) -> None:
    """Accepted finish: tool pairing + run.finished in one transaction, no further request."""
    led = h.ledger
    tool = h.registry.by_wire(FINISH_WIRE)
    with led.tx():
        op_id = _record_op(h, state, pos, 0, tc, tool, tc.arguments or "{}", "intent")
        led.append(
            "tool.started", {"operation_id": op_id, "call_id": tc.id, "tool": tool.name, "args": {}},
            run_id=state.run_id, actor="character",
        )
        _settle_op(h, state, op_id, tc, tool.name, "ok", ToolResult(True, {"finished": True}))
        end_run(h, state, "finished", "turn_finish")


async def _execute_tool(h: "Harness", state: RunState, pos: int, idx: int, tc: ToolCall, tools: dict) -> None:
    led = h.ledger
    tool = tools.get(tc.name)

    def reject(status: str, error: str) -> None:
        name = tool.name if tool else tc.name
        with led.tx():
            op_id = _record_op(h, state, pos, idx, tc, tool, tc.arguments or "", status)
            led.append("tool.started", {"operation_id": op_id, "call_id": tc.id, "tool": name,
                                        "args": _preview(tc.arguments or "")},
                       run_id=state.run_id, actor="character")
            _settle_op(h, state, op_id, tc, name, status, ToolResult(False, {"error": error}))

    if tool is None:
        if h.registry.by_wire(tc.name) is not None:
            return reject("error", f"{tc.name} isn't available right now")
        return reject("error", f"unknown tool {tc.name!r}")
    if tool.name == "turn.finish":
        return reject(
            "error", "turn_finish was not applied: call it alone, with no other tool calls in the same response"
        )
    try:
        args = tool.parse(tc.arguments)
    except ArgumentError as e:
        return reject("error", f"invalid arguments: {e}")

    if tool.atomic:
        try:
            with led.tx():
                op_id = _record_op(h, state, pos, idx, tc, tool, tc.arguments or "{}", "intent")
                led.append(
                    "tool.started",
                    {"operation_id": op_id, "call_id": tc.id, "tool": tool.name, "effect": tool.effect,
                     "args": _preview(args.model_dump())},
                    run_id=state.run_id, actor="character",
                )
                result = tool.handler(state, args, op_id)
                _settle_op(h, state, op_id, tc, tool.name, "ok" if result.ok else "error", result)
        except Exception as e:  # noqa: BLE001 - its transaction rolled back, so nothing was applied
            h.log.exception("tool %s raised", tool.name)
            return reject("error", f"{type(e).__name__}: {e} (nothing was changed)")
        await h.fault("tool.after_atomic_commit")
        if result.after_commit is not None:
            result.after_commit()
        return

    with led.tx():
        op_id = _record_op(h, state, pos, idx, tc, tool, tc.arguments or "{}", "intent")
        led.append(
            "tool.started",
            {"operation_id": op_id, "call_id": tc.id, "tool": tool.name, "effect": tool.effect,
             "args": _preview(args.model_dump())},
            run_id=state.run_id,
            actor="character",
        )
    await h.fault("tool.before_dispatch")
    with led.tx():
        led.x("UPDATE operations SET status = 'dispatched' WHERE id = ?", op_id)
    await h.fault("tool.after_dispatch")

    state.operation_id = op_id
    try:
        result = await tool.handler(state, args)
        status = "ok" if result.ok else "error"
    except Exception as e:  # noqa: BLE001
        h.log.exception("tool %s raised", tool.name)
        # A read-only tool that blew up did nothing; anything else may have half-happened.
        status = "error" if tool.replay_safe else "uncertain"
        result = ToolResult(False, {"error": f"{type(e).__name__}: {e}", "outcome": status})
    finally:
        state.operation_id = None
    await h.fault("tool.before_result")
    with led.tx():
        _settle_op(h, state, op_id, tc, tool.name, status, result)


def start_run(h: "Harness", job: dict) -> RunState:
    """Admit a wake: create the run and its opening context message in one transaction."""
    led = h.ledger
    run_id = new_id("run")
    with led.tx():
        blocks: list[str] = []
        fired = None
        wake = job.get("wake")
        if wake is not None:
            data = json.loads(wake["data"])
            origin = {"kind": wake["kind"], "wake_id": wake["id"], **data.get("origin", {})}
            led.x("UPDATE wakes SET status = 'admitted', run_id = ? WHERE id = ?", run_id, wake["id"])
            if wake["kind"] in ("heartbeat", "cron"):
                opening, extra, fired = h.scheduler.admit(run_id, wake, data)
                origin.update(extra)
                blocks.append(opening)
            elif wake["kind"] == "maintenance":
                origin["compaction_id"] = data["compaction_id"]
                blocks.append(compaction.opening(h, data))
            else:
                header = "host · recovery wake" if wake["kind"] == "recovery" else f"host · wake · {data.get('reason', 'manual')}"
                blocks.append(block(f"{header} · {h.time.stamp()}", data.get("text", "")))
        else:
            first = led.q1("SELECT * FROM inputs WHERE status = 'pending' ORDER BY received_at, rowid LIMIT 1")
            origin = {"kind": "input", "input_id": first["id"], "source_kind": first["source_kind"]}
            if first["principal_id"]:
                origin["principal_id"] = first["principal_id"]
        state = RunState(h, run_id, origin)
        resumes = origin.get("resumes_run_id")
        led.x(
            "INSERT INTO runs(id, status, origin, wake_id, resumes_run_id, started_at) VALUES(?, 'active', ?, ?, ?, ?)",
            run_id, dumps(origin), wake["id"] if wake is not None else None, resumes, h.time.iso(),
        )
        led.append("run.started", {"resumes_run_id": resumes} if resumes else {}, run_id=run_id, origin=origin)
        if fired is not None:
            led.append("schedule.fired", fired, run_id=run_id)
        blocks += drain_inputs(h, state, at_start=True)

        # Host notes (crash recovery, approvals...) ride on whichever ordinary wake comes first.
        notes = _deliver_notes(h, state)
        blocks += notes
        if notes:
            for w in led.q("SELECT id FROM wakes WHERE kind = 'recovery' AND status = 'queued'"):
                led.x("UPDATE wakes SET status = 'cancelled' WHERE id = ?", w["id"])
                led.append("wake.cancelled", {"wake_id": w["id"], "reason": "notes_delivered", "by_run": run_id})

        files = h.context.file_change_blocks()
        if files:
            led.append("context.note", {"kind": "file_changed", "count": len(files)}, run_id=run_id)
        blocks += files
        h.context.append(run_id, {"role": "user", "content": "\n\n".join(blocks)})
    return state
