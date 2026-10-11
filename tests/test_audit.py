"""Regressions for the 2026-10 external audit findings (commit 8c9d744).

Every secret here is a disposable file under pytest's temp directory."""

from __future__ import annotations

import json
import os
import shutil

import pytest
from conftest import Clock, events, human_messages, runs, tool_result, types

from whim import compaction, plugins
from whim.provider.base import ProviderError
from whim.provider.fake import ScriptedProvider, call, calls, finish, tool_call

SECRET = "TOPSECRET-c0ffee"
needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")


@pytest.fixture
def secret(tmp_path):
    p = tmp_path / "outside" / "private.txt"
    p.parent.mkdir()
    p.write_text(SECRET)
    return p


def sent_to_model(provider) -> str:
    return json.dumps([r.messages for r in provider.requests])


def assert_paired(messages):
    pending = []
    for m in messages:
        if m["role"] == "tool":
            assert pending and m["tool_call_id"] == pending.pop(0), "orphan tool result"
            continue
        assert not pending, f"unanswered tool calls: {pending}"
        if m["role"] == "assistant":
            pending = [tc["id"] for tc in m.get("tool_calls") or []]
    assert not pending, f"unanswered tool calls: {pending}"


# -- 1. plugin/host filesystem boundary ---------------------------------------------------

LINKER = '''
import os
from whim_plugin import Plugin
plugin = Plugin("linker")

@plugin.tool
def link(target: str, name: str) -> dict:
    "Replace a workspace file with a symlink."
    path = "/workspace/" + name
    if os.path.lexists(path):
        os.remove(path)
    os.symlink(target, path)
    return {"linked": name}

plugin.run()
'''


@needs_bwrap
async def test_plugin_symlinked_memory_is_not_followed_by_the_host(rig, secret):
    from test_plugins import activated

    p = ScriptedProvider([call("linker_link", target=str(secret), name="MEMORY.md"),
                          call("file_read", path="MEMORY.md"), finish(), finish()])
    h = await rig.start(p)
    await activated(rig, "plugins/ln", "linker", LINKER, permissions=["workspace.write"])
    h.submit_input("go")
    await h.wait_idle()
    h.submit_input("again")
    await h.wait_idle()
    assert (rig.cfg.workspace / "MEMORY.md").is_symlink()  # the plugin really did plant it
    assert SECRET not in sent_to_model(p)


async def test_planted_temp_symlink_cannot_redirect_file_write(rig, secret):
    os.symlink(secret, rig.cfg.workspace / "note.txt.whim-tmp")
    p = ScriptedProvider([call("file_write", path="note.txt", content="hello"), finish()])
    h = await rig.start(p)
    h.submit_input("write")
    await h.wait_idle()
    assert secret.read_text() == SECRET
    assert (rig.cfg.workspace / "note.txt").read_text() == "hello"


@pytest.mark.parametrize("planted", ["file", "dir"])
async def test_workspace_symlinks_are_refused_for_read_and_write(rig, secret, planted):
    ws = rig.cfg.workspace
    if planted == "file":
        os.symlink(secret, ws / "link.txt")
        read, write = "link.txt", "link.txt"
    else:
        os.symlink(secret.parent, ws / "linkdir")
        read, write = "linkdir/private.txt", "linkdir/new.txt"
    p = ScriptedProvider([calls(tool_call("file_read", path=read), tool_call("file_write", path=write, content="x")),
                          finish()])
    h = await rig.start(p)
    h.submit_input("go")
    await h.wait_idle()
    r, w = (tool_result(m) for m in p.requests[1].messages[-2:])
    assert not r["ok"]
    assert SECRET not in sent_to_model(p) and secret.read_text() == SECRET
    assert not (secret.parent / "new.txt").exists()
    if planted == "file":  # writing replaces the link itself, inside the workspace
        assert w["ok"] and not (ws / "link.txt").is_symlink() and (ws / "link.txt").read_text() == "x"
    else:
        assert not w["ok"]


