"""Plugins: install → validate → (approve) → activate, and the tool catalog they join.

A plugin is a small folder with `plugin.json` and a Python entry file that speaks MCP over
stdio (the bundled `whim_plugin` helper makes that a few lines). The host:

- install   copies the folder into a content-addressed store; its identity is
            `plugin:<name>@<hash>`, so later edits to the source never change it
- validate  runs it once in the strictest sandbox (no data, no network, short timeout)
            to list its tools, then checks names, schemas and sizes; nothing is trusted
            from the plugin except what passes these checks
- activate  needs every required scope granted for this exact implementation; missing
            scopes become one approval request for the designated human
- catalog   the active set of tools (built-ins + active plugin tools) is recorded as
            numbered generations; each provider request uses one generation and its
            tool calls are dispatched to exactly the implementations it listed

Plugin tools are never replay-safe and their annotations are ignored for trust.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from whim import authz, jsonschemas, mcp, safefs, sandbox
from whim.ledger import dumps
from whim.tools.registry import Tool, ToolResult

if TYPE_CHECKING:
    from whim.harness import Harness
    from whim.run import RunState

NAME_RE = re.compile(r"^[a-z][a-z0-9]{1,23}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
RESERVED = {"message", "turn", "file", "journal", "cron", "heartbeat", "history", "bootstrap", "plugin", "tool",
            "whim", "host", "system", "human", "question", "shell", "access", "config"}
MAX_FILES, MAX_BYTES, MAX_TOOLS = 50, 1024 * 1024, 20
NATIVE_DESCRIPTION_CHARS = 300
VALIDATION_LIMITS = sandbox.Limits(seconds=15, memory_bytes=768 * 1024 * 1024)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins(
  impl TEXT PRIMARY KEY,             -- plugin:<name>@<hash12>
  name TEXT NOT NULL,
  hash TEXT NOT NULL,
  status TEXT NOT NULL,              -- validated | active | denied | failed | superseded | removed
  manifest TEXT NOT NULL,
  tools TEXT NOT NULL DEFAULT '[]',
  error TEXT,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_generations(
  gen INTEGER PRIMARY KEY,
  digest TEXT NOT NULL,
  tools TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

HELP = """\
Plugins add reusable tools of your own. Use one when a capability would be worth having
again, not for one-off work.

Layout (write the files with file_write, e.g. under plugins/<name>/):
  plugins/wordcount/plugin.json
    {"name": "wordcount", "description": "Counting things in text.", "entry": "server.py",
     "permissions": [], "timeout_seconds": 20}
  plugins/wordcount/server.py
    from whim_plugin import Plugin
    plugin = Plugin("wordcount")

    @plugin.tool
    def count(text: str) -> dict:
        "Count the words in a text."
        return {"words": len(text.split())}

    plugin.run()

Rules: name is 2-24 lowercase letters/digits (not a built-in name); tool names are
lowercase with underscores; parameters come from type hints (str, int, float, bool,
list, dict) and defaults make them optional; the docstring is the description. Plain
Python standard library only. Each tool appears to you as <plugin>_<tool>.

Sandbox: no network, no access to your files, a private /tmp, and a persistent /data
folder of its own. Extra permissions can be requested in plugin.json: "net",
"workspace.read" (your workspace at /workspace, read-only), "workspace.write".

