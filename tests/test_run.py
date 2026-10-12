"""Character run semantics: explicit send, explicit finish, boundaries, limits."""

from __future__ import annotations

import json

from conftest import assert_paired, events, human_messages, runs, tool_result, types

from whim.context import CONTINUATION_CUE
from whim.provider.base import ProviderError
from whim.provider.fake import ScriptedProvider, call, calls, finish, say, tool_call


async def test_authored_text_is_not_delivered_only_send_is(rig):
    p = ScriptedProvider([
        say("Private notes: they said hello. I'll answer briefly.", reasoning="r1"),
        call("message_send", destination="reply_to_source", content="Hi!", reasoning="r2"),
        finish(reasoning="r3"),
    ])
    h = await rig.start(p)
    h.submit_input("hello")
    await h.wait_idle()

    assert human_messages(h) == ["Hi!"]
    [authored] = events(h, "message.completed")
    assert authored["data"]["content"].startswith("Private notes")
    assert [r["status"] for r in runs(h)] == ["finished"]
    assert len(p.requests) == 3  # three provider requests, one run

    # A text-only response is followed by the fixed host continuation cue, never a fake human.
    second = p.requests[1].messages
    assert second[-1] == {"role": "user", "content": CONTINUATION_CUE}
    # reasoning_content is replayed on every later request.
    third = p.requests[2].messages
    assistants = [m for m in third if m["role"] == "assistant"]
    assert [m["reasoning_content"] for m in assistants] == ["r1", "r2"]
    assert tool_result(third[-1])["status"] == "sent"


async def test_text_only_responses_pause_instead_of_finishing(rig):
    rig.cfg.limits.max_text_only_streak = 3
    h = await rig.start(ScriptedProvider([say("a"), say("b"), say("c")]))
    h.submit_input("hello")
    await h.wait_idle()
    [run] = runs(h)
    assert (run["status"], run["reason"]) == ("paused", "stalled")
    assert "run.finished" not in types(h)
    assert human_messages(h) == []


async def test_silent_host_wake_finishes_without_delivery(rig):
    h = await rig.start(ScriptedProvider([call("file_read", path="MEMORY.md"), finish()]))
    h.request_wake(reason="manual")
    await h.wait_idle()
    [run] = runs(h)
    assert run["status"] == "finished"
    assert not [t for t in types(h) if t.startswith("delivery.")]


async def test_input_during_tool_is_admitted_once_at_next_boundary(rig):
    def on_fault(point):
        if point == "tool.after_dispatch":
            rig.h.submit_input("are you there?")

    p = ScriptedProvider([call("file_read", path="MEMORY.md"), finish()])
    h = await rig.start(p, fault_hook=on_fault)
    h.submit_input("hello")
    await h.wait_idle()

    msgs = p.requests[1].messages
    assert msgs[-2]["role"] == "tool"  # tool result first, then the new input
    assert msgs[-1]["role"] == "user" and "are you there?" in msgs[-1]["content"]
    assert len(runs(h)) == 1
    admitted = events(h, "input.admitted")
    assert len(admitted) == 2 and {e["run_id"] for e in admitted} == {runs(h)[0]["id"]}
    assert "input.pending" in types(h)


async def test_input_racing_finish_becomes_next_wake(rig):
    def finishing(request):
        rig.h.submit_input("one more thing")  # arrives while the finish is in flight
        return finish()

    p = ScriptedProvider([finishing, finish()])
    h = await rig.start(p)
    h.submit_input("hello")
    await h.wait_idle()

    r1, r2 = runs(h)
    assert r1["status"] == r2["status"] == "finished"
    late = [e for e in events(h, "input.admitted")][1]
    assert late["run_id"] == r2["id"]
    assert "one more thing" in p.requests[1].messages[-1]["content"]
    # exactly once in the model context
    assert sum("one more thing" in (m.get("content") or "") for m in p.requests[1].messages) == 1


async def test_finish_mixed_with_other_calls_is_rejected_recoverably(rig):
    p = ScriptedProvider([
        calls(tool_call("file_read", path="MEMORY.md"), tool_call("turn_finish")),
        finish(),
    ])
    h = await rig.start(p)
    h.submit_input("hi")
    await h.wait_idle()
    read, fin = p.requests[1].messages[-2:]
    assert tool_result(read)["ok"] is True
    assert "call it alone" in tool_result(fin)["error"]
    [run] = runs(h)
    assert run["status"] == "finished" and run["requests"] == 2


async def test_unknown_tool_and_bad_args_return_errors(rig):
    p = ScriptedProvider([
        calls(tool_call("rm_rf", path="/"), tool_call("file_read", nope=1)),
        finish(),
    ])
    h = await rig.start(p)
    h.submit_input("hi")
    await h.wait_idle()
    unknown, bad = p.requests[1].messages[-2:]
    assert "unknown tool" in tool_result(unknown)["error"]
    assert "invalid arguments" in tool_result(bad)["error"]


async def test_request_limit_pauses(rig):
    rig.cfg.limits.max_requests_per_run = 2
    h = await rig.start(ScriptedProvider([call("file_read", path="MEMORY.md")] * 5))
    h.submit_input("hi")
    await h.wait_idle()
    [run] = runs(h)
    assert (run["status"], run["reason"]) == ("paused", "request_limit")


