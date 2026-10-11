"""The host scheduler: heartbeats and character-managed jobs, one clock, one wake queue.

Both kinds of schedule only *queue wakes*; the worker admits them in priority order
(human input, recovery, manual host wakes, due jobs, then heartbeats) and never
preempts a running turn. Admission is the fire: it is recorded in the run's opening
transaction (`schedule.fired`, emitted right after `run.started`).

Heartbeats: armed one interval after the first meeting completes; one wake at most;
misses coalesce; the next one is due an interval after actual admission.
Jobs: see `whim.cron`. One pending wake per job; missed recurrences coalesce to the
latest due occurrence; overdue one-shots run once.
"""

from __future__ import annotations

import json
import math
from datetime import timedelta
from itertools import takewhile
from typing import TYPE_CHECKING

from whim import bootstrap, cron, heartbeat
from whim.context import block
from whim.ledger import dumps, new_id
from whim.timebase import iso, parse

if TYPE_CHECKING:
    from whim.harness import Harness

MISSED_COUNT_CAP = 1000
JOB_INSTRUCTION = "Act on it as you see fit."


class Scheduler:
    def __init__(self, h: "Harness"):
        self.h = h

    # -- heartbeat state -----------------------------------------------------

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=self.h.cfg.heartbeat.interval_minutes)

    def state(self) -> dict:
        """Heartbeat schedule state."""
        return self.h.ledger.get_meta("heartbeat", {})

    def _save(self, st: dict) -> None:
        self.h.ledger.x("INSERT OR REPLACE INTO meta(key, value) VALUES('heartbeat', ?)", dumps(st))

    def queued_wake(self):
        return self.h.ledger.q1("SELECT * FROM wakes WHERE kind = 'heartbeat' AND status = 'queued' LIMIT 1")

    # -- ticking -------------------------------------------------------------

    def tick(self) -> float:
        """Advance every schedule to the current time. Returns seconds until the next
        tick is needed."""
        from whim import questions

        if questions.expire_due(self.h) or self.h.ledger.get_meta("context_blocked"):
            self.h._nudge()  # (re)check admission, e.g. once a compaction backoff has expired
        return min(self._tick_heartbeat(), self._tick_jobs())

    def _tick_heartbeat(self) -> float:
        h, led = self.h, self.h.ledger
        if not h.heartbeats_enabled or not bootstrap.is_complete(h):
            return 60.0
        now = h.time.now()
        st = self.state()
        if not st.get("next_due_at"):
            with led.tx():
                st = {**st, "next_due_at": iso(now + self.interval)}
                self._save(st)
                led.append("schedule.armed", {"schedule": "heartbeat", "next_due_at": st["next_due_at"],
                                              "interval_minutes": h.cfg.heartbeat.interval_minutes})
            return self.interval.total_seconds()
        due = parse(st["next_due_at"])
        if now < due:
            return (due - now).total_seconds()
        missed = 1 + math.floor((now - due) / self.interval)
        wake = self.queued_wake()
        with led.tx():
            if wake is None:
                wake_id = new_id("wake")
                data = {"occurrence_id": new_id("hbo"), "due_at": st["next_due_at"], "missed": missed}
                led.x("INSERT INTO wakes(id, kind, status, created_at, data) VALUES(?, 'heartbeat', 'queued', ?, ?)",
                      wake_id, iso(now), dumps(data))
                led.append("schedule.due", {"schedule": "heartbeat", "wake_id": wake_id, **data})
            else:
                data = json.loads(wake["data"])
                if missed > data.get("missed", 1):  # coalesce into the waiting wake
                    led.x("UPDATE wakes SET data = ? WHERE id = ?", dumps({**data, "missed": missed}), wake["id"])
                    led.append("schedule.coalesced", {"schedule": "heartbeat", "wake_id": wake["id"], "missed": missed})
        h._nudge()
        return 60.0

    def _tick_jobs(self) -> float:
        h, led = self.h, self.h.ledger
        now, tz = h.time.now(), h.time.tz
        soonest = 60.0
        queued_any = False
        for job in [dict(r) for r in led.q("SELECT * FROM jobs WHERE status = 'active' AND next_due_at IS NOT NULL")]:
            due = parse(job["next_due_at"])
            if due > now:
                soonest = min(soonest, (due - now).total_seconds())
                continue
            with led.tx():
                if job["kind"] == "once":
                    latest, count, nxt = due, 1, None
                else:
                    try:
                        expr = cron.CronExpr(job["spec"])
                    except cron.ScheduleError as e:  # a stored job that no longer parses: quarantine it
                        led.x("UPDATE jobs SET status = 'failed', error = ?, next_due_at = NULL WHERE id = ?",
                              str(e), job["id"])
                        cron.cancel_pending(h, job["id"], "invalid schedule")
                        led.append("host.warning", {"kind": "job_invalid", "job_id": job["id"], "message": str(e)})
                        continue
                    latest = expr.latest_at_or_before(now, tz) or due
                    later = takewhile(lambda t: t <= now, expr.occurrences_after(due, tz))
                    count = 1 + sum(1 for _, _x in zip(range(MISSED_COUNT_CAP), later))
                    nxt = expr.next_after(now, tz)
                wake = led.q1("SELECT * FROM wakes WHERE kind = 'cron' AND status = 'queued' "
                              "AND json_extract(data, '$.job_id') = ?", job["id"])
                if wake is None:
                    wake_id = new_id("wake")
                    data = {"job_id": job["id"], "occurrence_id": new_id("cro"), "due_at": iso(latest), "missed": count}
                    led.x("INSERT INTO wakes(id, kind, status, created_at, data) VALUES(?, 'cron', 'queued', ?, ?)",
                          wake_id, iso(now), dumps(data))
                    led.append("schedule.due", {"schedule": "cron", "wake_id": wake_id, **data})
                else:
                    data = json.loads(wake["data"])
                    data.update(due_at=iso(latest), missed=data.get("missed", 1) + count)
                    led.x("UPDATE wakes SET data = ? WHERE id = ?", dumps(data), wake["id"])
                    led.append("schedule.coalesced", {"schedule": "cron", "wake_id": wake["id"], "job_id": job["id"],
                                                      "due_at": data["due_at"], "missed": data["missed"]})
                led.x("UPDATE jobs SET next_due_at = ? WHERE id = ?", iso(nxt) if nxt else None, job["id"])
                queued_any = True
                if nxt is not None:
                    soonest = min(soonest, (nxt - now).total_seconds())
        if queued_any:
            h._nudge()
        return soonest

    # -- admission -----------------------------------------------------------

    def admit(self, run_id: str, wake, data: dict) -> tuple[str, dict, dict]:
        """For a scheduled wake being admitted: (opening block, origin extras,
        schedule.fired data). Call inside the run-opening transaction."""
        if wake["kind"] == "heartbeat":
            return self._admit_heartbeat(run_id, wake, data)
        return self._admit_job(run_id, wake, data)

    def _admit_heartbeat(self, run_id, wake, data):
        h, led = self.h, self.h.ledger
        now = h.time.now()
        rev, digest = heartbeat.revision(led), heartbeat.cue_digest(led)
        st = self.state()
        st = {**st, "next_due_at": iso(now + self.interval), "last_fired_at": iso(now),
              "last_run_id": run_id, "fires": st.get("fires", 0) + 1}
        self._save(st)
        due = parse(data["due_at"]) if data.get("due_at") else now
        fired = {
            "schedule": "heartbeat", "wake_id": wake["id"], "occurrence_id": data.get("occurrence_id"),
            "due_at": data.get("due_at"), "missed": data.get("missed", 1),
            "delay_seconds": round((now - due).total_seconds(), 3), "next_due_at": st["next_due_at"],
            "cue_revision": rev, "cue_digest": digest,
            "active_cues": len(heartbeat.cues(led, statuses=("active",))),
        }
        text = heartbeat.INSTRUCTION + "\n\n" + heartbeat.render_cues(led)
        return block(f"host · heartbeat · {h.time.stamp(now)}", text), {"cue_revision": rev}, fired

    def _admit_job(self, run_id, wake, data):
        h, led = self.h, self.h.ledger
        now = h.time.now()
        job = dict(led.q1("SELECT * FROM jobs WHERE id = ?", data["job_id"]))
        led.x("UPDATE jobs SET fires = fires + 1, last_fired_at = ?, last_run_id = ?, last_outcome = NULL, "
              "status = CASE kind WHEN 'once' THEN 'done' ELSE status END WHERE id = ?", iso(now), run_id, job["id"])
        due = parse(data["due_at"])
        fired = {
            "schedule": "cron", "wake_id": wake["id"], "job_id": job["id"], "occurrence_id": data["occurrence_id"],
            "due_at": data["due_at"], "missed": data.get("missed", 1),
            "delay_seconds": round((now - due).total_seconds(), 3), "job_revision": job["updated_rev"],
        }
        when = (f"once, at {h.time.stamp(job['spec'])}" if job["kind"] == "once"
                else f"recurring, cron \"{job['spec']}\" ({h.time.tz_name})")
        lines = [f"You scheduled this job ({when}). This occurrence was due {h.time.stamp(due)}."]
        if data.get("missed", 1) > 1:
            lines.append(f"It came due {data['missed']} times while you were busy or offline; "
                         "this one wake stands in for all of them.")
        lines.append(f"Instruction: {job['instruction']}")
        lines.append(JOB_INSTRUCTION)
        return (block(f"host · scheduled job {job['id']} · {h.time.stamp(now)}", "\n".join(lines)),
                {"job_id": job["id"]}, fired)