async def test_journal_folder_symlink_cannot_redirect_appends(rig, secret):
    os.symlink(secret.parent, rig.cfg.workspace / "journal")
    p = ScriptedProvider([call("journal_write", entry="hello"), call("history_search", query="topsecret"), finish()])
    h = await rig.start(p, clock=Clock())
    h.submit_input("go")
    await h.wait_idle()
    assert list(secret.parent.iterdir()) == [secret]
    assert SECRET not in sent_to_model(p)


async def test_character_file_symlinks_never_reach_system_prompt_or_compactor(rig, secret):
    (rig.cfg.workspace / "USER.md").unlink()
    os.symlink(secret, rig.cfg.workspace / "USER.md")
    (rig.cfg.workspace / "BOOTSTRAP.md").unlink()
    os.symlink(secret, rig.cfg.workspace / "BOOTSTRAP.md")
    compactor = ScriptedProvider([])
    p = ScriptedProvider([finish()])
    h = await rig.start(p, compactor=compactor)
    h.submit_input("hi")
    await h.wait_idle()
    assert SECRET not in sent_to_model(p)
    assert SECRET not in compaction._files_block(h)


def test_starter_files_are_never_written_through_a_dangling_symlink(tmp_path):
    from whim import onboard

    home = tmp_path / "home"
    onboard.init(home)
    target = tmp_path / "outside" / "created.txt"
    target.parent.mkdir()
    (home / "workspace" / "SOUL.md").unlink()
    os.symlink(target, home / "workspace" / "SOUL.md")  # dangling: target doesn't exist
    onboard.init(home)
    assert not target.exists()


async def test_plugin_install_refuses_symlinked_content(rig, secret):
    from test_plugins import WORDCOUNT, write_plugin

    h = await rig.start()
    d = write_plugin(rig, "plugins/wc", "wordcount", WORDCOUNT)
    os.symlink(secret.parent, d / "lib")
    with pytest.raises(plugins.PluginRejected, match="symbolic link"):
        await plugins.install(h, d, source="test")


# -- 2. external JSON Schema references --------------------------------------------------

def schema_cases(secret):
    file_uri = secret.with_suffix(".json").as_uri()
    secret.with_suffix(".json").write_text(json.dumps({"const": SECRET}))
    return [
        {"type": "object", "properties": {"x": {"$ref": file_uri}}},
        {"type": "object", "properties": {"x": {"allOf": [{"anyOf": [{"$ref": file_uri}]}]}}},
        {"type": "object", "$id": secret.parent.as_uri() + "/", "properties": {"x": {"$ref": "private.json"}}},
        {"type": "object", "properties": {"x": {"$dynamicRef": file_uri}}},
        {"type": "object", "properties": {"x": {"$ref": "http://127.0.0.1:9/schema.json"}}},
    ]


def test_external_refs_never_resolve_during_argument_validation(secret):
    from whim.tools.registry import ArgumentError, Tool

    for schema in schema_cases(secret):
        tool = Tool(name="p.t", description="d", params=None, json_schema=schema, effect="plugin", impl="plugin:p@x")
        with pytest.raises(ArgumentError) as e:
            tool.parse(json.dumps({"x": "nope"}))
        assert SECRET not in str(e.value)


def test_plugins_with_external_refs_are_rejected_at_validation(secret):
    for schema in schema_cases(secret):
        with pytest.raises(plugins.PluginRejected, match="external"):
            plugins._check_tools([{"name": "t", "description": "d", "inputSchema": schema}], "p")


