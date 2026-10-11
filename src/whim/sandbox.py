"""Run untrusted plugin code under bubblewrap.

Every plugin process gets a fresh, minimal filesystem view:
- the system's /usr read-only (for the Python interpreter and its stdlib),
- the plugin's own frozen code at /plugin (read-only),
- whim's stdlib-only plugin helper at /opt/whim (read-only),
- an empty private /tmp,
- optionally its persistent data folder at /data, and the character's workspace at
  /workspace (read-only or writable) when that scope was granted.
Nothing else from the host is visible: not the harness home, its database or control
socket, the credentials file, or the user's home. The environment is cleared, all
namespaces are unshared (no network unless granted), the process dies with its parent,
and CPU, memory, file size and open files are limited.
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path

SDK_DIR = Path(__file__).with_name("plugin_sdk")
MAX_MESSAGE_BYTES = 1024 * 1024


class SandboxUnavailable(Exception):
    pass


@dataclass
class Mounts:
    plugin_dir: Path
    data_dir: Path | None = None
    workspace: Path | None = None
    workspace_writable: bool = False
    network: bool = False


@dataclass
class Limits:
    seconds: float = 30.0
    memory_bytes: int = 1024 * 1024 * 1024
    file_bytes: int = 64 * 1024 * 1024
    open_files: int = 256


def bwrap_path() -> str:
    path = shutil.which("bwrap")
    if not path:
        raise SandboxUnavailable("bubblewrap (bwrap) is not installed; plugins cannot run")
    return path


def _system_binds() -> list[str]:
    args = ["--ro-bind", "/usr", "/usr"]
    for top in ("/bin", "/lib", "/lib64", "/sbin"):
        p = Path(top)
        if p.is_symlink():
            args += ["--symlink", os.readlink(p), top]
        elif p.is_dir():
            args += ["--ro-bind", top, top]
    for f in ("/etc/ld.so.cache", "/etc/localtime"):
        args += ["--ro-bind-try", f, f]
    return args


def command(python: str, entry: str, m: Mounts) -> list[str]:
    args = [bwrap_path(), "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            *_system_binds(), "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--ro-bind", str(SDK_DIR), "/opt/whim", "--ro-bind", str(m.plugin_dir), "/plugin"]
    if m.network:
        args += ["--share-net"]
        for f in ("/etc/resolv.conf", "/etc/hosts", "/etc/ssl", "/etc/ca-certificates", "/etc/nsswitch.conf"):
            args += ["--ro-bind-try", f, f]
    if m.data_dir is not None:
        args += ["--bind", str(m.data_dir), "/data"]
    if m.workspace is not None:
        args += ["--bind" if m.workspace_writable else "--ro-bind", str(m.workspace), "/workspace"]
    bootstrap = ("import sys,runpy; sys.path[:0]=['/opt/whim','/plugin']; "
                 f"runpy.run_path('/plugin/{entry}', run_name='__main__')")
    return [*args, "--chdir", "/plugin", python, "-I", "-c", bootstrap]


def _limit(lim: Limits):
    def apply() -> None:
        cpu = int(lim.seconds) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (lim.memory_bytes, lim.memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim.file_bytes, lim.file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (lim.open_files, lim.open_files))
    return apply


@dataclass
class Process:
    proc: asyncio.subprocess.Process
    stderr_tail: bytearray = field(default_factory=bytearray)

    async def send(self, message: bytes) -> None:
        self.proc.stdin.write(message + b"\n")
        await self.proc.stdin.drain()

    async def receive(self) -> bytes:
        line = await self.proc.stdout.readline()
        if not line:
            raise EOFError("plugin closed its output")
        return line

    async def close(self) -> None:
        if self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 1.0)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                self.kill()
                await self.proc.wait()

    def kill(self) -> None:
        if self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def start(python: str, entry: str, mounts: Mounts, limits: Limits) -> Process:
    proc = await asyncio.create_subprocess_exec(
        *command(python, entry, mounts),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, preexec_fn=_limit(limits), limit=MAX_MESSAGE_BYTES, env={},
    )
    p = Process(proc)

    async def drain_stderr() -> None:
        while chunk := await proc.stderr.read(4096):
            p.stderr_tail.extend(chunk)
            del p.stderr_tail[:-4096]

    asyncio.get_running_loop().create_task(drain_stderr())
    return p
