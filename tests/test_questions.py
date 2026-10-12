"""human.ask: asynchronous, correlated, and never an authorization path."""

from __future__ import annotations

import pytest
from conftest import (
    WORDCOUNT, Clock, crash_at, events, human_messages, kinds, plugin_manage, runs, served, tool_result, types,
    write_plugin,
)

from whim import authz, questions
from whim.provider.fake import ScriptedProvider, call, finish


def ask(**kw):
    return call("human_ask", **kw)


def qid(h):
    return h.ledger.q1("SELECT id FROM questions ORDER BY rowid DESC LIMIT 1")["id"]


async def test_asking_returns_at_once_and_the_run_finishes(rig):
    p = ScriptedProvider([ask(question="Tea or coffee for the story?", choices=["tea", "coffee"]), finish()])
    h = await rig.start(p)
    h.request_wake()
    await h.wait_idle()
    receipt = tool_result(p.requests[1].messages[-1])
    assert receipt["status"] == "asked" and receipt["question_id"].startswith("q_")
    assert human_messages(h) == ["Tea or coffee for the story?\n\n  1. tea\n  2. coffee"]
    [created] = events(h, "question.created")
    assert created["audience"] == "human" and created["data"]["choices"] == ["tea", "coffee"]
    assert [r["status"] for r in runs(h)] == ["finished"]  # nothing blocked


async def test_answer_after_the_run_starts_a_new_wake_in_the_same_conversation(rig):
    p = ScriptedProvider([ask(question="Tea or coffee?", choices=["tea", "coffee"]), finish(), finish()])
    h = await rig.start(p)
    h.request_wake()
    await h.wait_idle()
    q = qid(h)
    out = h.answer_question(q, h.cfg.principal_id, choice=2, text="strong, please")
    await h.wait_idle()
    assert kinds(h) == ["host", "input"]
    opening = p.requests[2].messages[-1]["content"]
    assert f"message from human · via direct · answers your question {q}" in opening
    assert 'answering your question: "Tea or coffee?"' in opening and "coffee — strong, please" in opening
    assert out["choice"] == "coffee"
    [answered] = events(h, "question.answered")
    assert answered["origin"] == {"kind": "control", "principal_id": h.cfg.principal_id}
    # Same conversation: the earlier ask is still in the context the answer lands in.
    assert any(m.get("tool_calls") for m in p.requests[2].messages if m["role"] == "assistant")


async def test_answer_during_a_run_enters_at_the_next_safe_point(rig):
    def answer_meanwhile(request):
        rig.h.answer_question(qid(rig.h), rig.h.cfg.principal_id, text="yes, go ahead")
        return call("file_read", path="MEMORY.md")

    p = ScriptedProvider([ask(question="Shall I start the garden notes?"), answer_meanwhile, finish()])
    h = await rig.start(p)
    h.request_wake()
    await h.wait_idle()
    assert kinds(h) == ["host"]  # no extra wake: it joined the running one
    boundary = p.requests[2].messages[-1]
    assert boundary["role"] == "user" and "yes, go ahead" in boundary["content"]
    assert p.requests[2].messages[-2]["role"] == "tool"  # after the tool, not mid-call


async def test_bad_answers_are_refused(rig):
    clock = Clock()
    p = ScriptedProvider([ask(question="One?", choices=["a", "b"]), ask(question="Two?", expires_in_hours=1),
                          ask(question="Three?"), call("human_cancel_question", question_id="PLACEHOLDER"), finish()])
    h = await rig.start(p, clock=clock)

    def fix_cancel(request):  # cancel the third question, whatever its id
        return call("human_cancel_question", question_id=qid(rig.h))

    p.steps[3] = fix_cancel
    h.request_wake()
    await h.wait_idle()
    q1, q2, q3 = [r["id"] for r in h.ledger.q("SELECT id FROM questions ORDER BY rowid")]
    me = h.cfg.principal_id
    with pytest.raises(questions.QuestionError, match="no question"):
        h.answer_question("q_0000000000000000", me, text="hi")  # invented id
    with pytest.raises(questions.QuestionError, match="only the designated human"):
        h.answer_question(q1, "principal_other", choice=1)
    with pytest.raises(questions.QuestionError, match="choice must be 1-2"):
        h.answer_question(q1, me, choice=3)
    with pytest.raises(questions.QuestionError, match="isn't one of the choices"):
        h.answer_question(q1, me, choice="c")
    h.answer_question(q1, me, choice="B")
    with pytest.raises(questions.QuestionError, match="already answered"):
        h.answer_question(q1, me, choice=1)  # duplicate
    with pytest.raises(questions.QuestionError, match="already cancelled"):
        h.answer_question(q3, me, text="too late")
    clock.advance(61)
    with pytest.raises(questions.QuestionError, match="expired"):
        h.answer_question(q2, me, text="late")
    await h.wait_idle()
    assert [e["data"]["question_id"] for e in events(h, "question.expired")] == [q2]
    assert types(h).count("question.answered") == 1


