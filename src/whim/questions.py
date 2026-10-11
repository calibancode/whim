"""human.ask: asynchronous questions to the designated human.

Asking is an explicit outbound action: a durable question record plus an ordinary
delivery of its text (and numbered choices) to the human. The tool returns a receipt at
once; the character keeps working or finishes. Nothing waits.

An answer comes only through the authenticated control path (`whim answer`, the direct
frontend later). It becomes an ordinary attributed message from the human, linked to the
question: admitted at the next safe point if a run is active, or starting a new wake in
the same conversation otherwise. Questions can be cancelled by the character or expire;
late, duplicate, misdirected or invented answers are refused.

Answering a question never grants anything: permissions change only through approvals.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from whim.context import block
from whim.ledger import dumps, new_id
from whim.timebase import parse
from whim.tools.registry import ToolResult

if TYPE_CHECKING:
    from whim.harness import Harness
    from whim.run import RunState

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions(
  id TEXT PRIMARY KEY,
  run_id TEXT,
  operation_id TEXT,
  recipient TEXT NOT NULL,           -- 'human' (the designated human) in v1
  text TEXT NOT NULL,
  choices TEXT NOT NULL,
  status TEXT NOT NULL,              -- pending | answered | cancelled | expired
  delivery_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  closed_at TEXT,
  answer_input_id TEXT,
  answer_choice TEXT,
  answer_text TEXT
);
"""


class QuestionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(description="What you want to ask.")
    choices: list[str] = Field(default_factory=list,
                               description="Optional answers to pick from (2-5). They can always answer in their own words.")
    expires_in_hours: float | None = Field(default=None, description="Optional: stop accepting answers after this long.")


class CancelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str


def ensure_schema(led) -> None:
    led.db.executescript(SCHEMA)


def repair_links(h: "Harness") -> int:
    """Link questions to the delivery that carried them where a crash in an earlier
    version left the link unset (the ask and its delivery share one operation)."""
    with h.ledger.tx():
        return h.ledger.x(
            "UPDATE questions SET delivery_id = (SELECT d.id FROM deliveries d WHERE d.operation_id = "
            "questions.operation_id AND d.destination = 'human' ORDER BY d.rowid LIMIT 1) "
            "WHERE delivery_id IS NULL AND operation_id IS NOT NULL AND EXISTS (SELECT 1 FROM deliveries d "
            "WHERE d.operation_id = questions.operation_id AND d.destination = 'human')").rowcount


def render(text: str, choices: list[str]) -> str:
    if not choices:
        return text
    return text + "\n\n" + "\n".join(f"  {i}. {c}" for i, c in enumerate(choices, 1))


async def ask(state: "RunState", args: AskArgs) -> ToolResult:
    h = state.harness
    text = args.question.strip()
    choices = [c.strip() for c in args.choices if c.strip()]
    if not text or len(text) > 2000:
        return ToolResult(False, {"error": "the question must be 1-2000 characters"})
    if choices and not 2 <= len(choices) <= 5:
        return ToolResult(False, {"error": "give 2-5 choices, or none"})
    if len(set(c.casefold() for c in choices)) != len(choices) or any(len(c) > 200 for c in choices):
        return ToolResult(False, {"error": "choices must be distinct and at most 200 characters"})
    if args.expires_in_hours is not None and not 0 < args.expires_in_hours <= 24 * 90:
        return ToolResult(False, {"error": "expires_in_hours must be between 0 and 2160"})
    qid = new_id("q")
    now = h.time.now()
    expires = h.time.iso(now + timedelta(hours=args.expires_in_hours)) if args.expires_in_hours else None
    with h.ledger.tx():
        h.ledger.x("INSERT INTO questions(id, run_id, operation_id, recipient, text, choices, status, created_at, "
                   "expires_at) VALUES(?, ?, ?, 'human', ?, ?, 'pending', ?, ?)",
                   qid, state.run_id, state.operation_id, text, dumps(choices), h.time.iso(now), expires)
        h.ledger.append("question.created", {"question_id": qid, "text": text, "choices": choices,
                                             "expires_at": expires}, run_id=state.run_id, actor="character",
                        audience="human")
    await h.deliveries.send(state, state.operation_id, "human", render(text, choices), None,
                            question_id=qid)  # also links the delivery to the question
    await h.fault("question.after_delivery")
    return ToolResult(True, {"question_id": qid, "status": "asked",
                             "note": "Their answer will come to you as a message, whenever they give it; "
                                     "nothing needs to wait for it."})