async def test_provider_failure_fails_run_and_next_wake_merges_user_turns(rig):
    def boom(request):
        raise ProviderError("503")

    p = ScriptedProvider([boom, finish()])
    h = await rig.start(p)
    h.submit_input("first")
    await h.wait_idle()
    h.submit_input("second")
    await h.wait_idle()
    r1, r2 = runs(h)
    assert (r1["status"], r1["reason"]) == ("failed", "provider_error")
    assert r2["status"] == "finished"
    msgs = p.requests[1].messages
    # No consecutive user messages are sent, even though two user rows are stored.
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "first" in msgs[1]["content"] and "second" in msgs[1]["content"]


async def test_human_input_is_admitted_before_host_wakes(rig):
    def queue_both(h):
        h.request_wake()
        h.submit_input("me first")

    h = await rig.start(ScriptedProvider([finish(), finish()]), before_start=queue_both)
    await h.wait_idle()
    assert [json.loads(r["origin"])["kind"] for r in runs(h)] == ["input", "host"]


async def test_missed_reply_check_stays_out_of_the_way(rig):
    p = ScriptedProvider([
        call("turn_finish"),  # silent finish without text: accepted
        call("message_send", destination="human", content="hi", authored="notes"), call("turn_finish", authored="done"),
        call("turn_finish", authored="private heartbeat notes"),  # nobody spoke: accepted
    ])
    h = await rig.start(p)
    h.submit_input("one")
    await h.wait_idle()
    h.submit_input("two")
    await h.wait_idle()
    h.request_wake()
    await h.wait_idle()
    assert [r["requests"] for r in runs(h)] == [1, 2, 1]
    assert "missed_reply_check" not in json.dumps(events(h, "context.note"))


async def test_missed_reply_nudge_never_forces_delivery_and_silence_can_win(rig):
    """Regression: the bounded nudge questions a finish once, never delivers anything
    itself, and a second turn_finish completes an intentionally silent run."""
    private = "I'd rather not answer this one yet; let it sit."
    p = ScriptedProvider([
        call("turn_finish", authored=private),
        call("turn_finish", authored=private + " Still choosing silence."),
    ])
    h = await rig.start(p)
    h.submit_input("anything to say?")
    await h.wait_idle()
    [run] = runs(h)
    assert (run["status"], run["reason"], run["requests"]) == ("finished", "turn_finish", 2)
    assert not [t for t in types(h) if t.startswith("delivery.")]  # nothing published
    notice = tool_result(p.requests[1].messages[-1])["error"]
    assert private not in notice and "call turn_finish again" in notice
    notes = [e for e in events(h, "context.note") if e["data"].get("kind") == "missed_reply_check"]
    assert len(notes) == 1  # at most once per run
    ops = [dict(o) for o in h.ledger.q("SELECT status FROM operations WHERE tool = 'turn.finish' ORDER BY rowid")]
    assert [o["status"] for o in ops] == ["error", "ok"]


# -- every committed tool call gets an outcome; budgets hold per call -------------------


async def test_unexpected_exception_in_any_tool_still_pairs_its_call(rig, monkeypatch):
    from whim import heartbeat

    def boom(*a, **k):
        raise RuntimeError("bug in the tool")

    monkeypatch.setattr(heartbeat, "apply_ops", boom)
    p = ScriptedProvider([calls(tool_call("heartbeat_manage", operations=[]),
                                tool_call("file_read", path="SOUL.md")), finish(), finish()])
    h = await rig.start(p)
    h.submit_input("go")
    await h.wait_idle()
    h.submit_input("next")
    await h.wait_idle()
    for req in p.requests:
        assert_paired(req.messages)
    first = tool_result(p.requests[1].messages[-2])
    assert not first["ok"] and "bug in the tool" in first["error"]


async def test_a_run_that_dies_mid_batch_leaves_no_unanswered_calls(rig, monkeypatch):
    from whim import run as runmod

    real = runmod._execute_tool
    seen = []

    async def dies_on_second(h, state, pos, idx, tc, tools):
        seen.append(idx)
        if idx == 1:
            raise RuntimeError("host bug after the first call")
        return await real(h, state, pos, idx, tc, tools)

    monkeypatch.setattr(runmod, "_execute_tool", dies_on_second)
    p = ScriptedProvider([calls(tool_call("file_read", path="SOUL.md"), tool_call("file_write", path="x.md",
                                                                                  content="x")), finish()])
    h = await rig.start(p)
    h.submit_input("go")
    await h.wait_idle()
    monkeypatch.setattr(runmod, "_execute_tool", real)
    h.submit_input("next")
    await h.wait_idle()
    assert runs(h)[0]["status"] == "failed"
    assert_paired(p.requests[-1].messages)
    assert not (rig.cfg.workspace / "x.md").exists()  # never retried


async def test_tool_budget_is_enforced_before_each_dispatch_across_batches(rig):
    rig.cfg.limits.max_tool_calls_per_run = 2
    p = ScriptedProvider([call("file_read", path="SOUL.md"),
                          calls(tool_call("message_send", destination="human", content="m0"),
                                *[tool_call("file_write", path=f"f{i}.md", content="x") for i in range(2)])])
    h = await rig.start(p)
    h.submit_input("go")
    await h.wait_idle()
    assert human_messages(h) == ["m0"] and not list(rig.cfg.workspace.glob("f*.md"))
    statuses = [r["status"] for r in h.ledger.q("SELECT status FROM operations ORDER BY rowid")]
    assert statuses == ["ok", "ok", "not_dispatched", "not_dispatched"]
    [run] = runs(h)
    assert (run["status"], run["reason"]) == ("paused", "tool_limit")
    assert_paired(h.context.messages())
