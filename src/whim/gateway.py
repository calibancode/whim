"""Remote gateway: a narrow, authenticated relay from Tailscale Serve to the control socket.

whim's control socket trusts whoever the kernel says is connecting (SO_PEERCRED). That
can't reach a phone, and it must never be exposed as is. This gateway is a separate
process that runs as the designated human's uid, so its own requests to the control
socket are the human's; it decides which remote requests may be relayed at all.

It listens only on a Unix socket in an owner-only directory, for Tailscale Serve
(`tailscale serve --bg unix:<socket>`), never on TCP. Every request must pass, in order:

1. Peer uid: the connecting process is tailscaled (by default uid 0, read from the
   kernel). Any other local process, including the character's own sandboxed commands
   and anything else running as the human, is refused before a header is read. Those
   are exactly the processes that could otherwise forge Serve's identity headers.
   (SO_PEERCRED proves only the connecting uid. Local processes can still come in
   through Serve itself, as the host's owner; the device token is what stops them.)
2. Tailnet identity: Serve's `Tailscale-User-Login` (which Serve strips from incoming
   requests and sets itself) names the configured login. Tailnet membership is not
   enough. Funnel (public) traffic and tagged devices carry no identity: refused.
3. Device token: a bearer token issued once by pairing (`whim-gateway pair` shows a
   short-lived one-time code). Only SHA-256 hashes are stored, outside the whim home.

Then only an allowlist of routes and methods is relayed, with bodies re-validated and
re-serialized field by field: the daemon attaches the principal itself, so nothing a
remote client sends can claim to be anyone else. A client's `Whim-Interface` header
(e.g. `android`) is re-validated and passed on as a label of where a message came from;
without a valid one the gateway names the interface `remote`. It carries no authority. Settings, access grants, restarts and
grant revocation are not reachable through the gateway at all.

Errors: 401 (no valid device token), 403 (peer or identity refused), 404 (route not
relayed), 400/413 (body), 502 (`outcome_unknown`: the daemon may have acted), 503
(`daemon_unreachable`: it certainly did not).
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import stat
import struct
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import aiohttp
import tomllib
from aiohttp import web
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from whim import config as whim_config
from whim import safefs
from whim.config import CHARACTER_FILES, INTERFACE_HEADER, INTERFACE_RE

log = logging.getLogger("whim.gateway")

VERSION = 1
MAX_BODY_BYTES = 2 * 1024 * 1024  # a 1 MiB message plus JSON escaping; the daemon checks text itself
MAX_TEXT_BYTES = 1024 * 1024
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_STREAMS = 8  # concurrent event streams, all devices together
REQUEST_TIMEOUT = 30.0
PAIRING_MINUTES = 10.0
PAIRING_ATTEMPTS = 5
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
LOGIN_HEADER = "Tailscale-User-Login"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U
TOKEN = web.RequestKey("device", dict)


# -- configuration and state ------------------------------------------------------------


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def default_config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "whim-gateway" / "config.toml"


@dataclass
class GatewayConfig:
    allowed_login: str
    whim_home: Path
    listen: Path
    state_dir: Path
    trusted_proxy_uids: tuple[int, ...] = (0,)

    @property
    def upstream(self) -> Path:
        return self.whim_home / "run" / "whim.sock"

    @property
    def devices_path(self) -> Path:
        return self.state_dir / "devices.json"


def default_listen() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "whim-gateway" / "gateway.sock"


def load_config(path: Path) -> GatewayConfig:
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"no gateway config at {path}; run `whim-gateway init --login you@example.com` first")
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path} is not valid TOML: {e}")
    login = str(raw.get("allowed_login", "")).strip()
    if "@" not in login:
        raise SystemExit(f"{path}: allowed_login must be a Tailscale login such as you@example.com")
    uids = raw.get("trusted_proxy_uids", [0])
    if not isinstance(uids, list) or not uids or not all(isinstance(u, int) and u >= 0 for u in uids):
        raise SystemExit(f"{path}: trusted_proxy_uids must be a non-empty list of uids")
    return GatewayConfig(
        allowed_login=login,
        whim_home=Path(raw.get("whim_home") or whim_config.default_home()).expanduser(),
        listen=Path(raw.get("listen") or default_listen()).expanduser(),
        state_dir=Path(raw.get("state_dir") or _xdg("XDG_STATE_HOME", ".local/state") / "whim-gateway").expanduser(),
        trusted_proxy_uids=tuple(uids),
    )


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> float:
    return time.time()


class Devices:
    """Paired devices and the pending pairing code, in one owner-only JSON file that the
    CLI and the server both change (under an advisory lock). Only hashes are kept."""

    def __init__(self, path: Path):
        self.path = path
        self._cache: tuple[int, dict] | None = None

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            yield data
            _write_private(self.path, json.dumps(data, indent=1).encode())
            self._cache = None

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {}
        data.setdefault("devices", [])
        data.setdefault("pairing", None)
        return data

    def snapshot(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return {"devices": [], "pairing": None}
        if self._cache is None or self._cache[0] != mtime:
            self._cache = (mtime, self._read())
        return self._cache[1]

    def authenticate(self, token: str) -> dict | None:
        digest = _hash(token)
        found = None
        for d in self.snapshot()["devices"]:
            if hmac.compare_digest(d["token_sha256"], digest):
                found = d
        return found

    def start_pairing(self, minutes: float = PAIRING_MINUTES) -> str:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
        with self._locked() as data:
            data["pairing"] = {"code_sha256": _hash(raw), "expires_at": _now() + minutes * 60, "failures": 0}
        return "-".join(raw[i:i + 4] for i in range(0, 12, 4))

    def pair(self, code: str, name: str) -> tuple[dict, str] | str:
        """(device, token) for the right code, else a reason. The code works once, for a
        few minutes, and is withdrawn after a few wrong guesses."""
        normal = re.sub(r"[^A-Z0-9]", "", code.upper())
        with self._locked() as data:
            p = data.get("pairing")
            if not p or p["expires_at"] < _now():
                data["pairing"] = None
                return "no pairing code is active; run `whim-gateway pair` on the host"
            if not hmac.compare_digest(p["code_sha256"], _hash(normal)):
                p["failures"] += 1
                if p["failures"] >= PAIRING_ATTEMPTS:
                    data["pairing"] = None
                    return "wrong code; too many attempts, so it was withdrawn"
                return "wrong code"
            data["pairing"] = None
            token = secrets.token_urlsafe(32)
            device = {"id": "dev_" + secrets.token_hex(6), "name": name[:60] or "phone",
                      "token_sha256": _hash(token), "created_at": _now(), "last_seen_at": None}
            data["devices"].append(device)
        return device, token

    def revoke(self, device_id: str) -> bool:
        with self._locked() as data:
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d["id"] != device_id]
            return len(data["devices"]) < before

    def touch(self, device_id: str) -> None:
        """Record use at most every few minutes (it is informational only)."""
        d = next((d for d in self.snapshot()["devices"] if d["id"] == device_id), None)
        if d is not None and (d.get("last_seen_at") or 0) < _now() - 300:
            with self._locked() as data:
                for x in data["devices"]:
                    if x["id"] == device_id:
                        x["last_seen_at"] = _now()


# -- request bodies (re-validated here, then re-serialized from these fields only) -------


def _bounded(text: str | None) -> str | None:
    if text is not None and len(text.encode("utf-8", "surrogatepass")) > MAX_TEXT_BYTES:
        raise ValueError(f"text is limited to {MAX_TEXT_BYTES} bytes (UTF-8)")
    return text


Text = Annotated[str, AfterValidator(_bounded)]
ClientId = Annotated[str, Field(min_length=1, max_length=128)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputBody(_Body):
    text: Text
    client_message_id: ClientId | None = None
    when: Literal["next_step", "next_run"] = "next_step"


class AnswerBody(_Body):
    choice: Annotated[str, Field(max_length=4096)] | int | None = None
    text: Text | None = None
    client_message_id: ClientId | None = None


class DecisionBody(_Body):
    decision: Literal["approve", "deny"]
    standing: bool = False


class ControlBody(_Body):
    action: Literal["stop", "interrupt", "steer"]
    text: Text | None = None
    client_message_id: ClientId | None = None


class PairBody(_Body):
    code: Annotated[str, Field(min_length=4, max_length=40)]
    device_name: Annotated[str, Field(max_length=60)] = "phone"


# -- the app ----------------------------------------------------------------------------


def peer_uid(request: web.Request) -> int | None:
    transport = request.transport
    sock = transport.get_extra_info("socket") if transport else None
    if sock is None or sock.family != socket.AF_UNIX:
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def _error(status: int, message: str, reason: str | None = None) -> web.Response:
    body = {"error": message}
    if reason:
        body["reason"] = reason
    return web.json_response(body, status=status, headers={"Cache-Control": "no-store"})


def character_name(home: Path) -> str:
    """As the TUI derives it: the name in IDENTITY.md, else the seed name."""
    text = safefs.read_text(home / "workspace", "IDENTITY.md") or ""
    m = re.search(r"(?im)^[\W_]*name[\W_]*?[:：]\s*\**\s*([^\s*—,.;:()\[\]]+)", text)
    if m:
        return m.group(1)
    try:
        return whim_config.load(home).seed_name or "whim"
    except whim_config.ConfigError:
        return "whim"


def documents(workspace: Path) -> list[dict]:
    """The character's own documents, read-only: its four character files and its
    journal. Nothing else in the workspace (and nothing outside it) is listed or served."""
    out = []

    def add(rel: str, kind: str) -> None:
        try:
            size = safefs.size(workspace, rel) if safefs.kind(workspace, rel) == "file" else None
        except (OSError, safefs.UnsafePath):
            size = None
        if size is not None:
            out.append({"path": rel, "kind": kind, "size": size})

    for name in CHARACTER_FILES:
        add(name, "character")
    try:
        names = [n for n, k in safefs.listdir(workspace, "journal") if k == "file" and n.endswith(".md")]
    except (FileNotFoundError, NotADirectoryError, safefs.UnsafePath):
        names = []
    for n in sorted(names, reverse=True)[:120]:
        add(f"journal/{n}", "journal")
    return out


def make_app(gcfg: GatewayConfig, *, uid_of=peer_uid, devices: Devices | None = None) -> web.Application:
    devices = devices or Devices(gcfg.devices_path)
    allowed = gcfg.allowed_login.casefold()
    streams = asyncio.Semaphore(MAX_STREAMS)

    @web.middleware
    async def guard(request: web.Request, handler):
        # 1. Only tailscaled may connect. Anything else local could forge the headers below.
        uid = uid_of(request)
        if uid is None or uid not in gcfg.trusted_proxy_uids:
            log.warning("refused a direct connection (peer uid %s)", uid)
            return _error(403, "forbidden")
        # 2. The designated human's tailnet identity, as Serve vouches for it.
        logins = request.headers.getall(LOGIN_HEADER, [])
        if len(logins) != 1 or logins[0].strip().casefold() != allowed or "Tailscale-Funnel-Request" in request.headers:
            log.warning("refused tailnet identity %r", logins[:2])
            return _error(403, "this tailnet identity is not allowed")
        if request.path == "/gateway/v1/pair":
            return await handler(request)
        # 3. A paired device.
        auth = request.headers.get("Authorization", "")
        device = devices.authenticate(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if device is None:
            return _error(401, "this device isn't paired (or was unpaired)", "unpaired")
        request[TOKEN] = device
        devices.touch(device["id"])
        return await handler(request)

    session_holder: dict[str, aiohttp.ClientSession] = {}

    def upstream() -> aiohttp.ClientSession:
        s = session_holder.get("s")
        if s is None or s.closed:
            s = session_holder["s"] = aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=str(gcfg.upstream)),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
                auto_decompress=False)
        return s

    async def close_upstream(_app) -> None:
        s = session_holder.pop("s", None)
        if s is not None:
            await s.close()

    def interface(request: web.Request) -> str:
        named = request.headers.get(INTERFACE_HEADER, "").strip()
        return named if INTERFACE_RE.fullmatch(named) else "remote"

    async def relay(method: str, path: str, body: dict | None = None,
                    request: web.Request | None = None) -> web.Response:
        headers = {INTERFACE_HEADER: interface(request)} if request is not None else None
        try:
            async with upstream().request(method, f"http://whim{path}", json=body, headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, sock_connect=5)) as r:
                data = await r.read()
                return web.Response(body=data, status=r.status, headers={
                    "Content-Type": r.headers.get("Content-Type", "application/json"), "Cache-Control": "no-store"})
        except (aiohttp.ClientConnectorError, FileNotFoundError, ConnectionRefusedError):
            return _error(503, "whim isn't running on the host", "daemon_unreachable")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return _error(502, f"lost the connection to whim ({type(e).__name__}); it may have acted on this",
                          "outcome_unknown")

    async def parse(request: web.Request, model: type[BaseModel]) -> BaseModel | web.Response:
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return _error(413, "request body too large")
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return _error(413, "request body too large")
        try:
            return model.model_validate_json(raw or b"{}")
        except ValidationError as e:
            return _error(400, "; ".join(f"{'.'.join(map(str, x['loc'])) or 'body'}: {x['msg']}" for x in e.errors()))

    def ident(request: web.Request) -> str | None:
        v = request.match_info["id"]
        return v if ID_RE.match(v) else None

    async def snapshot(request):
        return await relay("GET", "/v1/snapshot")

    async def events(request: web.Request) -> web.StreamResponse:
        q = request.query
        if set(q) - {"after", "follow"}:
            return _error(400, "unsupported query parameter")
        cursor = request.headers.get("Last-Event-ID") or q.get("after", "0")
        follow = q.get("follow", "1")
        if not cursor.isdigit() or len(cursor) > 18 or follow not in ("0", "1"):
            return _error(400, "bad cursor")
        if streams.locked():
            return _error(503, "too many open event streams", "busy")
        async with streams:
            try:
                r = await upstream().get(f"http://whim/v1/events?after={cursor}&follow={follow}",
                                         timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=60))
            except (aiohttp.ClientConnectorError, FileNotFoundError, ConnectionRefusedError):
                return _error(503, "whim isn't running on the host", "daemon_unreachable")
            except aiohttp.ClientError as e:
                return _error(502, f"couldn't open the event stream ({type(e).__name__})")
            async with r:
                if r.status != 200:
                    return web.Response(body=await r.read(), status=r.status,
                                        headers={"Content-Type": r.headers.get("Content-Type", "application/json")})
                resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                                   "X-Accel-Buffering": "no"})
                if follow == "0":  # a backlog: compress it (a live stream must not wait on a compressor)
                    resp.enable_compression()
                await resp.prepare(request)
                try:
                    async for chunk in r.content.iter_any():
                        await resp.write(chunk)
                except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError):
                    pass  # the client resumes from its last complete frame
                return resp

    async def post_input(request):
        body = await parse(request, InputBody)
        if isinstance(body, web.Response):
            return body
        return await relay("POST", "/v1/input", body.model_dump(exclude_none=True), request)

    async def get_questions(request):
        return await relay("GET", "/v1/questions")

    async def post_answer(request):
        if (qid := ident(request)) is None:
            return _error(404, "no such question")
        body = await parse(request, AnswerBody)
        if isinstance(body, web.Response):
            return body
        if body.choice is None and not (body.text or "").strip():
            return _error(400, "give a choice or some words")
        return await relay("POST", f"/v1/questions/{qid}/answer", body.model_dump(exclude_none=True), request)

    async def get_approvals(request):
        return await relay("GET", "/v1/approvals")

    async def post_approval(request):
        if (aid := ident(request)) is None:
            return _error(404, "no such approval request")
        body = await parse(request, DecisionBody)
        if isinstance(body, web.Response):
            return body
        log.info("approval %s: %s%s by %s", aid, body.decision, " (standing)" if body.standing else "",
                 request[TOKEN]["id"])
        return await relay("POST", f"/v1/approvals/{aid}", body.model_dump())

    async def get_control(request):
        return await relay("GET", "/v1/control")

    async def post_control(request):
        body = await parse(request, ControlBody)
        if isinstance(body, web.Response):
            return body
        return await relay("POST", "/v1/control", body.model_dump(exclude_none=True), request)

    async def pair(request):
        body = await parse(request, PairBody)
        if isinstance(body, web.Response):
            return body
        out = devices.pair(body.code, body.device_name.strip())
        if isinstance(out, str):
            log.warning("pairing refused: %s", out)
            return _error(403, out, "pairing_refused")
        device, token = out
        log.info("paired device %s (%s)", device["id"], device["name"])
        return web.json_response({"device_id": device["id"], "device_name": device["name"], "token": token},
                                 headers={"Cache-Control": "no-store"})

    async def unpair(request):
        devices.revoke(request[TOKEN]["id"])
        log.info("device %s unpaired itself", request[TOKEN]["id"])
        return web.json_response({"unpaired": request[TOKEN]["id"]})

    async def info(request):
        d = request[TOKEN]
        return web.json_response({"gateway_version": VERSION, "character_name": character_name(gcfg.whim_home),
                                  "device": {"id": d["id"], "name": d["name"]},
                                  "daemon_running": gcfg.upstream.exists()},
                                 headers={"Cache-Control": "no-store"})

    async def list_documents(request):
        return web.json_response({"documents": documents(gcfg.whim_home / "workspace")},
                                 headers={"Cache-Control": "no-store"})

    async def get_document(request):
        rel = request.query.get("path", "")
        if rel not in {d["path"] for d in documents(gcfg.whim_home / "workspace")}:
            return _error(404, "no such document")
        try:
            data = safefs.read_bytes(gcfg.whim_home / "workspace", rel, limit=MAX_DOCUMENT_BYTES)
        except (OSError, safefs.UnsafePath):
            return _error(404, "no such document")
        truncated = len(data) > MAX_DOCUMENT_BYTES
        return web.json_response({"path": rel, "text": data[:MAX_DOCUMENT_BYTES].decode("utf-8", errors="replace"),
                                  "truncated": truncated}, headers={"Cache-Control": "no-store"})

    async def not_relayed(request):
        return _error(404, "not available through the gateway")

    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY_BYTES)
    app.on_cleanup.append(close_upstream)
    r = app.router
    r.add_get("/v1/snapshot", snapshot)
    r.add_get("/v1/events", events)
    r.add_post("/v1/input", post_input)
    r.add_get("/v1/questions", get_questions)
    r.add_post("/v1/questions/{id}/answer", post_answer)
    r.add_get("/v1/approvals", get_approvals)
    r.add_post("/v1/approvals/{id}", post_approval)
    r.add_get("/v1/control", get_control)
    r.add_post("/v1/control", post_control)
    r.add_post("/gateway/v1/pair", pair)
    r.add_post("/gateway/v1/unpair", unpair)
    r.add_get("/gateway/v1/info", info)
    r.add_get("/gateway/v1/documents", list_documents)
    r.add_get("/gateway/v1/document", get_document)
    r.add_route("*", "/{tail:.*}", not_relayed)
    return app


# -- serving and the command line ---------------------------------------------------------


def prepare_listen_dir(path: Path) -> None:
    """The socket's directory must be ours and owner-only: nobody else may reach it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.parent.stat()
    if st.st_uid != os.getuid():
        raise SystemExit(f"{path.parent} isn't owned by you")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path.parent, 0o700)