async def cancel(state: "RunState", args: CancelArgs) -> ToolResult:
    h = state.harness
    with h.ledger.tx():
        row = h.ledger.q1("SELECT * FROM questions WHERE id = ?", args.question_id)
        if row is None:
            return ToolResult(False, {"error": f"no question {args.question_id!r}"})
        if row["status"] != "pending":
            return ToolResult(False, {"error": f"{args.question_id} is already {row['status']}"})
        h.ledger.x("UPDATE questions SET status = 'cancelled', closed_at = ? WHERE id = ?", h.time.iso(), row["id"])
        h.ledger.append("question.cancelled", {"question_id": row["id"]}, run_id=state.run_id, actor="character",
                        audience="human")
    return ToolResult(True, {"question_id": row["id"], "status": "cancelled"})


def _expire_if_due(h: "Harness", row) -> bool:
    if row["status"] == "pending" and row["expires_at"] and parse(row["expires_at"]) <= h.time.now():
        h.ledger.x("UPDATE questions SET status = 'expired', closed_at = ? WHERE id = ?", h.time.iso(), row["id"])
        h.ledger.append("question.expired", {"question_id": row["id"]}, audience="human")
        h.ledger.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'question', ?, 'pending', ?)",
                   new_id("note"), block(f"host · question expired · {h.time.stamp()}",
                                         f"Your question {row['id']} (\"{row['text'][:200]}\") expired unanswered."),
                   h.time.iso())
        return True
    return False


def expire_due(h: "Harness") -> int:
    """Expire overdue questions (called by the scheduler tick)."""
    n = 0
    with h.ledger.tx():
        for row in h.ledger.q("SELECT * FROM questions WHERE status = 'pending' AND expires_at IS NOT NULL"):
            n += _expire_if_due(h, row)
    return n


def answer(h: "Harness", question_id: str, principal_id: str, *, choice: str | int | None = None,
           text: str | None = None, client_message_id: str | None = None) -> dict:
    """The designated human answers (authenticated control path only)."""
    led = h.ledger
    if principal_id != h.cfg.principal_id:
        raise QuestionError("only the designated human can answer these questions", 403)
    with led.tx():  # an expiry that is due is recorded even though this answer is refused
        row = led.q1("SELECT * FROM questions WHERE id = ?", question_id)
        expired = row is not None and _expire_if_due(h, row)
    if expired:
        raise QuestionError(f"{question_id} has expired", 409)
    with led.tx():
        row = led.q1("SELECT * FROM questions WHERE id = ?", question_id)
        if row is None:
            raise QuestionError(f"no question {question_id!r}", 404)
        if row["status"] != "pending":
            raise QuestionError(f"{question_id} is already {row['status']}", 409)
        choices = json.loads(row["choices"])
        picked = None
        if choice not in (None, ""):
            if isinstance(choice, int) or str(choice).isdigit():
                i = int(choice)
                if not 1 <= i <= len(choices):
                    raise QuestionError(f"choice must be 1-{len(choices)}" if choices else "this question has no choices")
                picked = choices[i - 1]
            else:
                match = [c for c in choices if c.casefold() == str(choice).strip().casefold()]
                if not match:
                    raise QuestionError("that isn't one of the choices; answer in your own words with text instead")
                picked = match[0]
        words = (text or "").strip()
        if picked is None and not words:
            raise QuestionError("give a choice or some text")
        quoted = row["text"] if len(row["text"]) <= 200 else row["text"][:200] + "…"
        body = f"(answering your question: \"{quoted}\")\n"
        body += (f"{picked}" + (f" — {words}" if words else "")) if picked else words
        rec, dup = h.submit_input(body, principal_id=principal_id, question_id=question_id,
                                  source_message_id=client_message_id or f"answer:{question_id}")
        led.x("UPDATE questions SET status = 'answered', closed_at = ?, answer_input_id = ?, answer_choice = ?, "
              "answer_text = ? WHERE id = ?", h.time.iso(), rec["id"], picked, words or None, question_id)
        led.append("question.answered", {"question_id": question_id, "input_id": rec["id"], "choice": picked},
                   actor="human", audience="human", origin={"kind": "control", "principal_id": principal_id})
    return {"question_id": question_id, "input_id": rec["id"], "choice": picked}


def listing(h: "Harness", status: str = "pending") -> list[dict]:
    expire_due(h)
    rows = h.ledger.q("SELECT id, text, choices, status, created_at, expires_at FROM questions WHERE status = ? "
                      "ORDER BY created_at, rowid", status)
    return [{**dict(r), "choices": json.loads(r["choices"])} for r in rows]
