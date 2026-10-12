"""Shell commands the character runs: sandboxed, bounded, optionally interactive.

Each command runs under bubblewrap with a filesystem view built from whim.access: the
system's /usr (and the /bin, /lib links), a short list of /etc files programs need, a
private /tmp (also $HOME), and every root the character may use, mounted at its real
path, read-write only where that access was granted. Nothing else on the host exists
inside: not the whim home, its database or control socket, the credentials file, or the
rest of the user's home. The network is unshared unless `shell.net` is granted.
Running commands at all needs the `shell.run` grant; within that, commands need no
individual approval.

A command either finishes within the call's wait (its output and exit status are
returned) or keeps running as a session the character can poll, write to (through a
PTY when started with tty), and kill. Sessions belong to the daemon, not to a wake: they
outlive model calls and wakes, but not the daemon. When the daemon stops they are
killed and recorded as `lost`, and the character is told at its next wake.

Bounds: a wall-clock timeout per command (killed and reported as timed_out), a retained
output buffer per session (older output is dropped and the gap reported), at most
exec.max_sessions running at once, and rlimits on memory (data segment), file size,
open files and core dumps. Killing a session signals its process group, then kills the
sandbox, which takes every process inside its PID namespace with it.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import re
import resource
import signal
import struct
import termios
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from whim import access, sandbox
from whim.context import block
from whim.ledger import new_id
from whim.tools.registry import ToolResult

if TYPE_CHECKING:
    from whim.harness import Harness
    from whim.run import RunState

MAX_RETURN_BYTES = 24 * 1024  # output returned by one call; the middle of more is elided
HEAD_BYTES = 4 * 1024
MAX_WAIT_SECONDS = 60.0
SETSID = "/usr/bin/setsid"  # util-linux; gives a tty session's command its controlling terminal
ETC_FILES = ("ld.so.cache", "ld.so.conf", "ld.so.conf.d", "localtime", "passwd", "group", "nsswitch.conf",
             "hosts", "alternatives", "ssl", "ca-certificates", "pki", "gitconfig", "inputrc", "terminfo",
             "mime.types", "os-release", "protocols", "services", "gai.conf", "profile", "bash.bashrc")
ANSI_RE = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")
SIGNALS = {"INT": signal.SIGINT, "TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "HUP": signal.SIGHUP}

SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_sessions(
  id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  cwd TEXT NOT NULL,
  tty INTEGER NOT NULL,
  network INTEGER NOT NULL,
  status TEXT NOT NULL,              -- running | exited | killed | timed_out | lost
  exit_code INTEGER,
  run_id TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  reported INTEGER NOT NULL DEFAULT 0 -- the character has seen how it ended
);
"""


def ensure_schema(led) -> None:
    led.db.executescript(SCHEMA)


class ShellError(Exception):
    pass


@dataclass
class Session:
    id: str
    command: str
    cwd: str
    tty: bool
    network: bool
    roots: list[access.Root]
    proc: asyncio.subprocess.Process
    master: int | None = None
    self_write: bool = False  # started while whim's own code was writable
    buf: bytearray = field(default_factory=bytearray)
    base: int = 0  # absolute offset of buf[0]
    cursor: int = 0  # absolute offset the character has read up to
    status: str = "running"
    exit_code: int | None = None
    started: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    output: asyncio.Event = field(default_factory=asyncio.Event)
    eof: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def end(self) -> int:
        return self.base + len(self.buf)


def _etc_binds(network: bool) -> list[str]:
    args = []
    for name in ETC_FILES + (("resolv.conf",) if network else ()):
        args += ["--ro-bind-try", f"/etc/{name}", f"/etc/{name}"]
    return args


