"""Graceful, supervised restarts.

Adapted from drain-aware gateway restarts (Hermes Agent): stop taking new work, let the
work in progress finish within a bound, record a durable restart intent, exit with a code
the supervisor restarts on, and on the next start report the outcome to whoever asked.

Whim never relaunches itself and never runs supervisor commands. It restarts only when
`systemd --user` supervises it (whim.service, Restart=on-failure): it exits with
EXIT_RESTART and systemd starts it again. A foreground `whim run` refuses, since nothing
would bring it back.

Sequence:
1. request    the intent (who, why, a note, the source version) is saved in the ledger
              meta table and published as restart.requested; the daemon starts draining:
              input is still accepted and stored, but no new wake or compaction starts.
2. drain      wait for the current wake to end, at most restart.drain_seconds.
3. preflight  a fresh interpreter loads the saved configuration, imports every whim
              module and sets up the provider (`whim config validate --startup`). If that fails (bad settings, broken source)
              nothing restarts: the intent is withdrawn (restart.cancelled), draining ends,
              and the character is told why. So a restart can't take whim offline because
              of a mistake the check can see.
4. exit       ordinary shutdown (shell sessions are killed and recorded), exit 76.
5. startup    the new process finds the intent, publishes restart.completed with what
              changed (source version, settings now in effect), and leaves the character
              a continuation note; if the character asked, it also gets a wake.

A crash at any point is handled by ordinary recovery; an intent found at startup is
always reported, whether the restart happened as planned or not.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from whim.context import block
from whim.ledger import dumps, new_id

if TYPE_CHECKING:
    from whim.harness import Harness

EXIT_RESTART = 76  # EX_PROTOCOL; restarted by Restart=on-failure (and RestartForceExitStatus)
META_KEY = "restart_intent"
PREFLIGHT_SECONDS = 90.0


class RestartUnavailable(Exception):
    pass


def supervised() -> bool:
    """Running as the systemd user service, which will start us again."""
    if os.environ.get("WHIM_SUPERVISOR") == "systemd":
        return True
    if not os.environ.get("INVOCATION_ID"):
        return False
    try:
        return "/whim.service" in Path("/proc/self/cgroup").read_text()
    except OSError:
        return False


def source_version() -> str:
    """A digest of the running package's Python source."""
    pkg = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(pkg.rglob("*.py")):
        h.update(str(p.relative_to(pkg)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:12]


async def preflight(home: Path) -> tuple[bool, str]:
    """Would a new daemon start? Run the check in a fresh interpreter, like the restart will."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "whim", "--home", str(home), "config", "validate", "--startup",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), PREFLIGHT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "the startup check didn't finish in time"
    except OSError as e:
        return False, f"the startup check couldn't run: {e}"
    text = out.decode(errors="replace").strip()
    return proc.returncode == 0, text[-4000:]


def record(h: "Harness", *, by: str, reason: str, note: str, run_id: str | None) -> dict:
    """Save the intent. Inside a transaction."""
    intent = {"id": new_id("rst"), "by": by, "reason": reason, "note": note, "run_id": run_id,
              "requested_at": h.time.iso(), "version": source_version()}
    h.ledger.x("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", META_KEY, dumps(intent))
    h.ledger.append("restart.requested", {k: intent[k] for k in ("id", "by", "reason", "version")}, run_id=run_id,
                    actor="character" if by == "character" else "human")
    return intent


def cancel(h: "Harness", intent: dict, why: str) -> None:
    led = h.ledger
    with led.tx():
        led.x("DELETE FROM meta WHERE key = ?", META_KEY)
        led.append("restart.cancelled", {"id": intent["id"], "reason": why[-2000:]})
        led.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'restart', ?, 'pending', ?)",
              new_id("note"), block(f"host · restart cancelled · {h.time.stamp()}",
                                    f"The restart requested by {_who(intent)} ({intent['reason']}) did not happen; "
                                    f"whim is still running the previous code and settings.\n{why}"),
              h.time.iso())


def complete(h: "Harness") -> dict | None:
    """At startup: report a restart intent left by the previous process. Inside a tx."""
    from whim import config as cfgmod
    from whim import settings

    led = h.ledger
    intent = led.get_meta(META_KEY)
    if not intent:
        return None
    led.x("DELETE FROM meta WHERE key = ?", META_KEY)
    version = source_version()
    try:
        saved = cfgmod.load(h.cfg.home)
        waiting = settings.pending_restart(h.cfg, saved)
    except Exception:  # noqa: BLE001 - we are running, so it loaded; just don't fail here
        waiting = []
    changed = version != intent.get("version")
    led.append("restart.completed", {"id": intent["id"], "by": intent["by"], "version_before": intent.get("version"),
                                     "version_after": version})
    lines = [f"Whim restarted at {h.time.stamp()}, as requested by {_who(intent)} at "
             f"{h.time.stamp(intent['requested_at'])} (reason: {intent['reason']})."]
    lines.append("The implementation changed: source version " f"{intent.get('version')} → {version}."
                 if changed else "The implementation is unchanged.")
    if waiting:
        lines.append("Still waiting for a restart (not in effect): " + ", ".join(waiting))
    else:
        lines.append("All saved settings are in effect.")
    if intent.get("note"):
        lines.append(f"Your note from before the restart: {intent['note']}")
    led.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'restart', ?, 'pending', ?)",
          new_id("note"), block(f"host · restarted · {h.time.stamp()}", "\n".join(lines)), h.time.iso())
    if intent["by"] == "character":
        wake_id = new_id("wake")
        led.x("INSERT INTO wakes(id, kind, status, created_at, data) VALUES(?, 'host', 'queued', ?, ?)",
              wake_id, h.time.iso(), dumps({"reason": "restarted", "text": ""}))
        led.append("wake.queued", {"wake_id": wake_id, "kind": "host", "reason": "restarted"})
    return intent


def _who(intent: dict) -> str:
    return "you" if intent["by"] == "character" else "your human"
