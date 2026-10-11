"""An independent client of the documented wire protocol (docs/client_protocol.md).

Standard library only: no whim module (or test helper that uses one) is imported. It
starts a real daemon with the offline script provider through the CLI, then talks HTTP
over the Unix socket, reconstructing everything from native events and AG-UI frames.
"""

from __future__ import annotations

import http.client
import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = {
    "rules": [
        {"match": '"status": "sent"', "tool_calls": [{"name": "turn_finish"}]},
        {"match": '"status": "asked"', "tool_calls": [{"name": "turn_finish"}]},
        {"match": "answering your question", "tool_calls": [
            {"name": "message_send", "arguments": {"destination": "human", "content": "noted"}}]},
        {"match": "ask me", "tool_calls": [
            {"name": "human_ask", "arguments": {"question": "Tea or coffee?", "choices": ["tea", "coffee"]}}]},
        {"match": "hello", "content": "(a private thought)", "tool_calls": [
            {"name": "message_send", "arguments": {"destination": "reply_to_source", "content": "hi there"}}]},
    ],
    "default": {"tool_calls": [{"name": "turn_finish"}]},
}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 10.0):
        super().__init__("whim", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


class WhimClient:
    def __init__(self, sock: Path):
        self.sock = str(sock)

    def request(self, method: str, path: str, body=None, headers=None) -> tuple[int, object]:
        conn = UnixHTTPConnection(self.sock)
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = {"Content-Type": "application/json", **(headers or {})} if data else dict(headers or {})
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw.decode()
        finally:
            conn.close()

    def frames(self, path: str, headers=None, *, until=None, timeout=15.0) -> list[tuple[int | None, dict | None]]:
        """Complete SSE frames as (id, data); stops at EOF or once `until(data)` is true."""
        conn = UnixHTTPConnection(self.sock, timeout=timeout)
        out: list[tuple[int | None, dict | None]] = []
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            assert resp.status == 200, resp.read()
            assert resp.getheader("Content-Type").startswith("text/event-stream")
            fid, data = None, []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = resp.readline()
                if not line:
                    break
                line = line.decode().rstrip("\r\n")
                if line.startswith(":"):
                    continue  # keepalive comment
                if line.startswith("id:"):
                    fid = int(line[3:].strip())
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "" and (fid is not None or data):
                    ev = json.loads("\n".join(data)) if data else None
                    out.append((fid, ev))
                    fid, data = None, []
                    if until is not None and ev is not None and until(ev):
                        break
            return out
        finally:
            conn.close()

    def events(self, after: int = 0) -> list[dict]:
        return [e for _, e in self.frames(f"/v1/events?after={after}&follow=0") if e]

    def wait_event(self, after: int, pred) -> dict:
        frames = self.frames(f"/v1/events?after={after}&follow=1", until=pred)
        for _, e in frames:
            if e and pred(e):
                return e
        raise AssertionError("event never arrived")


def run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "whim", "--home", str(home), *args],
                          capture_output=True, text=True, timeout=30, check=True)


