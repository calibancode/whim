"""Real daemon process: CLI round trip, kill -9 mid-request, restart, graceful stop."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = {
    "rules": [
        # The interrupted run never got a response, so its user turn is merged with the
        # recovery opening; match the recovery notice before "slow".
        {"match": "recovery notice", "tool_calls": [
            {"name": "message_send", "arguments": {"destination": "human", "content": "I'm back."}}]},
        {"match": "slow", "delay": 60, "content": "taking my time"},
        {"match": '"status": "sent"', "tool_calls": [{"name": "turn_finish"}]},
        {"match": "hello", "tool_calls": [
            {"name": "message_send", "arguments": {"destination": "reply_to_source", "content": "hi there"}}]},
    ],
    "default": {"tool_calls": [{"name": "turn_finish"}]},
}


def whim(home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "whim", "--home", str(home), *args],
                          capture_output=True, text=True, timeout=20, check=check)


def ledger(home: Path) -> list[dict]:
    out = whim(home, "events", "--after", "0").stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def wait_for(pred, timeout=10.0, what=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (v := pred()):
            return v
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def start_daemon(home: Path) -> subprocess.Popen:
    sock = home / "run" / "whim.sock"
    before = sock.stat().st_mtime_ns if sock.exists() else None
    proc = subprocess.Popen([sys.executable, "-m", "whim", "--home", str(home), "run"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_for(lambda: sock.exists() and sock.stat().st_mtime_ns != before, what="daemon socket")
    return proc


def deliveries(evs):
    return [e["data"]["content"] for e in evs
            if e["type"] == "delivery.settled" and e["data"].get("destination") == "human"]


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "h"
    whim(home, "init")
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    cfg = home / "config.toml"
    text = cfg.read_text().replace('kind = "deepseek"', 'kind = "script"').replace(
        'script = ""', f'script = "{script}"')
    cfg.write_text(text)
    return home


def test_daemon_survives_kill_9_and_recovers(home):
    proc = start_daemon(home)
    try:
        assert stat.S_IMODE((home / "run").stat().st_mode) == 0o700
        assert stat.S_IMODE((home / "run" / "whim.sock").stat().st_mode) == 0o600
        second = whim(home, "run", check=False)
        assert second.returncode != 0 and "another whim daemon" in second.stderr

        whim(home, "send", "hello")
        wait_for(lambda: "hi there" in deliveries(ledger(home)), what="reply")

        whim(home, "send", "go slow please")
        # Wait until the slow provider request is in flight, then kill -9.
        wait_for(lambda: sum(e["type"] == "run.started" for e in ledger(home)) == 2, what="second run")
        time.sleep(0.3)
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(5)

    proc = start_daemon(home)
    try:
        evs = wait_for(lambda: (e := ledger(home)) and "I'm back." in deliveries(e) and
                       any(x["type"] == "run.finished" and x["seq"] > 0 for x in e[-3:]) and e,
                       what="recovery delivery")
        types = [e["type"] for e in evs]
        assert types.count("run.interrupted") == 1
        assert types.count("recovery.pending") == 1
        assert deliveries(evs) == ["hi there", "I'm back."]  # nothing re-sent
        snap = json.loads(whim(home, "status").stdout)
        assert snap["active_run"] is None and snap["queued_wakes"] == 0
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(10) == 0
    assert not (home / "run" / "whim.sock").exists()

    # Clean restart: no new interruption, no new recovery wake.
    proc = start_daemon(home)
    try:
        time.sleep(0.5)
        evs = ledger(home)
        assert [e["type"] for e in evs].count("run.interrupted") == 1
        assert [e["type"] for e in evs].count("wake.queued") == 1
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)


def test_cli_without_daemon_is_a_clear_error(home):
    r = whim(home, "send", "hi", check=False)
    assert r.returncode != 0 and "not running" in r.stderr


def test_init_does_not_need_api_key(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    r = subprocess.run([sys.executable, "-m", "whim", "--home", str(tmp_path / "x"), "init", "--name", "Wren"],
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr
    ws = tmp_path / "x" / "workspace"
    assert sorted(p.name for p in ws.iterdir()) == ["BOOTSTRAP.md", "IDENTITY.md", "MEMORY.md", "SOUL.md", "USER.md"]
    assert "invitation rather than a fixed persona: Wren" in (ws / "IDENTITY.md").read_text()


class Lines:
    """Lines from a child's stdout, read on a thread, waited for with a timeout."""

    def __init__(self, stream):
        import queue
        import threading

        self.q: "queue.Queue[str]" = queue.Queue()
        self.seen: list[str] = []
        threading.Thread(target=lambda: [self.q.put(line) for line in stream], daemon=True).start()

    def wait(self, needle: str, timeout: float = 15.0) -> str:
        import queue

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.q.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            self.seen.append(line)
            if needle in line:
                return line
        raise AssertionError(f"no {needle!r} in chat output:\n{''.join(self.seen)}")

    def drain(self, settle: float = 1.0) -> str:
        import queue

        while True:
            try:
                self.seen.append(self.q.get(timeout=settle))
            except queue.Empty:
                return "".join(self.seen)


def test_chat_survives_daemon_restarts_without_losing_replies(home):
    proc = start_daemon(home)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    chat = subprocess.Popen([sys.executable, "-m", "whim", "--home", str(home), "chat"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    out = Lines(chat.stdout)
    try:
        out.wait("connected")
        chat.stdin.write("hello\n")
        chat.stdin.flush()
        out.wait("◆ hi there")

        # Crash mid-request; the chat stays open and says it lost the connection.
        chat.stdin.write("go slow please\n")
        chat.stdin.flush()
        wait_for(lambda: sum(e["type"] == "run.started" for e in ledger(home)) == 2, what="slow run")
        proc.send_signal(signal.SIGKILL)
        proc.wait(5)
        out.wait("lost the connection")
        # The recovery wake replies while the chat is still reconnecting: it must show up.
        proc = start_daemon(home)
        out.wait("reconnected")
        out.wait("◆ I'm back.")

        # A clean restart too, then a normal exchange.
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)
        out.wait("lost the connection")
        proc = start_daemon(home)
        out.wait("reconnected")
        chat.stdin.write("hello again\n")
        chat.stdin.flush()
        out.wait("◆ hi there")
        time.sleep(0.5)
        chat.stdin.close()
        chat.wait(10)
        shown = out.drain()
        assert shown.count("◆ hi there") == 2 and shown.count("◆ I'm back.") == 1  # nothing lost or repeated
    finally:
        if chat.poll() is None:
            chat.kill()
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)


def test_chat_keeps_working_after_a_very_large_event(home):
    proc = start_daemon(home)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    chat = subprocess.Popen([sys.executable, "-m", "whim", "--home", str(home), "chat"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    out = Lines(chat.stdout)
    try:
        out.wait("connected")
        chat.stdin.write("hello " + "x" * 600_000 + "\n")  # an event past aiohttp's default line limit
        chat.stdin.flush()
        out.wait("◆ hi there")
        chat.stdin.write("hello again\n")
        chat.stdin.flush()
        out.wait("◆ hi there")
    finally:
        chat.kill()
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)