Workflow: plugin_manage install (copies and checks the code; reports problems) →
plugin_manage activate. Running new code, and any extra permission, needs your human's
approval: activate then tells you it is waiting, and the tool appears once they approve,
even within the same wake. Installing again with changed files makes a new version that
needs activating again. plugin_manage list shows your plugins; remove deactivates one.
"""


def ensure_schema(led) -> None:
    led.db.executescript(SCHEMA)


def store_root(h: "Harness") -> Path:
    return h.cfg.home / "plugins" / "store"


def data_root(h: "Harness") -> Path:
    return h.cfg.home / "plugins" / "data"


# -- install and validate -------------------------------------------------------------


class PluginRejected(Exception):
    pass


def _collect(h: "Harness", src: Path) -> list[tuple[str, bytes]]:
    """Read the plugin folder without following links. A folder inside the workspace (which
    plugins may have written to) is walked from the workspace root, so even a linked
    plugin folder itself is refused."""
    root, rel = src, ""
    try:
        rel = src.relative_to(h.cfg.workspace).as_posix()
        root = h.cfg.workspace
    except ValueError:
        pass
    try:
        files = safefs.walk_files(root, rel, max_files=MAX_FILES)
    except safefs.UnsafePath as e:
        raise PluginRejected(f"{e} (symbolic links are not allowed in plugins)") from None
    except (FileNotFoundError, NotADirectoryError):
        raise PluginRejected("no such plugin folder") from None
    if not files:
        raise PluginRejected("the folder is empty")
    if len(files) > MAX_FILES or sum(len(b) for _, b in files) > MAX_BYTES:
        raise PluginRejected(f"plugins are limited to {MAX_FILES} files and {MAX_BYTES // 1024} KiB")
    return files


def _manifest(files: list[tuple[str, bytes]]) -> dict:
    raw = dict(files).get("plugin.json")
    if raw is None:
        raise PluginRejected("plugin.json is missing")
    try:
        m = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PluginRejected(f"plugin.json is not valid JSON: {e}") from None
    if not isinstance(m, dict):
        raise PluginRejected("plugin.json must be an object")
    name = m.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PluginRejected("name must be 2-24 lowercase letters or digits, starting with a letter")
    if name in RESERVED:
        raise PluginRejected(f"{name!r} is reserved for built-in tools")
    entry = m.get("entry", "server.py")
    if not isinstance(entry, str) or not re.fullmatch(r"[A-Za-z0-9_]+\.py", entry) or entry not in dict(files):
        raise PluginRejected("entry must name a .py file at the top of the plugin folder")
    perms = m.get("permissions", [])
    if not isinstance(perms, list) or any(p not in authz.SCOPES[1:] for p in perms):
        raise PluginRejected(f"permissions may only include {', '.join(authz.SCOPES[1:])}")
    timeout = m.get("timeout_seconds", 20)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise PluginRejected("timeout_seconds must be between 1 and 120")
    return {"name": name, "entry": entry, "permissions": sorted(set(perms)), "timeout_seconds": float(timeout),
            "description": str(m.get("description", ""))[:500], "version": str(m.get("version", ""))[:40]}


def _check_tools(tools: list, plugin: str) -> list[dict]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    if not tools:
        raise PluginRejected("the plugin offers no tools")
    if len(tools) > MAX_TOOLS:
        raise PluginRejected(f"at most {MAX_TOOLS} tools per plugin")
    out, seen = [], set()
    for t in tools:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not TOOL_RE.match(t["name"]):
            raise PluginRejected("every tool needs a lowercase name (letters, digits, underscores)")
        if t["name"] in seen:
            raise PluginRejected(f"tool {t['name']!r} is listed twice")
        seen.add(t["name"])
        schema = t.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise PluginRejected(f"tool {t['name']!r}: inputSchema must be a JSON Schema object")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as e:
            raise PluginRejected(f"tool {t['name']!r}: invalid inputSchema: {e.message}") from None
        refs = jsonschemas.external_references(schema)
        if refs:
            raise PluginRejected(f"tool {t['name']!r}: inputSchema uses external references or $id "
                                 f"({refs[0]}); only '#/...' references within the schema are allowed")
        if len(dumps(schema)) > 8000:
            raise PluginRejected(f"tool {t['name']!r}: inputSchema is too large")
        desc = t.get("description") or ""
        if not isinstance(desc, str) or not desc.strip():
            raise PluginRejected(f"tool {t['name']!r} needs a description")
        out.append({"name": t["name"], "description": desc.strip()[:4000], "inputSchema": schema})
    return out


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_dirs_durable(base: Path, target: Path) -> None:
    """Create `target` (under the existing `base`) one folder at a time, syncing each new
    folder's parent so the whole path survives a crash."""
    path = base
    for part in target.relative_to(base).parts:
        parent, path = path, path / part
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            continue
        _fsync_dir(parent)


def _remove_staging(tmp: Path) -> None:
    """Remove a staging folder an earlier attempt left behind, though it may already be
    read-only. Never follows links: a link in its place is removed itself, and only
    folders reached without following links are made writable again (via their fds)."""
    try:
        st = os.lstat(tmp)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(tmp)
        return
    for _top, _dirs, _files, dfd in os.fwalk(tmp, follow_symlinks=False):
        os.fchmod(dfd, 0o700)
    shutil.rmtree(tmp)  # fd-based on Linux: safe against links swapped in meanwhile


def _stage(tmp: Path, files: list[tuple[str, bytes]]) -> None:
    for rel, data in files:
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    for p in sorted(tmp.rglob("*"), reverse=True):
        p.chmod(0o555 if p.is_dir() else 0o444)
        if p.is_dir():
            _fsync_dir(p)
    tmp.chmod(0o555)
    _fsync_dir(tmp)


