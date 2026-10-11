"""Character-managed scheduled jobs: one-shot (`at` / `in_minutes`) or recurring (5-field
cron in the configured local time zone).

Time rules (host-owned; documented choices):
- Recurring schedules are evaluated on local wall-clock time in `character.timezone`.
- A local time that doesn't exist (spring-forward gap) runs at the same offset past
  the gap, e.g. 02:30 runs at 03:30. A local time that occurs twice (fall-back) runs
  once, at its first occurrence.
- One-shot `at` times are stored as absolute instants when created. A nonexistent
  local `at` time is rejected; an ambiguous one means the first occurrence.
- Recurring jobs must fire at least 5 minutes apart and within 4 years; one-shots
  must be in the future and within 4 years.
- At most one pending execution per job. Missed recurrences coalesce into it, keeping
  only the latest due occurrence, with no backfill. An overdue one-shot runs once.
- No automatic retries after a job's run is admitted: its outcome (finished, failed,
  interrupted...) is recorded on the job, and the character can see it and decide.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from whim.ledger import Ledger, dumps
from whim.timebase import iso, parse

if TYPE_CHECKING:
    from whim.harness import Harness

MIN_INTERVAL = timedelta(minutes=5)
HORIZON_DAYS = 4 * 366
MAX_JOBS = 50
MAX_INSTRUCTION_BYTES = 64 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  instruction TEXT NOT NULL,
  kind TEXT NOT NULL,                -- once | cron
  spec TEXT NOT NULL,                -- ISO instant (once) or cron expression
  status TEXT NOT NULL,              -- active | paused | done | failed | removed
  next_due_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  created_rev INTEGER NOT NULL,
  updated_rev INTEGER NOT NULL,
  fires INTEGER NOT NULL DEFAULT 0,
  last_fired_at TEXT,
  last_run_id TEXT,
  last_outcome TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS job_history(
  rev INTEGER NOT NULL,
  job_id TEXT NOT NULL,
  op TEXT NOT NULL,
  before TEXT,
  after TEXT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  run_id TEXT
);
"""


def ensure_schema(led: Ledger) -> None:
    led.db.executescript(SCHEMA)


# -- cron expressions ----------------------------------------------------------

MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
          "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}


class ScheduleError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> tuple[frozenset[int], bool]:
    """Parse one cron field; returns (values, restricted)."""
    values: set[int] = set()
    for part in text.lower().split(","):
        m = re.fullmatch(r"(\*|[a-z0-9]+(?:-[a-z0-9]+)?)(?:/(\d+))?", part)
        if not m:
            raise ScheduleError(f"bad cron field {text!r}")
        rng, step = m.group(1), int(m.group(2) or 1)
        if step < 1:
            raise ScheduleError(f"bad step in {text!r}")

        def num(tok: str) -> int:
            if names and tok in names:
                return names[tok]
            if not tok.isdigit():
                raise ScheduleError(f"bad value {tok!r} in {text!r}")
            return int(tok)

        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (num(t) for t in rng.split("-"))
        else:
            a = b = num(rng)
            if m.group(2):
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ScheduleError(f"{text!r} is out of range {lo}-{hi}")
        values.update(range(a, b + 1, step))
    return frozenset(values), text != "*"