def test_local_refs_still_work():
    from whim.tools.registry import ArgumentError, Tool

    schema = {"type": "object", "$defs": {"n": {"type": "integer"}}, "properties": {"x": {"$ref": "#/$defs/n"}},
              "required": ["x"]}
    plugins._check_tools([{"name": "t", "description": "d", "inputSchema": schema}], "p")
    tool = Tool(name="p.t", description="d", params=None, json_schema=schema, effect="plugin", impl="plugin:p@x")
    assert tool.parse('{"x": 3}') == {"x": 3}
    with pytest.raises(ArgumentError):
        tool.parse('{"x": "three"}')


# -- 3. rolled-back events never reach the live stream -----------------------------------

async def test_rolled_back_savepoint_events_are_not_published(rig):
    h = await rig.start()
    q = h.ledger.subscribe()
    with h.ledger.tx():
        try:
            with h.ledger.savepoint():
                h.ledger.append("host.warning", {"kind": "phantom"})
                raise ValueError("rejected")
        except ValueError:
            pass
        h.ledger.append("host.warning", {"kind": "real"})
    live = []
    while not q.empty():
        live.append(q.get_nowait())
    replay = h.ledger.events_after(live[0]["seq"] - 1, 100) if live else []
    assert [e["data"]["kind"] for e in live] == ["real"]
    assert live == replay


async def test_rejected_cron_batch_publishes_nothing_it_rolled_back(rig):
    clock = Clock()
    p = ScriptedProvider([call("cron_manage", operations=[{"op": "add", "instruction": "x", "in_minutes": 5}]),
                          finish(),
                          # pause cancels the job's queued wake (an event), then the batch is rejected
                          call("cron_manage", operations=[{"op": "pause", "id": "cr_1"},
                                                          {"op": "remove", "id": "cr_404"}]),
                          call("file_read", path="SOUL.md"), finish(),
                          finish()])  # the job's wake, still pending
    h = await rig.start(p, clock=clock)
    h.submit_input("schedule")
    await h.wait_idle()
    clock.advance(6)
    q = h.ledger.subscribe()
    h.submit_input("pause it")  # admitted first; the job wake stays queued during this run
    h.scheduler.tick()
    await h.wait_idle()
    assert not tool_result(p.requests[3].messages[-1])["ok"]  # the batch was rejected
    live = []
    while not q.empty():
        live.append(q.get_nowait())
    seqs = [e["seq"] for e in live]
    assert len(seqs) == len(set(seqs)), "a sequence number was published twice"
    committed = h.ledger.events_after(seqs[0] - 1, 100000)
    assert live == committed  # live stream == replay, event for event
    assert "wake.cancelled" not in [e["type"] for e in live]
    assert [json.loads(r["origin"])["kind"] for r in runs(h)][-1] == "cron"  # the rolled-back pause didn't stick


# -- 4. every committed tool call gets an outcome -----------------------------------------

async def test_cron_date_overflow_is_a_tool_error_not_a_broken_transcript(rig):
    p = ScriptedProvider([call("cron_manage", operations=[{"op": "add", "instruction": "x",
                                                           "at": "9999-12-31T23:59:59-14:00"}]),
                          finish(), finish()])
    h = await rig.start(p)
    h.submit_input("far future")
    await h.wait_idle()
    h.submit_input("next")
    await h.wait_idle()
    assert [r["status"] for r in runs(h)] == ["finished", "finished"]
    assert not tool_result(p.requests[1].messages[-1])["ok"]
    assert_paired(p.requests[-1].messages)


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


# -- 5. forced compaction failure is bounded ------------------------------------------------

