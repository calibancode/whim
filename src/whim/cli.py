"""whim command line: init | run | chat | send | wake | events | status | config | access | restart | ..."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import AsyncIterator

import aiohttp

from whim import config as cfgmod
from whim import credentials, onboard
from whim.config import Config, ConfigError
from whim.provider.base import ProviderError

KEEPALIVE_GRACE = 45.0  # the daemon sends a keepalive every 15 s on a quiet stream
UNREACHABLE = (aiohttp.ClientError, OSError, asyncio.TimeoutError)
# Failing to connect at all means the request never left: definitely not received. Any
# other failure may come after the daemon acted on it: the outcome is unknown.
NOT_REACHED = (aiohttp.ClientConnectorError, FileNotFoundError, ConnectionRefusedError)
UNKNOWN_OUTCOME = ("  (outcome unknown: whim may have received that; check /status or /questions "
                   "before repeating it)")
MAX_EVENT_BYTES = 8 * 1024 * 1024  # the daemon's documented largest event (server.MAX_EVENT_BYTES)
OVERSIZED = {"type": "whim.client.oversized"}  # stands in for a frame larger than that


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=str(cfg.socket_path)),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
        )

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.session.close()

    async def post(self, path: str, body: dict) -> dict:
        status, data = await self.try_post(path, body)
        if status >= 400:
            raise SystemExit(f"error {status}: {data.get('error')}")
        return data

    async def try_post(self, path: str, body: dict) -> tuple[int, dict]:
        async with self.session.post(f"http://whim{path}", json=body) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except ValueError:
                data = {"error": text.strip() or r.reason}  # e.g. a plain-text 413 from the HTTP layer
            return r.status, data

    async def get(self, path: str) -> dict:
        async with self.session.get(f"http://whim{path}") as r:
            return await r.json()

    async def events(self, after: int, follow: bool) -> AsyncIterator[dict]:
        async for seq, ev in self.frames(after, follow):
            if ev is OVERSIZED:
                print(f"whim: event {seq} is larger than {MAX_EVENT_BYTES} bytes; skipped", file=sys.stderr)
            elif ev is not None:
                yield ev

    async def frames(self, after: int, follow: bool, on_open=None) -> AsyncIterator[tuple[int | None, dict | None]]:
        """(seq, event) per complete SSE frame; event is None for a frame that only moves
        the cursor (an event this viewer isn't entitled to). A followed stream that goes
        quiet past the server's keepalive is treated as dropped."""
        url = f"http://whim/v1/events?after={after}&follow={int(follow)}"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=KEEPALIVE_GRACE if follow else None)
        async with self.session.get(url, timeout=timeout) as r:
            if r.status != 200:
                raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status)
            if on_open is not None:
                on_open()
            seq, data, oversized = None, [], False
            async for raw in _lines(r.content, MAX_EVENT_BYTES + 64):
                if raw is None:  # a line over the limit: its frame can't be read
                    oversized = True
                    continue
                line = raw.decode().rstrip("\r")
                if line.startswith("id:"):
                    seq = int(line[3:].strip())
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "":
                    if oversized:
                        yield seq, OVERSIZED
                    elif seq is not None or data:
                        yield seq, (json.loads("\n".join(data)) if data else None)
                    seq, data, oversized = None, [], False


async def _lines(stream: aiohttp.StreamReader, limit: int) -> AsyncIterator[bytes | None]:
    """Lines without their newline, each at most `limit` bytes; None in place of a longer
    one, whose bytes are discarded as they arrive (memory stays bounded)."""
    buf, skipping = b"", False
    async for chunk in stream.iter_any():
        buf += chunk
        while (i := buf.find(b"\n")) >= 0:
            line, buf = buf[:i], buf[i + 1:]
            if skipping:
                skipping = False
                yield None
            elif len(line) > limit:
                yield None
            else:
                yield line
        if len(buf) > limit:
            skipping, buf = True, b""


