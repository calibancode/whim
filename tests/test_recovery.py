"""Crash at each durable boundary, restart, and check reconciliation + recovery wake."""

from __future__ import annotations

import json

import pytest
from conftest import Outbox, check_conformance, crash_at, events, human_messages, project_all, runs, tool_result, types

from whim import recovery
from whim.harness import Harness
from whim.provider.fake import ScriptedProvider, call, calls, finish, tool_call


def ops(h):
    return [dict(r) for r in h.ledger.q("SELECT * FROM operations ORDER BY rowid")]


def wakes(h, kind="recovery"):
    return [dict(r) for r in h.ledger.q("SELECT * FROM wakes WHERE kind = ? ORDER BY rowid", kind)]


async def crash_then_restart(rig, first_steps, point, *, recovery_steps=None, destinations=None, **kw):
    h = await rig.start(ScriptedProvider(first_steps), fault_hook=crash_at(point), destinations=destinations)
    h.submit_input("please do the thing")
    await rig.crashed()
    p2 = ScriptedProvider(recovery_steps if recovery_steps is not None else [finish()])
    h2 = await rig.start(p2, destinations=destinations, **kw)
    await h2.wait_idle()
    return h2, p2


async def test_crash_after_dispatch_of_write_is_uncertain_and_not_replayed(rig):
    h, p2 = await crash_then_restart(
        rig, [call("file_write", path="plan.md", content="v1")], "tool.after_dispatch"
    )
    first, second = runs(h)
    assert (first["status"], first["reason"]) == ("interrupted", "unclean_shutdown")
    assert second["status"] == "finished" and second["resumes_run_id"] == first["id"]
    [op] = ops(h)[:1]
    assert op["tool"] == "file.write" and op["status"] == "uncertain"
    # Provider transcript stays valid: the dangling call got a host-attributed result.
    msgs = p2.requests[0].messages
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_result(tool_msgs[0])["outcome"] == "uncertain"
    # The recovery wake carries a notice naming the uncertain effect; no fake human text.
    opening = msgs[-1]["content"]
    assert opening.startswith("⟦host · recovery wake")
    assert "file.write" in opening and "may or may not have taken effect" in opening
    assert "message from human" not in opening
    # Never re-dispatched.
    assert sum(1 for o in ops(h) if o["tool"] == "file.write") == 1


async def test_crash_before_dispatch_is_not_dispatched(rig):
    h, p2 = await crash_then_restart(
        rig, [call("file_write", path="plan.md", content="v1")], "tool.before_dispatch"
    )
    [op] = [o for o in ops(h) if o["tool"] == "file.write"]
    assert op["status"] == "not_dispatched"
    assert not (rig.cfg.workspace / "plan.md").exists()


async def test_crash_during_read_only_call_is_marked_safe_to_repeat(rig):
    h, p2 = await crash_then_restart(rig, [call("file_read", path="MEMORY.md")], "tool.before_result")
    [op] = [o for o in ops(h) if o["tool"] == "file.read"]
    assert op["status"] == "interrupted"
    assert "safe" in json.loads(op["result"])["error"]


async def test_crash_before_response_commit_loses_only_the_response(rig):
    h, p2 = await crash_then_restart(rig, [call("file_write", path="x.md", content="x")], "provider.after_response")
    # The uncommitted response never existed; no dangling call, no operation.
    assert [r["status"] for r in runs(h)] == ["interrupted", "finished"]
    assert [o["tool"] for o in ops(h)] == ["turn.finish"]
    assert not [m for m in p2.requests[0].messages if m["role"] == "assistant"]


async def test_crash_after_response_commit_pairs_call_without_op(rig):
    h, p2 = await crash_then_restart(rig, [call("file_write", path="x.md", content="x")], "provider.after_commit")
    [op] = [o for o in ops(h) if o["tool"] == "file.write"]
    assert op["status"] == "not_dispatched"
    msgs = p2.requests[0].messages
    assert msgs[-3]["role"] == "assistant" and msgs[-2]["role"] == "tool"
    assert tool_result(msgs[-2])["outcome"] == "not_dispatched"


async def test_external_send_crash_before_receipt_is_uncertain_and_not_resent(rig):
    outbox = Outbox()
    h, p2 = await crash_then_restart(
        rig,
        [call("message_send", destination="outbox", content="ping")],
        "delivery.before_receipt",
        destinations=[outbox],
    )
    assert outbox.delivered == ["ping"]  # delivered once, receipt lost in the crash
    [d] = [dict(r) for r in h.ledger.q("SELECT * FROM deliveries")]
    assert d["status"] == "uncertain"
    settled = events(h, "delivery.settled")
    assert [e["data"]["status"] for e in settled] == ["uncertain"]
    assert [o["status"] for o in ops(h) if o["tool"] == "message.send"] == ["uncertain"]


async def test_external_send_crash_before_dispatch_is_never_delivered(rig):
    outbox = Outbox()
    h, _ = await crash_then_restart(
        rig, [call("message_send", destination="outbox", content="ping")], "delivery.before_dispatch",
        destinations=[outbox],
    )
    assert outbox.delivered == []
    # Intent was durable, the dispatch never happened, but the host cannot prove that
    # for an arbitrary adapter, so it stays uncertain rather than being retried.
    assert [r["status"] for r in h.ledger.q("SELECT status FROM deliveries")] == ["uncertain"]


async def test_human_send_receipt_is_reused_after_crash(rig):
    h, p2 = await crash_then_restart(
        rig, [call("message_send", destination="human", content="hello there")], "tool.before_result"
    )
    assert human_messages(h) == ["hello there"]  # exactly once
    [op] = [o for o in ops(h) if o["tool"] == "message.send"]
    assert op["status"] == "ok"
    tool_msg = [m for m in p2.requests[0].messages if m["role"] == "tool"][0]
    assert tool_result(tool_msg)["status"] == "sent"
    # Nothing unresolved, so the notice doesn't warn about the send.
    assert "may or may not" not in p2.requests[0].messages[-1]["content"]