@pytest.fixture
def daemon(tmp_path):
    home = tmp_path / "home"
    run_cli(home, "init")
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace('kind = "deepseek"', 'kind = "script"')
                   .replace('script = ""', f'script = "{script}"'))
    sock = home / "run" / "whim.sock"
    proc = subprocess.Popen([sys.executable, "-m", "whim", "--home", str(home), "run", "--no-heartbeat"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while not sock.exists():
        assert time.monotonic() < deadline and proc.poll() is None, "daemon did not start"
        time.sleep(0.05)
    try:
        yield WhimClient(sock)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(15)


ENVELOPE = {"v", "seq", "id", "at", "type", "character_id", "thread_id", "actor", "audience", "data"}


def human_replies(evs):
    return [e for e in evs if e["type"] == "delivery.settled" and e["data"]["destination"] == "human"
            and e["data"]["status"] == "sent"]


def agui_messages(stream: list[dict]) -> dict[str, dict]:
    """Client-side AG-UI reduction; snapshots reconcile by message id."""
    msgs: dict[str, dict] = {}
    for e in stream:
        t = e["type"]
        if t == "MESSAGES_SNAPSHOT":
            msgs.update({m["id"]: m for m in e["messages"]})
        elif t == "TEXT_MESSAGE_START":
            msgs[e["messageId"]] = {"id": e["messageId"], "role": e.get("role", "assistant"), "content": "",
                                    **{k: e[k] for k in ("name", "metadata") if k in e}}
        elif t == "TEXT_MESSAGE_CONTENT":
            msgs[e["messageId"]]["content"] += e["delta"]
        elif t == "TOOL_CALL_RESULT":
            msgs[e["messageId"]] = {"id": e["messageId"], "role": "tool", "content": e["content"],
                                    "toolCallId": e["toolCallId"]}
    return msgs


def test_independent_client_conversation_questions_and_replay(daemon):
    c = daemon
    status, snap = c.request("GET", "/v1/snapshot")
    assert status == 200 and isinstance(snap["cursor"], int) and snap["active_run"] is None
    cursor = snap["cursor"]

    # Errors are JSON with a status a client can branch on.
    assert c.request("POST", "/v1/input", {})[0] == 400
    assert c.request("GET", "/v1/events?after=nope")[0] == 400
    assert c.request("GET", "/v1/agui?view=everything")[0] == 400
    status, body = c.request("POST", "/v1/control", {"action": "stop"})
    assert status == 409 and "error" in body  # nothing is running
    assert c.request("POST", "/v1/control", {"action": "stop", "principal_id": "me"})[0] == 400

    # Send, idempotently; identity comes from the socket, never from the body.
    status, rec = c.request("POST", "/v1/input", {"text": "hello", "client_message_id": "m-1",
                                                  "sender": {"role": "external"}, "origin": {"principal_id": "x"}})
    assert status == 202 and rec["duplicate"] is False
    status, again = c.request("POST", "/v1/input", {"text": "hello", "client_message_id": "m-1"})
    assert status == 200 and again["duplicate"] is True and again["id"] == rec["id"]

    reply = c.wait_event(cursor, lambda e: e["type"] == "run.finished")
    evs = c.events(cursor)
    assert evs[-1]["seq"] == reply["seq"]
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(set(seqs)) and seqs[0] > cursor
    for e in evs:
        assert ENVELOPE <= set(e) and e["audience"] in ("human", "operator")
    [received] = [e for e in evs if e["type"] == "input.received"]  # the duplicate made no event
    assert received["data"]["input_id"] == rec["id"] and received["seq"] == rec["seq"]
    assert received["data"]["sender"] == {"role": "designated_human", "verified_by": "direct", "label": "human"}
    assert [r["data"]["content"] for r in human_replies(evs)] == ["hi there"]
    private = [e["data"]["content"] for e in evs if e["type"] == "message.completed"]
    assert private == ["(a private thought)"]  # authored, never delivered
    started = [e for e in evs if e["type"] == "tool.started"]
    finished = {e["data"]["operation_id"]: e["data"] for e in evs if e["type"] == "tool.finished"}
    assert {e["data"]["operation_id"] for e in started} == set(finished)
    assert sum(e["data"]["tool_calls"] for e in evs if e["type"] == "provider.request.finished") == len(started)

    # Resume from the middle with Last-Event-ID: exactly the rest, nothing repeated.
    mid = seqs[len(seqs) // 2]
    resumed = [e for _, e in c.frames("/v1/events?follow=0", {"Last-Event-ID": str(mid)}) if e]
    assert [e["seq"] for e in resumed] == [s for s in seqs if s > mid]

    # A question, answered once; the answer correlates through events alone.
    cursor = reply["seq"]
    c.request("POST", "/v1/input", {"text": "ask me something", "client_message_id": "m-2"})
    created = c.wait_event(cursor, lambda e: e["type"] == "question.created")
    qid = created["data"]["question_id"]
    status, pending = c.request("GET", "/v1/questions")
    assert status == 200 and [q["id"] for q in pending["pending"]] == [qid]
    assert pending["pending"][0]["choices"] == ["tea", "coffee"]
    c.wait_event(created["seq"], lambda e: e["type"] == "run.finished")
    status, answered = c.request("POST", f"/v1/questions/{qid}/answer", {"choice": 2})
    assert status == 200 and answered["choice"] == "coffee"
    assert c.request("POST", f"/v1/questions/{qid}/answer", {"choice": 1})[0] == 409
    assert c.request("POST", "/v1/questions/q_nope/answer", {"choice": 1})[0] == 404
    noted = c.wait_event(created["seq"], lambda e: e["type"] == "delivery.settled"
                         and e["data"].get("content") == "noted")
    evs = c.events(cursor)
    asked = [e for e in human_replies(evs) if e["data"].get("question_id") == qid]
    assert len(asked) == 1 and "Tea or coffee?" in asked[0]["data"]["content"]
    [answer_in] = [e for e in evs if e["type"] == "input.received" and e["data"].get("question_id") == qid]
    assert answer_in["data"]["input_id"] == answered["input_id"]

    # Snapshot agrees with the events up to its cursor.
    status, snap = c.request("GET", "/v1/snapshot")
    assert snap["cursor"] >= noted["seq"]
    shown = {m["id"]: m for m in snap["messages_to_human"]}
    assert shown[asked[0]["data"]["delivery_id"]]["question_id"] == qid
    assert [m["content"] for m in snap["messages_to_human"]][-1] == "noted"

    # AG-UI: the conversation view holds only real exchanges, with their metadata.
    frames = c.frames("/v1/agui?view=conversation&follow=0")
    stream = [e for _, e in frames if e]
    assert stream[0]["type"] == "RUN_STARTED"
    assert all(fid is None or fid > 0 for fid, _ in frames)
    msgs = list(agui_messages(stream).values())
    meta = [m["metadata"]["whim"] for m in msgs]
    assert [(m["role"], m["content"]) for m in msgs][:2] == [("user", "hello"), ("assistant", "hi there")]
    assert not any(m["kind"] == "authored" for m in meta)
    assert {"kind": "delivery", "destination": "human", "question_id": qid} in meta
    assert any(m["kind"] == "input" and m.get("question_id") == qid for m in meta)
    activity = agui_messages([e for _, e in c.frames("/v1/agui?view=activity&follow=0") if e])
    assert any(m.get("metadata", {}).get("whim") == {"kind": "authored", "delivered": False}
               for m in activity.values())
    assert any(m["role"] == "tool" for m in activity.values())
