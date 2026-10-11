"""Startup reconciliation after an unclean stop.

For every run still marked `active`:
- tool operations that never dispatched are `not_dispatched` (nothing happened);
- dispatched read-only (replay-safe) calls are `interrupted` and may simply be repeated;
- dispatched sends are reconciled against their delivery record: a ledger-confirmed
  receipt is reused, an unconfirmed external dispatch becomes `uncertain`;
- any other dispatched effect is `uncertain`. Nothing is ever re-dispatched here.
- every tool call in the model context gets a paired, clearly host-attributed result,
  so the provider transcript stays valid;
- every outcome settled here is published like a live one: `tool.finished` with the
  result the character is shown (`recovered: true`), preceded by a `tool.started` for a
  call that was never recorded, so clients can reconstruct it from events alone;
- the run closes as `run.interrupted`; a later wake runs under a new run id.

Then a recovery note is queued for the character, and at most one recovery wake,
bounded across repeated crashes by `limits.max_recovery_attempts`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from whim import compaction, cron
from whim.context import block
from whim.ledger import dumps, new_id

if TYPE_CHECKING:
    from whim.harness import Harness

NOT_DISPATCHED = "Not executed: the harness stopped before this call was dispatched."
INTERRUPTED_SAFE = (
    "Did not complete: the harness stopped while this call was running. It is read-only, "
    "so repeating it is safe."
)
UNCERTAIN = (
    "Outcome unknown: the harness stopped while this call was running. It may or may not "
    "have taken effect. Check before repeating it."
)


def _result(ok: bool, **data) -> str:
    return json.dumps({"ok": ok, **data}, ensure_ascii=False)


def _finished(h: "Harness", run_id: str, op_id: str, call_id: str, tool: str, status: str, content: str,
              reason: str) -> None:
    """Publish a settled outcome with the same shape as a live tool.finished."""
    from whim.run import _preview

    data = json.loads(content)
    data.pop("ok", None)
    h.ledger.append("tool.finished", {"operation_id": op_id, "call_id": call_id, "tool": tool, "status": status,
                                      "result": _preview(data), "reason": reason, "recovered": True},
                    run_id=run_id)


def _resolve_op(h: "Harness", op) -> tuple[str, str]:
    """Terminal (status, model-facing result JSON) for a non-terminal operation."""
    led = h.ledger
    if op["status"] == "intent":
        return "not_dispatched", _result(False, outcome="not_dispatched", error=NOT_DISPATCHED)
    if op["effect"] == "send":
        d = led.q1("SELECT * FROM deliveries WHERE operation_id = ?", op["id"])
        if d is None:
            return "not_dispatched", _result(False, outcome="not_dispatched", error=NOT_DISPATCHED)
        if d["status"] == "sent":
            return "ok", _result(True, delivery_id=d["id"], status="sent", destination=d["destination"],
                                 note="Receipt confirmed during recovery.")
        if d["status"] == "failed":
            return "error", _result(False, delivery_id=d["id"], status="failed", destination=d["destination"])
        if d["status"] == "requested":
            led.x("UPDATE deliveries SET status = 'uncertain' WHERE id = ?", d["id"])
            led.append("delivery.settled", {"delivery_id": d["id"], "destination": d["destination"],
                                            "status": "uncertain", "reason": "unclean_shutdown"}, run_id=op["run_id"])
        return "uncertain", _result(False, outcome="uncertain", delivery_id=d["id"], destination=d["destination"],
                                    error=UNCERTAIN)
    tool = h.registry.by_impl(op["impl"])  # built-ins only: plugin tools never replay or reconcile
    if tool is not None and tool.reconcile is not None:
        return tool.reconcile(h, op)
    if op["replay_safe"]:
        # Safety is bound to the exact audited implementation that was dispatched.
        if tool is not None and tool.replay_safe:
            return "interrupted", _result(False, outcome="interrupted", error=INTERRUPTED_SAFE)
    return "uncertain", _result(False, outcome="uncertain", error=UNCERTAIN)


def _pair_dangling_calls(h: "Harness", run_id: str, reason: str) -> None:
    """Give every tool call in this run's context a tool result (appended in call order)."""
    led = h.ledger
    rows = led.q("SELECT pos, message FROM context WHERE run_id = ? ORDER BY pos", run_id)
    answered: set[str] = set()
    calls: list[tuple[int, int, dict]] = []
    for r in rows:
        m = json.loads(r["message"])
        if m["role"] == "assistant":
            for i, tc in enumerate(m.get("tool_calls") or []):
                calls.append((r["pos"], i, tc))
        elif m["role"] == "tool":
            answered.add(m["tool_call_id"])
    for pos, idx, tc in calls:
        if tc["id"] in answered:
            continue
        op = led.q1("SELECT * FROM operations WHERE context_pos = ? AND call_index = ?", pos, idx)
        if op is None:
            # The response committed but the call was never recorded: it never ran.
            from whim.run import _preview

            content = _result(False, outcome="not_dispatched", error=NOT_DISPATCHED)
            tool = h.registry.by_wire(tc["function"]["name"])
            name = tool.name if tool else tc["function"]["name"]
            op_id = new_id("op")
            led.x(
                "INSERT INTO operations(id, run_id, context_pos, call_index, call_id, tool, impl, effect, "
                "replay_safe, args, status, result) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'not_dispatched', ?)",
                op_id, run_id, pos, idx, tc["id"], name,
                tool.impl if tool else "unknown",
                tool.effect if tool else "control",
                tc["function"]["arguments"], content,
            )
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except ValueError:
                args = tc["function"]["arguments"]
            led.append("tool.started", {"operation_id": op_id, "call_id": tc["id"], "tool": name,
                                        "args": _preview(args), "recovered": True},
                       run_id=run_id, actor="character")
            _finished(h, run_id, op_id, tc["id"], name, "not_dispatched", content, reason)
        else:
            content = op["result"]
        h.context.append(run_id, {"role": "tool", "tool_call_id": tc["id"], "content": content})


