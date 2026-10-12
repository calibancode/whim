"""AG-UI 1.0 projection: schema validity, stream conformance, views, audience, replay."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import (
    Clock, Outbox, TestChat, check_conformance, crash_at, events, project_all, read_sse, runs, served, until,
)

from whim import agui
from whim.provider.base import Completion, ProviderError
from whim.provider.fake import ScriptedProvider, call, calls, finish, say, tool_call

SECRET_REASONING = "PRIVATE-CHAIN-OF-THOUGHT-91"


def usage(**u):
    return {"prompt_tokens": 100, "completion_tokens": 20, "cache_hit_tokens": 64, "reasoning_tokens": 5, **u}


def with_usage(c: Completion, **u) -> Completion:
    c.usage, c.model = usage(**u), "deepseek-v4-pro"
    return c


async def build_history(rig):
    """A ledger with every kind of run the projection must handle."""
    rig.cfg.limits.max_text_only_streak = 1
    clock = Clock()

    def boom(request):
        raise ProviderError("503")

    p = ScriptedProvider([
        # run 1: human message; private text + tool + send to human + unknown tool + finish
        with_usage(call("file_read", path="MEMORY.md", authored="Private: they said hi.", reasoning=SECRET_REASONING)),
        with_usage(calls(tool_call("message_send", destination="human", content="Hello!"),
                         tool_call("no_such_tool"))),
        with_usage(call("message_send", destination="outbox", content="external")),
        with_usage(finish()),
        # run 2: host wake that finishes silently
        finish(),
        # run 3: paused by the text-only limit
        say("just thinking"),
        # run 4: provider failure
        boom,
        # run 5: a scheduled job
        call("cron_manage", operations=[{"op": "add", "instruction": "later", "in_minutes": 5}]),
        finish(),
        finish(),
    ])
    h = await rig.start(p, clock=clock, destinations=[Outbox()])
    h.submit_input("hi there")
    await h.wait_idle()
    h.request_wake()
    await h.wait_idle()
    h.submit_input("think")
    await h.wait_idle()
    h.submit_input("fail")
    await h.wait_idle()
    h.submit_input("schedule something")
    await h.wait_idle()
    clock.advance(5)
    h.scheduler.tick()
    await h.wait_idle()

    # run 7: crash mid-run, then restart (recovery wake)
    h = await rig.restart(ScriptedProvider([call("file_write", path="x.md", content="x")]),
                          clock=clock, fault_hook=crash_at("tool.after_dispatch"))
    h.submit_input("crash please")
    await rig.crashed()
    h = await rig.start(ScriptedProvider([finish()]), clock=clock, destinations=[Outbox()])
    await h.wait_idle()
    return h


@pytest.fixture
async def history(rig):
    return await build_history(rig)


@pytest.mark.parametrize("view", agui.VIEWS)
async def test_every_projected_event_is_schema_valid_and_conformant(history, view):
    stream = project_all(history, view)
    assert stream
    for e in stream:
        agui.validate(e)
    check_conformance(stream)
    assert not any(e["type"].startswith("REASONING") for e in stream)
    assert SECRET_REASONING not in json.dumps(stream)


async def test_run_lifecycle_mapping_and_ids(history):
    stream = project_all(history, "activity")
    starts = [e for e in stream if e["type"] == "RUN_STARTED"]
    ends = [e for e in stream if e["type"] in ("RUN_FINISHED", "RUN_ERROR")]
    assert [s["runId"] for s in starts] == [r["id"] for r in runs(history)]
    assert {s["threadId"] for s in starts} == {history.cfg.thread_id}
    by_status = {r["status"] + ":" + (r["reason"] or ""): r["id"] for r in runs(history)}
    end_for = {e.get("runId"): e for e in ends if "runId" in e}
    paused = end_for[by_status["paused:stalled"]]
    assert paused["type"] == "RUN_FINISHED" and paused["outcome"] == {"type": "cancelled"}
    errors = [e for e in ends if e["type"] == "RUN_ERROR"]
    assert [e["code"] for e in errors] == ["provider_error", "interrupted"]
    first = end_for[runs(history)[0]["id"]]
    assert first["usage"] == [{"provider": "scripted", "model": "deepseek-v4-pro", "inputTokens": 400,
                               "outputTokens": 80, "cachedInputTokens": 256, "reasoningTokens": 20,
                               "totalTokens": 480}]
    # Runs without reported usage omit counts rather than reporting zeros.
    silent = end_for[runs(history)[1]["id"]]
    assert all("inputTokens" not in u for u in silent.get("usage", []))


async def test_conversation_view_has_only_real_exchanges(history):
    stream = project_all(history, "conversation")
    msgs = [e for e in stream if e["type"] == "TEXT_MESSAGE_START"]
    contents = {e["messageId"]: e["delta"] for e in stream if e["type"] == "TEXT_MESSAGE_CONTENT"}
    roles = [(m["role"], contents[m["messageId"]]) for m in msgs]
    assert ("user", "hi there") in roles and ("assistant", "Hello!") in roles
    assert all(text != "Private: they said hi." for _, text in roles)
    assert all(m["metadata"]["whim"]["kind"] in ("input", "delivery") for m in msgs)
    delivered = [m["messageId"] for m in msgs if m["role"] == "assistant"]
    assert delivered == [e["data"]["delivery_id"] for e in events(history, "delivery.settled")
                         if e["data"]["destination"] == "human"]
    assert not [e for e in stream if e["type"].startswith("TOOL_CALL")
                or (e["type"] == "CUSTOM" and e["name"] != "whim.approval.requested")]


async def test_activity_marks_private_text_and_shows_tools(history):
    stream = project_all(history, "activity")
    authored = [e for e in stream if e["type"] == "TEXT_MESSAGE_START"
                and e["metadata"]["whim"]["kind"] == "authored"]
    assert authored and all(e["metadata"]["whim"]["delivered"] is False for e in authored)
    starts = {e["toolCallId"]: e["toolCallName"] for e in stream if e["type"] == "TOOL_CALL_START"}
    ops = {r["id"]: r["tool"] for r in history.ledger.q("SELECT id, tool FROM operations")}
    assert starts == ops  # every call, by stable operation id, including rejected ones
    assert "no_such_tool" in starts.values()
    names = {e["name"] for e in stream if e["type"] == "CUSTOM"}
    assert {"whim.delivery", "whim.schedule.fired", "whim.jobs.changed"} <= names
    assert "whim.provider.request.finished" not in names  # inspect only


async def test_audience_filtering_for_a_non_operator_viewer(history):
    stream = project_all(history, "inspect", entitlements=("human",))
    for e in stream:
        agui.validate(e)
    check_conformance(stream)
    kinds = {e["metadata"]["whim"]["kind"] for e in stream if e["type"] == "TEXT_MESSAGE_START"}
    assert kinds == {"input", "delivery"}
    assert not [e for e in stream if e["type"].startswith("TOOL_CALL") or e["type"] == "CUSTOM"]
    assert all("usage" not in e and "metadata" not in e for e in stream
               if e["type"] in ("RUN_FINISHED", "RUN_ERROR"))


async def test_projection_is_deterministic_across_restart(rig, history):
    before = project_all(history, "inspect")
    h = await rig.restart(ScriptedProvider())
    assert project_all(h, "inspect") == before


async def test_sse_replay_matches_projection_and_resumes_mid_run(history):
    expected = project_all(history, "activity")
    async with served(history) as c:
        frames = await read_sse(c, "/v1/agui?view=activity&follow=0")
        got = [e for _, e in frames if e is not None]
        assert got == expected
        # Ids only on the last frame of each ledger event's group.
        for i, (fid, e) in enumerate(frames):
            if e is not None and fid is None:
                assert frames[i + 1][0] is None or frames[i + 1][1] is not None
        # Resume from a cursor inside the first run (after its tool call started).
        first_run = runs(history)[0]["id"]
        cursor = next(e["seq"] for e in events(history, "tool.started") if e["run_id"] == first_run)
        resumed = [e for _, e in await read_sse(c, f"/v1/agui?view=activity&follow=0&after={cursor}") if e]
        assert resumed[0]["type"] == "RUN_STARTED" and resumed[0]["runId"] == first_run
        assert resumed[0]["metadata"]["whim"]["restated"] is True
        snapshot = resumed[1]
        assert snapshot["type"] == "MESSAGES_SNAPSHOT"  # the open tool call is restated, as a snapshot
        agui.validate(snapshot)
        check_conformance(resumed)
        pr = agui.Projector(history, "activity")
        tail = [x for ev in history.ledger.events_after(cursor, 100000) for x in pr.project(ev)]
        assert resumed[2:] == tail  # nothing lost, nothing completed repeated
        # The snapshot is the complete message set so far, consistent with the live ids.
        before = [x for ev in history.ledger.events_after(0, cursor) for x in pr.project(ev)]
        ids = {e.get("messageId") or e.get("parentMessageId") for e in before
               if e["type"] in ("TEXT_MESSAGE_START", "TOOL_CALL_START", "TOOL_CALL_RESULT")}
        assert {m["id"] for m in snapshot["messages"]} == ids

        # Resuming inside a run with no open tool call restates only RUN_STARTED.
        cursor2 = next(e["seq"] for e in events(history, "input.admitted") if e["run_id"] == first_run)
        resumed2 = [e for _, e in await read_sse(c, f"/v1/agui?view=activity&follow=0&after={cursor2}") if e]
        assert resumed2[0]["type"] == "RUN_STARTED" and resumed2[1]["type"] != "MESSAGES_SNAPSHOT"
        check_conformance(resumed2)


async def test_last_event_id_header_and_live_follow(rig):
    h = await rig.start(ScriptedProvider([call("message_send", destination="human", content="live!"), finish()]))
    async with served(h) as c:
        cursor = h.ledger.last_seq()
        task = asyncio.create_task(read_sse(c, f"/v1/agui?after={cursor}",
                                            stop=lambda e: e["type"] == "RUN_FINISHED"))
        await asyncio.sleep(0.05)
        await c.post("/v1/input", {"text": "hello"})
        frames = await task
        live = [e for _, e in frames if e]
        check_conformance(live)
        assert [(e["role"]) for e in live if e["type"] == "TEXT_MESSAGE_START"] == ["user", "assistant"]
        last_id = [fid for fid, _ in frames if fid is not None][-1]
        async with c.session.get("http://whim/v1/agui?follow=0", headers={"Last-Event-ID": str(last_id)}) as r:
            assert (await r.read()).strip() == b""  # nothing after the last id


async def test_viewing_never_wakes_the_character(history):
    before = (len(runs(history)), history.ledger.q1("SELECT COUNT(*) c FROM wakes")["c"], history.ledger.last_seq())
    async with served(history) as c:
        for view in agui.VIEWS:
            await read_sse(c, f"/v1/agui?view={view}&follow=0")
        await read_sse(c, "/v1/events?follow=0")
    await asyncio.sleep(0.05)
    after = (len(runs(history)), history.ledger.q1("SELECT COUNT(*) c FROM wakes")["c"], history.ledger.last_seq())
    assert after == before


async def test_native_stream_and_agui_filter_audience_on_replay(history):
    async with served(history, entitlements=frozenset({"human"})) as c:
        native = [e for _, e in await read_sse(c, "/v1/events?follow=0") if e]
        projected = [e for _, e in await read_sse(c, "/v1/agui?view=inspect&follow=0") if e]
    assert native and {e["audience"] for e in native} == {"human"}
    assert "Private: they said hi." not in json.dumps(native + projected)
    assert not [e for e in projected if e["type"].startswith("TOOL_CALL")]


# -- snapshots and resumption agree with the live stream --------------------------------


def reduce_agui(stream, messages=None):
    """A client's message state from AG-UI events (snapshots reconcile by id)."""
    msgs = dict(messages or {})
    args = {}
    for e in stream:
        t = e["type"]
        if t == "MESSAGES_SNAPSHOT":
            for m in e["messages"]:
                msgs[m["id"]] = json.loads(json.dumps(m))
        elif t == "TEXT_MESSAGE_START":
            m = {"id": e["messageId"], "role": e["role"], "content": ""}
            for k in ("name", "metadata"):
                if k in e:
                    m[k] = e[k]
            msgs[m["id"]] = m
        elif t == "TEXT_MESSAGE_CONTENT":
            msgs[e["messageId"]]["content"] += e["delta"]
        elif t == "TOOL_CALL_START":
            args[e["toolCallId"]] = ""
            msgs[e["parentMessageId"]] = {"id": e["parentMessageId"], "role": "assistant", "toolCalls": [
                {"id": e["toolCallId"], "type": "function", "function": {"name": e["toolCallName"], "arguments": ""}}]}
        elif t == "TOOL_CALL_ARGS":
            args[e["toolCallId"]] += e["delta"]
        elif t == "TOOL_CALL_END":
            msgs[f"{e['toolCallId']}.call"]["toolCalls"][0]["function"]["arguments"] = args[e["toolCallId"]]
        elif t == "TOOL_CALL_RESULT":
            msgs[e["messageId"]] = {"id": e["messageId"], "role": "tool", "content": e["content"],
                                    "toolCallId": e["toolCallId"]}
    return msgs