async def install(h: "Harness", src: Path, source: str) -> dict:
    """Copy, identify and validate a plugin. Returns its row (status validated/failed)."""
    files = _collect(h, src)
    manifest = _manifest(files)
    digest = hashlib.sha256()
    for rel, data in files:
        digest.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    full = digest.hexdigest()
    impl = f"plugin:{manifest['name']}@{full[:12]}"
    existing = h.ledger.q1("SELECT * FROM plugins WHERE impl = ?", impl)
    if existing is not None and existing["status"] not in ("failed", "removed"):
        return dict(existing)
    dest = store_root(h) / full
    if not dest.exists():
        # Fully durable before the ledger records it: an existing store entry is trusted
        # as is (never rewritten), so a torn copy after a power cut would stay torn.
        _make_dirs_durable(h.cfg.home, store_root(h))
        tmp = dest.with_name(dest.name + ".tmp")
        _remove_staging(tmp)  # raises if it can't: never stage on top of leftovers
        try:
            _stage(tmp, files)
            tmp.rename(dest)
        except BaseException:
            try:
                _remove_staging(tmp)
            except OSError:
                h.log.exception("could not remove plugin staging folder %s", tmp)  # retried next time
            raise
    _fsync_dir(dest.parent)  # also when an earlier attempt failed right after renaming
    now = h.time.iso()
    with h.ledger.tx():
        h.ledger.x("INSERT OR REPLACE INTO plugins(impl, name, hash, status, manifest, source, created_at, updated_at) "
                   "VALUES(?, ?, ?, 'installing', ?, ?, ?, ?)", impl, manifest["name"], full, dumps(manifest),
                   source, now, now)
        h.ledger.append("plugin.installed", {"impl": impl, "name": manifest["name"], "source": source,
                                             "permissions": manifest["permissions"]})
    try:
        listed = await mcp.list_tools(h.cfg.plugins.python, manifest["entry"],
                                      sandbox.Mounts(plugin_dir=dest), VALIDATION_LIMITS)
        tools = _check_tools(listed, manifest["name"])
        for t in tools:
            wire = f"{manifest['name']}_{t['name']}"
            clash = h.registry.by_wire(wire)
            if clash is not None:
                raise PluginRejected(f"tool {wire!r} would collide with {clash.name}")
    except (PluginRejected, mcp.PluginError, sandbox.SandboxUnavailable) as e:
        detail = str(e)
        if isinstance(e, mcp.PluginError) and e.stderr.strip():
            detail += "\nstderr: " + e.stderr.strip()[-1500:]
        with h.ledger.tx():
            h.ledger.x("UPDATE plugins SET status = 'failed', error = ?, updated_at = ? WHERE impl = ?",
                       detail, h.time.iso(), impl)
            h.ledger.append("plugin.failed", {"impl": impl, "error": detail[:2000]})
        return dict(h.ledger.q1("SELECT * FROM plugins WHERE impl = ?", impl))
    with h.ledger.tx():
        h.ledger.x("UPDATE plugins SET status = 'validated', tools = ?, error = NULL, updated_at = ? WHERE impl = ?",
                   dumps(tools), h.time.iso(), impl)
        h.ledger.append("plugin.validated", {"impl": impl, "tools": [t["name"] for t in tools]})
    return dict(h.ledger.q1("SELECT * FROM plugins WHERE impl = ?", impl))


def required_scopes(row) -> list[str]:
    return ["plugin.run", *json.loads(row["manifest"])["permissions"]]


