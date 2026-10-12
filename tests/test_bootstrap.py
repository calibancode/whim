"""First-meeting bootstrap lifecycle."""

from __future__ import annotations

import getpass
import json

from conftest import crash_at, events, runs, tool_names, tool_result, types

from whim import bootstrap, onboard
from whim.provider.fake import ScriptedProvider, call, calls, finish, tool_call

BOOT_HEADER = "⟦host · first meeting · from BOOTSTRAP.md"


def boot_count(messages) -> int:
    return sum((m.get("content") or "").count(BOOT_HEADER) for m in messages if m["role"] == "user")


def status(h) -> str:
    return bootstrap.state(h)["status"]


async def test_init_seeds_files_and_pending_state(rig):
    ws = rig.cfg.workspace
    assert sorted(p.name for p in ws.iterdir()) == ["BOOTSTRAP.md", "IDENTITY.md", "MEMORY.md", "SOUL.md", "USER.md"]
    h = await rig.start()
    assert status(h) == "pending"
    # Re-running init never resets progress or recreates a retired note.
    onboard.init(rig.cfg.home)
    assert status(h) == "pending"


async def test_first_direct_message_gets_the_note_once_before_the_human(rig):
    p = ScriptedProvider([finish(), finish()])
    h = await rig.start(p)
    h.submit_input("hello?")
    await h.wait_idle()
    opening = p.requests[0].messages[-1]["content"]
    assert opening.startswith(BOOT_HEADER)
    assert opening.index(BOOT_HEADER) < opening.index("⟦message from human")
    assert "call bootstrap_complete" in opening
    assert "bootstrap_complete" in tool_names(p.requests[0])
    assert status(h) == "active" and types(h).count("bootstrap.started") == 1
    # Not in the system prompt, and no account metadata anywhere in what the model sees.
    sent = json.dumps(p.requests[0].messages)
    assert "First meeting" not in p.requests[0].messages[0]["content"]
    assert rig.cfg.principal_id not in sent and getpass.getuser() not in sent

    h.submit_input("still there?")
    await h.wait_idle()
    assert boot_count(p.requests[1].messages) == 1  # only the original copy, in history
    assert BOOT_HEADER not in p.requests[1].messages[-1]["content"]
    assert "bootstrap_complete" in tool_names(p.requests[1])


async def test_host_wake_before_meeting_does_not_start_it(rig):
    p = ScriptedProvider([call("bootstrap_complete"), finish()])
    h = await rig.start(p)
    h.request_wake()
    await h.wait_idle()
    assert BOOT_HEADER not in json.dumps(p.requests[0].messages)
    assert "bootstrap_complete" not in tool_names(p.requests[0])
    # Called anyway by name: refused, nothing changes.
    assert "isn't available right now" in tool_result(p.requests[1].messages[-1])["error"]
    assert status(h) == "pending"


async def test_human_arriving_mid_host_wake_starts_meeting_at_boundary(rig):
    def human_arrives(request):
        rig.h.submit_input("hi, I'm here")
        return call("file_read", path="SOUL.md")

    p = ScriptedProvider([human_arrives, finish()])
    h = await rig.start(p)
    h.request_wake()
    await h.wait_idle()
    boundary = p.requests[1].messages[-1]["content"]
    assert boundary.startswith(BOOT_HEADER) and "hi, I'm here" in boundary
    assert "bootstrap_complete" in tool_names(p.requests[1])
    assert status(h) == "active"


async def test_editing_character_files_does_not_complete(rig):
    p = ScriptedProvider([
        calls(tool_call("file_write", path="IDENTITY.md", content="# Identity\n\nI'm Moss."),
              tool_call("file_write", path="USER.md", content="# User\n\nLikes teal."),
              tool_call("file_write", path="SOUL.md", content="# Soul\n\nCurious."),
              tool_call("file_write", path="MEMORY.md", content="# Memory\n\nWe met."),
              tool_call("file_write", path="BOOTSTRAP.md", content="done")),
        finish(),
    ])
    h = await rig.start(p)
    h.submit_input("hello")
    await h.wait_idle()
    assert status(h) == "active"
    assert "managed by the host" in tool_result(p.requests[1].messages[-1])["error"]
    assert (rig.cfg.workspace / "BOOTSTRAP.md").read_text().startswith("# First meeting")