async def rich_history(rig):
    """Direct and external input, private text, a delivered question and its answer."""

    p = ScriptedProvider([
        call("human_ask", question="Tea or coffee?", choices=["tea", "coffee"], authored="(private musing)"),
        call("message_send", destination="human", content="Asked!"),
        finish(),
        finish(),  # the answer's wake
        call("message_send", destination="reply_to_source", content="hi Alice"),
        finish(),
    ])
    h = await rig.start(p, destinations=[TestChat()])
    h.submit_input("hello")
    await h.wait_idle()
    q = h.ledger.q1("SELECT id FROM questions")["id"]
    h.answer_question(q, h.cfg.principal_id, choice=1)
    await h.wait_idle()
    h.attach_adapter("testchat").submit(sender_id="u-42", sender_name="Alice", channel="garden", text="hi!",
                                        message_id="m1")
    await h.wait_idle()
    return h


@pytest.mark.parametrize("view", ["conversation", "activity", "inspect"])
async def test_snapshots_are_semantically_equal_to_the_live_stream(rig, view):
    from whim import agui

    h = await rich_history(rig)
    pr = agui.Projector(h, view)
    evs = h.ledger.events_after(0, 100000)
    live = reduce_agui(x for ev in evs for x in pr.project(ev))
    # The metadata that matters survives into the live state in the first place.
    metas = [m.get("metadata", {}).get("whim", {}) for m in live.values()]
    q = h.ledger.q1("SELECT id FROM questions")["id"]
    assert {"kind": "delivery", "destination": "human", "question_id": q} in metas
    assert any(m.get("kind") == "input" and m.get("question_id") == q for m in metas)
    if view != "conversation":
        assert {"kind": "authored", "delivered": False} in metas
        assert any(m.get("sender", {}).get("id") == "u-42" for m in metas)
    # At every cursor, a snapshot equals what a live client had accumulated by then.
    for i, ev in enumerate(evs):
        upto = reduce_agui(x for e in evs[: i + 1] for x in pr.project(e))
        snap = pr.messages(ev["seq"])
        assert [m["id"] for m in snap] == list(upto), ev["seq"]
        assert {m["id"]: m for m in snap} == upto, ev["seq"]
        # A reconnecting client (prefix + tail) converges on the same state as live.
        resumed = reduce_agui(pr.resume_prefix(ev["seq"]) + [x for e in evs[i + 1:] for x in pr.project(e)], upto)
        assert resumed == live


async def test_restated_run_started_matches_the_live_one(rig):
    from whim import agui

    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return finish()

    h = await rig.start(ScriptedProvider([call("file_read", path="MEMORY.md"), slow]))
    h.submit_input("hello")
    await until(lambda: events(h, "tool.finished"))
    pr = agui.Projector(h, "inspect")
    started = next(x for ev in events(h, "run.started") for x in pr.project(ev))
    restated = pr.resume_prefix(h.ledger.last_seq())[0]
    assert restated["metadata"]["whim"].pop("restated") is True
    assert restated == started
    gate.set()
    await h.wait_idle()
