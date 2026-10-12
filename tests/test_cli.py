"""The CLI client when acknowledgements are lost: input is retried by id and never
duplicated, controls are never repeated, and nothing is claimed unsent that may have arrived."""

from __future__ import annotations

import asyncio

import pytest
from conftest import served

from whim.provider.fake import ScriptedProvider, finish


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