async def test_completion_is_durable_retires_note_and_tool(rig):
    p = ScriptedProvider([call("bootstrap_complete"), call("message_send", destination="human", content="hi"),
                          finish(), finish()])
    h = await rig.start(p)
    h.submit_input("hello")
    await h.wait_idle()
    assert tool_result(p.requests[1].messages[-1]) == {"ok": True, "status": "complete",
                                                       "note": "BOOTSTRAP.md has been retired."}
    assert status(h) == "complete" and "bootstrap.completed" in types(h)
    assert not (rig.cfg.workspace / "BOOTSTRAP.md").exists()
    # Withdrawn at the next request boundary, within the same run.
    assert "bootstrap_complete" not in tool_names(p.requests[1])
    [run] = runs(h)
    assert run["status"] == "finished"  # completing didn't end the turn

    # Never again: not after restart, not after re-init, not on later direct messages.
    onboard.init(rig.cfg.home)
    assert not (rig.cfg.workspace / "BOOTSTRAP.md").exists()
    p2 = ScriptedProvider([finish()])
    h = await rig.restart(p2)
    h.submit_input("hello again")
    await h.wait_idle()
    assert boot_count(p2.requests[0].messages) == 1  # the historical copy only
    assert BOOT_HEADER not in p2.requests[0].messages[-1]["content"]
    assert "bootstrap_complete" not in tool_names(p2.requests[0])


async def test_second_completion_is_refused(rig):
    p = ScriptedProvider([calls(tool_call("bootstrap_complete"), tool_call("bootstrap_complete")), finish()])
    h = await rig.start(p)
    h.submit_input("hello")
    await h.wait_idle()
    first, second = p.requests[1].messages[-2:]
    assert tool_result(first)["ok"] and "already complete" in tool_result(second)["error"]
    assert types(h).count("bootstrap.completed") == 1


async def test_crash_mid_meeting_resumes_without_repeating_note(rig):
    h = await rig.start(ScriptedProvider([call("file_write", path="IDENTITY.md", content="# Identity\n\nMoss?")]),
                        fault_hook=crash_at("tool.after_dispatch"))
    h.submit_input("hello")
    await rig.crashed()

    p2 = ScriptedProvider([finish(), finish()])
    h = await rig.start(p2)
    await h.wait_idle()  # recovery wake
    h.submit_input("you there?")
    await h.wait_idle()
    assert status(h) == "active"
    assert types(h).count("bootstrap.started") == 1
    for req in p2.requests:
        assert boot_count(req.messages) == 1
        assert "bootstrap_complete" in tool_names(req)


async def test_crash_between_completion_and_file_removal(rig):
    h = await rig.start(ScriptedProvider([call("bootstrap_complete")]), fault_hook=crash_at("tool.after_atomic_commit"))
    h.submit_input("hello")
    await rig.crashed()
    assert (rig.cfg.workspace / "BOOTSTRAP.md").exists()  # committed, not yet removed

    p2 = ScriptedProvider([finish()])
    h = await rig.start(p2)
    await h.wait_idle()
    assert status(h) == "complete"
    assert not (rig.cfg.workspace / "BOOTSTRAP.md").exists()
    # Atomic: the call has a confirmed result, so recovery doesn't call it uncertain.
    [op] = [dict(o) for o in h.ledger.q("SELECT * FROM operations WHERE tool = 'bootstrap.complete'")]
    assert op["status"] == "ok"
    assert "bootstrap.complete" not in p2.requests[0].messages[-1]["content"]


async def test_note_removed_by_operator_before_meeting_is_skipped(rig):
    (rig.cfg.workspace / "BOOTSTRAP.md").unlink()
    p = ScriptedProvider([finish()])
    h = await rig.start(p)
    h.submit_input("hello")
    await h.wait_idle()
    assert status(h) == "complete"
    assert BOOT_HEADER not in json.dumps(p.requests[0].messages)
    assert [e["data"]["kind"] for e in events(h, "host.warning")] == ["bootstrap_file_missing"]