async def test_failing_forced_compaction_backs_off_and_lets_humans_through(rig):
    from test_compaction import LONG, Character

    def down(request):
        raise ProviderError("503")

    clock = Clock()
    compactor = ScriptedProvider([down] * 50)
    h = await rig.start(Character(), clock=clock, compactor=compactor)
    base = compaction.estimate(h)
    c = rig.cfg.compaction
    c.trigger_tokens, c.force_tokens, c.tail_tokens, c.min_growth_tokens = base + 500, base + 600, 300, 100
    c.retry_minutes = 10
    for i in range(3):
        h.submit_input(f"message {i}: {LONG}")
        await h.wait_idle(timeout=5)
    for _ in range(3):
        clock.advance(11)
        h.submit_input("tick")
        await h.wait_idle(timeout=5)
    started = types(h).count("compaction.started")
    assert started == 1 and len(compactor.requests) == c.max_summary_attempts
    # Human input keeps flowing while compaction is suppressed (no context_limit pauses).
    h.submit_input("still there?")
    await h.wait_idle(timeout=5)
    assert runs(h)[-1]["status"] == "finished"
    # Suppression survives a restart.
    h = await rig.restart(Character(), clock=clock, compactor=compactor)
    h.submit_input("after restart")
    await h.wait_idle(timeout=5)
    assert types(h).count("compaction.started") == 1
    st = compaction.status(h)
    assert st["suppressed_until"] and st["last"]["status"] == "failed"
    # Later, one new attempt is allowed.
    clock.advance(24 * 60)
    h.submit_input("much later")
    await h.wait_idle(timeout=5)
    assert types(h).count("compaction.started") == 2


# -- 6. the tool-call budget is enforced per call -------------------------------------------

async def test_tool_budget_is_enforced_before_each_dispatch(rig):
    rig.cfg.limits.max_tool_calls_per_run = 1
    p = ScriptedProvider([calls(*[tool_call("message_send", destination="human", content=f"m{i}") for i in range(4)])])
    h = await rig.start(p)
    h.submit_input("go")
    await h.wait_idle()
    assert human_messages(h) == ["m0"]
    statuses = [r["status"] for r in h.ledger.q("SELECT status FROM operations ORDER BY rowid")]
    assert statuses == ["ok", "not_dispatched", "not_dispatched", "not_dispatched"]
    [run] = runs(h)
    assert (run["status"], run["reason"]) == ("paused", "tool_limit")
    assert_paired(h.context.messages())
    assert events is not None


async def test_tool_budget_spans_batches(rig):
    rig.cfg.limits.max_tool_calls_per_run = 2
    p = ScriptedProvider([call("file_read", path="SOUL.md"),
                          calls(*[tool_call("file_write", path=f"f{i}.md", content="x") for i in range(3)])])
    h = await rig.start(p)
    h.submit_input("go")
    await h.wait_idle()
    assert [p_.name for p_ in sorted(rig.cfg.workspace.glob("f*.md"))] == ["f0.md"]
    assert runs(h)[0]["reason"] == "tool_limit"
    assert_paired(h.context.messages())


# -- corrective pass: backoff arithmetic -----------------------------------------------------

def _seed_failures(h, n):
    h.ledger.set_meta("compaction_backoff", {"failures": n, "until": "2000-01-01T00:00:00.000Z", "last_error": "x"})


@pytest.mark.parametrize("prior", [35, 10**12])
async def test_backoff_saturates_after_many_failures_maintenance_path(rig, prior):
    from test_compaction import LONG, Character

    def down(request):
        raise ProviderError("503")

    clock = Clock()
    rig.cfg.compaction.max_maintenance_attempts = 1
    h = await rig.start(Character(maintenance=[down]), clock=clock, compactor=ScriptedProvider([]))
    _seed_failures(h, prior)
    base = compaction.estimate(h)
    c = rig.cfg.compaction
    c.trigger_tokens, c.force_tokens, c.tail_tokens, c.min_growth_tokens = base + 500, base + 9000, 300, 100
    for i in range(3):
        h.submit_input(f"message {i}: {LONG}")
        await h.wait_idle(timeout=5)
    assert [r["status"] for r in h.ledger.q("SELECT status FROM compactions")] == ["failed"]
    assert not h.ledger.q("SELECT 1 FROM runs WHERE status = 'active'")  # nothing stranded
    b = h.ledger.get_meta("compaction_backoff")
    assert b["failures"] == prior + 1
    from whim.timebase import parse
    assert parse(b["until"]) == clock.now + compaction.MAX_BACKOFF
    h = await rig.restart(Character(), clock=clock, compactor=ScriptedProvider([]))  # startup recovery works
    h.submit_input("still here?")
    await h.wait_idle(timeout=5)
    assert runs(h)[-1]["status"] == "finished"


