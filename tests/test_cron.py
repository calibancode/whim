"""Scheduled jobs: expressions, DST, coalescing, priority, persistence, outcomes."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import Clock, crash_at, events, human_messages, runs, tool_result, types

from whim import cron
from whim.provider.base import ProviderError
from whim.provider.fake import ScriptedProvider, call, finish

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# -- expressions and time rules ---------------------------------------------------------


def test_cron_fields_and_names():
    e = cron.CronExpr("*/15 9-17 * * mon-fri")
    assert e.minutes == {0, 15, 30, 45} and e.hours == set(range(9, 18)) and e.dows == {1, 2, 3, 4, 5}
    assert cron.CronExpr("@daily").hours == {0}
    assert cron.CronExpr("0 0 * jan,jul 7").dows == {0}  # 7 is Sunday too
    for bad in ["* * * *", "61 * * * *", "0 25 * * *", "*/0 * * * *", "0 0 * * funday", "5-1 * * * *"]:
        with pytest.raises(cron.ScheduleError):
            cron.CronExpr(bad)


def test_dom_and_dow_combine_like_classic_cron():
    e = cron.CronExpr("0 12 1 * mon")  # the 1st of the month OR any Monday
    occ = [t.astimezone(LA).date().isoformat() for t in e.occurrences_after(utc(2026, 10, 10), LA)][:4]
    assert occ == ["2026-10-12", "2026-10-19", "2026-10-26", "2026-11-01"]


def test_validation_rules():
    now = utc(2026, 10, 10, 19)
    with pytest.raises(cron.ScheduleError, match="never fires"):
        cron.validate_cron("0 0 30 2 *", now, LA)
    with pytest.raises(cron.ScheduleError, match="5 minutes apart"):
        cron.validate_cron("* * * * *", now, LA)
    cron.validate_cron("*/5 * * * *", now, LA)
    with pytest.raises(cron.ScheduleError, match="already passed"):
        cron.parse_at("2026-10-10 11:00", now, LA)  # 18:00Z, before now
    assert cron.parse_at("2026-10-10 13:00", now, LA) == utc(2026, 10, 10, 20)
    assert cron.parse_at("2026-10-10T21:00:00Z", now, LA) == utc(2026, 10, 10, 21)


def test_spring_forward_gap():
    e = cron.CronExpr("30 2 * * *")  # 2026-03-08 02:30 doesn't exist in Los Angeles
    occ = list(zip(range(3), e.occurrences_after(utc(2026, 3, 7, 0), LA)))
    times = [t for _, t in occ]
    assert times == [utc(2026, 3, 7, 10, 30), utc(2026, 3, 8, 10, 30), utc(2026, 3, 9, 9, 30)]
    assert times[1].astimezone(LA).strftime("%H:%M %Z") == "03:30 PDT"  # shifted past the gap
    with pytest.raises(cron.ScheduleError, match="doesn't exist"):
        cron.parse_at("2026-03-08 02:30", utc(2026, 3, 1), LA)


def test_fall_back_runs_ambiguous_time_once():
    e = cron.CronExpr("30 1 * * *")  # 2026-11-01 01:30 happens twice in Los Angeles
    times = [t for _, t in zip(range(3), e.occurrences_after(utc(2026, 10, 31, 12), LA))]
    assert times == [utc(2026, 11, 1, 8, 30), utc(2026, 11, 2, 9, 30), utc(2026, 11, 3, 9, 30)]
    assert cron.parse_at("2026-11-01 01:30", utc(2026, 10, 1), LA) == utc(2026, 11, 1, 8, 30)


# -- tool and scheduler ---------------------------------------------------------------------


def manage(*ops, expected_revision=None):
    args = {"operations": list(ops)}
    if expected_revision is not None:
        args["expected_revision"] = expected_revision
    return call("cron_manage", **args)


async def setup(rig, *ops, steps=(), clock=None):
    """Create jobs through the character's tool, then run `steps` for later wakes."""
    clock = clock or Clock()
    p = ScriptedProvider([manage(*ops), finish(), *steps])
    h = await rig.start(p, clock=clock)
    h.submit_input("please schedule it")
    await h.wait_idle()
    res = tool_result(p.requests[1].messages[-1])
    assert res["ok"], res
    return h, p, clock, res


