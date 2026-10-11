"""Minimal MCP stdio client: just what whim needs from a plugin process.

Newline-delimited JSON-RPC 2.0 over the sandboxed process's stdin/stdout: `initialize`,
`notifications/initialized`, then `tools/list` or `tools/call`. One short session per
operation, so a crashed or hung plugin never outlives the call that started it.

Everything a plugin returns is untrusted data: sizes are bounded, shapes are checked,
and self-declared annotations (e.g. readOnlyHint) are ignored for trust decisions.
"""

from __future__ import annotations

import asyncio
import json

from whim import sandbox

PROTOCOL_VERSION = "2025-06-18"


class PluginError(Exception):
    """The plugin misbehaved: crashed, timed out, or broke the protocol."""

    def __init__(self, message: str, *, timed_out: bool = False, stderr: str = ""):
        super().__init__(message)
        self.timed_out = timed_out
        self.stderr = stderr


class Session:
    def __init__(self, proc: sandbox.Process):
        self.proc = proc
        self.next_id = 0

    async def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        mid = self.next_id
        await self.proc.send(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode())
        while True:
            try:
                msg = json.loads(await self.proc.receive())
            except json.JSONDecodeError as e:
                raise PluginError(f"plugin wrote something that isn't JSON-RPC: {e}") from None
            if not isinstance(msg, dict):
                raise PluginError("plugin sent a non-object message")
            if msg.get("id") == mid and "method" not in msg:
                if "error" in msg:
                    err = msg["error"] if isinstance(msg["error"], dict) else {}
                    raise PluginError(f"{method} failed: {err.get('message', 'error')}")
                result = msg.get("result")
                if not isinstance(result, dict):
                    raise PluginError(f"{method} returned no result object")
                return result
            if "method" in msg and "id" in msg:  # a request from the server: we offer nothing
                await self.proc.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32601, "message": "not supported by this client"}}).encode())
            # notifications (logging, progress, list_changed) are ignored

    async def notify(self, method: str) -> None:
        await self.proc.send(json.dumps({"jsonrpc": "2.0", "method": method}).encode())


async def _with_session(python: str, entry: str, mounts: sandbox.Mounts, limits: sandbox.Limits, work):
    proc = await sandbox.start(python, entry, mounts, limits)
    try:
        async def go():
            s = Session(proc)
            await s.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "whim", "version": "0.1"}})
            await s.notify("notifications/initialized")
            return await work(s)
        return await asyncio.wait_for(go(), limits.seconds)
    except asyncio.TimeoutError:
        proc.kill()
        raise PluginError(f"timed out after {limits.seconds:g}s", timed_out=True,
                          stderr=proc.stderr_tail.decode(errors="replace")) from None
    except (EOFError, BrokenPipeError, ConnectionResetError, ValueError) as e:
        proc.kill()
        await proc.proc.wait()
        hint = (" It exited normally without answering: does the entry file end by calling plugin.run()?"
                if proc.proc.returncode == 0 else "")
        raise PluginError(f"plugin process ended unexpectedly ({type(e).__name__}: {e}); exit code "
                          f"{proc.proc.returncode}.{hint}", stderr=proc.stderr_tail.decode(errors="replace")) from None
    finally:
        await proc.close()


async def list_tools(python: str, entry: str, mounts: sandbox.Mounts, limits: sandbox.Limits) -> list[dict]:
    async def work(s: Session) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            result = await s.request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list):
                raise PluginError("tools/list returned no tool list")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise PluginError("tools/list never finished paging")
    return await _with_session(python, entry, mounts, limits, work)


async def call_tool(python: str, entry: str, mounts: sandbox.Mounts, limits: sandbox.Limits,
                    name: str, arguments: dict) -> dict:
    async def work(s: Session) -> dict:
        return await s.request("tools/call", {"name": name, "arguments": arguments})
    return await _with_session(python, entry, mounts, limits, work)
