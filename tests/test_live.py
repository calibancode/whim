"""Opt-in live check against the first-party DeepSeek API.

Run with:  whim auth set   (once), then  pytest -m live -s
Verifies the multi-request tool loop with reasoning_content replay (the API returns 400
if it is missing), explicit send/finish, user_id, and reports cache hit/miss tokens.
"""

from __future__ import annotations

import json
import os

import pytest
from conftest import events, human_messages, runs

from whim import credentials
from whim.provider.deepseek import DeepSeekProvider


def _have_key() -> bool:
    try:
        return credentials.lookup("deepseek", "DEEPSEEK_API_KEY") is not None
    except credentials.CredentialError:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _have_key(), reason="no DeepSeek key (run `whim auth set`)"),
]


async def test_live_tool_loop_with_reasoning_replay(rig):
    rig.cfg.provider.model = os.environ.get("WHIM_LIVE_MODEL", rig.cfg.provider.model)
    h = await rig.start(DeepSeekProvider(rig.cfg.provider))
    h.submit_input("Hi! Please read your MEMORY.md, then send me one short sentence about it, then finish.")
    await h.wait_idle(timeout=300)
    h.submit_input("Thanks. Reply with one word, then finish.")
    await h.wait_idle(timeout=300)

    statuses = [(r["status"], r["reason"]) for r in runs(h)]
    usage = [e["data"]["usage"] for e in events(h, "provider.request.finished")]
    print("\nruns:", statuses)
    print("delivered:", human_messages(h))
    for u in usage:
        print("usage:", u)
    assert all(s != "failed" for s, _ in statuses), statuses
    assert len(usage) >= 3  # at least one multi-request run, so reasoning was replayed
    assert any(m.get("reasoning_content") for m in h.context.messages())
    key = credentials.lookup("deepseek", "DEEPSEEK_API_KEY")[0].reveal()
    assert key not in json.dumps(events(h)) and key not in json.dumps(h.context.messages())


# --- real daemon, real model, end to end ---------------------------------------------

def _daemon(home):
    import subprocess
    import sys
    import time

    sock = home / "run" / "whim.sock"
    sock.unlink(missing_ok=True)
    log = open(home / "daemon.log", "a")
    proc = subprocess.Popen([sys.executable, "-m", "whim", "--home", str(home), "run"],
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        if sock.exists():
            return proc
        time.sleep(0.1)
    raise AssertionError("daemon did not start")


def _ledger(home):
    import sqlite3

    c = sqlite3.connect(f"file:{home / 'whim.db'}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _say(home, text, timeout=240):
    import subprocess
    import sys
    import time

    with _ledger(home) as c:
        before = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    subprocess.run([sys.executable, "-m", "whim", "--home", str(home), "send", text],
                   check=True, capture_output=True, timeout=30)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _ledger(home) as c:
            n = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            busy = c.execute("SELECT (SELECT COUNT(*) FROM runs WHERE status='active') + "
                             "(SELECT COUNT(*) FROM inputs WHERE status='pending') + "
                             "(SELECT COUNT(*) FROM wakes WHERE status='queued')").fetchone()[0]
        if n > before and busy == 0:
            return
        time.sleep(0.5)
    raise AssertionError(f"no idle after {text!r}")


def test_live_daemon_end_to_end(tmp_path):
    """Real daemon + real model: tools, explicit send/finish, private text, restart."""
    import signal

    from whim import onboard

    home = tmp_path / "home"
    cfg, _ = onboard.init(home)
    model = os.environ.get("WHIM_LIVE_MODEL")
    if model:
        text = cfg.config_path.read_text().replace(f'model = "{cfg.provider.model}"', f'model = "{model}"')
        cfg.config_path.write_text(text)

    proc = _daemon(home)
    try:
        _say(home, "Please create a file notes/color.md recording that my favorite color is teal, then tell me it's done.")
        _say(home, "Before answering, jot your raw thoughts down as ordinary private text that I won't see, then send "
                   "me a one-sentence answer: is it better to be consistent or curious?")
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(60) == 0
    proc = _daemon(home)
    try:
        _say(home, "The service you run in was just restarted. What's my favorite color, and where did you record it?")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(60)

    with _ledger(home) as c:
        runs = [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY rowid")]
        evs = [dict(r) for r in c.execute("SELECT type, run_id, data FROM events ORDER BY seq")]
        ops = [dict(r) for r in c.execute("SELECT tool, status FROM operations")]
        ctx = [json.loads(r["message"]) for r in c.execute("SELECT message FROM context ORDER BY pos")]
    data = lambda t: [json.loads(e["data"]) for e in evs if e["type"] == t]  # noqa: E731
    delivered = [d["content"] for d in data("delivery.settled") if d.get("destination") == "human"]
    authored = [d["content"] for d in data("message.completed")]
    usage = [d["usage"] for d in data("provider.request.finished")]
    print("\nruns:", [(r["status"], r["reason"], r["requests"]) for r in runs])
    print("delivered:", delivered)
    print("authored (private):", [a[:120] for a in authored])
    print("continuation cues:", sum(d.get("kind") == "continuation" for d in data("context.note")))
    print("totals:", {k: sum((u.get(k) or 0) for u in usage) for k in
                      ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens")},
          "requests:", len(usage))

    # Every run ended by the character's own explicit finish; nothing failed.
    assert [(r["status"], r["reason"]) for r in runs] == [("finished", "turn_finish")] * 3
    assert any(r["requests"] >= 3 for r in runs)  # multi-request run (tool → send → finish)
    # A real tool ran and its effect is on disk.
    assert ("file.write", "ok") in [(o["tool"], o["status"]) for o in ops]
    assert "teal" in (home / "workspace" / "notes" / "color.md").read_text().lower()
    # Only explicit sends reach the human; private authored text never does.
    assert len(delivered) == sum(1 for o in ops if o["tool"] == "message.send" and o["status"] == "ok")
    # The text it wrote as private raw thoughts never reached anyone. (A character may still
    # choose to send words it first wrote privately; every delivery is an explicit send.)
    thoughts = [a for a in authored if "raw" in a.lower() or len(a) > 300]
    assert all(t not in delivered for t in thoughts)
    # The conversation continued across the restart.
    assert "teal" in delivered[-1].lower()
    # Thinking-mode continuation state was kept and replayed without provider errors.
    assert any(m.get("reasoning_content") for m in ctx if m["role"] == "assistant")
