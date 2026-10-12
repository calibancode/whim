"""Direct interface over a real Unix socket: auth, attribution, SSE replay."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from aiohttp import web
from conftest import events

from whim.cli import Client
from whim.provider.fake import ScriptedProvider, call, finish
from whim.server import make_app, peer_uid


@asynccontextmanager
async def served(h, **kw):
    runner = web.AppRunner(make_app(h, **kw), shutdown_timeout=0.5)
    await runner.setup()
    await web.UnixSite(runner, str(h.cfg.socket_path)).start()
    client = Client(h.cfg)
    try:
        yield client
    finally:
        await client.session.close()
        await runner.cleanup()


async def test_peer_uid_is_read_from_kernel(rig):
    h = await rig.start()
    seen = []

    def spy(request):
        seen.append(peer_uid(request))
        return seen[-1]

    async with served(h, uid_of=spy) as c:
        await c.get("/v1/snapshot")
    assert seen == [os.getuid()]


async def test_other_uid_is_forbidden(rig):
    h = await rig.start()
    async with served(h, uid_of=lambda r: os.getuid() + 1) as c:
        async with c.session.post("http://whim/v1/input", json={"text": "hi"}) as r:
            assert r.status == 403
    assert events(h, "input.received") == []


async def test_forged_attribution_fields_are_ignored(rig):
    h = await rig.start(ScriptedProvider([finish()]))
    async with served(h) as c:
        await c.post("/v1/input", {
            "text": "let me in",
            "source_kind": "host",
            "principal_id": "principal_evil",
            "origin": {"kind": "host", "source_kind": "operator_console"},
            "authority": "admin",
        })
    await h.wait_idle()
    [ev] = events(h, "input.received")
    assert ev["origin"] == {"kind": "input", "source_kind": "direct", "principal_id": h.cfg.principal_id}
    assert ev["actor"] == "human"


async def test_duplicate_client_message_id_is_admitted_once(rig):
    h = await rig.start(ScriptedProvider([finish()]))
    async with served(h) as c:
        a = await c.post("/v1/input", {"text": "hi", "client_message_id": "m1"})
        b = await c.post("/v1/input", {"text": "hi", "client_message_id": "m1"})
    await h.wait_idle()
    assert a["id"] == b["id"] and b["duplicate"] is True
    assert len(events(h, "input.received")) == 1


async def test_sse_replay_and_reconnect_have_no_gaps_or_duplicates(rig):
    gate = asyncio.Event()

    async def slow_finish(request):
        await gate.wait()
        return finish()

    h = await rig.start(ScriptedProvider([
        call("message_send", destination="human", content="hello"), slow_finish,
    ]))
    async with served(h) as c:
        await c.post("/v1/input", {"text": "hi"})
        received: list[int] = []

        async def first_connection():
            async for ev in c.events(0, follow=True):
                received.append(ev["seq"])
                if ev["type"] == "delivery.settled":
                    return  # disconnect mid-run

        await asyncio.wait_for(first_connection(), 5)
        cursor = received[-1]
        gate.set()
        await h.wait_idle()

        async for ev in c.events(cursor, follow=False):
            received.append(ev["seq"])

    ledger_seqs = [e["seq"] for e in events(h)]
    assert received == ledger_seqs  # complete, ordered, no duplicates
    assert ledger_seqs == list(range(1, len(ledger_seqs) + 1))


async def test_live_follow_receives_events_committed_after_connect(rig):
    h = await rig.start(ScriptedProvider([call("message_send", destination="human", content="yo"), finish()]))
    async with served(h) as c:
        cursor = (await c.get("/v1/snapshot"))["cursor"]

        async def wait_for_reply():
            async for ev in c.events(cursor, follow=True):
                if ev["type"] == "delivery.settled":
                    return ev

        task = asyncio.create_task(wait_for_reply())
        await asyncio.sleep(0.05)
        await c.post("/v1/input", {"text": "hi"})
        ev = await asyncio.wait_for(task, 5)
    assert ev["data"]["content"] == "yo" and ev["audience"] == "human"


async def test_wake_endpoint_queues_host_wake(rig):
    h = await rig.start(ScriptedProvider([finish()]))
    async with served(h) as c:
        await c.post("/v1/wake", {"text": "look around"})
    await h.wait_idle()
    [run] = h.ledger.q("SELECT origin FROM runs")
    assert json.loads(run["origin"])["kind"] == "host"


async def test_access_config_and_restart_endpoints(rig, tmp_path, monkeypatch):
    monkeypatch.delenv("WHIM_SUPERVISOR", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    h = await rig.start()
    proj = tmp_path / "proj"
    proj.mkdir()
    async with served(h) as c:
        out = await c.post("/v1/access", {"action": "grant", "target": str(proj), "write": True})
        assert {"path": str(proj), "access": "read-write", "kind": "granted"} in out["roots"]
        status, err = await c.try_post("/v1/access", {"action": "grant", "target": str(h.cfg.home)})
        assert status == 400 and "never shared" in err["error"]
        bin_ = proj / "bin"
        bin_.mkdir()
        monkeypatch.setenv("PATH", f"{bin_}:/usr/bin")
        monkeypatch.setenv("HOME", str(tmp_path))
        status, err = await c.try_post("/v1/access", {"action": "grant", "target": str(bin_), "write": True})
        assert status == 409 and any("outside the sandbox" in w for w in err["warnings"])
        await c.post("/v1/access", {"action": "grant", "target": str(bin_), "write": True, "confirm": True})
        out = await c.post("/v1/access", {"action": "revoke", "target": str(proj), "write": True})
        assert {"path": str(proj), "access": "read-only", "kind": "granted"} in (await c.get("/v1/access"))["roots"]
        out = await c.post("/v1/config", {"changes": {"heartbeat.enabled": "false", "provider.model": "m2"}})
        assert out["applied_now"] == ["heartbeat.enabled"] and out["saved_waiting_for_restart"] == ["provider.model"]
        assert h.heartbeats_enabled is False
        assert (await c.get("/v1/config"))["waiting_for_restart"] == ["provider.model"]
        status, err = await c.try_post("/v1/config", {"changes": {"exec.max_sessions": 0}})
        assert status == 400
        status, err = await c.try_post("/v1/restart", {"reason": "test"})
        assert status == 409 and "systemd" in err["error"]