def command_line(h: "Harness", command: str, cwd: str, roots: list[access.Root], *, tty: bool,
                 network: bool) -> list[str]:
    mounted = {r.path for r in roots}
    tools = [p for p in access.tool_dirs(h) if p in mounted]
    args = [sandbox.bwrap_path(), "--unshare-all", "--die-with-parent", "--clearenv",
            "--setenv", "PATH", ":".join([*map(str, tools), "/usr/local/bin:/usr/bin:/bin"]),
            "--setenv", "HOME", "/tmp",
            "--setenv", "LANG", "C.UTF-8", "--setenv", "TERM", "dumb", "--setenv", "NO_COLOR", "1",
            "--setenv", "PAGER", "cat", "--setenv", "GIT_PAGER", "cat",
            "--setenv", "WHIM_WORKSPACE", str(roots[0].path),
            *sandbox._system_binds(), *_etc_binds(network),
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    if not tty:
        args.append("--new-session")  # no controlling terminal to reach (TIOCSTI)
    if network:
        args.append("--share-net")
    # Roots and masks, parents before what's nested in them: a hidden place inside a root
    # is covered by an empty tmpfs (or /dev/null for a file), a read-only one is mounted
    # again read-only, and a root granted inside a hidden place is mounted after it.
    mounts = [(r.path, "bind" if r.writable else "ro-bind") for r in roots] + access.masks(h, roots)
    for path, how in sorted(mounts, key=lambda m: len(m[0].parts)):
        p = str(path)
        if how == "hidden":
            args += ["--tmpfs", p] if path.is_dir() else ["--ro-bind", "/dev/null", p]
        else:
            args += ["--bind" if how == "bind" else "--ro-bind", p, p]
    # With a terminal, the command gets its own session with the PTY as controlling
    # terminal, so Ctrl-C and friends reach it rather than the sandbox around it.
    run = [SETSID, "--ctty", "--wait"] if tty else []
    return [*args, "--chdir", cwd, *run, h.cfg.exec.shell, "-c", command]


def _preexec(h: "Harness"):
    e = h.cfg.exec
    mem, fsize = e.memory_mb * 1024 * 1024, e.file_size_mb * 1024 * 1024

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_DATA, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def _clean(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return ANSI_RE.sub("", text).replace("\r\n", "\n")


class Shell:
    """The daemon's shell sessions (one per Harness)."""

    def __init__(self, h: "Harness"):
        self.h = h
        self.sessions: dict[str, Session] = {}

    # -- lifecycle ----------------------------------------------------------------------

    def reconcile(self) -> None:
        """At startup: sessions recorded as running died with the previous daemon."""
        led = self.h.ledger
        with led.tx():
            for r in led.q("SELECT id, command FROM exec_sessions WHERE status = 'running'"):
                led.x("UPDATE exec_sessions SET status = 'lost', ended_at = ? WHERE id = ?", self.h.time.iso(), r["id"])
                led.append("exec.exited", {"session_id": r["id"], "status": "lost", "reason": "unclean_shutdown"})
            lost = led.q("SELECT id, command FROM exec_sessions WHERE status = 'lost' AND reported = 0")
            if lost:
                lines = "\n".join(f"- {r['id']}: {r['command'][:120]}" for r in lost)
                led.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'exec', ?, 'pending', ?)",
                      new_id("note"), block(f"host · shell sessions ended · {self.h.time.stamp()}",
                                            "These shell sessions were still running when the harness stopped, "
                                            "and ended with it (their output is gone):\n" + lines),
                      self.h.time.iso())
                led.x("UPDATE exec_sessions SET reported = 1 WHERE status = 'lost'")

    async def close(self) -> None:
        """Daemon shutdown: kill everything; recorded as lost."""
        for s in list(self.sessions.values()):
            if s.status == "running":
                self._finish(s, "lost", reason="daemon_stopped")
                self._kill(s, signal.SIGKILL)
        for s in list(self.sessions.values()):
            try:
                await asyncio.wait_for(s.proc.wait(), 2.0)
            except asyncio.TimeoutError:
                pass
            self._close_master(s)

    def running(self) -> list[Session]:
        return [s for s in self.sessions.values() if s.status == "running"]

    # -- start --------------------------------------------------------------------------

    async def start(self, command: str, cwd: str | None, *, tty: bool, timeout: float | None,
                    run_id: str | None) -> Session:
        h, e = self.h, self.h.cfg.exec
        if not e.enabled:
            raise ShellError("shell commands are turned off in this whim's configuration (exec.enabled)")
        if not access.shell_allowed(h):
            raise ShellError("running shell commands needs your human's permission; ask with host_access "
                             "(op request, target shell)")
        if len(self.running()) >= e.max_sessions:
            raise ShellError(f"{e.max_sessions} sessions are already running; kill one first (shell_list)")
        roots = access.roots(h)
        root, rel = access.resolve(h, cwd or str(roots[0].path))
        workdir = os.path.normpath(os.path.join(root.path, rel))
        if not os.path.isdir(workdir):
            raise ShellError(f"{workdir} is not a folder")
        if tty and not os.path.exists(SETSID):
            raise ShellError(f"tty sessions need {SETSID} (util-linux), which isn't installed")
        timeout = min(timeout or (e.max_timeout_seconds if tty else e.default_timeout_seconds), e.max_timeout_seconds)
        network = access.network_allowed(h)
        argv = command_line(h, command, workdir, roots, tty=tty, network=network)
        master = None
        if tty:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            stdio = {"stdin": slave, "stdout": slave, "stderr": slave}
        else:
            stdio = {"stdin": asyncio.subprocess.DEVNULL, "stdout": asyncio.subprocess.PIPE,
                     "stderr": asyncio.subprocess.STDOUT}
        try:
            proc = await asyncio.create_subprocess_exec(*argv, **stdio, start_new_session=True,
                                                        preexec_fn=_preexec(h), env={})
        except BaseException:
            if master is not None:
                os.close(master)
            raise
        finally:
            if tty:
                os.close(slave)
        s = Session(new_id("sh"), command, workdir, tty, network, roots, proc, master,
                    self_write=access.self_writable(h))
        s.deadline = s.started + timeout
        self.sessions[s.id] = s
        with h.ledger.tx():
            h.ledger.x("INSERT INTO exec_sessions(id, command, cwd, tty, network, status, run_id, started_at) "
                       "VALUES(?, ?, ?, ?, ?, 'running', ?, ?)", s.id, command, workdir, int(tty), int(network),
                       run_id, h.time.iso())
            h.ledger.append("exec.started", {"session_id": s.id, "command": command[:2000], "cwd": workdir,
                                             "tty": tty, "network": network, "timeout_seconds": timeout},
                            run_id=run_id, actor="character")
        loop = asyncio.get_running_loop()
        if tty:
            os.set_blocking(master, False)
            loop.add_reader(master, self._on_pty, s)
        else:
            loop.create_task(self._pump(s))
        loop.create_task(self._watch(s))
        return s

    def _append(self, s: Session, data: bytes) -> None:
        s.buf.extend(data)
        limit = self.h.cfg.exec.output_buffer_bytes
        if len(s.buf) > limit:
            drop = len(s.buf) - limit
            del s.buf[:drop]
            s.base += drop
        s.output.set()

    def _on_pty(self, s: Session) -> None:
        try:
            data = os.read(s.master, 65536)
        except OSError:  # EIO: every process holding the terminal has gone
            data = b""
        if data:
            self._append(s, data)
        else:
            asyncio.get_running_loop().remove_reader(s.master)
            s.eof.set()
            s.output.set()

    async def _pump(self, s: Session) -> None:
        while chunk := await s.proc.stdout.read(65536):
            self._append(s, chunk)
        s.eof.set()
        s.output.set()

    async def _watch(self, s: Session) -> None:
        try:
            await asyncio.wait_for(s.proc.wait(), max(0.0, s.deadline - time.monotonic()))
        except asyncio.TimeoutError:
            if s.status == "running":
                self._finish(s, "timed_out")
            self._kill(s, signal.SIGKILL)
            await s.proc.wait()
        try:  # collect what was still in flight
            await asyncio.wait_for(s.eof.wait(), 1.0)
        except asyncio.TimeoutError:
            pass
        if s.status == "running":
            self._finish(s, "exited", exit_code=s.proc.returncode)
        self._close_master(s)

    def _close_master(self, s: Session) -> None:
        if s.master is not None:
            try:
                asyncio.get_running_loop().remove_reader(s.master)
            except (RuntimeError, ValueError):
                pass
            os.close(s.master)
            s.master = None

    def _finish(self, s: Session, status: str, *, exit_code: int | None = None, reason: str | None = None) -> None:
        s.status, s.exit_code = status, exit_code
        h = self.h
        idle = h.active_run is None
        with h.ledger.tx():
            h.ledger.x("UPDATE exec_sessions SET status = ?, exit_code = ?, ended_at = ? WHERE id = ?",
                       status, exit_code, h.time.iso(), s.id)
            h.ledger.append("exec.exited", {"session_id": s.id, "status": status, "exit_code": exit_code,
                                            "seconds": round(time.monotonic() - s.started, 3),
                                            **({"reason": reason} if reason else {})})
            if idle and status != "lost" and s.cursor < s.end:
                # Ended between wakes with output nobody read: mention it at the next wake.
                h.ledger.x("INSERT INTO notes(id, kind, text, status, created_at) VALUES(?, 'exec', ?, 'pending', ?)",
                           new_id("note"), block(f"host · shell session ended · {h.time.stamp()}",
                                                 f"Session {s.id} ({s.command[:120]}) {_ended(s)}. shell_write "
                                                 "with no input shows its remaining output."),
                           h.time.iso())
        s.done.set()
        s.output.set()

    def _kill(self, s: Session, sig: int) -> None:
        try:
            os.killpg(s.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        if sig == signal.SIGKILL and s.proc.returncode is None:
            try:
                s.proc.kill()  # the sandbox's own process; its PID namespace dies with it
            except ProcessLookupError:
                pass

    # -- interaction --------------------------------------------------------------------

    def get(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            row = self.h.ledger.q1("SELECT status FROM exec_sessions WHERE id = ?", session_id)
            if row is not None:
                raise ShellError(f"{session_id} is {row['status']} and its output is no longer kept")
            raise ShellError(f"no session {session_id!r} (shell_list shows them)")
        return s

    async def write(self, s: Session, data: str) -> None:
        if s.status != "running":
            raise ShellError(f"{s.id} has already ended ({_ended(s)})")
        if not s.tty:
            raise ShellError(f"{s.id} has no terminal for input; start it with tty to interact with it")
        raw = data.encode()
        while raw:
            try:
                n = os.write(s.master, raw)
            except BlockingIOError:
                await asyncio.sleep(0.02)
                continue
            raw = raw[n:]

    async def wait(self, s: Session, seconds: float) -> None:
        """Until the session ends, or `seconds` pass; with output arriving, return once it
        has been quiet briefly (so an interactive prompt comes back promptly)."""
        deadline = time.monotonic() + min(max(seconds, 0.0), MAX_WAIT_SECONDS)
        while s.status == "running" and (left := deadline - time.monotonic()) > 0:
            s.output.clear()
            try:
                await asyncio.wait_for(s.output.wait(), left)
            except asyncio.TimeoutError:
                break
            if s.tty and s.status == "running":
                try:  # settle: keep reading while output is still flowing
                    while True:
                        s.output.clear()
                        await asyncio.wait_for(s.output.wait(), min(0.3, max(0.0, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    if s.cursor < s.end and seconds > 0:
                        break
        if s.status != "running":
            try:
                await asyncio.wait_for(s.done.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    def read(self, s: Session) -> dict:
        """New output since the last read, bounded, plus the session's state."""
        start = max(s.cursor, s.base)
        dropped = start - s.cursor
        new = bytes(s.buf[start - s.base:])
        s.cursor = s.end
        out: dict = {"session_id": s.id, "status": s.status}
        if len(new) > MAX_RETURN_BYTES:
            omitted = len(new) - MAX_RETURN_BYTES
            text = (_clean(new[:HEAD_BYTES]) + f"\n…[{omitted} bytes of output omitted]…\n"
                    + _clean(new[-(MAX_RETURN_BYTES - HEAD_BYTES):]))
        else:
            text = _clean(new)
        out["output"] = text
        if dropped:
            out["dropped_bytes"] = dropped
        if s.status == "running":
            out["running_seconds"] = round(time.monotonic() - s.started, 1)
        else:
            out["exit_code"] = s.exit_code
            if s.proc.returncode is not None and s.proc.returncode < 0:
                out["signal"] = signal.Signals(-s.proc.returncode).name
            self._mark_reported(s)
        return out

    def _mark_reported(self, s: Session) -> None:
        with self.h.ledger.tx():
            self.h.ledger.x("UPDATE exec_sessions SET reported = 1 WHERE id = ?", s.id)
        if s.cursor >= s.end:
            self.sessions.pop(s.id, None)  # nothing more to say about it

    async def kill(self, s: Session, sig: str = "TERM") -> None:
        if s.status != "running":
            return
        number = SIGNALS[sig]
        if number != signal.SIGKILL:
            self._kill(s, number)
            try:
                await asyncio.wait_for(s.done.wait(), 3.0)
                return
            except asyncio.TimeoutError:
                pass
        self._finish(s, "killed", exit_code=s.proc.returncode)
        self._kill(s, signal.SIGKILL)
        try:
            await asyncio.wait_for(s.proc.wait(), 3.0)
        except asyncio.TimeoutError:
            pass

    def enforce_access(self) -> list[str]:
        """After a revocation: kill sessions whose mounts or network are no longer allowed."""
        roots = {r.path: r.writable for r in access.roots(self.h)}
        net, shell_ok = access.network_allowed(self.h), access.shell_allowed(self.h)
        self_ok = access.self_writable(self.h)
        killed = []
        for s in self.running():
            stale = (not shell_ok or (s.network and not net) or (s.self_write and not self_ok)
                     or any(r.path not in roots or (r.writable and not roots[r.path]) for r in s.roots))
            if stale:
                self._finish(s, "killed", reason="access_revoked")
                self._kill(s, signal.SIGKILL)
                killed.append(s.id)
        return killed

    def listing(self) -> list[dict]:
        out = []
        for s in self.sessions.values():
            out.append({"session_id": s.id, "command": s.command[:200], "cwd": s.cwd, "tty": s.tty,
                        "status": s.status, "unread_bytes": max(0, s.end - max(s.cursor, s.base)),
                        **({"running_seconds": round(time.monotonic() - s.started, 1)} if s.status == "running"
                           else {"exit_code": s.exit_code})})
        return out


def _ended(s: Session) -> str:
    match s.status:
        case "exited":
            return f"exited with code {s.exit_code}"
        case "timed_out":
            return "was killed at its timeout"
        case "killed":
            return "was killed"
    return s.status


# -- tools ---------------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecArgs(_Args):
    command: str = Field(description="A shell command line (run with the configured shell's -c).")
    cwd: str | None = Field(default=None, description="Working folder: an absolute path in a folder you can use, "
                                                      "or relative to your workspace. Default: your workspace.")
    tty: bool = Field(default=False, description="Run in a terminal, so you can type to it later with shell_write "
                                                 "(interactive programs, REPLs, prompts).")
    timeout_seconds: float | None = Field(default=None, gt=0, description="Kill it after this long. Default: "
                                          "exec.default_timeout_seconds, or the maximum for tty sessions.")
    wait_seconds: float = Field(default=10.0, ge=0, le=MAX_WAIT_SECONDS,
                                description="How long to wait for it before returning; it keeps running after.")


class WriteArgs(_Args):
    session_id: str
    input: str = Field(default="", description='Text to type; include "\\n" for Enter. Control keys as '
                       'characters: "\\u0003" Ctrl-C, "\\u0004" Ctrl-D, "\\u001b" Esc. Empty: just read new output.')
    wait_seconds: float = Field(default=2.0, ge=0, le=MAX_WAIT_SECONDS)


class KillArgs(_Args):
    session_id: str
    signal: Literal["INT", "TERM", "HUP", "KILL"] = "TERM"


class ListArgs(_Args):
    pass


async def exec_tool(state: "RunState", args: ExecArgs) -> ToolResult:
    sh = state.harness.shell
    try:
        s = await sh.start(args.command, args.cwd, tty=args.tty, timeout=args.timeout_seconds, run_id=state.run_id)
    except (ShellError, PermissionError, sandbox.SandboxUnavailable) as e:
        return ToolResult(False, {"error": str(e)})
    await sh.wait(s, args.wait_seconds)
    out = sh.read(s)
    if s.status == "running":
        out["note"] = "still running; shell_write reads more output (or types to a tty), shell_kill stops it"
    return ToolResult(s.status in ("running", "exited"), out)


async def write_tool(state: "RunState", args: WriteArgs) -> ToolResult:
    sh = state.harness.shell
    try:
        s = sh.get(args.session_id)
        if args.input:
            await sh.write(s, args.input)
    except ShellError as e:
        return ToolResult(False, {"error": str(e)})
    await sh.wait(s, args.wait_seconds)
    return ToolResult(True, sh.read(s))


async def kill_tool(state: "RunState", args: KillArgs) -> ToolResult:
    sh = state.harness.shell
    try:
        s = sh.get(args.session_id)
    except ShellError as e:
        return ToolResult(False, {"error": str(e)})
    await sh.kill(s, args.signal)
    return ToolResult(True, sh.read(s))


async def list_tool(state: "RunState", args: ListArgs) -> ToolResult:
    return ToolResult(True, {"sessions": state.harness.shell.listing()})


def recorded(h: "Harness", limit: int = 20) -> list[dict]:
    return [dict(r) for r in h.ledger.q("SELECT * FROM exec_sessions ORDER BY started_at DESC LIMIT ?", limit)]





def register(reg) -> None:
    from whim.tools.registry import Tool

    reg.register(Tool(
        name="shell.exec",
        description=("Run a shell command in a sandbox that sees your workspace and the folders you have access "
                     "to (network only if granted). Returns its output and exit code, or a session id if it's "
                     "still running after wait_seconds. tty=true for interactive programs."),
        params=ExecArgs, effect="write", impl="whim.builtin.shell_exec@1", handler=exec_tool))
    reg.register(Tool(
        name="shell.write",
        description="Type input (keys, control characters) to a running tty session, and/or read its new output.",
        params=WriteArgs, effect="write", impl="whim.builtin.shell_write@1", handler=write_tool))
    reg.register(Tool(
        name="shell.kill",
        description="Stop a running session and everything it started (INT, TERM, HUP, or KILL).",
        params=KillArgs, effect="write", impl="whim.builtin.shell_kill@1", handler=kill_tool))
    reg.register(Tool(
        name="shell.list",
        description="Your shell sessions: running ones, and ended ones with output you haven't read.",
        params=ListArgs, effect="read", impl="whim.builtin.shell_list@1", handler=list_tool, replay_safe=True))