async def serve(gcfg: GatewayConfig) -> None:
    prepare_listen_dir(gcfg.listen)
    runner = web.AppRunner(make_app(gcfg), handle_signals=False, shutdown_timeout=1.0,
                           access_log_format='%r %s %b %Tfs')
    await runner.setup()
    gcfg.listen.unlink(missing_ok=True)
    old = os.umask(0o177)
    try:
        await web.UnixSite(runner, str(gcfg.listen)).start()
    finally:
        os.umask(old)
    log.info("listening on %s for tailscaled (uids %s), login %s; relaying to %s", gcfg.listen,
             ",".join(map(str, gcfg.trusted_proxy_uids)), gcfg.allowed_login, gcfg.upstream)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()
    gcfg.listen.unlink(missing_ok=True)


def cmd_init(args: argparse.Namespace) -> None:
    path: Path = args.config
    if path.exists() and not args.force:
        raise SystemExit(f"{path} already exists (use --force to replace it)")
    home = (args.whim_home or whim_config.default_home()).expanduser()
    text = (
        "# whim-gateway: relays requests from Tailscale Serve to the whim control socket.\n"
        "# Only this Tailscale login may use it, and only from a paired device.\n"
        f"allowed_login = {json.dumps(args.login)}\n"
        f"whim_home = {json.dumps(str(home))}\n"
        f"listen = {json.dumps(str(args.listen or default_listen()))}\n"
        "# Processes allowed to connect to the socket: tailscaled runs as root.\n"
        "trusted_proxy_uids = [0]\n"
    )
    _write_private(path, text.encode())
    print(f"wrote {path}")