def settle_dangling(h: "Harness", run_id: str, reason: str) -> list[dict]:
    """Give every tool call this run committed a recorded outcome: unsettled operations are
    resolved conservatively (never re-run) and calls without a result get one, so the
    provider transcript stays valid. Returns unresolved effects. Inside a transaction."""
    led = h.ledger
    unresolved: list[dict] = []
    for op in led.q("SELECT * FROM operations WHERE run_id = ? AND status IN ('intent', 'dispatched') ORDER BY rowid",
                    run_id):
        status, content = _resolve_op(h, op)
        led.x("UPDATE operations SET status = ?, result = ? WHERE id = ?", status, content, op["id"])
        _finished(h, run_id, op["id"], op["call_id"], op["tool"], status, content, reason)
        if status in ("uncertain", "interrupted"):
            args = json.loads(op["args"] or "{}")
            if not isinstance(args, dict):
                args = {}
            unresolved.append({"operation_id": op["id"], "tool": op["tool"], "status": status,
                               "args": {k: (v[:120] + "…" if isinstance(v, str) and len(v) > 120 else v)
                                        for k, v in args.items()}})
    _pair_dangling_calls(h, run_id, reason)
    return unresolved


def close_run(h: "Harness", run, reason: str) -> tuple[list[dict], list[str]]:
    """Close a run that stopped without finishing (crash, or the human's interrupt): settle
    its tool calls, mark it interrupted. Never re-runs anything. Returns (unresolved
    effects, notice lines). Inside a transaction."""
    led = h.ledger
    unresolved = settle_dangling(h, run["id"], reason)
    led.x("UPDATE runs SET status = 'interrupted', ended_at = ?, reason = ? WHERE id = ?", h.time.iso(), reason, run["id"])
    origin = json.loads(run["origin"])
    if compaction.is_maintenance(origin):
        compaction.on_run_end(h, origin, run["id"], "interrupted", reason)
    led.append("run.interrupted", {"reason": reason, "unresolved": unresolved}, run_id=run["id"])
    cron.record_outcome(h, origin, run["id"], "interrupted", reason)
    current = led.q1("SELECT requests, tool_calls FROM runs WHERE id = ?", run["id"])
    lines = [f"- Wake {run['id']} (started {h.time.stamp(run['started_at'])}) was cut off after "
             f"{current['requests']} model request(s) and {current['tool_calls']} completed tool call(s)."]
    for u in unresolved:
        what = ("may or may not have taken effect" if u["status"] == "uncertain"
                else "did not finish (read-only, safe to repeat)")
        lines.append(f"  - {u['tool']} {dumps(u['args'])}: {what}")
    return unresolved, lines


def reconcile(h: "Harness") -> list[str]:
    """Close runs left active by an unclean stop. Returns their ids."""
    led = h.ledger
    stale = led.q("SELECT * FROM runs WHERE status = 'active' ORDER BY started_at")
    if not stale:
        return []
    with led.tx():
        lines: list[str] = []
        unresolved_all: list[dict] = []
        for run in stale:
            unresolved, run_lines = close_run(h, run, "unclean_shutdown")
            if compaction.is_maintenance(json.loads(run["origin"])):
                continue  # its retry (queued by close_run) tells the character what was saved
            lines += run_lines
            unresolved_all += unresolved

        if not lines:
            return [r["id"] for r in stale]
        text = block(
            f"host · recovery notice · {h.time.stamp()}",
            "The harness stopped unexpectedly while you were in the middle of something. "
            "The interrupted wake was closed; this is a new one.\n"
            + "\n".join(lines)
            + "\nNothing was retried automatically. Inspect, continue, or let it go as you see fit.",
        )
        note_id = new_id("note")
        led.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'recovery', ?, 'pending', ?)",
              note_id, text, h.time.iso())
        led.append("recovery.pending", {"note_id": note_id, "run_ids": [r["id"] for r in stale],
                                        "unresolved": unresolved_all})

        attempts = led.get_meta("recovery_attempts", 0)
        queued = led.q1("SELECT id FROM wakes WHERE kind = 'recovery' AND status = 'queued'")
        if queued is not None:
            pass  # one recovery wake at most; the queued one will carry the new note too
        elif attempts >= h.cfg.limits.max_recovery_attempts:
            led.append("host.warning", {"kind": "recovery_suspended", "attempts": attempts,
                                        "message": "automatic recovery wakes suspended after repeated crashes; "
                                                   "the notice will ride on the next ordinary wake"})
        else:
            wake_id = new_id("wake")
            led.x("INSERT INTO wakes(id, kind, status, created_at, data) VALUES(?, 'recovery', 'queued', ?, ?)",
                  wake_id, h.time.iso(), dumps({"origin": {"resumes_run_id": stale[-1]["id"]}}))
            led.append("wake.queued", {"wake_id": wake_id, "kind": "recovery"})
            led.x("INSERT OR REPLACE INTO meta(key, value) VALUES('recovery_attempts', ?)", dumps(attempts + 1))
    return [r["id"] for r in stale]