def kinds(h):
    return [json.loads(r["origin"])["kind"] for r in runs(h)]


def job(h, job_id="cr_1"):
    return dict(h.ledger.q1("SELECT * FROM jobs WHERE id = ?", job_id))


async def tick_idle(h):
    h.scheduler.tick()
    await h.wait_idle()


async def test_one_shot_job_runs_once_with_its_instruction(rig):
    h, p, clock, res = await setup(rig, {"op": "add", "instruction": "Ask Sam how the interview went.",
                                         "in_minutes": 90}, steps=[finish()])
    [listed] = res["jobs"]
    assert listed["schedule"].startswith("once at 2026-10-10 13:30:00 -0700") and listed["next"]
    clock.advance(89)
    await tick_idle(h)
    assert kinds(h) == ["input"]
    clock.advance(1)
    await tick_idle(h)
    assert kinds(h) == ["input", "cron"]
    opening = p.requests[2].messages[-1]["content"]
    assert opening.startswith("⟦host · scheduled job cr_1 · 2026-10-10 13:30:00 -0700⟧")
    assert "Instruction: Ask Sam how the interview went." in opening
    assert job(h)["status"] == "done" and job(h)["last_outcome"] == "finished"
    # schedule.fired comes right after run.started, inside the run.
    t = types(h)
    assert t[t.index("schedule.fired") - 1] == "run.started"
    clock.advance(600)
    await tick_idle(h)
    assert kinds(h) == ["input", "cron"]  # never again


async def test_overdue_one_shot_runs_once_after_downtime(rig):
    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "Check the oven", "in_minutes": 30})
    clock.advance(60 * 24)
    h = await rig.restart(ScriptedProvider([finish()]), clock=clock)
    for _ in range(3):
        await tick_idle(h)
    assert kinds(h) == ["input", "cron"]
    fired = events(h, "schedule.fired")[0]["data"]
    assert fired["missed"] == 1 and fired["delay_seconds"] == (24 * 60 - 30) * 60


async def test_recurring_misses_coalesce_to_latest_occurrence(rig):
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return finish()

    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "Hourly look around", "cron": "0 * * * *"},
                                 steps=[slow, finish()])
    h.submit_input("a long task")
    await asyncio.sleep(0.05)
    for _ in range(10):  # five hours of ticks while the character is busy
        clock.advance(30)
        h.scheduler.tick()
    gate.set()
    await h.wait_idle()
    assert kinds(h) == ["input", "input", "cron"]
    [due] = events(h, "schedule.due")
    fired = events(h, "schedule.fired")[0]["data"]
    assert fired["missed"] == 5 and fired["due_at"] == "2026-10-11T00:00:00.000Z"  # latest, 17:00 local
    assert due["data"]["due_at"] == "2026-10-10T20:00:00.000Z"  # first one
    assert job(h)["next_due_at"] == "2026-10-11T01:00:00.000Z"  # next after now, no backfill


async def test_recurring_misses_while_offline_coalesce(rig):
    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "Daily note", "cron": "0 9 * * *"})
    clock.advance(60 * 24 * 3)
    h = await rig.restart(ScriptedProvider([finish()]), clock=clock)
    await tick_idle(h)
    await tick_idle(h)
    assert kinds(h) == ["input", "cron"]
    assert events(h, "schedule.fired")[0]["data"]["missed"] == 3


async def test_jobs_keep_independent_pending_state(rig):
    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "A", "in_minutes": 10},
                                 {"op": "add", "instruction": "B", "cron": "*/30 * * * *"},
                                 steps=[finish(), finish()])
    clock.advance(31)
    await tick_idle(h)
    assert kinds(h) == ["input", "cron", "cron"]
    assert {json.loads(r["origin"])["job_id"] for r in runs(h)[1:]} == {"cr_1", "cr_2"}


