"""The character host: admission, the single serialized worker, and wiring.

Admission order for the next run: pending human input first, then a queued recovery
wake, then other host wakes. A running run is never preempted; input that arrives
meanwhile is persisted at once and admitted at the run's next request boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import datetime
from typing import Any, Callable

from whim import (
    authz,
    bootstrap,
    compaction,
    cron,
    heartbeat,
    journal,
    plugins,
    questions,
    recovery,
)
from whim.config import Config
from whim.context import Context, block
from whim.delivery import HUMAN, Deliveries
from whim.ledger import Ledger, dumps, new_id
from whim.provider.base import Provider
from whim.run import execute, start_run
from whim.scheduler import Scheduler
from whim.timebase import Timebase
from whim.tools.builtin import register_builtins
from whim.tools.registry import Registry

DIRECT = "direct"


def attribution(source_kind: str, route: dict) -> dict:
    """Who sent an input, as the host verified it (published on input.received). The
    direct interface authenticates the designated human by socket peer credentials; an
    adapter vouches for its platform's sender, who is never given the human's authority."""
    out = {"role": "designated_human" if source_kind == DIRECT else "external", "verified_by": source_kind,
           "label": route.get("label", "human")}
    for key, field in (("sender_id", "id"), ("sender_name", "name"), ("channel", "channel")):
        if route.get(key) is not None:
            out[field] = route[key]
    return out


class Harness:
    def __init__(
        self,
        cfg: Config,
        provider: Provider,
        *,
        fault_hook: Callable[[str], Any] | None = None,
        destinations: list | None = None,
        clock: Callable[[], datetime] | None = None,
        compactor: Provider | None = None,
    ):
        self.cfg = cfg
        self.provider = provider
        self._compactor = compactor
        self.log = logging.getLogger("whim")
        self.time = Timebase(cfg.timezone, clock)
        self.ledger = Ledger(cfg.db_path, character_id=cfg.character_id, thread_id=cfg.thread_id, time=self.time)
        heartbeat.ensure_schema(self.ledger)
        journal.ensure_schema(self.ledger)
        cron.ensure_schema(self.ledger)
        compaction.ensure_schema(self.ledger)
        authz.ensure_schema(self.ledger)
        plugins.ensure_schema(self.ledger)
        questions.ensure_schema(self.ledger)
        self.context = Context(self.ledger, cfg.workspace)
        self.deliveries = Deliveries(self.ledger)
        for d in destinations or []:
            self.deliveries.add_destination(d)
        self.registry = Registry()
        register_builtins(self.registry, self.deliveries, cfg.timezone)
        self.fault_hook = fault_hook
        self.heartbeats_enabled = cfg.heartbeat.enabled
        self.scheduler = Scheduler(self)
        self._scheduler_task: asyncio.Task | None = None
        self.active_run: str | None = None
        self.active_state = None  # the RunState of the active run
        self.current_step: dict | None = None  # what the active run is doing right now
        self.provider_call: asyncio.Future | None = None
        self._run_task: asyncio.Task | None = None
        self._interrupting: str | None = None
        self._kick = asyncio.Event()
        self._idle = asyncio.Event()
        self._stopping = False
        self._worker: asyncio.Task | None = None

    @property
    def compactor(self) -> Provider:
        """The model that writes context checkpoints, created on first use."""
        if self._compactor is None:
            self._compactor = _compactor_for(self.cfg, self.provider)
        return self._compactor

    # -- lifecycle -----------------------------------------------------------

    async def start(self, *, run_scheduler: bool = False) -> list[str]:
        """`run_scheduler` starts the background scheduler clock (the daemon does; tests
        drive `scheduler.tick()` themselves with a controlled clock). It runs whether or
        not heartbeats are enabled: jobs and question expiry don't depend on them."""
        interrupted = recovery.reconcile(self)
        questions.repair_links(self)
        bootstrap.retire_file(self)  # finish a retirement a crash interrupted
        self.context.ensure_epoch()
        with self.ledger.tx():
            plugins.commit_generation(self, "startup")
            self.ledger.append("host.started", {"interrupted_runs": interrupted})
        self._worker = asyncio.create_task(self._work(), name="whim-worker")
        if run_scheduler:
            self._scheduler_task = asyncio.create_task(self._schedule(), name="whim-scheduler")
        return interrupted

    async def _schedule(self) -> None:
        while not self._stopping:
            try:
                delay = self.scheduler.tick()
            except Exception:  # noqa: BLE001 - never let a scheduling bug kill the daemon
                self.log.exception("scheduler tick failed")
                delay = 60.0
            await asyncio.sleep(min(max(delay, 1.0), 60.0))

    async def stop(self, grace: float | None = None) -> None:
        """Stop admitting new runs; give an active run `grace` seconds, then abandon it.
        An abandoned run stays `active` on disk and is reconciled on next start."""
        self._stopping = True
        self._kick.set()
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
        if self._worker is None:
            return
        grace = self.cfg.limits.shutdown_grace_seconds if grace is None else grace
        try:
            await asyncio.wait_for(asyncio.shield(self._worker), grace)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._worker.cancel()
            try:
                await self._worker
            except BaseException:  # noqa: BLE001
                pass
        except BaseException:  # noqa: BLE001 - worker already died (e.g. simulated crash)
            pass

    def close(self) -> None:
        self.ledger.close()

    async def fault(self, point: str) -> None:
        """Crash-injection seam for tests; a no-op in production."""
        if self.fault_hook is not None:
            out = self.fault_hook(point)
            if inspect.isawaitable(out):
                await out

    # -- ingress -------------------------------------------------------------

    def submit_input(
        self,
        text: str,
        *,
        source_kind: str = DIRECT,
        principal_id: str | None = None,
        source_message_id: str | None = None,
        route: dict | None = None,
        mode: str = "normal",
        question_id: str | None = None,
    ) -> tuple[dict, bool]:
        """Persist attributed input. Attribution comes from the caller's verified
        transport, never from the payload. Returns (record, duplicate)."""
        led = self.ledger
        if source_message_id is not None:
            dup = led.q1(
                "SELECT id, status, run_id FROM inputs WHERE source_kind = ? AND source_message_id = ?",
                source_kind, source_message_id,
            )
            if dup is not None:
                return dict(dup), True
        if route is None:
            route = {"via": source_kind, "label": "human", "destination": HUMAN}
        input_id = new_id("in")
        with led.tx():
            led.x(
                "INSERT INTO inputs(id, source_kind, source_message_id, principal_id, route, text, received_at, status, "
                "mode, question_id) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                input_id, source_kind, source_message_id, principal_id, dumps(route), text, self.time.iso(),
                mode, question_id,
            )
            origin = {"kind": "input", "source_kind": source_kind}
            if principal_id:
                origin["principal_id"] = principal_id
            ev = led.append(
                "input.received",
                {"input_id": input_id, "text": text, "source_message_id": source_message_id, "via": route.get("via"),
                 "sender": attribution(source_kind, route), "reply_destination": route.get("destination"),
                 **({"mode": mode} if mode != "normal" else {}), **({"question_id": question_id} if question_id else {})},
                actor="human" if source_kind == DIRECT else "external",
                origin=origin,
                audience="human" if source_kind == DIRECT else "operator",
            )
            if self.active_run is not None:
                led.append("input.pending", {"input_id": input_id}, run_id=self.active_run, cause_id=ev["id"])
        self._nudge()
        return {"id": input_id, "status": "pending", "seq": ev["seq"]}, False

    def request_wake(self, *, kind: str = "host", reason: str = "manual", text: str = "") -> str | None:
        """Queue a non-input wake. Heartbeats are refused until the first meeting is complete."""
        if kind not in ("host", "heartbeat"):
            raise ValueError(f"unknown wake kind {kind!r}")
        if kind == "heartbeat" and not (self.heartbeats_enabled and bootstrap.is_complete(self)):
            return None
        wake_id = new_id("wake")
        with self.ledger.tx():
            self.ledger.x(
                "INSERT INTO wakes(id, kind, status, created_at, data) VALUES(?, ?, 'queued', ?, ?)",
                wake_id, kind, self.time.iso(), dumps({"reason": reason, "text": text}),
            )
            self.ledger.append("wake.queued", {"wake_id": wake_id, "kind": kind, "reason": reason})
        self._nudge()
        return wake_id

    def decide_approval(self, approval_id: str, approve: bool, principal_id: str, *, standing: bool = False) -> dict:
        """The designated human decides an approval request (authenticated control path only).
        Approving a plugin's request activates it; the character is told either way, at its
        next step if it is mid-wake (so new tools arrive without restarting anything)."""
        led = self.ledger
        with led.tx():
            row = authz.decide(self, approval_id, approve, principal_id, standing=standing)
            plugin = led.q1("SELECT * FROM plugins WHERE impl = ?", row["subject"])
            text = f"Your human {'approved' if approve else 'declined'} request {approval_id}."
            if plugin is not None:
                current = plugins.latest(self, plugin["name"])
                if approve and current is not None and current["impl"] == plugin["impl"] \
                        and plugin["status"] == "validated":
                    plugins.activate(self, plugin["impl"], f"approved {approval_id}")
                    tools = ", ".join(f"{plugin['name']}_{t['name']}" for t in json.loads(plugin["tools"]))
                    text += f" Plugin {plugin['name']} is active; its tools are available now: {tools}."
                elif not approve and plugin["status"] == "validated":
                    led.x("UPDATE plugins SET status = 'denied', updated_at = ? WHERE impl = ?",
                          self.time.iso(), plugin["impl"])
                    text += f" Plugin {plugin['name']} stays inactive."
            led.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'approval', ?, 'pending', ?)",
                  new_id("note"), block(f"host · approval decided · {self.time.stamp()}", text), self.time.iso())
        if self.active_run is None:
            self.request_wake(reason="approval")
        self._nudge()
        return row

    # -- host controls (designated human only; the control endpoint has authenticated them) --

    def _require_human(self, principal_id: str) -> None:
        if principal_id != self.cfg.principal_id:
            raise authz.AuthzError("only the designated human can control runs")

    def _control_event(self, action: str, principal_id: str, **data) -> None:
        with self.ledger.tx():
            self.ledger.append("control.requested", {"action": action, **data}, run_id=self.active_run,
                               actor="human", origin={"kind": "control", "principal_id": principal_id})

    def current(self) -> dict:
        state = self.active_state
        if state is None:
            return {"active_run": None}
        row = self.ledger.q1("SELECT * FROM runs WHERE id = ?", state.run_id)
        return {"active_run": state.run_id, "origin": state.origin, "started_at": row["started_at"],
                "requests": state.requests, "tool_calls": state.tool_calls, "step": self.current_step,
                "stop_requested": state.stop_requested}

    def control_stop(self, principal_id: str) -> dict:
        """Stop the active run at its next safe point (after the step in progress)."""
        self._require_human(principal_id)
        if self.active_state is None:
            raise authz.AuthzError("nothing is running")
        self.active_state.stop_requested = True
        self._control_event("stop", principal_id)
        return self.current()

    def control_interrupt(self, principal_id: str) -> dict:
        """Cancel the active run now, mid-step if need be. Whatever it was doing is settled
        conservatively (uncertain effects stay uncertain); nothing is retried."""
        self._require_human(principal_id)
        if self._run_task is None or self.active_state is None:
            raise authz.AuthzError("nothing is running")
        self._control_event("interrupt", principal_id, step=self.current_step)
        self._interrupting = self.active_state.run_id
        self._run_task.cancel()
        return {"interrupting": self._interrupting}

    def control_steer(self, text: str, principal_id: str, client_message_id: str | None = None) -> dict:
        """Admit a message to the active run right away: a model response still being
        written is discarded and requested again with the message; a running tool is not
        cut off (the message enters as soon as it finishes)."""
        self._require_human(principal_id)
        rec, _ = self.submit_input(text, principal_id=principal_id, source_message_id=client_message_id,
                                   mode="steer")
        state = self.active_state
        self._control_event("steer", principal_id, input_id=rec["id"],
                            cancelled_model_request=bool(state and self.provider_call))
        if state is not None and self.provider_call is not None and not self.provider_call.done():
            state.steered = True
            self.provider_call.cancel()
        return rec

    def _close_interrupted(self, state) -> None:
        with self.ledger.tx():
            run = self.ledger.q1("SELECT * FROM runs WHERE id = ?", state.run_id)
            unresolved, lines = recovery.close_run(self, run, "operator_interrupt")
            self.ledger.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'control', ?, 'pending', ?)",
                          new_id("note"), block(f"host · wake interrupted · {self.time.stamp()}",
                                                "Your human interrupted this wake mid-step.\n" + "\n".join(lines)
                                                + "\nNothing was retried. What you do about it is up to you."),
                          self.time.iso())
        self.current_step = None

    def answer_question(self, question_id: str, principal_id: str, **kw) -> dict:
        out = questions.answer(self, question_id, principal_id, **kw)
        self._nudge()
        return out

    def attach_adapter(self, name: str):
        """A handle for host-installed adapter code (see whim.adapters)."""
        from whim.adapters import AdapterHandle

        return AdapterHandle(self, name)

    def revoke_grant(self, grant_id: str, principal_id: str) -> dict:
        with self.ledger.tx():
            row = authz.revoke(self, grant_id, principal_id)
            plugins.commit_generation(self, f"revoked {grant_id}")
        return row

    def _nudge(self) -> None:
        self._idle.clear()
        self._kick.set()

    # -- worker --------------------------------------------------------------

    def _next_job(self) -> dict | None:
        if self._stopping:
            return None
        led = self.ledger
        pending = led.q1("SELECT 1 FROM inputs WHERE status = 'pending' LIMIT 1") is not None
        upkeep, urgent = compaction.next_job(self, pending)
        if upkeep is not None and urgent:
            return upkeep
        est = compaction.estimate(self)
        full = est >= compaction.hard_limit(self)
        if full and upkeep is None:
            # Context is full and compaction can't run yet: admit nothing (input stays
            # durably pending) rather than send oversized requests.
            compaction.note_blocked(self, est, True)
            return None
        compaction.note_blocked(self, est, False)
        if full:
            return upkeep
        if pending:
            return {"wake": None}
        recovery_wake = led.q1("SELECT * FROM wakes WHERE status = 'queued' AND kind = 'recovery' LIMIT 1")
        if recovery_wake is not None:
            return {"wake": recovery_wake}
        if upkeep is not None:
            return upkeep
        heartbeats_ok = self.heartbeats_enabled and bootstrap.is_complete(self)
        wake = led.q1(
            "SELECT * FROM wakes WHERE status = 'queued' AND kind != 'maintenance' AND (kind != 'heartbeat' OR ?) "
            "ORDER BY CASE kind WHEN 'recovery' THEN 0 WHEN 'host' THEN 1 WHEN 'cron' THEN 2 ELSE 3 END, "
            "created_at LIMIT 1",
            int(heartbeats_ok),
        )
        return {"wake": wake} if wake is not None else None

    async def _work(self) -> None:
        while True:
            self._kick.clear()
            job = self._next_job()
            if job is None:
                self._idle.set()
                if self._stopping:
                    return
                await self._kick.wait()
                continue
            self._idle.clear()
            if "compaction" in job:
                await compaction.run_job(self, job["compaction"])  # failures back off inside
                continue
            state = start_run(self, job)
            self.active_run, self.active_state = state.run_id, state
            self._run_task = asyncio.ensure_future(execute(self, state))
            try:
                await self._run_task
            except asyncio.CancelledError:
                if self._interrupting != state.run_id or asyncio.current_task().cancelling():
                    raise  # shutdown: left active on purpose, reconciled at next start
                self._close_interrupted(state)
            finally:
                self.active_run, self.active_state, self._run_task = None, None, None
                self._interrupting = None

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Test/CLI helper: wait until nothing is running or admissible."""
        async def _wait():
            while True:
                await self._idle.wait()
                await asyncio.sleep(0)
                if self._idle.is_set() and self._next_job() is None:
                    return
        await asyncio.wait_for(_wait(), timeout)


def _compactor_for(cfg: Config, provider: Provider) -> Provider:
    """The Flash-class model that writes checkpoints; the main provider for offline kinds."""
    if cfg.provider.kind != "deepseek":
        return provider
    from dataclasses import replace

    from whim.provider.deepseek import DeepSeekProvider

    c = cfg.compaction
    return DeepSeekProvider(replace(cfg.provider, model=c.model, thinking=c.thinking))