async def test_backoff_saturates_after_many_failures_summary_path(rig):
    from test_compaction import LONG, Character

    def down(request):
        raise ProviderError("503")

    clock = Clock()
    rig.cfg.compaction.max_summary_attempts = 1
    h = await rig.start(Character(), clock=clock, compactor=ScriptedProvider([down]))
    _seed_failures(h, 10**9)
    base = compaction.estimate(h)
    c = rig.cfg.compaction
    c.trigger_tokens, c.force_tokens, c.tail_tokens, c.min_growth_tokens = base + 500, base + 9000, 300, 100
    for i in range(3):
        h.submit_input(f"message {i}: {LONG}")
        await h.wait_idle(timeout=5)
    assert [r["status"] for r in h.ledger.q("SELECT status FROM compactions")] == ["failed"]
    assert compaction.status(h)["suppressed_until"] is not None


def test_backoff_delay_policy():
    assert compaction.backoff_delay(1, 10).total_seconds() == 3600
    assert compaction.backoff_delay(2, 10).total_seconds() == 7200
    assert compaction.backoff_delay(6, 10) == compaction.MAX_BACKOFF
    for n in (35, 36, 10**6, 10**18):
        assert compaction.backoff_delay(n, 10) == compaction.MAX_BACKOFF


# -- corrective pass: bounded behaviour when compaction is unavailable --------------------

SUMMARY_TEXT = ("## Recent life\n- lots of garden notes.\n## What matters to me\n- the garden.\n"
                "## Things underway\n- notes.\n## Particulars\n- none.\n## Loose ends\n- none.\n") * 3


def _small_window(rig, h, *, room=4000, reserve=1000):
    base = compaction.estimate(h)
    c = rig.cfg.compaction
    c.context_window_tokens = base + room
    c.reserve_tokens = reserve
    c.trigger_tokens, c.force_tokens = base + room // 3, base + room // 2
    c.tail_tokens, c.min_growth_tokens = 300, 100
    return c


def _within_window(provider, window):
    for r in provider.requests:
        assert len(json.dumps(r.messages) + json.dumps(r.tools)) / compaction.CHARS_PER_TOKEN < window


async def test_full_context_with_compaction_unavailable_holds_input_safely(rig):
    from test_compaction import LONG, Character, summary

    def down(request):
        raise ProviderError("503")

    clock = Clock()
    rig.cfg.compaction.max_summary_attempts = 1
    character = Character()
    h = await rig.start(character, clock=clock, compactor=ScriptedProvider([down]))
    c = _small_window(rig, h)
    for i in range(12):  # well past the ceiling, with compaction failing and then suppressed
        h.submit_input(f"message {i}: {LONG}")
        await h.wait_idle(timeout=5)
    _within_window(character, c.context_window_tokens)
    st = compaction.status(h)
    assert st["blocked"] and st["suppressed_until"]
    asked = len(character.requests)
    h.submit_input("are you there?")
    await h.wait_idle(timeout=5)
    assert len(character.requests) == asked  # nothing more was sent
    assert h.ledger.q1("SELECT COUNT(*) c FROM inputs WHERE status = 'pending'")["c"] >= 1  # durably kept
    assert [e["data"]["kind"] for e in events(h, "host.warning")].count("context_full") == 1
    from whim import doctor
    assert any(f.check == "context" and f.level == "error" and "waiting" in f.message
               for f in doctor.Doctor(rig.cfg.home).run())

    # A restart while blocked starts cleanly and still sends nothing.
    character2 = Character()
    h = await rig.restart(character2, clock=clock, compactor=ScriptedProvider([summary(SUMMARY_TEXT)] * 10))
    await h.wait_idle(timeout=5)
    assert character2.requests == [] and compaction.status(h)["blocked"]

    # Once the backoff expires, compaction runs and the waiting messages are taken up.
    clock.advance(24 * 60 + 1)
    h.scheduler.tick()
    for _ in range(5):
        await h.wait_idle(timeout=10)
        h.scheduler.tick()
    _within_window(character2, c.context_window_tokens)
    assert compaction.status(h)["blocked"] is None
    assert h.ledger.q1("SELECT COUNT(*) c FROM inputs WHERE status = 'pending'")["c"] == 0
    assert types(h).count("context.compacted") >= 1
    assert runs(h)[-1]["status"] == "finished"  # the character got to respond afterwards
    resumed = h.ledger.q("SELECT status FROM wakes WHERE json_extract(data, '$.reason') = 'resume_after_compaction'")
    assert resumed and all(r["status"] == "admitted" for r in resumed)


