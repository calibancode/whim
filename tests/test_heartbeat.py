"""Heartbeat schedule and standing cues, driven by a controlled clock."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import Clock, events, human_messages, runs, tool_result, types

from whim import heartbeat
from whim.provider.fake import ScriptedProvider, call, finish

HB = "⟦host · heartbeat"


def complete_bootstrap(h):
    with h.ledger.tx():
        h.ledger.x("INSERT OR REPLACE INTO meta(key, value) VALUES('bootstrap', '{\"status\": \"complete\"}')")


def add_cues(h, *ops):
    with h.ledger.tx():
        heartbeat.apply_ops(h.ledger, [heartbeat.CueOp(**o) for o in ops], actor="test", run_id=None,
                            expected_revision=None, now="2026-10-10T12:00:00Z")


def kinds(h):
    return [json.loads(r["origin"])["kind"] for r in runs(h)]


def next_due(h):
    return heartbeat.parse(h.scheduler.state()["next_due_at"])


async def ready(rig, steps, clock=None, **kw):
    clock = clock or Clock()
    h = await rig.start(ScriptedProvider(steps), clock=clock, **kw)
    complete_bootstrap(h)
    h.scheduler.tick()  # arms: first heartbeat one interval from now
    return h, clock


async def test_gate_until_first_meeting_completes(rig):
    clock = Clock()
    p = ScriptedProvider([finish(), call("bootstrap_complete"), finish(), finish()])
    h = await rig.start(p, clock=clock)
    for _ in range(3):
        clock.advance(120)
        h.scheduler.tick()
    assert "schedule.armed" not in types(h) and "schedule.due" not in types(h)
    h.submit_input("hello")
    await h.wait_idle()
    clock.advance(120)
    h.scheduler.tick()
    assert "schedule.armed" not in types(h)  # meeting active, still gated

    h.submit_input("good to meet you")
    await h.wait_idle()
    h.scheduler.tick()
    [armed] = events(h, "schedule.armed")
    assert heartbeat.parse(armed["data"]["next_due_at"]) == clock.now + timedelta(minutes=60)
    h.scheduler.tick()
    assert "schedule.due" not in types(h)  # no immediate heartbeat after onboarding
    clock.advance(60)
    h.scheduler.tick()
    await h.wait_idle()
    assert kinds(h) == ["input", "input", "heartbeat"]


async def test_heartbeat_opening_shows_instruction_and_active_cues_only(rig):
    h, clock = await ready(rig, [finish()])
    add_cues(h, {"op": "add", "instruction": "Keep reading the essay in notes/essay.md"},
             {"op": "add", "instruction": "Check on the garden idea", "priority": "high"},
             {"op": "add", "instruction": "Old idea"})
    add_cues(h, {"op": "pause", "id": "hb_3"})
    clock.advance(60)
    h.scheduler.tick()
    await h.wait_idle()
    opening = rig.provider.requests[0].messages[-1]["content"]
    assert opening.startswith(HB)
    assert heartbeat.INSTRUCTION in opening
    assert opening.index("hb_2 [high]") < opening.index("hb_1:")  # high priority first
    assert "Old idea" not in opening and "message from human" not in opening
    [fired] = events(h, "schedule.fired")
    assert fired["data"]["cue_revision"] == 2 and fired["data"]["active_cues"] == 2
    [run] = runs(h)
    assert json.loads(run["origin"])["cue_revision"] == 2


async def test_no_cues_is_stated_plainly(rig):
    h, clock = await ready(rig, [finish()])
    clock.advance(60)
    h.scheduler.tick()
    await h.wait_idle()
    assert "You have no standing cues." in rig.provider.requests[0].messages[-1]["content"]


async def test_heartbeat_waits_for_active_run_and_human_input_goes_first(rig):
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return finish()

    h, clock = await ready(rig, [slow, finish(), finish()])
    h.submit_input("long task")
    await asyncio.sleep(0.05)
    clock.advance(61)
    h.scheduler.tick()  # due while busy: queued, not interrupting
    h.submit_input("another message")  # pending human input arrives too
    await asyncio.sleep(0.05)
    assert [r["status"] for r in runs(h)] == ["active"]
    gate.set()
    await h.wait_idle()
    assert kinds(h) == ["input", "input", "heartbeat"]
    assert all(r["status"] == "finished" for r in runs(h))


async def test_reanchors_to_actual_admission(rig):
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return finish()

    h, clock = await ready(rig, [slow, finish()])
    due = next_due(h)
    h.submit_input("busy work")
    await asyncio.sleep(0.05)
    clock.advance(70)  # due at +60, character busy until +70
    h.scheduler.tick()
    gate.set()
    await h.wait_idle()
    [fired] = events(h, "schedule.fired")
    assert heartbeat.parse(fired["data"]["due_at"]) == due
    assert fired["data"]["delay_seconds"] == 600.0
    assert next_due(h) == clock.now + timedelta(minutes=60)  # from admission, not from due


async def test_missed_intervals_coalesce_into_one(rig):
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return finish()

    h, clock = await ready(rig, [slow, finish()])
    h.submit_input("very long task")
    await asyncio.sleep(0.05)
    for _ in range(6):  # five and a half hours of ticks while busy
        clock.advance(55)
        h.scheduler.tick()
    gate.set()
    await h.wait_idle()
    assert kinds(h) == ["input", "heartbeat"]
    assert types(h).count("schedule.due") == 1
    [fired] = events(h, "schedule.fired")
    assert fired["data"]["missed"] == 5  # due at +60; now +330 -> 1 + floor(270/60)


async def test_offline_gap_coalesces_after_restart(rig):
    h, clock = await ready(rig, [])
    h = await rig.restart(ScriptedProvider([finish()]), clock=clock)
    clock.advance(60 * 24)  # down for a day
    h.scheduler.tick()
    await h.wait_idle()
    assert kinds(h) == ["heartbeat"]
    assert events(h, "schedule.fired")[0]["data"]["missed"] == 24


async def test_human_conversation_does_not_touch_schedule(rig):
    h, clock = await ready(rig, [call("message_send", destination="human", content="hi"), finish()])
    due = next_due(h)
    clock.advance(30)
    h.submit_input("hello")
    await h.wait_idle()
    h.scheduler.tick()
    assert next_due(h) == due
    assert not [t for t in types(h) if t.startswith("schedule.") and t != "schedule.armed"]


async def test_queued_heartbeat_and_schedule_survive_restart(rig):
    gate = asyncio.Event()

    async def never(request):
        await gate.wait()

    h, clock = await ready(rig, [never])
    add_cues(h, {"op": "add", "instruction": "Water the ideas"})
    h.submit_input("start something")
    await asyncio.sleep(0.05)
    clock.advance(61)
    h.scheduler.tick()
    assert len([w for w in h.ledger.q("SELECT * FROM wakes WHERE kind='heartbeat' AND status='queued'")]) == 1

    p2 = ScriptedProvider([finish(), finish()])
    h = await rig.restart(p2, clock=clock)  # abandons the active run
    h.scheduler.tick()  # must not queue a second heartbeat
    await h.wait_idle()
    assert kinds(h) == ["input", "recovery", "heartbeat"]
    assert types(h).count("schedule.due") == 1 and types(h).count("schedule.fired") == 1
    assert "Water the ideas" in p2.requests[1].messages[-1]["content"]


async def test_heartbeat_can_finish_silently(rig):
    h, clock = await ready(rig, [call("file_read", path="MEMORY.md"), finish()])
    clock.advance(60)
    h.scheduler.tick()
    await h.wait_idle()
    [run] = runs(h)
    assert run["status"] == "finished" and human_messages(h) == []
    assert not [t for t in types(h) if t.startswith("delivery.")]


async def test_heartbeat_can_send_a_message(rig):
    h, clock = await ready(rig, [call("message_send", destination="human", content="Thought of you."), finish()])
    clock.advance(60)
    h.scheduler.tick()
    await h.wait_idle()
    assert human_messages(h) == ["Thought of you."]


async def test_disabled_heartbeats_neither_schedule_nor_run(rig):
    rig.cfg.heartbeat.enabled = False
    clock = Clock()
    h = await rig.start(ScriptedProvider([finish()]), clock=clock)
    complete_bootstrap(h)
    clock.advance(500)
    h.scheduler.tick()
    assert h.request_wake(kind="heartbeat") is None
    with h.ledger.tx():
        h.ledger.x("INSERT INTO wakes(id, kind, status, created_at, data) "
                   "VALUES('wake_x', 'heartbeat', 'queued', '2026-01-01T00:00:00Z', '{}')")
    h._nudge()
    await h.wait_idle()
    assert runs(h) == [] and "schedule.armed" not in types(h)


async def test_background_scheduler_runs_in_daemon_mode(rig):
    rig.cfg.heartbeat.interval_minutes = 0.0005  # 30 ms; the loop polls at least every second
    h = await rig.start(ScriptedProvider([finish()]), before_start=complete_bootstrap, run_scheduler=True)
    for _ in range(40):
        if runs(h):
            break
        await asyncio.sleep(0.1)
    await h.wait_idle()
    assert kinds(h)[:1] == ["heartbeat"]


# -- cue management through the character's tool -------------------------------------


def manage(*ops, expected_revision=None):
    args = {"operations": list(ops)}
    if expected_revision is not None:
        args["expected_revision"] = expected_revision
    return call("heartbeat_manage", **args)


async def run_tools(rig, *steps):
    p = ScriptedProvider([*steps, finish()])
    h = await rig.start(p, clock=Clock())
    h.submit_input("go")
    await h.wait_idle()
    results = [tool_result(r.messages[-1]) for r in p.requests[1:]]
    return h, results


async def test_cue_lifecycle_via_tool(rig):
    h, res = await run_tools(
        rig,
        manage({"op": "add", "instruction": "Draft the short story in notes/story.md"},
               {"op": "add", "instruction": "Ask how the sourdough is doing", "priority": "low"}),
        manage({"op": "update", "id": "hb_1", "instruction": "Finish the story draft", "priority": "high"},
               {"op": "pause", "id": "hb_2"}),
        manage({"op": "resume", "id": "hb_2"}, {"op": "remove", "id": "hb_1"}),
        manage(),  # view
    )
    assert [r["revision"] for r in res] == [1, 2, 3, 3]
    assert res[0]["applied"] == [{"op": "add", "id": "hb_1"}, {"op": "add", "id": "hb_2"}]
    assert res[1]["cues"][0] == {"id": "hb_1", "instruction": "Finish the story draft", "status": "active",
                                 "priority": "high"}
    assert res[3]["cues"] == [{"id": "hb_2", "instruction": "Ask how the sourdough is doing",
                               "status": "active", "priority": "low"}]
    # Removed cues keep their audit history.
    hist = h.ledger.q("SELECT rev, op FROM cue_history WHERE cue_id = 'hb_1' ORDER BY rev")
    assert [(r["rev"], r["op"]) for r in hist] == [(1, "add"), (2, "update"), (3, "remove")]
    assert [e["data"]["revision"] for e in events(h, "cues.changed")] == [1, 2, 3]


async def test_cue_edits_are_all_or_nothing(rig):
    h, res = await run_tools(
        rig,
        manage({"op": "add", "instruction": "one"}),
        manage({"op": "add", "instruction": "two"}, {"op": "pause", "id": "hb_99"}),
        manage({"op": "add", "instruction": "one"}),
        manage({"op": "update", "id": "hb_1"}),
        manage({"op": "add", "instruction": "three"}, expected_revision=0),
    )
    assert res[0]["ok"]
    assert "no cue 'hb_99'" in res[1]["error"] and res[1]["revision"] == 1
    assert "same text as hb_1" in res[2]["error"]
    assert "new instruction and/or priority" in res[3]["error"]
    assert "conflict" in res[4]["error"]
    assert [c["instruction"] for c in heartbeat.cues(h.ledger)] == ["one"]
    assert heartbeat.revision(h.ledger) == 1
    assert [r["status"] for r in runs(h)] == ["finished"]  # errors never broke the run


async def test_cue_size_ceiling(rig):
    big = "x" * (heartbeat.MAX_CUE_BYTES // 2 + 10)
    h, res = await run_tools(rig, manage({"op": "add", "instruction": big}),
                             manage({"op": "add", "instruction": big + "y"}))
    assert res[0]["ok"] and "limit" in res[1]["error"]
    assert len(heartbeat.cues(h.ledger)) == 1


async def test_edits_during_heartbeat_apply_to_next_heartbeat(rig):
    h, clock = await ready(rig, [manage({"op": "add", "instruction": "New cue from this heartbeat"}), finish(),
                                 finish()])
    clock.advance(60)
    h.scheduler.tick()
    await h.wait_idle()
    first = rig.provider.requests[0].messages[-1]["content"]
    assert "You have no standing cues." in first
    clock.advance(60)
    h.scheduler.tick()
    await h.wait_idle()
    second = rig.provider.requests[2].messages[-1]["content"]
    assert "hb_1: New cue from this heartbeat" in second
    fired = events(h, "schedule.fired")
    assert [f["data"]["cue_revision"] for f in fired] == [0, 1]


async def test_cues_survive_restart(rig):
    h, _ = await run_tools(rig, manage({"op": "add", "instruction": "Remember the lighthouse idea"}))
    h = await rig.restart(ScriptedProvider())
    assert [c["id"] for c in heartbeat.cues(h.ledger)] == ["hb_1"]
    assert heartbeat.revision(h.ledger) == 1


@pytest.mark.parametrize("op", [{"op": "add"}, {"op": "remove"}])
async def test_cue_ops_validate_required_fields(rig, op):
    _, res = await run_tools(rig, manage(op))
    assert not res[0]["ok"]