async def test_pending_human_input_carries_recovery_note_instead_of_extra_wake(rig):
    h = await rig.start(ScriptedProvider([call("file_write", path="a.md", content="a")]),
                        fault_hook=crash_at("tool.after_dispatch"))
    h.submit_input("start")
    await rig.crashed()

    p2 = ScriptedProvider([finish()])
    h2 = await rig.start(p2, before_start=lambda h: h.submit_input("hello again"))
    await h2.wait_idle()
    r = runs(h2)
    assert [x["status"] for x in r] == ["interrupted", "finished"]
    assert json.loads(r[1]["origin"])["kind"] == "input"
    opening = p2.requests[0].messages[-1]["content"]
    assert "hello again" in opening and "recovery notice" in opening
    assert [w["status"] for w in wakes(h2)] == ["cancelled"]
    assert len(p2.requests) == 1


async def test_reconcile_twice_queues_one_recovery_wake(rig):
    h = await rig.start(ScriptedProvider([call("file_write", path="a.md", content="a")]),
                        fault_hook=crash_at("tool.after_dispatch"))
    h.submit_input("start")
    await rig.crashed()

    # Two startups that die before admitting anything.
    for _ in range(2):
        tmp = Harness(rig.cfg, ScriptedProvider())
        recovery.reconcile(tmp)
        tmp.close()
    h3 = await rig.start(ScriptedProvider([finish()]))
    await h3.wait_idle()
    assert len(wakes(h3)) == 1
    assert [r["status"] for r in runs(h3)] == ["interrupted", "finished"]


async def test_repeated_crashes_bound_recovery_wakes(rig):
    rig.cfg.limits.max_recovery_attempts = 2
    h = await rig.start(ScriptedProvider([call("file_write", path="a.md", content="a")]),
                        fault_hook=crash_at("tool.after_dispatch"))
    h.submit_input("start")
    await rig.crashed()
    for _ in range(2):  # each recovery wake crashes immediately
        await rig.start(ScriptedProvider(), fault_hook=crash_at("provider.before_request"))
        await rig.crashed()
    h = await rig.start(ScriptedProvider())
    await h.wait_idle()
    assert len(wakes(h)) == 2
    assert [r["status"] for r in runs(h)] == ["interrupted"] * 3
    warn = events(h, "host.warning")
    assert warn and warn[-1]["data"]["kind"] == "recovery_suspended"
    # The notes wait for the next ordinary wake instead.
    pending = h.ledger.q("SELECT COUNT(*) c FROM notes WHERE status = 'pending'")[0]["c"]
    assert pending == 1


async def test_paused_and_failed_runs_do_not_trigger_recovery(rig):
    rig.cfg.limits.max_text_only_streak = 1
    from whim.provider.fake import say

    h = await rig.start(ScriptedProvider([say("hm")]))
    h.submit_input("hi")
    await h.wait_idle()
    h = await rig.restart(ScriptedProvider())
    await h.wait_idle()
    assert wakes(h) == []
    assert "recovery.pending" not in types(h)


# -- recovered outcomes as a client sees them ------------------------------------------


def reconstruct_tools(evs):
    """Tool operations and outcomes as an independent client sees them: events only."""
    started = {e["data"]["operation_id"]: e["data"] for e in evs if e["type"] == "tool.started"}
    finished = {e["data"]["operation_id"]: e["data"] for e in evs if e["type"] == "tool.finished"}
    return started, finished


RECOVERY_CASES = {
    "uncertain write": ([call("file_write", path="plan.md", content="v1")], "tool.after_dispatch"),
    "never dispatched": ([call("file_write", path="plan.md", content="v1")], "tool.before_dispatch"),
    "read cut off": ([call("file_read", path="MEMORY.md")], "tool.before_result"),
    "calls never recorded": ([calls(tool_call("file_write", path="a.md", content="a"),
                                    tool_call("file_read", path="MEMORY.md"))], "provider.after_commit"),
    "external send cut off": ([call("message_send", destination="outbox", content="hi")], "delivery.before_receipt"),
    "journal cut off": ([call("journal_write", entry="a thought")], "journal.after_append"),
}


@pytest.mark.parametrize("case", RECOVERY_CASES)
async def test_recovered_tool_outcomes_are_complete_in_the_event_protocol(rig, case):

    steps, point = RECOVERY_CASES[case]
    h, _ = await crash_then_restart(rig, steps, point, destinations=[Outbox()])
    evs = events(h)
    started, finished = reconstruct_tools(evs)
    ops = {r["id"]: dict(r) for r in h.ledger.q("SELECT * FROM operations")}
    assert set(started) == set(finished) == set(ops)
    for op_id, op in ops.items():
        s, f = started[op_id], finished[op_id]
        assert (s["call_id"], s["tool"]) == (f["call_id"], f["tool"]) == (op["call_id"], op["tool"])
        assert f["status"] == op["status"]
        model = json.loads(op["result"])
        model.pop("ok", None)
        assert f["result"] == model  # the outcome the character was told, not only a status
    # Every provider-requested call is accounted for by a lifecycle pair.
    requested = sum(e["data"]["tool_calls"] for e in evs if e["type"] == "provider.request.finished")
    assert requested == len(started)
    # And the AG-UI activity view shows each call with its result, conformantly.
    stream = project_all(h, "activity")
    check_conformance(stream)
    results = {e["toolCallId"] for e in stream if e["type"] == "TOOL_CALL_RESULT"}
    assert results == set(ops)