def render_activity(ev: dict) -> str | None:
    t, d = ev["type"], ev.get("data", {})
    run = (ev.get("run_id") or "")[-6:]
    match t:
        case "run.started":
            return f"· run {run} started ({ev.get('origin', {}).get('kind')})"
        case "run.finished" | "run.paused" | "run.failed" | "run.interrupted":
            return f"· run {run} {t.split('.')[1]} ({d.get('reason')})"
        case "message.completed":
            text = d["content"].strip().replace("\n", " ")
            return f"· authored: {text[:100]}{'…' if len(text) > 100 else ''}"
        case "tool.finished":
            return f"· {d.get('tool')} → {d.get('status')}"
        case "delivery.settled" if d.get("destination") != "human":
            return f"· delivery to {d.get('destination')}: {d.get('status')}"
        case "provider.request.finished":
            u = d.get("usage", {})
            if u.get("cache_hit_tokens") is not None:
                return f"· model request {d.get('request_index')}: cache {u['cache_hit_tokens']} hit / {u['cache_miss_tokens']} miss"
        case "host.warning" | "recovery.pending":
            return f"· {t}: {json.dumps(d)[:160]}"
    return None


class Outbox:
    """Messages typed in chat, sent in order. Each keeps its payload and client_message_id
    until the daemon answers: a lost acknowledgement is retried with the same id, which
    the daemon deduplicates, so a message can never arrive twice. Only an HTTP refusal
    drops one (reported as rejected)."""

    def __init__(self, c: "Client"):
        self.c = c
        self.items: list[dict] = []  # {"body", "state": "new" | "unsent" | "unknown"}
        self.changed = asyncio.Event()

    def add(self, text: str, when: str = "next_step") -> None:
        body = {"text": text, "client_message_id": str(uuid.uuid4())}
        if when != "next_step":
            body["when"] = when
        self.items.append({"body": body, "state": "new"})
        self.changed.set()

    async def run(self) -> None:
        delay = 0.5
        while True:
            await self.changed.wait()
            self.changed.clear()
            while self.items:
                item = self.items[0]
                try:
                    status, data = await self.c.try_post("/v1/input", item["body"])
                except NOT_REACHED:
                    if item["state"] == "new":
                        print("  (whim isn't reachable; your message is kept and will be sent when it is)",
                              flush=True)
                    item["state"] = "unsent" if item["state"] != "unknown" else "unknown"
                except UNREACHABLE:
                    if item["state"] != "unknown":
                        print("  (not confirmed: whim may already have it; retrying with the same message id, "
                              "so it can't arrive twice)", flush=True)
                    item["state"] = "unknown"
                else:
                    self.items.pop(0)
                    delay = 0.5
                    if status >= 400:
                        print(f"  (rejected by whim, not sent: {data.get('error')})", flush=True)
                    elif item["state"] != "new":
                        print("  (message confirmed" + (", it had arrived the first time)" if data.get("duplicate")
                                                        else ")"), flush=True)
                    continue
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def drain(self, timeout: float) -> list[dict]:
        """Wait up to `timeout` for everything to be confirmed; returns what wasn't."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self.items and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        return self.items


CHAT_HELP = """commands:
  /answer [N] [words]   answer the latest question (choice N, words, or both)
  /questions            list unanswered questions
  /steer <text>         get this to the character right away, even mid-wake
  /later <text>         hold a message until its current wake ends
  /stop                 stop the current wake at a safe point
  /interrupt            stop it immediately
  /status               what it's doing now
  /help                 this list"""


def render_question(d: dict) -> str:
    lines = [f"  [question {d['question_id']}] answer with /answer"
             + (" <number> [words]" if d.get("choices") else " <words>")]
    return "\n".join(lines)


async def chat_command(c: Client, line: str, latest_question: list, outbox: Outbox) -> None:
    cmd, _, rest = line[1:].partition(" ")
    rest = rest.strip()
    mid = str(uuid.uuid4())
    if cmd == "answer":
        qs = (await c.get("/v1/questions"))["pending"]
        if not qs:
            print("  (no unanswered questions)")
            return
        q = next((x for x in qs if x["id"] == (latest_question[0] if latest_question else None)), qs[-1])
        first, _, words = rest.partition(" ")
        body = {"choice": int(first), "text": words or None} if first.isdigit() else {"text": rest}
        status, data = await c.try_post(f"/v1/questions/{q['id']}/answer", {**body, "client_message_id": mid})
        print(f"  (answered {q['id']})" if status < 400 else f"  ({data.get('error')})")
    elif cmd == "questions":
        for q in (await c.get("/v1/questions"))["pending"]:
            print(f"  {q['id']}: {q['text']}")
            for i, ch in enumerate(q["choices"], 1):
                print(f"     {i}. {ch}")
    elif cmd in ("stop", "interrupt"):
        status, data = await c.try_post("/v1/control", {"action": cmd})
        print(f"  ({cmd} requested)" if status < 400 else f"  ({data.get('error')})")
    elif cmd == "steer" and rest:
        status, data = await c.try_post("/v1/control", {"action": "steer", "text": rest, "client_message_id": mid})
        print("  (steering)" if status < 400 else f"  ({data.get('error')})")
    elif cmd == "later" and rest:
        outbox.add(rest, when="next_run")
        print("  (held until its current wake ends)")
    elif cmd == "status":
        cur = await c.get("/v1/control")
        print("  " + (json.dumps(cur) if cur.get("active_run") else "(idle)"))
    else:
        print(CHAT_HELP)


async def cmd_chat(cfg: Config, activity: bool) -> None:
    async with Client(cfg) as c:
        snap = await c.get("/v1/snapshot")
        for m in snap["messages_to_human"][-5:]:
            print(f"◆ {m['content']}")
        print("(connected; Ctrl-D to leave, /help for commands. The character replies only when it chooses to.)")
        latest_question: list = []
        for q in (await c.get("/v1/questions"))["pending"][-1:]:
            latest_question[:] = [q["id"]]
            print(render_question({"question_id": q["id"], "choices": q["choices"]}))

        def show(ev: dict) -> None:
            d = ev.get("data", {})
            if ev["type"] == "delivery.settled" and d.get("destination") == "human" and d.get("status") == "sent":
                print(f"\n◆ {d['content']}\n", flush=True)
            elif ev["type"] == "question.created":
                latest_question[:] = [d["question_id"]]
                print(render_question(d), flush=True)
            elif ev["type"] == "approval.requested":
                print(f"  [approval {d['approval_id']}] {d['summary']}\n  (whim approve {d['approval_id']} / "
                      f"whim deny {d['approval_id']})", flush=True)
            elif activity and (line := render_activity(ev)):
                print(line, flush=True)

        async def follow() -> None:
            """Follow the event stream for as long as the chat is open, resuming from the
            last event processed whenever the connection drops (daemon restart, crash)."""
            cursor, down, delay = snap["cursor"], False, 0.5

            def opened() -> None:
                nonlocal down, delay
                if down:
                    print("  (reconnected; catching up)", flush=True)
                down, delay = False, 0.5

            while True:
                try:
                    async for seq, ev in c.frames(cursor, follow=True, on_open=opened):
                        if ev is OVERSIZED:
                            print(f"  (event {seq} is too large to show; skipped)", flush=True)
                        elif ev is not None:
                            try:
                                show(ev)
                            except Exception as e:  # noqa: BLE001 - one bad event must not stop the chat
                                print(f"  (couldn't show event {seq}: {type(e).__name__}: {e})", flush=True)
                        if seq is not None:
                            cursor = seq  # only after the event is handled: never skipped silently
                except UNREACHABLE:
                    pass
                except Exception as e:  # noqa: BLE001 - e.g. a malformed stream: report it and reconnect
                    print(f"  (event stream error: {type(e).__name__}: {e})", flush=True)
                if not down:
                    print("  (lost the connection to whim; reconnecting...)", flush=True)
                    down = True
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

        follower = asyncio.create_task(follow())
        outbox = Outbox(c)
        sender = asyncio.create_task(outbox.run())
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                line = line.rstrip("\n")
                if line.startswith("/"):
                    # Controls are not retried: stop, interrupt and steer act on whatever is
                    # running when they arrive, so a blind repeat could hit a later wake.
                    try:
                        await chat_command(c, line, latest_question, outbox)
                    except NOT_REACHED:
                        print("  (not sent: whim isn't reachable right now)", flush=True)
                    except UNREACHABLE:
                        print(UNKNOWN_OUTCOME, flush=True)
                elif line.strip():
                    outbox.add(line)
            left = await outbox.drain(10.0)
            for item in left:
                what = ("may or may not have arrived" if item["state"] == "unknown" else "was not sent")
                print(f"  (leaving with an unconfirmed message, which {what}: {item['body']['text'][:60]!r})",
                      flush=True)
        finally:
            follower.cancel()
            sender.cancel()


async def cmd_events(cfg: Config, after: int, follow: bool) -> None:
    async with Client(cfg) as c:
        async for ev in c.events(after, follow):
            print(json.dumps(ev, ensure_ascii=False), flush=True)


def read_secret(prompt: str) -> str:
    """Read a secret without echo. Never accepted as a command-line argument (shell history)."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().strip()  # piped, e.g. from a password manager