class CronExpr:
    def __init__(self, text: str):
        self.text = text.strip()
        fields = MACROS.get(self.text.lower(), self.text).split()
        if len(fields) != 5:
            raise ScheduleError("a cron expression has 5 fields: minute hour day-of-month month day-of-week")
        self.minutes, _ = _field(fields[0], 0, 59)
        self.hours, _ = _field(fields[1], 0, 23)
        self.doms, self.dom_r = _field(fields[2], 1, 31)
        self.months, _ = _field(fields[3], 1, 12, MONTHS)
        dows, self.dow_r = _field(fields[4], 0, 7, DAYS)
        self.dows = frozenset(d % 7 for d in dows)

    def _day_ok(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok, dow_ok = d.day in self.doms, (d.isoweekday() % 7) in self.dows
        if self.dom_r and self.dow_r:  # classic cron: either matches
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def _day_instants(self, d: date, tz) -> list[datetime]:
        out = {datetime.combine(d, time(h, m), tzinfo=tz).astimezone(timezone.utc)
               for h in self.hours for m in self.minutes}
        return sorted(out)

    def occurrences_after(self, after: datetime, tz) -> Iterator[datetime]:
        """Occurrences strictly after `after`, in order (UTC instants)."""
        day = after.astimezone(tz).date() - timedelta(days=1)  # DST shifts can cross midnight
        for _ in range(HORIZON_DAYS + 2):
            if self._day_ok(day):
                for inst in self._day_instants(day, tz):
                    if inst > after:
                        yield inst
            day += timedelta(days=1)

    def next_after(self, after: datetime, tz) -> datetime | None:
        return next(self.occurrences_after(after, tz), None)

    def latest_at_or_before(self, now: datetime, tz) -> datetime | None:
        day = now.astimezone(tz).date() + timedelta(days=1)
        for _ in range(HORIZON_DAYS + 2):
            if self._day_ok(day):
                past = [i for i in self._day_instants(day, tz) if i <= now]
                if past:
                    return past[-1]
            day -= timedelta(days=1)
        return None


def validate_cron(text: str, now: datetime, tz) -> CronExpr:
    expr = CronExpr(text)
    occ = list(_take(expr.occurrences_after(now, tz), 30))
    if not occ:
        raise ScheduleError("that cron expression never fires")
    if any(b - a < MIN_INTERVAL for a, b in zip(occ, occ[1:])):
        raise ScheduleError("recurring jobs must be at least 5 minutes apart")
    return expr


def _take(it: Iterator, n: int):
    for i, x in enumerate(it):
        if i >= n:
            return
        yield x


def parse_at(text: str, now: datetime, tz) -> datetime:
    s = text.strip().replace("T", " ")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise ScheduleError('use a local date and time like "2026-10-11 09:00"') from None
    if dt.tzinfo is None:
        local = dt.replace(tzinfo=tz, fold=0)
        roundtrip = local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if roundtrip != dt:
            raise ScheduleError(f"{text} doesn't exist in {tz} (clocks skip it); pick another time")
        dt = local
    dt = dt.astimezone(timezone.utc)
    if dt <= now:
        raise ScheduleError(f"{text} has already passed")
    if dt - now > timedelta(days=HORIZON_DAYS):
        raise ScheduleError("that is too far ahead")
    return dt


# -- tool operations -----------------------------------------------------------


class JobOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add", "update", "pause", "resume", "remove"]
    id: str | None = Field(default=None, description="Job id; required for everything except add.")
    instruction: str | None = Field(default=None, description="What the wake is for, in your own words.")
    at: str | None = Field(default=None, description='One-shot local date/time, e.g. "2026-10-11 09:00".')
    in_minutes: float | None = Field(default=None, description="One-shot, this many minutes from now.")
    cron: str | None = Field(default=None, description='Recurring, 5-field cron in local time, e.g. "0 9 * * 1-5".')


def revision(led: Ledger) -> int:
    return led.get_meta("cron_revision", 0)


def _timing(o: JobOp, now: datetime, tz) -> tuple[str, str, datetime] | None:
    try:
        return _timing_unchecked(o, now, tz)
    except (OverflowError, ValueError) as e:  # e.g. a date at the edge of what can be represented
        if isinstance(e, ScheduleError):
            raise
        raise ScheduleError(f"that time can't be scheduled ({e})") from None


def _timing_unchecked(o: JobOp, now: datetime, tz) -> tuple[str, str, datetime] | None:
    given = [k for k in ("at", "in_minutes", "cron") if getattr(o, k) is not None]
    if not given:
        return None
    if len(given) > 1:
        raise ScheduleError("give only one of at, in_minutes, cron")
    if o.at is not None:
        due = parse_at(o.at, now, tz)
        return "once", iso(due), due
    if o.in_minutes is not None:
        if not 1 <= o.in_minutes <= HORIZON_DAYS * 1440:
            raise ScheduleError("in_minutes must be at least 1")
        due = now + timedelta(minutes=o.in_minutes)
        return "once", iso(due), due
    expr = validate_cron(o.cron, now, tz)
    return "cron", expr.text, expr.next_after(now, tz)


def describe(job: dict, h: "Harness") -> dict:
    out = {
        "id": job["id"], "instruction": job["instruction"], "status": job["status"],
        "schedule": (f"once at {h.time.stamp(job['spec'])}" if job["kind"] == "once"
                     else f"cron \"{job['spec']}\" ({h.time.tz_name})"),
    }
    if job["next_due_at"]:
        out["next"] = h.time.stamp(job["next_due_at"])
    if h.ledger.q1("SELECT 1 FROM wakes WHERE kind = 'cron' AND status = 'queued' AND json_extract(data, '$.job_id') = ?",
                   job["id"]):
        out["pending"] = True
    if job["last_fired_at"]:
        out["last_run"] = {"at": h.time.stamp(job["last_fired_at"]), "outcome": job["last_outcome"] or "running"}
    if job["error"]:
        out["error"] = job["error"]
    return out


def listing(h: "Harness") -> list[dict]:
    led = h.ledger
    rows = [dict(r) for r in led.q("SELECT * FROM jobs WHERE status IN ('active', 'paused', 'failed') ORDER BY created_rev")]
    done = [dict(r) for r in led.q("SELECT * FROM jobs WHERE status = 'done' ORDER BY updated_rev DESC LIMIT 5")]
    return [describe(j, h) for j in rows + done[::-1]]


def cancel_pending(h: "Harness", job_id: str, reason: str) -> None:
    led = h.ledger
    for w in led.q("SELECT id FROM wakes WHERE kind = 'cron' AND status = 'queued' AND json_extract(data, '$.job_id') = ?",
                   job_id):
        led.x("UPDATE wakes SET status = 'cancelled' WHERE id = ?", w["id"])
        led.append("wake.cancelled", {"wake_id": w["id"], "job_id": job_id, "reason": reason})


def apply_ops(h: "Harness", ops: list[JobOp], *, actor: str, run_id: str | None,
              expected_revision: int | None) -> tuple[int, list[dict]]:
    """All or nothing; call inside a transaction (the caller rolls back on ScheduleError)."""
    led = h.ledger
    now, tz = h.time.now(), h.time.tz
    rev = revision(led)
    if expected_revision is not None and expected_revision != rev:
        raise ScheduleError(f"conflict: jobs are at revision {rev}, not {expected_revision}; review and retry")
    if not ops:
        return rev, []
    new_rev, stamp = rev + 1, iso(now)
    results = []
    for i, o in enumerate(ops, 1):
        where = f"operation {i} ({o.op})"
        try:
            timing = _timing(o, now, tz) if o.op in ("add", "update") else None
        except ScheduleError as e:
            raise ScheduleError(f"{where}: {e}") from None
        if o.op == "add":
            text = (o.instruction or "").strip()
            if not text:
                raise ScheduleError(f"{where}: instruction is required")
            if timing is None:
                raise ScheduleError(f"{where}: give one of at, in_minutes, cron")
            n = led.get_meta("cron_next_id", 1)
            job_id = f"cr_{n}"
            led.x("INSERT OR REPLACE INTO meta(key, value) VALUES('cron_next_id', ?)", dumps(n + 1))
            kind, spec, due = timing
            led.x("INSERT INTO jobs(id, instruction, kind, spec, status, next_due_at, created_at, updated_at, "
                  "created_rev, updated_rev) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                  job_id, text, kind, spec, iso(due), stamp, stamp, new_rev, new_rev)
            before = None
        else:
            if not o.id:
                raise ScheduleError(f"{where}: id is required")
            row = led.q1("SELECT * FROM jobs WHERE id = ? AND status != 'removed'", o.id)
            if row is None:
                raise ScheduleError(f"{where}: no job {o.id!r}")
            job_id, job = o.id, dict(row)
            before = {k: job[k] for k in ("instruction", "kind", "spec", "status", "next_due_at")}
            if o.op == "update":
                if o.instruction is None and timing is None:
                    raise ScheduleError(f"{where}: give a new instruction and/or timing")
                if o.instruction is not None:
                    if not o.instruction.strip():
                        raise ScheduleError(f"{where}: instruction is empty; use remove instead")
                    job["instruction"] = o.instruction.strip()
                if timing is not None:
                    job["kind"], job["spec"], due = timing
                    job["next_due_at"] = iso(due)
                    cancel_pending(h, job_id, "rescheduled")
                    if job["status"] in ("done", "failed"):
                        job["status"], job["error"] = "active", None
            elif o.op == "pause":
                job["status"] = "paused"
                cancel_pending(h, job_id, "paused")
            elif o.op == "resume":
                if job["status"] != "paused":
                    raise ScheduleError(f"{where}: {job_id} isn't paused")
                job["status"] = "active"
                if job["kind"] == "cron":  # no backfill for the paused stretch
                    nxt = CronExpr(job["spec"]).next_after(now, tz)
                    job["next_due_at"] = iso(nxt) if nxt else None
                elif job["next_due_at"] is None and job["fires"] == 0:
                    job["next_due_at"] = job["spec"]  # an overdue one-shot still runs once
            elif o.op == "remove":
                job["status"] = "removed"
                job["next_due_at"] = None
                cancel_pending(h, job_id, "removed")
            led.x("UPDATE jobs SET instruction = ?, kind = ?, spec = ?, status = ?, next_due_at = ?, error = ?, "
                  "updated_at = ?, updated_rev = ? WHERE id = ?",
                  job["instruction"], job["kind"], job["spec"], job["status"], job["next_due_at"],
                  job.get("error"), stamp, new_rev, job_id)
        after_row = dict(led.q1("SELECT instruction, kind, spec, status, next_due_at FROM jobs WHERE id = ?", job_id))
        led.x("INSERT INTO job_history(rev, job_id, op, before, after, at, actor, run_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
              new_rev, job_id, o.op, dumps(before) if before else None, dumps(after_row), stamp, actor, run_id)
        results.append({"op": o.op, "id": job_id})
    live = led.q1("SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(CAST(instruction AS BLOB))), 0) AS b FROM jobs "
                  "WHERE status IN ('active', 'paused')")
    if live["n"] > MAX_JOBS:
        raise ScheduleError(f"at most {MAX_JOBS} active or paused jobs")
    if live["b"] > MAX_INSTRUCTION_BYTES:
        raise ScheduleError("job instructions are too long in total; keep them short and point to a file")
    led.x("INSERT OR REPLACE INTO meta(key, value) VALUES('cron_revision', ?)", dumps(new_rev))
    led.append("jobs.changed", {"revision": new_rev, "actor": actor, "operations": results}, run_id=run_id, actor=actor)
    return new_rev, results


def record_outcome(h: "Harness", origin: dict, run_id: str, status: str, reason: str | None) -> None:
    """Note how a job's run ended. Call inside the transaction that ends the run."""
    if origin.get("kind") == "cron" and origin.get("job_id"):
        outcome = status if status == "finished" else f"{status} ({reason})"
        h.ledger.x("UPDATE jobs SET last_outcome = ? WHERE id = ? AND last_run_id = ?", outcome, origin["job_id"], run_id)


__all__ = ["CronExpr", "JobOp", "ScheduleError", "apply_ops", "listing", "parse", "record_outcome"]
