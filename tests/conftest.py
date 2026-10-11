from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from whim import onboard
from whim.harness import Harness
from whim.provider.fake import ScriptedProvider


class Clock:
    """The single controllable time source for a test: drives scheduling, stored
    timestamps and every local time the character sees."""

    def __init__(self, now=None):
        from datetime import datetime, timezone

        self.now = now or datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, minutes: float = 0, **kw):
        from datetime import timedelta

        self.now += timedelta(minutes=minutes, **kw)


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
    c, _ = onboard.init(tmp_path / "home")
    c.timezone = "America/Los_Angeles"  # deterministic local times and DST behaviour
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


def events(h: Harness, *types: str) -> list[dict]:
    evs = h.ledger.events_after(0, 100000)
    return [e for e in evs if not types or e["type"] in types]


def types(h: Harness) -> list[str]:
    return [e["type"] for e in events(h)]


def runs(h: Harness) -> list[dict]:
    return [dict(r) for r in h.ledger.q("SELECT * FROM runs ORDER BY started_at, rowid")]


def human_messages(h: Harness) -> list[str]:
    return [e["data"]["content"] for e in events(h, "delivery.settled") if e["data"]["destination"] == "human"]


def tool_result(msg: dict) -> dict:
    assert msg["role"] == "tool"
    return json.loads(msg["content"])


class Outbox:
    """Synthetic external destination used to exercise uncertain delivery."""

    name = "outbox"
    description = "test outbox"

    def __init__(self):
        self.delivered: list[str] = []

    async def deliver(self, delivery_id: str, content: str, reply_to: str | None, target=None) -> dict:
        self.delivered.append(content)
        return {"outbox_index": len(self.delivered) - 1}
