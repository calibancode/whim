"""Regressions for the third audit pass (corrective)."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import events, runs

from whim import compaction
from whim.provider.fake import ScriptedProvider, call, finish


def kinds(h):
    return [json.loads(r["origin"])["kind"] for r in runs(h)]


# -- 1. maintenance stop race ---------------------------------------------------------


async def test_stop_latched_during_maintenance_beats_its_turn_finish(rig):
    from test_compaction import Character, comp, setup, talk

    gate, entered = asyncio.Event(), asyncio.Event()

    async def finishing_maintenance(request):
        entered.set()
        await gate.wait()
        return finish()  # the in-flight response is a standalone turn_finish

    h, _ = await setup(rig, character=Character(maintenance=[finishing_maintenance]))
    task = asyncio.create_task(talk(h, 5))
    await asyncio.wait_for(entered.wait(), 5)
    h.control_stop(h.cfg.principal_id)
    gate.set()
    await task
    await h.wait_idle()
    maint = next(r for r in runs(h) if json.loads(r["origin"])["kind"] == "maintenance")
    assert (maint["status"], maint["reason"]) == ("interrupted", "operator_stop")
    assert comp(h)["status"] == "cancelled" and not events(h, "context.compacted", "memory.checkpointed")
    op = h.ledger.q1("SELECT status FROM operations WHERE run_id = ? AND tool = 'turn.finish'", maint["id"])
    assert op["status"] == "not_dispatched"
    assert compaction.status(h)["suppressed_until"] is not None


async def test_ordinary_run_still_finishes_when_stopped_mid_request(rig):
    gate, entered = asyncio.Event(), asyncio.Event()

    async def finishing(request):
        entered.set()
        await gate.wait()
        return finish()

    h = await rig.start(ScriptedProvider([finishing]))
    h.request_wake()
    await asyncio.wait_for(entered.wait(), 5)
    h.control_stop(h.cfg.principal_id)
    gate.set()
    await h.wait_idle()
    assert (runs(h)[0]["status"], runs(h)[0]["reason"]) == ("finished", "turn_finish")


# -- 4. question/delivery crash consistency -------------------------------------------


async def test_question_delivery_association_survives_a_crash(rig):
    from conftest import crash_at
    from test_agui import served

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


async def test_incomplete_associations_from_earlier_versions_are_repaired(rig):
    h = await rig.start(ScriptedProvider([call("human_ask", question="Tea?"), finish()]))
    h.request_wake()
    await h.wait_idle()
    with h.ledger.tx():
        h.ledger.x("UPDATE questions SET delivery_id = NULL")  # as a crash in an older version left it
    h = await rig.restart(ScriptedProvider())
    q = h.ledger.q1("SELECT delivery_id FROM questions")
    assert q["delivery_id"] == h.ledger.q1("SELECT id FROM deliveries")["id"]


# -- 3/6. plugin store staging and durability -----------------------------------------


@pytest.fixture
async def plugin_src(rig):
    from test_plugins import WORDCOUNT, write_plugin

    write_plugin(rig, "plugins/wc", "wordcount", WORDCOUNT)
    h = await rig.start()
    return h, rig.cfg.workspace / "plugins/wc"


def staging_of(h, src):
    from whim import plugins

    return [p for p in plugins.store_root(h).iterdir() if p.name.endswith(".tmp")]


async def test_failed_install_cleans_up_and_a_retry_succeeds(plugin_src, monkeypatch):
    from whim import plugins

    h, src = plugin_src
    real, calls = plugins._fsync_dir, []

    def failing(path):
        calls.append(path)
        if len(calls) == 1:  # syncing the staging tree, already made read-only
            raise OSError(5, "Input/output error")
        return real(path)

    monkeypatch.setattr(plugins, "_fsync_dir", failing)
    with pytest.raises(OSError):
        await plugins.install(h, src, "test")
    monkeypatch.setattr(plugins, "_fsync_dir", real)
    row = await plugins.install(h, src, "test")
    assert (plugins.store_root(h) / row["hash"] / "plugin.json").exists()
    assert not staging_of(h, src)


async def test_staging_left_read_only_by_a_crash_is_removed_safely(plugin_src, tmp_path):
    import os

    from whim import plugins

    h, src = plugin_src
    row = await plugins.install(h, src, "test")
    store = plugins.store_root(h)
    final = store / row["hash"]
    os.chmod(final, 0o755)
    for p in final.rglob("*"):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    final.rename(store / (row["hash"] + ".tmp"))  # as a crash mid-install leaves it
    (store / (row["hash"] + ".tmp") / "sub").mkdir()
    for p in [*(store / (row["hash"] + ".tmp")).rglob("*"), store / (row["hash"] + ".tmp")]:
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    with h.ledger.tx():
        h.ledger.x("UPDATE plugins SET status = 'failed'")
    row = await plugins.install(h, src, "test")
    assert (store / row["hash"] / "plugin.json").exists() and not staging_of(h, src)

    # A link planted where the staging folder goes is removed itself, never followed.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    os.chmod(outside, 0o555)
    import shutil

    os.chmod(final, 0o755)
    for p in final.rglob("*"):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    shutil.rmtree(final)
    (store / (row["hash"] + ".tmp")).symlink_to(outside)
    with h.ledger.tx():
        h.ledger.x("UPDATE plugins SET status = 'failed'")
    await plugins.install(h, src, "test")
    assert (outside / "keep.txt").read_text() == "keep" and os.stat(outside).st_mode & 0o777 == 0o555
    os.chmod(outside, 0o755)


async def test_new_store_folders_are_durable_in_their_parents(plugin_src, monkeypatch):
    import os

    from whim import plugins

    h, src = plugin_src
    home = h.cfg.home
    assert not (home / "plugins").exists()
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_ino), real(fd))[1])
    await plugins.install(h, src, "test")
    assert home.stat().st_ino in synced  # holds the new plugins/ entry
    assert (home / "plugins").stat().st_ino in synced  # holds store/
    assert plugins.store_root(h).stat().st_ino in synced  # holds the installed entry


# -- 5. large events --------------------------------------------------------------------


async def test_client_reads_events_up_to_the_supported_size(rig):
    from test_agui import served

    from whim import server

    h = await rig.start(ScriptedProvider([finish()]))
    async with served(h) as c:
        text = "é" * (server.MAX_TEXT_BYTES // 2)  # the largest accepted input
        status, _ = await c.try_post("/v1/input", {"text": text})
        assert status == 202
        await h.wait_idle()
        evs = [e async for e in c.events(0, follow=False)]
        assert any(e["type"] == "input.received" and e["data"]["text"] == text for e in evs)
        status, body = await c.try_post("/v1/input", {"text": "x" * (server.MAX_TEXT_BYTES + 1)})
        assert status == 400 and "error" in body  # refused clearly, never half-accepted


async def test_an_oversized_frame_is_reported_and_skipped_not_fatal(rig, monkeypatch):
    from test_agui import served

    from whim import cli

    monkeypatch.setattr(cli, "MAX_EVENT_BYTES", 4096)
    h = await rig.start(ScriptedProvider([finish(), finish()]))
    async with served(h) as c:
        await c.try_post("/v1/input", {"text": "x" * 10_000})
        await h.wait_idle()
        await c.try_post("/v1/input", {"text": "small"})
        await h.wait_idle()
        frames = [f async for f in c.frames(0, follow=False)]
    big = next(seq for seq, ev in frames if ev is cli.OVERSIZED)
    assert big == next(e["seq"] for e in events(h, "input.received"))  # identified by its seq, cursor moves on
    assert any(ev is not cli.OVERSIZED and ev and ev["type"] == "input.received"
               and ev["data"]["text"] == "small" for _, ev in frames)


# -- 2. ambiguous chat submission ----------------------------------------------------


class ScriptedStdin:
    """Feeds chat its lines, then end of input once `done()` says so."""

    def __init__(self, lines, done):
        self.lines, self.done = list(lines), done

    def readline(self):
        import time

        if self.lines:
            return self.lines.pop(0) + "\n"
        deadline = time.monotonic() + 10
        while not self.done() and time.monotonic() < deadline:
            time.sleep(0.05)
        return ""


async def chat_with_lost_acks(rig, monkeypatch, lines, lose, done):
    """Run `whim chat` against an in-process daemon whose first `lose` POST /v1/input
    acknowledgements are lost after the daemon has committed the message."""
    import aiohttp
    from test_agui import served

    from whim import cli

    real, lost = cli.Client.try_post, {"n": 0}

    async def try_post(self, path, body):
        out = await real(self, path, body)
        if path == "/v1/input" and lost["n"] < lose:
            lost["n"] += 1
            raise aiohttp.ServerDisconnectedError()
        return out

    monkeypatch.setattr(cli.Client, "try_post", try_post)
    h = await rig.start(ScriptedProvider([finish()] * 5))
    monkeypatch.setattr("sys.stdin", ScriptedStdin(lines, lambda: done(h)))
    async with served(h):
        await cli.cmd_chat(rig.cfg, activity=False)
    await h.wait_idle()
    return h


async def test_lost_acknowledgement_is_retried_with_the_same_id_never_duplicated(rig, monkeypatch, capsys):
    h = await chat_with_lost_acks(rig, monkeypatch, ["hello there"], lose=1,
                                  done=lambda h: h.ledger.q1("SELECT 1 FROM inputs") is not None)
    out = capsys.readouterr().out
    rows = h.ledger.q("SELECT text, source_message_id FROM inputs")
    assert [r["text"] for r in rows] == ["hello there"]  # accepted once
    assert "not sent" not in out  # never claimed unsent when it might have arrived
    assert "not confirmed" in out and "confirmed" in out.split("not confirmed", 1)[1]


async def test_rejected_input_is_reported_as_rejected_and_chat_continues(rig, monkeypatch, capsys):
    from whim import server

    h = await chat_with_lost_acks(rig, monkeypatch, ["x" * (server.MAX_TEXT_BYTES + 1), "after"], lose=0,
                                  done=lambda h: h.ledger.q1("SELECT 1 FROM inputs") is not None)
    out = capsys.readouterr().out
    assert "rejected" in out and [r["text"] for r in h.ledger.q("SELECT text FROM inputs")] == ["after"]


async def test_one_shot_commands_retry_input_by_id_and_never_repeat_controls(rig, monkeypatch, capsys):
    import aiohttp
    from test_agui import served

    from whim import cli

    real, seen = cli.Client.post, []

    async def flaky(self, path, body):
        seen.append(path)
        out = await real(self, path, body)
        if seen.count(path) == 1:
            raise aiohttp.ServerDisconnectedError()  # acted on, acknowledgement lost
        return out

    monkeypatch.setattr(cli.Client, "post", flaky)
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return finish()

    h = await rig.start(ScriptedProvider([slow, finish()]))
    async with served(h):
        await asyncio.to_thread(cli.main, ["--home", str(rig.cfg.home), "send", "hi"])
        assert [r["text"] for r in h.ledger.q("SELECT text FROM inputs")] == ["hi"]  # retried, deduplicated
        with pytest.raises(SystemExit, match="outcome unknown"):
            await asyncio.to_thread(cli.main, ["--home", str(rig.cfg.home), "stop", "--now"])
        assert seen.count("/v1/control") == 1  # an interrupt is never sent twice
        gate.set()
        await h.wait_idle()