async def test_priority_human_then_manual_then_jobs_then_heartbeat(rig):
    from test_heartbeat import complete_bootstrap

    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return finish()

    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "Job", "in_minutes": 30},
                                 steps=[slow, finish(), finish(), finish(), finish()])
    complete_bootstrap(h)
    h.scheduler.tick()  # arm heartbeat
    h.submit_input("busy")
    await asyncio.sleep(0.05)
    clock.advance(61)
    h.scheduler.tick()  # job and heartbeat both due while busy
    h.request_wake()
    h.submit_input("me first")
    gate.set()
    await h.wait_idle()
    assert kinds(h) == ["input", "input", "input", "host", "cron", "heartbeat"]


async def test_pause_resume_remove_and_reschedule(rig):
    h, p, clock, _ = await setup(
        rig,
        {"op": "add", "instruction": "Recurring", "cron": "0 * * * *"},
        {"op": "add", "instruction": "Once", "in_minutes": 30},
        steps=[manage({"op": "pause", "id": "cr_1"}, {"op": "pause", "id": "cr_2"}), finish(),
               manage({"op": "resume", "id": "cr_1"}, {"op": "resume", "id": "cr_2"}), finish(),
               finish(), finish()],
    )
    clock.advance(61)
    h.scheduler.tick()  # both queued
    # They're queued but the human comes first and pauses them; pausing cancels the pending wakes.
    h.submit_input("pause both")
    await h.wait_idle()
    assert kinds(h) == ["input", "input"]
    assert [e["data"]["reason"] for e in events(h, "wake.cancelled")] == ["paused", "paused"]
    clock.advance(180)
    await tick_idle(h)
    assert kinds(h) == ["input", "input"]  # paused: nothing runs
    h.submit_input("resume both")
    await h.wait_idle()
    await tick_idle(h)
    # The overdue one-shot runs once; the recurring job doesn't backfill the paused hours.
    assert kinds(h) == ["input", "input", "input", "cron"]
    assert json.loads(runs(h)[-1]["origin"])["job_id"] == "cr_2"
    assert job(h, "cr_1")["next_due_at"] == "2026-10-11T00:00:00.000Z"


async def test_update_instruction_keeps_pending_and_timing_reschedules(rig):
    h, p, clock, _ = await setup(
        rig, {"op": "add", "instruction": "Old words", "in_minutes": 10},
        steps=[manage({"op": "update", "id": "cr_1", "instruction": "New words"}), finish(), finish()],
    )
    clock.advance(11)
    h.scheduler.tick()
    h.request_wake()  # manual wake goes before the job and edits it
    await h.wait_idle()
    assert kinds(h) == ["input", "host", "cron"]
    opening = p.requests[-1].messages[-1]["content"]
    assert "Instruction: New words" in opening
    assert types(h).count("schedule.due") == 1 and "wake.cancelled" not in types(h)  # same pending wake

    p2 = ScriptedProvider([manage({"op": "update", "id": "cr_2", "in_minutes": 60}), finish(), finish()])
    h = await rig.restart(p2, clock=clock)
    with h.ledger.tx():
        h.ledger.x("INSERT INTO jobs SELECT 'cr_2', instruction, kind, spec, 'active', next_due_at, created_at, "
                   "updated_at, created_rev, updated_rev, 0, NULL, NULL, NULL, NULL FROM jobs WHERE id = 'cr_1'")
        h.ledger.x("UPDATE jobs SET next_due_at = ? WHERE id = 'cr_2'", "2026-10-10T19:00:00.000Z")
    h.scheduler.tick()  # cr_2 due now: queued
    h.submit_input("push it back an hour")
    await h.wait_idle()
    assert [e["data"]["reason"] for e in events(h, "wake.cancelled")] == ["rescheduled"]
    assert job(h, "cr_2")["next_due_at"] == "2026-10-10T20:11:00.000Z"