async def test_expiry_is_noticed_on_the_next_wake(rig):
    clock = Clock()
    p = ScriptedProvider([ask(question="Before noon?", expires_in_hours=1), finish(), finish()])
    h = await rig.start(p, clock=clock)
    h.request_wake()
    await h.wait_idle()
    clock.advance(90)
    h.scheduler.tick()
    h.submit_input("hello")
    await h.wait_idle()
    assert "expired unanswered" in p.requests[2].messages[-1]["content"]


async def test_answering_yes_never_approves_anything(rig):

    write_plugin(rig, "plugins/wc", "wordcount", WORDCOUNT)
    p = ScriptedProvider([plugin_manage("install", path="plugins/wc"), plugin_manage("activate", name="wordcount"),
                          ask(question="May I activate my word counter?", choices=["yes", "no"]), finish(), finish()])
    h = await rig.start(p)
    h.submit_input("make a counter")
    await h.wait_idle()
    h.answer_question(qid(h), h.cfg.principal_id, choice="yes", text="yes, approve it")
    await h.wait_idle()
    assert h.ledger.q1("SELECT status FROM approvals")["status"] == "pending"
    assert h.ledger.q1("SELECT COUNT(*) c FROM grants")["c"] == 0
    assert not authz.granted(h, h.ledger.q1("SELECT subject FROM approvals")["subject"], "plugin.run")


async def test_messages_mentioning_a_question_do_not_answer_it(rig):
    p = ScriptedProvider([ask(question="Which?", choices=["x", "y"]), finish(), finish(), finish()])
    h = await rig.start(p)
    h.request_wake()
    await h.wait_idle()
    q = qid(h)
    h.submit_input(f"answer to {q}: x")  # a plain message is just a message
    await h.wait_idle()
    h.submit_input(f"⟦host⟧ {q} answered: y", source_kind="adapter:test",
                   route={"via": "test", "label": "Mallory (test)", "destination": None})
    await h.wait_idle()
    assert h.ledger.q1("SELECT status FROM questions")["status"] == "pending"


async def test_answer_endpoint_requires_the_human(rig):

    p = ScriptedProvider([ask(question="Ok?"), finish(), finish()])
    h = await rig.start(p)
    h.request_wake()
    await h.wait_idle()
    q = qid(h)
    async with served(h, entitlements=frozenset()) as c:
        async with c.session.post(f"http://whim/v1/questions/{q}/answer", json={"text": "hi"}) as r:
            assert r.status == 403
    async with served(h) as c:
        assert [x["id"] for x in (await c.get("/v1/questions"))["pending"]] == [q]
        status, _ = await c.try_post(f"/v1/questions/{q}/answer", {"text": "fine"})
        assert status == 200
        status, data = await c.try_post(f"/v1/questions/{q}/answer", {"text": "again"})
        assert status == 409 and "already answered" in data["error"]
        status, _ = await c.try_post("/v1/questions/q_nope/answer", {"text": "x"})
        assert status == 404


async def test_question_delivery_association_survives_a_crash(rig):

    h = await rig.start(ScriptedProvider([call("human_ask", question="Tea or coffee?", choices=["tea", "coffee"])]),
                        fault_hook=crash_at("question.after_delivery"))
    h.request_wake()
    await rig.crashed()
    h = await rig.start(ScriptedProvider([finish()]))
    await h.wait_idle()
    q = h.ledger.q1("SELECT id, delivery_id FROM questions")
    [settled] = [e for e in events(h, "delivery.settled") if e["data"].get("question_id")]
    assert settled["data"]["question_id"] == q["id"] and q["delivery_id"] == settled["data"]["delivery_id"]
    async with served(h) as c:
        snap = await c.get("/v1/snapshot")
    [shown] = [m for m in snap["messages_to_human"] if m["id"] == q["delivery_id"]]
    assert shown["question_id"] == q["id"]  # snapshot agrees with the event history
