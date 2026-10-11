"""Regressions for the medium-severity audit findings (second stabilization pass)."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import Clock, Outbox, events, runs

from whim import compaction
from whim.provider.base import ProviderError
from whim.provider.fake import ScriptedProvider, call, calls, finish, tool_call


def kinds(h):
    return [json.loads(r["origin"])["kind"] for r in runs(h)]


async def until(pred, timeout=5.0):
    async def go():
        while not pred():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(go(), timeout)


# -- scheduler independence -------------------------------------------------------------


async def test_jobs_and_question_expiry_run_without_heartbeats(rig):
    """The daemon's background clock drives jobs and question expiry even when
    heartbeats are disabled; only heartbeats themselves stay off."""
    rig.cfg.heartbeat.enabled = False
    clock = Clock()
    p = ScriptedProvider([
        call("cron_manage", operations=[{"op": "add", "instruction": "water the plants", "in_minutes": 5}]),
        call("human_ask", question="Before noon?", expires_in_hours=1),
        finish(),
        finish(),  # the job's wake
    ])
    h = await rig.start(p, clock=clock)
    h.request_wake()
    await h.wait_idle()
    clock.advance(90)
    h = await rig.restart(p, clock=clock, run_scheduler=True)  # as the daemon starts it
    await until(lambda: "cron" in kinds(h) and events(h, "question.expired"))
    await h.wait_idle()
    assert kinds(h) == ["host", "cron"]
    assert not events(h, "schedule.armed") and h.ledger.q1("SELECT 1 FROM wakes WHERE kind = 'heartbeat'") is None


# -- interrupted maintenance ------------------------------------------------------------


@pytest.mark.parametrize("action", ["stop", "interrupt"])
async def test_stopping_maintenance_cancels_the_compaction_instead_of_retrying(rig, action):
    from test_compaction import Character, comp, setup, talk

    gate, entered = asyncio.Event(), asyncio.Event()

    async def slow_maintenance(request):
        entered.set()
        await gate.wait()
        return call("journal_write", entry="half a thought")

    h, clock = await setup(rig, character=Character(maintenance=[slow_maintenance]), trigger=2500, force=4000)
    task = asyncio.create_task(talk(h, 5))
    await asyncio.wait_for(entered.wait(), 5)
    h.submit_input("are you there?")  # waits behind maintenance
    getattr(h, f"control_{action}")(h.cfg.principal_id)
    gate.set()
    await task
    await h.wait_idle()

    assert kinds(h).count("maintenance") == 1  # no replacement maintenance
    assert comp(h)["status"] == "cancelled"
    assert [e["data"]["compaction_id"] for e in events(h, "compaction.cancelled")] == [comp(h)["id"]]
    assert not events(h, "compaction.failed")  # a deliberate cancellation is not a failure
    assert h.ledger.get_meta("compaction_backoff") is None
    assert kinds(h)[-1] == "input"  # the human's message went through
    st = compaction.status(h)
    assert st["suppressed_until"] is not None and st.get("failures_in_a_row") is None

    # After the hold, compaction starts afresh (a new compaction, not a retry).
    clock.advance(61)
    await talk(h, 1)
    assert kinds(h).count("maintenance") == 2 and comp(h)["status"] == "committed"


async def test_crashed_maintenance_is_still_retried(rig):
    """Recoverable failure keeps its retry; only deliberate cancellation stops it."""
    from test_compaction import Character, comp, setup, talk

    def down(request):
        raise ProviderError("503")

    h, _ = await setup(rig, character=Character(maintenance=[down, finish()]))
    await talk(h, 5)
    assert kinds(h).count("maintenance") == 2 and comp(h)["status"] == "committed"
    assert not events(h, "compaction.cancelled")


# -- recovery event completeness --------------------------------------------------------


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
    from test_agui import check_conformance, project_all
    from test_recovery import crash_then_restart

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


# -- external sender attribution --------------------------------------------------------


async def test_verified_sender_attribution_is_published_to_entitled_viewers(rig):
    from test_adapters import start
    from test_agui import project_all, read_sse, served

    h, chat, adapter = await start(rig, [call("message_send", destination="reply_to_source", content="hi Alice"),
                                         finish(), finish()])
    adapter.submit(sender_id="u-42", sender_name="Alice", channel="garden", text="hello!", message_id="m1")
    await h.wait_idle()
    [ev] = events(h, "input.received")
    assert ev["data"]["sender"] == {"role": "external", "verified_by": "adapter:testchat", "id": "u-42",
                                    "name": "Alice", "label": "Alice (testchat)", "channel": "garden"}
    assert ev["data"]["reply_destination"] == "testchat:garden"
    assert ev["audience"] == "operator" and "principal_id" not in ev["origin"]  # never the human's authority
    [req] = events(h, "delivery.requested")
    assert req["data"]["destination"] == "testchat:garden" and req["data"]["requested_as"] == "reply_to_source"

    # AG-UI: the operator's activity view carries the attribution on the user message.
    [start_ev] = [e for e in project_all(h, "activity") if e["type"] == "TEXT_MESSAGE_START" and e["role"] == "user"]
    assert start_ev["name"] == "Alice (testchat)"
    assert start_ev["metadata"]["whim"]["sender"]["id"] == "u-42"
    assert start_ev["metadata"]["whim"]["reply_destination"] == "testchat:garden"

    # The designated human's direct messages are attributed by the host, never the body.
    async with served(h) as c:
        await c.post("/v1/input", {"text": "me", "sender": {"role": "external", "id": "spoof"},
                                   "origin": {"principal_id": "x"}})
        await h.wait_idle()
        direct = events(h, "input.received")[-1]
        assert direct["data"]["sender"] == {"role": "designated_human", "verified_by": "direct", "label": "human"}
        assert direct["data"]["reply_destination"] == "human"
    # A viewer without operator entitlement never sees the external sender.
    async with served(h, entitlements=frozenset({"human"})) as c:
        native = [e for _, e in await read_sse(c, "/v1/events?follow=0") if e]
        projected = [e for _, e in await read_sse(c, "/v1/agui?view=inspect&follow=0") if e]
    assert "u-42" not in json.dumps(native + projected) and "Alice" not in json.dumps(native + projected)


# -- AG-UI snapshot fidelity ------------------------------------------------------------


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
    from test_adapters import TestChat

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


# -- slow subscribers --------------------------------------------------------------------


def test_subscriber_queues_are_bounded(cfg):
    from whim.ledger import Ledger

    led = Ledger(cfg.db_path, character_id="c", thread_id="t")
    dropped = []
    q = led.subscribe(limit=3, on_overflow=lambda: dropped.append(1))
    for i in range(5):
        with led.tx():
            led.append("host.warning", {"i": i})
    assert q.qsize() == 3 and dropped == [1]  # dropped once, at the first event it couldn't hold
    assert q not in led._subscribers  # never fed again, so never grows
    led.close()


async def test_a_stalled_client_is_disconnected_and_resumes_by_cursor(rig, monkeypatch):
    from test_agui import read_sse, served

    from whim import ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "SUBSCRIBER_QUEUE_LIMIT", 4)
    h = await rig.start()
    big = "x" * 200_000
    async with served(h) as c:
        start = h.ledger.last_seq()
        reader, writer = await asyncio.open_unix_connection(str(h.cfg.socket_path))
        writer.write(f"GET /v1/events?after={start} HTTP/1.0\r\nHost: whim\r\n\r\n".encode())
        await writer.drain()
        await until(lambda: h.ledger._subscribers)
        for i in range(40):  # far more than the socket buffers hold; the client reads nothing
            with h.ledger.tx():
                h.ledger.append("host.warning", {"i": i, "pad": big})
            await asyncio.sleep(0)
        await until(lambda: not h.ledger._subscribers)  # dropped instead of buffering without end
        raw = b""
        while chunk := await asyncio.wait_for(reader.read(1 << 20), 5):
            raw += chunk  # the server closed the connection: read to EOF
        writer.close()
        # As an SSE client does: only complete frames (ended by a blank line) count.
        body = raw.split(b"\r\n\r\n", 1)[1].decode(errors="replace")
        frames = body.split("\n\n")[:-1]
        ids = [int(line[3:]) for f in frames for line in f.splitlines() if line.startswith("id:")]
        last = ids[-1] if ids else start
        assert last < h.ledger.last_seq()  # it really was cut off
        rest = [e for _, e in await read_sse(c, f"/v1/events?follow=0&after={last}") if e]
        assert [e["data"]["i"] for e in rest] == list(range(len(ids), 40))  # nothing lost, nothing repeated


# -- file durability --------------------------------------------------------------------


class FsyncSpy:
    """Records durability-relevant syscalls as (call, inode, is_dir)."""

    def __init__(self, monkeypatch, on_dir_sync=None):
        import os

        self.log = []
        real_fsync, real_replace, real_mkdir = os.fsync, os.replace, os.mkdir

        def fsync(fd):
            st = os.fstat(fd)
            import stat as s
            self.log.append(("fsync", st.st_ino, s.S_ISDIR(st.st_mode)))
            out = real_fsync(fd)
            if s.S_ISDIR(st.st_mode) and on_dir_sync is not None:
                on_dir_sync()
            return out

        def replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            self.log.append(("replace", os.fstat(dst_dir_fd).st_ino if dst_dir_fd is not None else None, True))
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def mkdir(path, mode=0o777, *, dir_fd=None):
            self.log.append(("mkdir", os.fstat(dir_fd).st_ino if dir_fd is not None else None, True))
            return real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "mkdir", mkdir)

    def synced_after(self, call: str, ino: int) -> bool:
        """Some fsync of directory `ino` follows the last `call` made in it."""
        idx = max(i for i, e in enumerate(self.log) if e[0] == call and e[1] == ino)
        return any(e == ("fsync", ino, True) for e in self.log[idx + 1:])


def test_atomic_write_makes_the_rename_and_new_folders_durable(tmp_path, monkeypatch):
    from whim import safefs

    spy = FsyncSpy(monkeypatch)
    safefs.write_atomic(tmp_path, "notes/deep/plan.md", b"v1")
    root, notes, deep = (tmp_path.stat().st_ino, (tmp_path / "notes").stat().st_ino,
                         (tmp_path / "notes/deep").stat().st_ino)
    assert spy.synced_after("replace", deep)  # the new name survives a power cut
    assert spy.synced_after("mkdir", root) and spy.synced_after("mkdir", notes)  # and the folders holding it
    file_syncs = [i for i, e in enumerate(spy.log) if e[0] == "fsync" and not e[2]]
    assert file_syncs and file_syncs[0] < spy.log.index(("replace", deep, True))  # data before the name


def test_exclusive_create_is_durable(tmp_path, monkeypatch):
    from whim import safefs

    spy = FsyncSpy(monkeypatch)
    assert safefs.create_exclusive(tmp_path, "IDENTITY.md", b"me")
    assert ("fsync", (tmp_path / "IDENTITY.md").stat().st_ino, False) in spy.log
    assert ("fsync", tmp_path.stat().st_ino, True) in spy.log


async def test_file_write_is_durable_before_the_ledger_says_ok(rig, monkeypatch):
    p = ScriptedProvider([call("file_write", path="notes/plan.md", content="v1"), finish()])
    h = await rig.start(p)
    statuses = []

    def op_status():
        row = h.ledger.q1("SELECT status FROM operations WHERE tool = 'file.write'")
        statuses.append(row["status"] if row else None)

    FsyncSpy(monkeypatch, on_dir_sync=op_status)
    h.submit_input("write it")
    await h.wait_idle()
    assert statuses == ["dispatched"] * len(statuses) and statuses  # all synced while still in flight
    assert h.ledger.q1("SELECT status FROM operations WHERE tool = 'file.write'")["status"] == "ok"


async def test_plugin_store_copy_is_durable_before_it_is_recorded(rig, monkeypatch):
    import os

    from test_plugins import WORDCOUNT, write_plugin

    from whim import plugins

    write_plugin(rig, "plugins/wc", "wordcount", WORDCOUNT)
    h = await rig.start()
    synced, renamed = set(), []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd):
        synced.add(os.fstat(fd).st_ino)
        return real_fsync(fd)

    def rename(src, dst, **kw):
        renamed.append(dst)
        return real_rename(src, dst, **kw)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    row = await plugins.install(h, rig.cfg.workspace / "plugins/wc", "test")
    store = plugins.store_root(h) / row["hash"]
    assert renamed
    for p in [store, *store.rglob("*")]:
        assert p.stat().st_ino in synced, p  # every file and folder of the copy
    assert store.parent.stat().st_ino in synced  # and the rename that publishes it


async def test_native_snapshot_correlates_question_deliveries(rig):
    from test_agui import served

    h = await rich_history(rig)
    q = h.ledger.q1("SELECT id FROM questions")["id"]
    async with served(h) as c:
        snap = await c.get("/v1/snapshot")
    by_content = {m["content"].split("\n")[0]: m for m in snap["messages_to_human"]}
    assert by_content["Tea or coffee?"]["question_id"] == q
    assert by_content["Asked!"]["question_id"] is None
    assert snap["cursor"] == h.ledger.last_seq()
