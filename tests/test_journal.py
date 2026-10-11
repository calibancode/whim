"""Host-dated, append-only journal with crash reconciliation."""

from __future__ import annotations

import json

from conftest import Clock, crash_at, events, runs, tool_result

from whim.provider.fake import ScriptedProvider, call, calls, finish, tool_call

# 2026-10-10 19:00 UTC is 12:00 in Los Angeles (PDT).


def jwrite(text):
    return call("journal_write", entry=text)


async def run_steps(rig, *steps, clock=None, text="go"):
    p = ScriptedProvider([*steps, finish()])
    h = await rig.start(p, clock=clock or Clock())
    h.submit_input(text)
    await h.wait_idle()
    return h, p, [tool_result(r.messages[-1]) for r in p.requests[1:]]


async def test_entries_are_appended_to_the_host_dated_file(rig):
    clock = Clock()

    def later(request):
        clock.advance(95)
        return jwrite("Second thing.\nWith two lines.")

    h, p, res = await run_steps(rig, jwrite("Walked through the notes folder."), later, clock=clock)
    assert res[0] == {"ok": True, "path": "journal/2026-10-10.md", "time": "12:00"}
    assert res[1]["time"] == "13:35"
    text = (rig.cfg.workspace / "journal/2026-10-10.md").read_text()
    assert text == ("# Saturday, 10 October 2026\n\n## 12:00\n\nWalked through the notes folder.\n"
                    "\n## 13:35\n\nSecond thing.\nWith two lines.\n")


async def test_date_follows_configured_time_zone_and_rolls_over(rig):
    rig.cfg.timezone = "Pacific/Auckland"  # 2026-10-10 19:00Z is 08:00 on the 11th there
    clock = Clock()

    def after_midnight(request):
        clock.advance(hours=16)  # 00:00 on the 12th in Auckland
        return jwrite("New day.")

    h, p, res = await run_steps(rig, jwrite("Morning."), after_midnight, clock=clock)
    assert [r["path"] for r in res] == ["journal/2026-10-11.md", "journal/2026-10-12.md"]
    assert (rig.cfg.workspace / "journal/2026-10-12.md").read_text().startswith("# Monday, 12 October 2026\n")


async def test_journal_is_readable_but_not_rewritable(rig):
    h, p, res = await run_steps(
        rig,
        jwrite("hello"),
        calls(tool_call("file_read", path="journal"), tool_call("file_read", path="journal/2026-10-10.md"),
              tool_call("file_write", path="journal/2026-10-10.md", content="rewritten")),
    )
    listing, day, rewrite = (tool_result(m) for m in p.requests[2].messages[-3:])
    assert listing["entries"] == ["2026-10-10.md"]
    assert "hello" in day["content"]
    assert "append-only" in rewrite["error"]
    assert "rewritten" not in (rig.cfg.workspace / "journal/2026-10-10.md").read_text()


async def test_journal_is_separate_from_curated_files(rig):
    before = {n: (rig.cfg.workspace / n).read_text() for n in ("MEMORY.md", "USER.md", "IDENTITY.md", "SOUL.md")}
    h, p, res = await run_steps(rig, jwrite("Something happened."), call("file_read", path="SOUL.md"))
    assert {n: (rig.cfg.workspace / n).read_text() for n in before} == before
    assert not any("changed · current content" in (m.get("content") or "") for r in p.requests for m in r.messages)


async def test_empty_entry_is_refused(rig):
    h, p, res = await run_steps(rig, jwrite("   "))
    assert "empty" in res[0]["error"]
    assert not (rig.cfg.workspace / "journal").exists()


async def crash_and_recover(rig, point, *, tamper=None):
    clock = Clock()
    h = await rig.start(ScriptedProvider([jwrite("A quiet afternoon.")]), clock=clock, fault_hook=crash_at(point))
    h.submit_input("go")
    await rig.crashed()
    if tamper:
        tamper()
    p2 = ScriptedProvider([finish()])
    h = await rig.start(p2, clock=clock)
    await h.wait_idle()
    [op] = [dict(o) for o in h.ledger.q("SELECT * FROM operations WHERE tool = 'journal.write'")]
    path = rig.cfg.workspace / "journal/2026-10-10.md"
    return h, p2, op, (path.read_text() if path.exists() else None)


async def test_crash_after_append_is_confirmed_not_repeated(rig):
    h, p2, op, text = await crash_and_recover(rig, "journal.after_append")
    assert op["status"] == "ok" and json.loads(op["result"])["note"] == "Entry confirmed during recovery."
    assert text.count("A quiet afternoon.") == 1
    assert "may or may not" not in p2.requests[0].messages[-1]["content"]


async def test_crash_before_append_is_known_not_written(rig):
    h, p2, op, text = await crash_and_recover(rig, "journal.before_append")
    assert op["status"] == "not_dispatched" and text is None
    assert "safe" in json.loads(op["result"])["error"]


async def test_crash_before_plan_is_known_not_written(rig):
    h, p2, op, text = await crash_and_recover(rig, "tool.after_dispatch")
    assert op["status"] == "not_dispatched" and text is None


async def test_partial_append_is_uncertain(rig):
    def torn():
        path = rig.cfg.workspace / "journal/2026-10-10.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Saturday, 10 October 2026\n\n## 12:")  # a torn write

    h, p2, op, text = await crash_and_recover(rig, "journal.before_append", tamper=torn)
    assert op["status"] == "uncertain"
    assert "journal.write" in p2.requests[0].messages[-1]["content"]  # named in the recovery notice
    assert [r["status"] for r in runs(h)] == ["interrupted", "finished"]
    assert events(h, "recovery.pending")[0]["data"]["unresolved"][0]["tool"] == "journal.write"
