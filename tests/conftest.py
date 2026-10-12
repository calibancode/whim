"""Shared test infrastructure: a controllable clock, simulated crashes, the harness rig,
ledger queries, the control socket, and small plugin and destination fixtures."""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web

from whim import agui, authz, onboard, plugins
from whim.cli import Client
from whim.harness import Harness
from whim.provider.fake import ScriptedProvider
from whim.server import make_app


class Clock:
    """The single controllable time source for a test: drives scheduling, stored
    timestamps and every local time the character sees."""

    def __init__(self, now=None):
        self.now = now or datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, minutes: float = 0, **kw):
        self.now += timedelta(minutes=minutes, **kw)


needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")


class SimulatedCrash(BaseException):
    """Escapes every `except Exception`, like the process dying at that instant."""


def crash_at(point: str, *, times: int = 1):
    remaining = {"n": times}

    def hook(p: str) -> None:
        if p == point and remaining["n"] > 0:
            remaining["n"] -= 1
            raise SimulatedCrash(point)

    return hook


@pytest.fixture(autouse=True)
def isolated_credentials(request, tmp_path_factory, monkeypatch):
    """Never touch the real credentials file or a real key, except in live tests."""
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setenv("WHIM_CREDENTIALS", str(tmp_path_factory.mktemp("creds") / "credentials.json"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.fixture
def cfg(tmp_path: Path):
    from whim import settings

    c, _ = onboard.init(tmp_path / "home")
    c.timezone = "America/Los_Angeles"  # deterministic local times and DST behaviour
    settings.save(c, {"character.timezone": c.timezone})  # so the running and saved configurations agree
    return c


class Rig:
    """Starts/stops/crashes harness instances against one home directory."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.h: Harness | None = None

    async def start(self, provider=None, *, fault_hook=None, destinations=None, before_start=None,
                    run_scheduler=False, **kw) -> Harness:
        self.provider = provider or ScriptedProvider()
        self.h = Harness(self.cfg, self.provider, fault_hook=fault_hook, destinations=destinations, **kw)
        if before_start is not None:
            before_start(self.h)
        await self.h.start(run_scheduler=run_scheduler)
        return self.h

    async def crashed(self, timeout: float = 5.0) -> None:
        """Wait for the worker to die from a SimulatedCrash, then drop the instance."""
        with pytest.raises(SimulatedCrash):
            await asyncio.wait_for(self.h._worker, timeout)
        self.h.close()
        self.h = None

    async def restart(self, provider=None, **kw) -> Harness:
        if self.h is not None:
            await self.h.stop(grace=0)
            self.h.close()
        return await self.start(provider, **kw)

    async def close(self) -> None:
        if self.h is not None:
            await self.h.stop(grace=1)
            self.h.close()
            self.h = None


@pytest.fixture
async def rig(cfg):
    r = Rig(cfg)
    yield r
    await r.close()


SECRET = "TOPSECRET-c0ffee"


@pytest.fixture
def secret(tmp_path):
    """A file outside the workspace whose content must never reach the model."""
    p = tmp_path / "outside" / "private.txt"
    p.parent.mkdir()
    p.write_text(SECRET)
    return p


def sent_to_model(provider) -> str:
    return json.dumps([r.messages for r in provider.requests])


async def until(pred, timeout=5.0):
    async def go():
        while not pred():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(go(), timeout)


# -- what the ledger and the model saw --------------------------------------------------


def events(h: Harness, *types: str) -> list[dict]:
    evs = h.ledger.events_after(0, 100000)
    return [e for e in evs if not types or e["type"] in types]


def types(h: Harness) -> list[str]:
    return [e["type"] for e in events(h)]


def runs(h: Harness) -> list[dict]:
    return [dict(r) for r in h.ledger.q("SELECT * FROM runs ORDER BY started_at, rowid")]


def kinds(h: Harness) -> list[str]:
    """What started each run, in order (input, host, cron, heartbeat, recovery, ...)."""
    return [json.loads(r["origin"])["kind"] for r in runs(h)]


def human_messages(h: Harness) -> list[str]:
    return [e["data"]["content"] for e in events(h, "delivery.settled") if e["data"]["destination"] == "human"]


def tool_result(msg: dict) -> dict:
    assert msg["role"] == "tool"
    return json.loads(msg["content"])


def tool_names(request) -> list[str]:
    return [t["function"]["name"] for t in request.tools]


def assert_paired(messages) -> None:
    """A valid provider transcript: every tool call answered, in order, right after its
    assistant message; no orphan results."""
    pending = []
    for m in messages:
        if m["role"] == "tool":
            assert pending and m["tool_call_id"] == pending.pop(0), "orphan or out-of-order tool result"
            continue
        assert not pending, f"unanswered tool calls: {pending}"
        if m["role"] == "assistant":
            pending = [tc["id"] for tc in m.get("tool_calls") or []]
    assert not pending, f"unanswered tool calls: {pending}"


def complete_bootstrap(h: Harness) -> None:
    with h.ledger.tx():
        h.ledger.x("INSERT OR REPLACE INTO meta(key, value) VALUES('bootstrap', '{\"status\": \"complete\"}')")


# -- the control socket and AG-UI -------------------------------------------------------


@asynccontextmanager
async def served(h, **kw):
    """The daemon's HTTP app on its real Unix socket, with the CLI's client."""
    runner = web.AppRunner(make_app(h, **kw), shutdown_timeout=0.5)
    await runner.setup()
    await web.UnixSite(runner, str(h.cfg.socket_path)).start()
    client = Client(h.cfg)
    try:
        yield client
    finally:
        await client.session.close()
        await runner.cleanup()


async def read_sse(client, path, *, stop=None, timeout=5.0):
    """[(id or None, event or None)] frames; id-only frames have event None."""
    frames = []

    async def go():
        async with client.session.get(f"http://whim{path}") as r:
            fid, data = None, []
            async for raw in r.content:
                line = raw.decode().rstrip("\n")
                if line.startswith("id:"):
                    fid = int(line[3:].strip())
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "":
                    if fid is not None or data:
                        frames.append((fid, json.loads("\n".join(data)) if data else None))
                        if stop and data and stop(frames[-1][1]):
                            return
                    fid, data = None, []

    await asyncio.wait_for(go(), timeout)
    return frames


def project_all(h, view, entitlements=("operator", "human")):
    pr = agui.Projector(h, view, entitlements)
    return [x for ev in h.ledger.events_after(0, 100000) for x in pr.project(ev)]


def check_conformance(stream: list[dict]) -> None:
    """The AG-UI 1.0 ordering rules we rely on (lifecycle, text and tool streaming)."""
    open_run, closed = None, set()
    open_msgs, open_calls, ended_calls = set(), set(), set()
    for e in stream:  # calls restated by a snapshot count as known
        if e["type"] == "MESSAGES_SNAPSHOT":
            ended_calls |= {c["id"] for m in e["messages"] for c in m.get("toolCalls", [])}
    assert not stream or stream[0]["type"] in ("RUN_STARTED", "RUN_ERROR"), stream[:1]
    for e in stream:
        t = e["type"]
        if t == "RUN_STARTED":
            assert open_run is None, "RUN_STARTED while a run is open"
            assert e["runId"] not in closed, "runId reused"
            open_run = e["runId"]
            continue
        assert open_run is not None, f"{t} outside a run"
        if t in ("RUN_FINISHED", "RUN_ERROR"):
            assert not open_msgs and not open_calls, "run closed with open message/tool call"
            closed.add(open_run)
            open_run = None
        elif t == "TEXT_MESSAGE_START":
            open_msgs.add(e["messageId"])
        elif t == "TEXT_MESSAGE_CONTENT":
            assert e["messageId"] in open_msgs
        elif t == "TEXT_MESSAGE_END":
            open_msgs.remove(e["messageId"])
        elif t == "TOOL_CALL_START":
            open_calls.add(e["toolCallId"])
        elif t == "TOOL_CALL_ARGS":
            assert e["toolCallId"] in open_calls
        elif t == "TOOL_CALL_END":
            open_calls.remove(e["toolCallId"])
            ended_calls.add(e["toolCallId"])
        elif t == "TOOL_CALL_RESULT":
            assert e["toolCallId"] in ended_calls, "result for a call that never ended"
    assert open_run is None, "stream ends with an open run"


def assert_valid_agui(h) -> None:
    """Every view of the whole history is schema-valid and conformant."""
    for view in agui.VIEWS:
        stream = project_all(h, view)
        for e in stream:
            agui.validate(e)
        check_conformance(stream)


# -- destinations and plugins -----------------------------------------------------------


class Outbox:
    """Synthetic external destination used to exercise uncertain delivery."""

    name = "outbox"
    description = "test outbox"

    def __init__(self):
        self.delivered: list[str] = []

    async def deliver(self, delivery_id: str, content: str, reply_to: str | None, target=None) -> dict:
        self.delivered.append(content)
        return {"outbox_index": len(self.delivered) - 1}


class TestChat:
    """A pretend chat platform adapter: verifies senders, delivers to rooms."""

    __test__ = False
    name = "testchat"
    description = "test chat platform"

    def __init__(self, mode="ok"):
        self.mode = mode
        self.sent = []

    async def deliver(self, delivery_id, content, reply_to, target=None):
        from whim.delivery import DeliveryFailed

        if self.mode == "reject":
            raise DeliveryFailed("room is read-only")
        if self.mode == "explode":
            raise ConnectionError("socket closed mid-send")
        self.sent.append((target, content))
        return {"platform_message_id": f"tc-{len(self.sent)}"}


WORDCOUNT = '''
from whim_plugin import Plugin
plugin = Plugin("wordcount")

@plugin.tool
def count(text: str) -> dict:
    "Count the words in a text."
    return {"words": len(text.split())}

plugin.run()
'''


def manifest(name, **kw):
    return json.dumps({"name": name, "description": f"{name} tools", "entry": "server.py", **kw})


def write_plugin(rig, folder, name, code, **kw):
    d = rig.cfg.workspace / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(manifest(name, **kw))
    (d / "server.py").write_text(code)
    return d


def plugin_manage(op, **kw):
    from whim.provider.fake import call

    return call("plugin_manage", op=op, **kw)


def preapprove(h, row):
    """Grant and activate directly, as an earlier approval would have (no wake)."""
    with h.ledger.tx():
        req = authz.request(h, row["impl"], plugins.required_scopes(row), "test", None)
        authz.decide(h, req["id"], True, h.cfg.principal_id)
        plugins.activate(h, row["impl"], "test")


async def activated(rig, folder, name, code, **kw):
    """A plugin written, installed, approved and active."""
    write_plugin(rig, folder, name, code, **kw)
    row = await plugins.install(rig.h, rig.cfg.workspace / folder, source="test")
    assert row["status"] == "validated", row["error"]
    preapprove(rig.h, row)
    return row


def approve_pending(h, standing=False):
    for a in h.ledger.q("SELECT id FROM approvals WHERE status = 'pending'"):
        h.decide_approval(a["id"], True, h.cfg.principal_id, standing=standing)