def cmd_auth(args: argparse.Namespace, home: Path) -> None:
    try:
        env_var = cfgmod.load(home).provider.api_key_env
    except ConfigError:
        env_var = cfgmod.ProviderConfig().api_key_env
    try:
        match args.action:
            case "set":
                key = read_secret(f"{args.provider} API key (input hidden): ")
                path = credentials.store(args.provider, key)
                print(f"saved {args.provider} API key to {path} (owner-only)")
                if os.environ.get(env_var):
                    print(f"note: ${env_var} is set and overrides the saved key")
            case "remove":
                removed = credentials.remove(args.provider)
                print(f"removed {args.provider} API key" if removed else f"no saved {args.provider} API key")
            case "status":
                found = credentials.lookup(args.provider, env_var)
                print(f"{args.provider}: configured ({found[1]})" if found else f"{args.provider}: not configured")
    except credentials.CredentialError as e:
        raise SystemExit(str(e)) from None


def daemon_running(cfg: Config) -> bool:
    import fcntl

    lock = cfg.run_dir / "whim.lock"
    if not lock.exists():
        return False
    with open(lock, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def _config_pairs(items: list[str]) -> dict[str, str]:
    if len(items) == 2 and "=" not in items[0]:
        return {items[0]: items[1]}
    out = {}
    for item in items:
        key, eq, value = item.partition("=")
        if not eq:
            raise SystemExit(f"expected key=value, got {item!r}")
        out[key.strip()] = value.strip()
    return out


def _show_value(v) -> str:
    return json.dumps(v) if isinstance(v, str) else str(v).lower() if isinstance(v, bool) else str(v)


def cmd_config(args: argparse.Namespace, home: Path) -> None:
    """Inspect, change and validate settings. Works with or without a running daemon: a
    running daemon applies changes itself (live where possible); otherwise the file is
    updated directly, with the same validation."""
    from whim import settings

    if args.action == "validate":
        import importlib
        import pkgutil

        import whim

        try:
            cfg = cfgmod.load(home)
        except ConfigError as e:
            raise SystemExit(f"invalid: {e}") from None
        for mod in pkgutil.walk_packages(whim.__path__, "whim."):
            if mod.name != "whim.__main__":
                importlib.import_module(mod.name)
        if args.startup:  # what `whim run` also needs; failing that exits 78, which isn't restarted
            from whim.provider import ProviderError, from_config

            try:
                from_config(cfg.provider)
            except ProviderError as e:
                raise SystemExit(f"invalid: the provider can't be set up: {e}") from None
        print(f"ok: {cfg.config_path} is valid and the whim package ({Path(whim.__file__).parent}) imports")
        return
    try:
        cfg = cfgmod.load(home)
    except ConfigError as e:
        raise SystemExit(str(e)) from None
    if args.action in ("show", "get"):
        waiting: list[str] = []
        if daemon_running(cfg) and cfg.socket_path.exists():
            async def fetch():
                async with Client(cfg) as c:
                    return await c.get("/v1/config")
            try:
                waiting = asyncio.run(fetch()).get("waiting_for_restart", [])
            except UNREACHABLE:
                pass
        if args.action == "get":
            if not args.items:
                raise SystemExit("usage: whim config get KEY")
            try:
                d = settings.describe(cfg, args.items[0])
            except ConfigError as e:
                raise SystemExit(str(e)) from None
            print(f"{d['key']} = {_show_value(d['value'])}\n  type {d['type']}, default {_show_value(d['default'])}, "
                  f"applies: {d['applies']}" + ("; character changes need approval" if d["needs_approval"] else "")
                  + ("\n  saved, waiting for a restart" if d["key"] in waiting else ""))
            return
        values = settings.effective(cfg)
        prefix = args.items[0] if args.items else ""
        if args.json:
            print(json.dumps({k: v for k, v in values.items() if k.startswith(prefix)}, indent=2))
            return
        for k, v in values.items():
            if k.startswith(prefix):
                marks = {"restart": " (restart)", "fixed": " (fixed)"}.get(settings.policy(k).apply, "")
                print(f"{k} = {_show_value(v)}{marks}" + ("   ← saved, waiting for a restart" if k in waiting else ""))
        return
    # set
    changes = _config_pairs(args.items)
    if daemon_running(cfg):
        async def post():
            async with Client(cfg) as c:
                return await c.post("/v1/config", {"changes": changes})
        out = asyncio.run(post())
    else:
        try:
            _, after, diff = settings.update(home, changes)
        except ConfigError as e:
            raise SystemExit(f"not saved: {e}") from None
        out = {"changed": sorted(diff), "applied_now": [], "saved_waiting_for_restart": []}
    print(f"changed: {', '.join(out['changed']) or 'nothing (already set)'}")
    if out["applied_now"]:
        print(f"in effect now: {', '.join(out['applied_now'])}")
    if out["saved_waiting_for_restart"]:
        print(f"saved, takes effect after a restart: {', '.join(out['saved_waiting_for_restart'])} (whim restart)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="whim")
    ap.add_argument("--home", type=Path, default=None, help="harness home (default $WHIM_HOME or ~/.local/share/whim)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create or complete the harness home (no model calls)")
    p.add_argument("--name", default="", help="optional suggested name for the character")
    p.add_argument("--model", default=None)
    p = sub.add_parser("run", help="run the character daemon in the foreground")
    p.add_argument("--no-heartbeat", action="store_true", help="disable heartbeats for this session")
    p = sub.add_parser("chat", help="talk with the character")
    p.add_argument("--activity", action="store_true", help="also show runs, tools and authored text")
    p = sub.add_parser("send", help="send one message")
    p.add_argument("text")
    p.add_argument("--next-run", action="store_true", help="hold it until the character's current wake ends")
    sub.add_parser("questions", help="list the character's unanswered questions")
    p = sub.add_parser("answer", help="answer a question: a choice number, words, or both")
    p.add_argument("id")
    p.add_argument("choice", nargs="?")
    p.add_argument("--text", default=None)
    p = sub.add_parser("stop", help="stop the current wake at a safe point (--now: interrupt immediately)")
    p.add_argument("--now", action="store_true")
    p = sub.add_parser("steer", help="get a message to the character right away, even mid-wake")
    p.add_argument("text")
    p = sub.add_parser("wake", help="wake the character without a message")
    p.add_argument("--text", default="")
    p = sub.add_parser("events", help="print ledger events as JSON lines")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--follow", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("doctor", help="diagnose this whim home (read-only unless --fix)")
    p.add_argument("--fix", action="store_true", help="apply narrow safe repairs (permissions, stale socket...)")
    p.add_argument("--live", action="store_true", help="also check that DeepSeek is reachable (no tokens used)")
    p = sub.add_parser("service", help="run whim as a systemd --user service")
    p.add_argument("action", choices=["print", "install", "uninstall"])
    p.add_argument("--enable", action="store_true", help="install: also enable and start it now")
    sub.add_parser("approvals", help="list pending approval requests, grants and plugins")
    p = sub.add_parser("approve", help="approve a pending request")
    p.add_argument("id")
    p.add_argument("--always-sandboxed", action="store_true",
                   help="also let future plugins run sandboxed without asking (extra permissions still ask)")
    p = sub.add_parser("deny", help="deny a pending request")
    p.add_argument("id")
    p = sub.add_parser("revoke", help="revoke a grant")
    p.add_argument("id")
    p = sub.add_parser("config", help="inspect, change and validate settings (secrets: see `whim auth`)")
    p.add_argument("action", choices=["show", "get", "set", "validate"])
    p.add_argument("items", nargs="*", help="show: [section]; get: KEY; set: KEY VALUE or KEY=VALUE ...")
    p.add_argument("--json", action="store_true", help="show: as JSON")
    p.add_argument("--startup", action="store_true", help=argparse.SUPPRESS)  # validate: also the provider
    p = sub.add_parser("access", help="folders, shell commands and network the character may use")
    p.add_argument("action", nargs="?", default="list", choices=["list", "grant", "revoke"])
    p.add_argument("target", nargs="?", help="a folder path, `self` (whim's own source), `shell` or `network`")
    p.add_argument("--write", action="store_true", help="grant: read-write (default read-only); "
                                                         "revoke: only the write access")
    p.add_argument("--tool", action="store_true", help="grant: also put the folder's programs on PATH for shell "
                                                        "commands (e.g. ~/.cargo/bin)")
    p.add_argument("--yes", action="store_true", help="grant: don't ask to confirm warnings")
    p = sub.add_parser("restart", help="restart the service daemon after its current wake (checked first)")
    p.add_argument("--reason", default="requested by the human")
    p = sub.add_parser("auth", help="manage provider API keys (stored owner-only, outside the repo)")
    p.add_argument("action", choices=["set", "status", "remove"])
    p.add_argument("provider", nargs="?", default="deepseek", choices=list(credentials.PROVIDERS))
    args = ap.parse_args(argv)
    home = (args.home or cfgmod.default_home()).expanduser().resolve()

    if args.cmd == "init":
        cfg, created = onboard.init(home, seed_name=args.name, model=args.model)
        for path in created:
            print(f"created {path}")
        print(f"home: {home}\nnext: whim auth set && whim run")
        return

    if args.cmd == "auth":
        cmd_auth(args, home)
        return

    if args.cmd == "config":
        cmd_config(args, home)
        return

    if args.cmd == "doctor":
        from whim import doctor as doc

        findings = doc.Doctor(home, fix=args.fix, live=args.live).run()
        print(doc.report(findings))
        sys.exit(1 if any(f.level == "error" for f in findings) else 0)

    try:
        cfg = cfgmod.load(home)
    except ConfigError as e:
        print(e, file=sys.stderr)
        sys.exit(78 if args.cmd == "run" else 1)

    if args.cmd == "run":
        from whim.server import AlreadyRunning, serve

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if args.no_heartbeat:
            cfg.heartbeat.enabled = False
        from whim import service

        try:
            if asyncio.run(serve(cfg)):
                from whim import restart

                sys.exit(restart.EXIT_RESTART)  # the systemd unit starts whim again
        except AlreadyRunning as e:  # distinct exit codes so a supervisor won't restart-loop
            print(e, file=sys.stderr)
            sys.exit(service.EXIT_ALREADY_RUNNING)
        except (ProviderError, ConfigError) as e:
            print(e, file=sys.stderr)
            sys.exit(service.EXIT_CONFIG)
        return

    if args.cmd == "service":
        from whim import service

        if args.action == "print":
            print(service.unit_text(cfg), end="")
        elif args.action == "install":
            for line in service.install(cfg, enable=args.enable):
                print(line)
            if not args.enable:
                print("enable with: systemctl --user enable --now whim.service")
        else:
            for line in service.uninstall():
                print(line)
        return

    if not cfg.socket_path.exists():
        raise SystemExit("the whim daemon is not running (start it with `whim run`)")

    async def oneshot(path: str, body: dict) -> None:
        # Input carries a client_message_id the daemon deduplicates, so it is retried with
        # that same id. Anything else acts on current state and is never repeated blindly.
        retry = path == "/v1/input"
        unknown = False
        async with Client(cfg) as c:
            for attempt in range(5 if retry else 1):
                try:
                    print(json.dumps(await c.post(path, body)))
                    return
                except NOT_REACHED:
                    pass
                except UNREACHABLE:
                    unknown = True
                if retry:
                    await asyncio.sleep(0.5 * 2 ** attempt)
        if not unknown:
            raise SystemExit("not sent: the whim daemon isn't reachable")
        raise SystemExit("outcome unknown: the daemon may have received this; check `whim status`, "
                         "`whim questions` or `whim events` before repeating it")

    async def show_questions() -> None:
        async with Client(cfg) as c:
            qs = (await c.get("/v1/questions"))["pending"]
        for q in qs:
            print(f"{q['id']}: {q['text']}")
            for i, ch in enumerate(q["choices"], 1):
                print(f"    {i}. {ch}")
            print(f"    answer: whim answer {q['id']} " + ("<number> [--text words]" if q["choices"] else "--text words"))
        if not qs:
            print("no unanswered questions")

    async def show_approvals() -> None:
        async with Client(cfg) as c:
            data = await c.get("/v1/approvals")
        for a in data["pending"]:
            print(f"PENDING {a['id']}  {a['summary']}\n        approve: whim approve {a['id']}   deny: whim deny {a['id']}")
        if not data["pending"]:
            print("no pending requests")
        for g in data["grants"]:
            print(f"grant {g['id']}: {g['scope']} for {g['subject']}")
        for p in data["plugins"]:
            print(f"plugin {p['impl']}: {p['status']}")

    async def show_access() -> None:
        async with Client(cfg) as c:
            data = await c.get("/v1/access")
        for r in data["roots"]:
            print(f"{r['access']:<10}  {r['path']}  ({r['kind']}{', on PATH' if r['path'] in data['tools_on_path'] else ''})")
        print(f"shell commands: {'allowed' if data['shell'] else 'not allowed'}"
              f"; network for them: {'allowed' if data['shell_network'] else 'not allowed'}")
        for s in data["sessions"]:
            print(f"session {s['session_id']} {s['status']}: {s['command'][:80]}")

    def access_body() -> dict:
        if not args.target:
            raise SystemExit("give a folder path, `self`, `shell` or `network`")
        target = args.target
        if target not in ("self", "shell", "network"):
            target = os.path.abspath(os.path.expanduser(target))
        return {"action": args.action, "target": target, "write": args.write, "tool": args.tool}

    async def grant_access() -> None:
        body = {**access_body(), "confirm": args.yes}
        async with Client(cfg) as c:
            status, out = await c.try_post("/v1/access", body)
            if status == 409 and out.get("warnings"):
                for w in out["warnings"]:
                    print(f"warning: {w}", file=sys.stderr)
                if not sys.stdin.isatty():
                    raise SystemExit("not granted: confirm with --yes")
                if input("Grant anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                    raise SystemExit("not granted")
                status, out = await c.try_post("/v1/access", {**body, "confirm": True})
        if status >= 400:
            raise SystemExit(out.get("error", f"failed ({status})"))
        print(json.dumps(out))

    async def status() -> None:
        async with Client(cfg) as c:
            print(json.dumps(await c.get("/v1/snapshot"), indent=2, ensure_ascii=False))

    try:
        match args.cmd:
            case "chat":
                asyncio.run(cmd_chat(cfg, args.activity))
            case "send":
                asyncio.run(oneshot("/v1/input", {"text": args.text, "client_message_id": str(uuid.uuid4()),
                                                  "when": "next_run" if args.next_run else "next_step"}))
            case "questions":
                asyncio.run(show_questions())
            case "answer":
                choice = int(args.choice) if args.choice and args.choice.isdigit() else None
                text = args.text if args.text is not None else (args.choice if choice is None else None)
                asyncio.run(oneshot(f"/v1/questions/{args.id}/answer", {"choice": choice, "text": text,
                                                                         "client_message_id": str(uuid.uuid4())}))
            case "stop":
                asyncio.run(oneshot("/v1/control", {"action": "interrupt" if args.now else "stop"}))
            case "steer":
                asyncio.run(oneshot("/v1/control", {"action": "steer", "text": args.text,
                                                    "client_message_id": str(uuid.uuid4())}))
            case "wake":
                asyncio.run(oneshot("/v1/wake", {"text": args.text}))
            case "events":
                asyncio.run(cmd_events(cfg, args.after, args.follow))
            case "status":
                asyncio.run(status())
            case "approvals":
                asyncio.run(show_approvals())
            case "approve" | "deny":
                asyncio.run(oneshot(f"/v1/approvals/{args.id}", {
                    "decision": args.cmd, "standing": bool(getattr(args, "always_sandboxed", False))}))
            case "revoke":
                asyncio.run(oneshot(f"/v1/grants/{args.id}/revoke", {}))
            case "access" if args.action == "list":
                asyncio.run(show_access())
            case "access" if args.action == "grant":
                asyncio.run(grant_access())
            case "access":
                asyncio.run(oneshot("/v1/access", access_body()))
            case "restart":
                asyncio.run(oneshot("/v1/restart", {"reason": args.reason}))
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    except aiohttp.ClientConnectionError as e:
        raise SystemExit(f"cannot reach the whim daemon: {e}") from None
