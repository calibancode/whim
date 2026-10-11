"""Direct-interface service: HTTP over a Unix domain socket.

Authentication (implementation proposal, validated by tests): the socket lives in a
0700 directory with 0600 permissions, and every request's peer uid is read from the
kernel (SO_PEERCRED) and must equal the configured designated-human uid. The host
then attaches the opaque principal id itself; request bodies cannot set origin,
principal, or authority.

  POST /v1/input      {"text": str, "client_message_id": str?}
  POST /v1/wake       {"reason": str?, "text": str?}       host control
  GET  /v1/events     ?after=<seq>&follow=0|1  (SSE; id = durable seq; Last-Event-ID honoured)
  GET  /v1/agui       ?view=conversation|activity|inspect&after=<seq>&follow=0|1  (AG-UI 1.0 SSE)
  GET  /v1/snapshot
  GET/POST /v1/control               current run; {"action": "stop"|"interrupt"|"steer", "text"?}
  GET  /v1/questions                 the character's pending questions
  POST /v1/questions/{id}/answer     {"choice"?: n|text, "text"?: str}
  GET  /v1/approvals                 pending requests, grants, plugins      (designated human)
  POST /v1/approvals/{id}           {"decision": "approve"|"deny", "standing": bool}
  POST /v1/grants/{id}/revoke
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import socket
import struct
from typing import Annotated, Literal

from aiohttp import web
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

from whim import agui, authz, compaction, cron, questions
from whim import provider as providers
from whim.config import Config, ConfigError
from whim.harness import DIRECT, Harness

KEEPALIVE_SECONDS = 15.0
# Supported sizes. Text a client submits is at most MAX_TEXT_BYTES (UTF-8), so every event
# stays well under MAX_EVENT_BYTES, the largest single SSE data line a client must accept.
# Request bodies may be larger than the text because of JSON escaping.
MAX_TEXT_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024


def _bounded(text: str | None) -> str | None:
    if text is not None and len(text.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        raise ValueError(f"text is limited to {MAX_TEXT_BYTES} bytes (UTF-8)")
    return text


Text = Annotated[str, AfterValidator(_bounded)]
PRINCIPAL = web.RequestKey("principal_id", str)
ENTITLEMENTS = web.RequestKey("entitlements", frozenset)
PREFIX = web.RequestKey("sse_prefix", list)
# v1 has one authenticated principal, the designated human, entitled to everything.
PRINCIPAL_ENTITLEMENTS = frozenset({"operator", "human"})


class InputBody(BaseModel):
    model_config = ConfigDict(extra="ignore")  # forged origin/principal fields are dropped
    text: Text
    client_message_id: str | None = None
    when: Literal["next_step", "next_run"] = "next_step"  # next_run: hold until the current run ends


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["stop", "interrupt", "steer"]
    text: Text | None = None
    client_message_id: str | None = None


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: str | int | None = None
    text: Text | None = None
    client_message_id: str | None = None


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    standing: bool = False  # also let any future validated plugin run without asking (no extra scopes)


class WakeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = "manual"
    text: Text = ""


def peer_uid(request: web.Request) -> int | None:
    transport = request.transport
    sock = transport.get_extra_info("socket") if transport else None
    if sock is None or sock.family != socket.AF_UNIX:
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def make_app(h: Harness, *, uid_of=peer_uid, entitlements: frozenset = PRINCIPAL_ENTITLEMENTS) -> web.Application:
    cfg = h.cfg

    @web.middleware
    async def authenticate(request: web.Request, handler):
        uid = uid_of(request)
        if uid is None or uid != cfg.allowed_uid:
            return web.json_response({"error": "forbidden"}, status=403)
        request[PRINCIPAL] = cfg.principal_id
        request[ENTITLEMENTS] = entitlements
        return await handler(request)

    async def post_input(request: web.Request) -> web.Response:
        try:
            body = InputBody.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        rec, dup = h.submit_input(
            body.text,
            source_kind=DIRECT,
            principal_id=request[PRINCIPAL],
            source_message_id=body.client_message_id,
            mode="next_run" if body.when == "next_run" else "normal",
        )
        return web.json_response({**rec, "duplicate": dup}, status=200 if dup else 202)

    async def post_wake(request: web.Request) -> web.Response:
        try:
            body = WakeBody.model_validate(await request.json() if request.can_read_body else {})
        except (ValidationError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"wake_id": h.request_wake(reason=body.reason, text=body.text)}, status=202)

    async def stream(request: web.Request, frames_for) -> web.StreamResponse:
        """SSE over the ledger: backlog after the cursor, then live, gap-free.
        `frames_for(ev)` returns the SSE frames for one ledger event (may be empty)."""
        try:
            after = int(request.headers.get("Last-Event-ID") or request.query.get("after", "0"))
        except ValueError:
            return web.json_response({"error": "bad cursor"}, status=400)
        follow = request.query.get("follow", "1") != "0"
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        # Subscribe before reading the backlog so nothing committed in between is missed;
        # duplicates are dropped by seq. A client too slow to keep up is disconnected (and
        # resumes from its last id) instead of having events buffered for it without end.
        overflowed = asyncio.Event()

        def overflow() -> None:
            overflowed.set()
            if request.transport is not None:
                request.transport.close()

        q = h.ledger.subscribe(on_overflow=overflow)
        last = after
        try:
            for frame in request.get(PREFIX, []):
                await resp.write(frame)
            while batch := h.ledger.events_after(last, 500):
                for ev in batch:
                    for frame in frames_for(ev):
                        await resp.write(frame)
                    last = ev["seq"]
            if not follow:
                return resp
            while True:
                if overflowed.is_set() and q.empty():
                    h.log.warning("event stream subscriber fell behind at seq %d; disconnected", last)
                    return resp
                try:
                    ev = await asyncio.wait_for(q.get(), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                if ev["seq"] > last:
                    for frame in frames_for(ev):
                        await resp.write(frame)
                    last = ev["seq"]
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            h.ledger.unsubscribe(q)
        return resp

    async def get_events(request: web.Request) -> web.StreamResponse:
        """Native ledger events, filtered by the viewer's audience entitlements."""
        allowed = request[ENTITLEMENTS]

        def frames(ev: dict) -> list[bytes]:
            if ev["audience"] not in allowed:
                return [f"id: {ev['seq']}\n\n".encode()]
            data = json.dumps(ev, ensure_ascii=False)
            return [f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {data}\n\n".encode()]

        return await stream(request, frames)

    async def get_agui(request: web.Request) -> web.StreamResponse:
        """AG-UI 1.0 projection. `view` = conversation | activity | inspect."""
        try:
            projector = agui.Projector(h, request.query.get("view", "conversation"), request[ENTITLEMENTS])
            after = int(request.headers.get("Last-Event-ID") or request.query.get("after", "0"))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        request[PREFIX] = [f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode()
                           for e in projector.resume_prefix(after)]

        def frames(ev: dict) -> list[bytes]:
            out = projector.project(ev)
            if not out:
                return [f"id: {ev['seq']}\n\n".encode()]  # advance the cursor without an event
            *head, tail = out
            return ([f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in head]
                    + [f"id: {ev['seq']}\ndata: {json.dumps(tail, ensure_ascii=False)}\n\n".encode()])

        return await stream(request, frames)

    def operator_only(request: web.Request) -> web.Response | None:
        if "operator" not in request[ENTITLEMENTS]:
            return web.json_response({"error": "forbidden"}, status=403)
        return None

    async def get_approvals(request: web.Request) -> web.Response:
        if (denied := operator_only(request)) is not None:
            return denied
        rows = h.ledger.q("SELECT impl, name, status, manifest, tools, error FROM plugins "
                          "WHERE status NOT IN ('superseded', 'removed') ORDER BY name")
        return web.json_response({**authz.listing(h), "plugins": [dict(r) for r in rows]})

    async def post_approval(request: web.Request) -> web.Response:
        if (denied := operator_only(request)) is not None:
            return denied
        try:
            body = DecisionBody.model_validate(await request.json())
            row = h.decide_approval(request.match_info["id"], body.decision == "approve", request[PRINCIPAL],
                                    standing=body.standing)
        except (ValidationError, json.JSONDecodeError, authz.AuthzError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(row)

    async def post_revoke(request: web.Request) -> web.Response:
        if (denied := operator_only(request)) is not None:
            return denied
        try:
            row = h.revoke_grant(request.match_info["id"], request[PRINCIPAL])
        except authz.AuthzError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(row)

    async def get_control(request: web.Request) -> web.Response:
        if (denied := operator_only(request)) is not None:
            return denied
        return web.json_response(h.current())

    async def post_control(request: web.Request) -> web.Response:
        if (denied := operator_only(request)) is not None:
            return denied
        try:
            body = ControlBody.model_validate(await request.json())
            if body.action == "stop":
                out = h.control_stop(request[PRINCIPAL])
            elif body.action == "interrupt":
                out = h.control_interrupt(request[PRINCIPAL])
            else:
                if not (body.text or "").strip():
                    return web.json_response({"error": "steer needs text"}, status=400)
                out = h.control_steer(body.text, request[PRINCIPAL], body.client_message_id)
        except (ValidationError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except authz.AuthzError as e:
            return web.json_response({"error": str(e)}, status=409)
        return web.json_response(out)

    async def get_questions(request: web.Request) -> web.Response:
        if "human" not in request[ENTITLEMENTS]:
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response({"pending": questions.listing(h)})

    async def post_answer(request: web.Request) -> web.Response:
        if "human" not in request[ENTITLEMENTS]:
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = AnswerBody.model_validate(await request.json())
            out = h.answer_question(request.match_info["id"], request[PRINCIPAL], choice=body.choice,
                                    text=body.text, client_message_id=body.client_message_id)
        except (ValidationError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except questions.QuestionError as e:
            return web.json_response({"error": str(e)}, status=e.status)
        return web.json_response(out)

    async def get_snapshot(request: web.Request) -> web.Response:
        led = h.ledger
        runs = led.q("SELECT id, status, reason, started_at, ended_at, requests, tool_calls FROM runs "
                     "ORDER BY started_at DESC LIMIT 5")
        msgs = led.q("SELECT d.id, d.run_id, d.content, d.status, q.id AS question_id FROM deliveries d "
                     "LEFT JOIN questions q ON q.delivery_id = d.id WHERE d.destination = 'human' "
                     "ORDER BY d.rowid DESC LIMIT 20")
        return web.json_response({
            "cursor": led.last_seq(),
            "character_id": cfg.character_id,
            "thread_id": cfg.thread_id,
            "active_run": h.active_run,
            "pending_inputs": led.q1("SELECT COUNT(*) c FROM inputs WHERE status = 'pending'")["c"],
            "queued_wakes": led.q1("SELECT COUNT(*) c FROM wakes WHERE status = 'queued'")["c"],
            "recent_runs": [dict(r) for r in runs],
            "heartbeat": {"enabled": h.heartbeats_enabled, **h.scheduler.state(),
                          "cue_revision": led.get_meta("cues_revision", 0),
                          "active_cues": led.q1("SELECT COUNT(*) c FROM cues WHERE status = 'active'")["c"]},
            "jobs": cron.listing(h),
            "context": compaction.status(h),
            "messages_to_human": [dict(m) for m in reversed(msgs)],
        })

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/v1/input", post_input)
    app.router.add_post("/v1/wake", post_wake)
    app.router.add_get("/v1/events", get_events)
    app.router.add_get("/v1/agui", get_agui)
    app.router.add_get("/v1/approvals", get_approvals)
    app.router.add_get("/v1/control", get_control)
    app.router.add_post("/v1/control", post_control)
    app.router.add_get("/v1/questions", get_questions)
    app.router.add_post("/v1/questions/{id}/answer", post_answer)
    app.router.add_post("/v1/approvals/{id}", post_approval)
    app.router.add_post("/v1/grants/{id}/revoke", post_revoke)
    app.router.add_get("/v1/snapshot", get_snapshot)
    return app


class AlreadyRunning(Exception):
    pass


def acquire_lock(cfg: Config):
    f = open(cfg.run_dir / "whim.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise AlreadyRunning(f"another whim daemon owns {cfg.home}") from None
    f.write(str(os.getpid()))
    f.flush()
    return f


async def serve(cfg: Config) -> None:
    lock = acquire_lock(cfg)
    provider = providers.from_config(cfg.provider)
    h = Harness(cfg, provider)
    interrupted = await h.start(run_scheduler=True)
    if not h.heartbeats_enabled:
        h.log.info("heartbeats disabled")
    if interrupted:
        h.log.warning("reconciled %d interrupted run(s): %s", len(interrupted), ", ".join(interrupted))
    runner = web.AppRunner(make_app(h), handle_signals=False, shutdown_timeout=1.0)
    await runner.setup()
    sock = cfg.socket_path
    if len(os.fsencode(sock)) > 107:
        raise ConfigError(f"the control socket path is too long for a Unix socket ({sock}); "
                             "use a shorter whim home")
    sock.unlink(missing_ok=True)  # stale socket from a dead daemon; we hold the lock
    old_umask = os.umask(0o177)
    try:
        await web.UnixSite(runner, str(sock)).start()
    finally:
        os.umask(old_umask)
    h.log.info("listening on %s", sock)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    h.log.info("shutting down")
    await h.stop()
    await runner.cleanup()
    sock.unlink(missing_ok=True)
    await provider.close()
    if h._compactor is not None and h._compactor is not provider:
        await h._compactor.close()
    h.close()
    lock.close()
