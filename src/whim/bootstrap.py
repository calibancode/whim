"""First-meeting bootstrap lifecycle (host state, not inferred from character files).

  pending  -> seeded by `whim init`; BOOTSTRAP.md exists but has not been shown
  active   -> shown once, as a host-attributed block, in the first wake that admits
              direct input from the designated human; it stays in the conversation
              history from then on, so restarts and later wakes don't repeat it
  complete -> the character called bootstrap_complete (or the operator removed the
              file before the meeting); BOOTSTRAP.md is deleted and never shown again

State lives in the ledger (`meta.bootstrap`) and changes only in transactions. Editing
IDENTITY.md, USER.md or any other file never changes it. Until it is `complete`,
heartbeat wakes are not admitted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from whim.context import block, char_file
from whim.ledger import Ledger, dumps

if TYPE_CHECKING:
    from whim.harness import Harness

FILE = "BOOTSTRAP.md"
PENDING, ACTIVE, COMPLETE = "pending", "active", "complete"


def state(h: "Harness") -> dict:
    st = h.ledger.get_meta("bootstrap")
    if st is None:  # home created before bootstrap tracking existed
        st = {"status": PENDING if (h.cfg.workspace / FILE).exists() else COMPLETE}
    return st


def is_complete(h: "Harness") -> bool:
    return state(h)["status"] == COMPLETE


def _set(led: Ledger, st: dict) -> None:
    led.x("INSERT OR REPLACE INTO meta(key, value) VALUES('bootstrap', ?)", dumps(st))


def opening_blocks(h: "Harness", run_id: str, admitted: list) -> list[str]:
    """Blocks to place before newly admitted inputs. Call inside the admitting transaction.

    Supplies BOOTSTRAP.md the first time direct human input is admitted, and again only
    if the context it was shown in has been replaced (compaction, Milestone 3)."""
    st = state(h)
    if st["status"] == COMPLETE or not any(r["source_kind"] == "direct" for r in admitted):
        return []
    if st["status"] == ACTIVE and st.get("epoch") == h.context.epoch():
        return []
    led = h.ledger
    text, file_state = char_file(h.cfg.workspace, FILE)
    if file_state == "unsafe":  # a link or special file is never read, and doesn't end the meeting either
        led.append("host.warning", {"kind": "bootstrap_file_unsafe",
                                    "message": "BOOTSTRAP.md is not a regular file; not shown"}, run_id=run_id)
        return []
    if text is None:
        _set(led, {**st, "status": COMPLETE, "completed_at": h.time.iso(), "reason": "file_removed_by_operator"})
        led.append("bootstrap.completed", {"reason": "file_removed_by_operator"}, run_id=run_id)
        led.append("host.warning", {"kind": "bootstrap_file_missing",
                                    "message": "BOOTSTRAP.md was removed before the first meeting; skipping it"})
        return []
    first = st["status"] == PENDING
    _set(led, {**st, "status": ACTIVE, "epoch": h.context.epoch(),
               "started_run": st.get("started_run", run_id), "shown_at": h.time.iso()})
    led.append("bootstrap.started" if first else "context.note",
               {"kind": "bootstrap_supplied", "resupplied": not first}, run_id=run_id)
    return [block(f"host · first meeting · from {FILE} (temporary) · {h.time.stamp()}", text.rstrip())]


def complete(h: "Harness", run_id: str, operation_id: str) -> tuple[bool, dict]:
    """Commit completion. Call inside the tool's transaction. Returns (ok, result data)."""
    st = state(h)
    if st["status"] == COMPLETE:
        return False, {"error": "your first meeting is already complete"}
    if st["status"] == PENDING:
        return False, {"error": "your first meeting hasn't started yet"}
    _set(h.ledger, {**st, "status": COMPLETE, "completed_at": h.time.iso(), "completed_run": run_id,
                    "operation_id": operation_id, "reason": "bootstrap_complete"})
    h.ledger.append("bootstrap.completed", {"reason": "bootstrap_complete", "operation_id": operation_id},
                    run_id=run_id, actor="character")
    return True, {"status": "complete", "note": f"{FILE} has been retired."}


def retire_file(h: "Harness") -> None:
    """Delete BOOTSTRAP.md once completion is durable. Idempotent; also run at startup
    to finish a retirement interrupted by a crash."""
    if is_complete(h):
        (h.cfg.workspace / FILE).unlink(missing_ok=True)  # unlink removes a link itself, never its target