def cmd_pair(gcfg: GatewayConfig, args: argparse.Namespace) -> None:
    code = Devices(gcfg.devices_path).start_pairing(args.minutes)
    print(f"Pairing code (valid {args.minutes:g} minutes, once): {code}")
    print(f"In the app, enter your host's Serve URL (https://<host>.<tailnet>.ts.net) and this code.")
    print(f"Only {gcfg.allowed_login}, connecting through Tailscale Serve, can use it.")


def cmd_devices(gcfg: GatewayConfig) -> None:
    data = Devices(gcfg.devices_path).snapshot()
    if not data["devices"]:
        print("no paired devices")
    for d in data["devices"]:
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["last_seen_at"])) if d.get("last_seen_at") else "never"
        made = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["created_at"]))
        print(f"{d['id']}  {d['name']}  paired {made}  last seen {seen}")
    if data.get("pairing") and data["pairing"]["expires_at"] > _now():
        print("a pairing code is active")


def cmd_check(gcfg: GatewayConfig) -> int:
    problems = 0

    def say(ok: bool, text: str) -> None:
        nonlocal problems
        problems += not ok
        print(("ok    " if ok else "PROBLEM ") + text)

    say(gcfg.upstream.exists(), f"whim control socket {gcfg.upstream}")
    say(gcfg.listen.exists(), f"gateway socket {gcfg.listen} (is `whim-gateway serve` running?)")
    if gcfg.listen.parent.exists():
        mode = stat.S_IMODE(gcfg.listen.parent.stat().st_mode)
        say(mode & 0o077 == 0, f"{gcfg.listen.parent} is owner-only ({oct(mode)})")
    say(gcfg.trusted_proxy_uids == (0,), f"trusted proxy uids {list(gcfg.trusted_proxy_uids)} (tailscaled is uid 0)")
    say(not str(gcfg.state_dir).startswith(str(gcfg.whim_home)), f"state {gcfg.state_dir} is outside the whim home")
    n = len(Devices(gcfg.devices_path).snapshot()["devices"])
    print(f"info  {n} paired device(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="whim-gateway", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=default_config_path())
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write a gateway configuration")
    p.add_argument("--login", required=True, help="the Tailscale login allowed to connect (you@example.com)")
    p.add_argument("--whim-home", type=Path, default=None)
    p.add_argument("--listen", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    sub.add_parser("serve", help="run the gateway (for Tailscale Serve to proxy to)")
    p = sub.add_parser("pair", help="show a one-time code to pair a phone")
    p.add_argument("--minutes", type=float, default=PAIRING_MINUTES)
    sub.add_parser("devices", help="list paired devices")
    p = sub.add_parser("revoke", help="unpair a device")
    p.add_argument("device_id")
    sub.add_parser("check", help="check the setup")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "init":
        return cmd_init(args)
    gcfg = load_config(args.config)
    if args.cmd == "serve":
        asyncio.run(serve(gcfg))
    elif args.cmd == "pair":
        cmd_pair(gcfg, args)
    elif args.cmd == "devices":
        cmd_devices(gcfg)
    elif args.cmd == "revoke":
        if not Devices(gcfg.devices_path).revoke(args.device_id):
            raise SystemExit(f"no such device: {args.device_id} (see `whim-gateway devices`)")
        print(f"unpaired {args.device_id}; its token no longer works")
    elif args.cmd == "check":
        sys.exit(cmd_check(gcfg))


if __name__ == "__main__":
    main()