def latest(h: "Harness", name: str):
    return h.ledger.q1("SELECT * FROM plugins WHERE name = ? AND status NOT IN ('superseded', 'removed') "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", name)


def activate(h: "Harness", impl: str, reason: str) -> None:
    """Make `impl` the active version of its plugin. Call inside a transaction."""
    led = h.ledger
    row = led.q1("SELECT * FROM plugins WHERE impl = ?", impl)
    for old in led.q("SELECT impl FROM plugins WHERE name = ? AND status = 'active' AND impl != ?", row["name"], impl):
        led.x("UPDATE plugins SET status = 'superseded', updated_at = ? WHERE impl = ?", h.time.iso(), old["impl"])
        led.append("plugin.deactivated", {"impl": old["impl"], "reason": "superseded"})
    led.x("UPDATE plugins SET status = 'active', updated_at = ? WHERE impl = ?", h.time.iso(), impl)
    led.append("plugin.activated", {"impl": impl, "tools": [f"{row['name']}_{t['name']}"
                                                            for t in json.loads(row["tools"])]})
    commit_generation(h, reason)


def deactivate(h: "Harness", impl: str, status: str, reason: str) -> None:
    h.ledger.x("UPDATE plugins SET status = ?, updated_at = ? WHERE impl = ?", status, h.time.iso(), impl)
    h.ledger.append("plugin.deactivated", {"impl": impl, "reason": reason})
    commit_generation(h, reason)


# -- catalog --------------------------------------------------------------------------


def _plugin_tools(h: "Harness", row) -> list[Tool]:
    manifest = json.loads(row["manifest"])
    out = []
    for spec in json.loads(row["tools"]):
        desc = spec["description"]
        short = desc if len(desc) <= NATIVE_DESCRIPTION_CHARS else desc[:NATIVE_DESCRIPTION_CHARS] + "…"
        out.append(Tool(
            name=f"{row['name']}.{spec['name']}",
            description=f"{short} (plugin {row['name']})",
            params=None,
            json_schema=spec["inputSchema"],
            effect="plugin",
            impl=row["impl"],
            handler=_runner(row["impl"], manifest, spec["name"]),
            help=desc,
        ))
    return out


def active_tools(h: "Harness") -> list[Tool]:
    tools = []
    for row in h.ledger.q("SELECT * FROM plugins WHERE status = 'active' ORDER BY name"):
        if not authz.missing(h, row["impl"], required_scopes(row)):
            tools.extend(_plugin_tools(h, row))
    return tools


def snapshot(h: "Harness") -> tuple[int, dict[str, Tool]]:
    """The tools for one provider request: (generation, wire name -> tool)."""
    tools = {t.wire_name: t for t in h.registry.offered(h)}
    for t in active_tools(h):
        tools.setdefault(t.wire_name, t)  # a built-in name always wins (install also refuses clashes)
    gen = h.ledger.q1("SELECT MAX(gen) g FROM catalog_generations")["g"] or 0
    return gen, dict(sorted(tools.items()))


def _digest(h: "Harness") -> tuple[str, list[dict]]:
    entries = [{"name": t.wire_name, "impl": t.impl, "schema": hashlib.sha256(dumps(t.schema()).encode()).hexdigest()[:12]}
               for t in sorted(list(h.registry.all()) + active_tools(h), key=lambda t: t.wire_name)]
    return hashlib.sha256(dumps(entries).encode()).hexdigest(), entries


def commit_generation(h: "Harness", reason: str) -> int:
    """Record a new catalog generation if the set of tools changed. Inside a transaction."""
    led = h.ledger
    digest, entries = _digest(h)
    last = led.q1("SELECT gen, digest, tools FROM catalog_generations ORDER BY gen DESC LIMIT 1")
    if last is not None and last["digest"] == digest:
        return last["gen"]
    gen = (last["gen"] if last else 0) + 1
    led.x("INSERT INTO catalog_generations(gen, digest, tools, reason, created_at) VALUES(?, ?, ?, ?, ?)",
          gen, digest, dumps(entries), reason, h.time.iso())
    before = {e["name"]: e for e in json.loads(last["tools"])} if last else {}
    after = {e["name"]: e for e in entries}
    led.append("catalog.changed", {
        "generation": gen, "reason": reason,
        "added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
        "changed": sorted(n for n in set(after) & set(before) if after[n] != before[n]),
    })
    return gen


# -- execution ------------------------------------------------------------------------


def mounts_for(h: "Harness", row, manifest: dict) -> sandbox.Mounts:
    data = data_root(h) / row["name"]
    data.mkdir(parents=True, exist_ok=True)
    perms = set(manifest["permissions"])
    ws = h.cfg.workspace if perms & {"workspace.read", "workspace.write"} else None
    return sandbox.Mounts(plugin_dir=store_root(h) / row["hash"], data_dir=data, workspace=ws,
                          workspace_writable="workspace.write" in perms, network="net" in perms)


def _runner(impl: str, manifest: dict, tool_name: str):
    async def run(state: "RunState", args: Any) -> ToolResult:
        h = state.harness
        row = h.ledger.q1("SELECT * FROM plugins WHERE impl = ?", impl)
        # Re-check at call time: the version this request was offered must still be the
        # active, authorized one. Never fall through to a different implementation.
        if row is None or row["status"] != "active":
            return ToolResult(False, {"error": "this tool's plugin version is no longer active; it was not run",
                                      "outcome": "not_run"})
        need = authz.missing(h, impl, required_scopes(row))
        if need:
            return ToolResult(False, {"error": f"not authorized ({', '.join(need)}); it was not run",
                                      "outcome": "not_run"})
        result = await mcp.call_tool(h.cfg.plugins.python, manifest["entry"], mounts_for(h, row, manifest),
                                     sandbox.Limits(seconds=manifest["timeout_seconds"]), tool_name, args)
        content = result.get("content") if isinstance(result.get("content"), list) else []
        text = "\n".join(str(c.get("text", ""))[:20000] for c in content if isinstance(c, dict))
        data: dict = {"result": text[:20000]}
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and len(dumps(structured)) <= 20000:
            data = {"result": structured}
        if result.get("isError"):
            return ToolResult(False, {"error": text[:4000] or "the plugin reported an error"})
        return ToolResult(True, data)
    return run