async def test_ordinary_work_continues_during_backoff_below_the_ceiling(rig):
    from test_compaction import LONG, Character

    def down(request):
        raise ProviderError("503")

    rig.cfg.compaction.max_summary_attempts = 1
    h = await rig.start(Character(), clock=Clock(), compactor=ScriptedProvider([down]))
    c = _small_window(rig, h, room=9000, reserve=1000)
    for i in range(7):
        h.submit_input(f"message {i}: {LONG}")
        await h.wait_idle(timeout=5)
    st = compaction.status(h)
    assert st["suppressed_until"] and not st["blocked"]
    assert c.force_tokens < compaction.estimate(h) < compaction.hard_limit(h)
    assert runs(h)[-1]["status"] == "finished"
    assert "context_limit" not in [r["reason"] for r in runs(h)[2:]]  # no pauses once suppressed


async def test_maintenance_without_room_is_skipped_and_the_compactor_still_runs(rig):
    from test_compaction import Character, summary

    character = Character(maintenance=[call("journal_write", entry="never reached")])
    h = await rig.start(character, clock=Clock(), compactor=ScriptedProvider([summary(SUMMARY_TEXT)]))
    c = _small_window(rig, h, room=4000, reserve=1000)
    h.submit_input("x " * 12000)  # one message that leaves no room even for maintenance
    for _ in range(3):
        await h.wait_idle(timeout=5)
    [ck] = events(h, "memory.checkpointed")
    assert ck["data"]["skipped"] == "no room left for maintenance"
    assert types(h).count("context.compacted") == 1
    assert not (rig.cfg.workspace / "journal").exists()
    _within_window(character, c.context_window_tokens)


async def test_compactor_prompt_stays_within_its_window(rig):
    from test_compaction import LONG, Character, summary

    compactor = ScriptedProvider([summary(SUMMARY_TEXT)])
    h = await rig.start(Character(), clock=Clock(), compactor=compactor)
    rig.cfg.compaction.enabled = False
    for i in range(20):  # build a long history first
        h.submit_input(f"message {i}: {LONG}")
        await h.wait_idle(timeout=5)
    c = rig.cfg.compaction
    c.enabled = True
    est = compaction.estimate(h)
    c.context_window_tokens = est + 2000  # the compactor's window is now tight
    c.summary_max_tokens = 1500
    c.reserve_tokens = 1000
    c.trigger_tokens = c.force_tokens = est - 1
    c.tail_tokens, c.min_growth_tokens = 300, 100
    h.submit_input("one more")
    await h.wait_idle(timeout=10)
    req = compactor.requests[-1]
    limit_chars = (c.context_window_tokens - c.summary_max_tokens) * compaction.CHARS_PER_TOKEN
    assert len(req.messages[0]["content"]) + len(req.messages[1]["content"]) < limit_chars
    assert "earlier entries are left out of this transcript because of its size" in req.messages[1]["content"]