async def test_validation_errors_are_all_or_nothing(rig):
    p = ScriptedProvider([
        manage({"op": "add", "instruction": "ok", "in_minutes": 5},
               {"op": "add", "instruction": "bad", "at": "2026-03-08 02:30"}),
        manage({"op": "add", "instruction": "x", "in_minutes": 5, "cron": "0 * * * *"}),
        manage({"op": "add", "instruction": "x", "cron": "* * * * *"}),
        manage({"op": "add", "in_minutes": 5}),
        manage({"op": "add", "instruction": "x", "at": "yesterday"}),
        manage({"op": "remove", "id": "cr_9"}),
        manage({"op": "add", "instruction": "x", "in_minutes": 5}, expected_revision=3),
        finish(),
    ])
    h = await rig.start(p, clock=Clock())
    h.submit_input("go")
    await h.wait_idle()
    errs = [tool_result(r.messages[-1])["error"] for r in p.requests[1:]]
    assert errs[0].startswith("operation 2 (add)") and "doesn't exist" in errs[0]
    assert "only one of" in errs[1] and "5 minutes apart" in errs[2] and "instruction is required" in errs[3]
    assert "local date and time" in errs[4] and "no job" in errs[5] and "conflict" in errs[6]
    assert h.ledger.q1("SELECT COUNT(*) c FROM jobs")["c"] == 0 and cron.revision(h.ledger) == 0


async def test_jobs_and_pending_wakes_survive_restart(rig):
    gate = asyncio.Event()

    async def never(request):
        await gate.wait()

    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "Survive", "cron": "0 * * * *"}, steps=[never])
    h.submit_input("busy")
    await asyncio.sleep(0.05)
    clock.advance(61)
    h.scheduler.tick()
    h = await rig.restart(ScriptedProvider([finish(), finish()]), clock=clock)
    await tick_idle(h)
    assert kinds(h) == ["input", "input", "recovery", "cron"]
    assert types(h).count("schedule.due") == 1 and cron.revision(h.ledger) == 1
    assert [e["data"]["revision"] for e in events(h, "jobs.changed")] == [1]


async def test_job_runs_can_be_silent_or_send(rig):
    h, p, clock, _ = await setup(
        rig, {"op": "add", "instruction": "Quiet check", "in_minutes": 5},
        {"op": "add", "instruction": "Say hi", "in_minutes": 10},
        steps=[call("file_read", path="MEMORY.md"), finish(),
               call("message_send", destination="human", content="hi from a job"), finish()],
    )
    clock.advance(5)
    await tick_idle(h)
    assert human_messages(h) == []
    clock.advance(5)
    await tick_idle(h)
    assert human_messages(h) == ["hi from a job"]


async def test_failed_job_run_is_recorded_not_retried(rig):
    def boom(request):
        raise ProviderError("503")

    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "Try", "in_minutes": 5}, steps=[boom])
    clock.advance(5)
    await tick_idle(h)
    await tick_idle(h)
    assert kinds(h) == ["input", "cron"]
    assert job(h)["last_outcome"] == "failed (provider_error)"


async def test_crashed_job_run_records_interruption(rig):
    clock = Clock()
    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "Fragile", "in_minutes": 5}, clock=clock)
    h = await rig.restart(ScriptedProvider(), clock=clock, fault_hook=crash_at("provider.before_request"))
    clock.advance(5)
    h.scheduler.tick()
    await rig.crashed()
    h = await rig.start(ScriptedProvider([finish()]), clock=clock)
    await h.wait_idle()
    assert job(h)["last_outcome"] == "interrupted (unclean_shutdown)"
    assert kinds(h) == ["input", "cron", "recovery"]  # not re-run; recovery wake instead


async def test_stored_invalid_schedule_is_quarantined(rig):
    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "x", "cron": "0 * * * *"})
    with h.ledger.tx():
        h.ledger.x("UPDATE jobs SET spec = 'garbage' WHERE id = 'cr_1'")
    clock.advance(61)
    await tick_idle(h)
    assert job(h)["status"] == "failed" and "5 fields" in job(h)["error"]
    assert [e["data"]["kind"] for e in events(h, "host.warning")] == ["job_invalid"]
    assert kinds(h) == ["input"]


async def test_jobs_run_regardless_of_first_meeting_and_heartbeat_settings(rig):
    rig.cfg.heartbeat.enabled = False
    h, p, clock, _ = await setup(rig, {"op": "add", "instruction": "x", "in_minutes": 5}, steps=[finish()])
    clock.advance(5)
    await tick_idle(h)
    assert kinds(h) == ["input", "cron"]
