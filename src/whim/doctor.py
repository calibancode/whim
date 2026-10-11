"""whim doctor: offline diagnostics for a whim home.

Read-only unless `fix=True`, and then only narrow repairs that touch no character state:
file permissions, a stale control socket, a leftover BOOTSTRAP.md after the first
meeting, and missing starter files. Provider connectivity is checked only with
`live=True` (a model-listing call; no tokens). Findings never include credentials or
the contents of character files.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from whim import config as cfgmod
from whim import credentials, safefs
from whim.config import CHARACTER_FILES, Config, ConfigError

LEVELS = ("ok", "info", "warn", "error")


@dataclass
class Finding:
    level: str
    check: str
    message: str
    fix: str | None = None  # how to resolve it (or what --fix did)


class Doctor:
    def __init__(self, home: Path, *, fix: bool = False, live: bool = False):
        self.home, self.fix, self.live = home, fix, live
        self.findings: list[Finding] = []
        self.cfg: Config | None = None

    def add(self, level: str, check: str, message: str, fix: str | None = None) -> None:
        self.findings.append(Finding(level, check, message, fix))

    def _mode(self, path: Path, want: int, check: str) -> None:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & ~want:
            if self.fix:
                path.chmod(want)
                self.add("ok", check, f"{path.name} was mode {oct(mode)}; set to {oct(want)}", "fixed")
            else:
                self.add("warn", check, f"{path.name} is mode {oct(mode)}; should be {oct(want)}",
                         "whim doctor --fix")

    # -- checks ------------------------------------------------------------------

    def run(self) -> list[Finding]:
        if not self.home.exists():
            self.add("error", "home", f"no whim home at {self.home}", "whim init")
            return self.findings
        self._mode(self.home, 0o700, "home")
        try:
            self.cfg = cfgmod.load(self.home)
            self._mode(self.cfg.config_path, 0o600, "config")
            self.add("ok", "config", f"valid (model {self.cfg.provider.model}, time zone {self.cfg.timezone})")
        except ConfigError as e:
            self.add("error", "config", str(e), "fix config.toml or run whim init")
            return self.findings
        for check in (self.credentials, self.database, self.workspace, self.daemon, self.sandbox, self.plugins,
                      self.state, self.service):
            try:
                check()
            except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
                self.add("error", check.__name__, f"check failed: {type(e).__name__}: {e}")
        if self.live:
            self.provider()
        return self.findings

    def credentials(self) -> None:
        env = self.cfg.provider.api_key_env
        if self.cfg.provider.kind != "deepseek":
            self.add("info", "credentials", f"provider kind {self.cfg.provider.kind!r} needs no key")
            return
        path = credentials.default_path()
        if path.exists():
            st = path.stat()
            if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o077 and self.fix:
                path.chmod(0o600)
                self.add("ok", "credentials", "credentials file was readable by others; set to 0600", "fixed")
        try:
            found = credentials.lookup("deepseek", env)
        except credentials.CredentialError as e:
            self.add("error", "credentials", str(e), "whim doctor --fix (permissions) or whim auth set")
            return
        if found is None:
            self.add("error", "credentials", "no DeepSeek API key configured", "whim auth set")
        else:
            source = "environment override" if found[1].startswith("$") else "credentials file"
            self.add("ok", "credentials", f"DeepSeek key present ({source}); not shown")

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{self.cfg.db_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    def _q(self, sql: str, *args):
        try:
            with self._db() as c:
                return c.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []  # table from a later version not created yet

    def database(self) -> None:
        if not self.cfg.db_path.exists():
            self.add("error", "database", "whim.db is missing", "whim init")
            return
        self._mode(self.cfg.db_path, 0o600, "database")
        try:
            with self._db() as c:
                result = c.execute("PRAGMA integrity_check").fetchone()[0]
                mode = c.execute("PRAGMA journal_mode").fetchone()[0]
                seq = c.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        except sqlite3.DatabaseError as e:
            self.add("error", "database", f"cannot read whim.db: {e}", "restore from a backup")
            return
        if result != "ok":
            self.add("error", "database", f"integrity check failed: {result[:200]}", "restore from a backup")
        else:
            self.add("ok", "database", f"integrity ok, {mode} journal, {seq} events")

    def workspace(self) -> None:
        from whim import onboard

        ws = self.cfg.workspace
        boot = self._q("SELECT value FROM meta WHERE key = 'bootstrap'")
        status = json.loads(boot[0]["value"]).get("status") if boot else None
        lived = status == "complete" or bool(self._q("SELECT 1 FROM runs LIMIT 1"))
        present = ws.is_dir()
        missing = [n for n in CHARACTER_FILES if not present or safefs.kind(ws, n) == "missing"]
        unsafe = [n for n in CHARACTER_FILES if present and safefs.kind(ws, n) == "other"]
        if unsafe:
            self.add("error", "workspace", f"not regular files (e.g. links): {', '.join(unsafe)}; whim won't read them",
                     "replace them with regular files")
        what = "the workspace folder" if not present else ", ".join(missing)
        if missing and not self.fix:
            if lived:
                self.add("error", "workspace", f"missing {what}: these held the character's own identity and "
                         "memory. --fix can only write blank scaffolds; it cannot recover the originals.",
                         "restore from a backup if you have one; otherwise whim doctor --fix")
            else:
                self.add("warn", "workspace", f"missing {what}", "whim doctor --fix (writes starter files)")
        elif missing:
            ws.mkdir(parents=True, exist_ok=True)
            for n in missing:
                text = onboard.STARTERS[n]
                safefs.create_exclusive(ws, n, (text.format(seed="") if n == "IDENTITY.md" else text).encode())
            if lived:
                self.add("warn", "workspace", f"wrote blank scaffolds for {', '.join(missing)}; the original contents "
                         "were NOT recovered (past events remain in the ledger, journal history and checkpoints)",
                         "fixed (scaffolds only)")
            else:
                self.add("ok", "workspace", f"recreated starter files: {', '.join(missing)}", "fixed")
        elif not unsafe:
            self.add("ok", "workspace", "character files present")

        note = ws / "BOOTSTRAP.md"
        note_kind = safefs.kind(ws, "BOOTSTRAP.md") if ws.is_dir() else "missing"
        if status == "complete" and note_kind != "missing":
            if self.fix:
                note.unlink()
                self.add("ok", "bootstrap", "removed BOOTSTRAP.md left after the completed first meeting", "fixed")
            else:
                self.add("warn", "bootstrap", "first meeting complete but BOOTSTRAP.md still exists", "whim doctor --fix")
        elif status in ("pending", "active") and note_kind == "missing":
            # Losing the note mid-onboarding must not silently skip or end the first meeting.
            if self.fix:
                safefs.create_exclusive(ws, "BOOTSTRAP.md", onboard.STARTERS["BOOTSTRAP.md"].encode())
                self.add("ok", "bootstrap", f"restored the first-meeting note (meeting still {status})", "fixed")
            else:
                self.add("warn", "bootstrap", f"first meeting {status} but BOOTSTRAP.md is missing; the meeting "
                         "would be treated as skipped", "whim doctor --fix")
        else:
            self.add("ok" if status == "complete" else "info", "bootstrap", f"first meeting {status or 'not started'}")

    def daemon_running(self) -> bool:
        lock = self.cfg.run_dir / "whim.lock"
        if not lock.exists():
            return False
        with open(lock, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False

    def daemon(self) -> None:
        sock = self.cfg.socket_path
        if len(os.fsencode(sock)) > 107:
            self.add("error", "socket", "the control socket path is too long for a Unix socket", "use a shorter home")
        if self.cfg.run_dir.exists():
            self._mode(self.cfg.run_dir, 0o700, "run dir")
        running = self.daemon_running()
        if running:
            ok = sock.exists() and stat.S_ISSOCK(sock.stat().st_mode)
            self.add("ok" if ok else "error", "daemon",
                     "running" + ("" if ok else ", but its control socket is missing"),
                     None if ok else "restart the daemon")
            if ok:
                self._mode(sock, 0o600, "socket")
        elif sock.exists():
            if self.fix:
                sock.unlink()
                self.add("ok", "daemon", "not running; removed its stale control socket", "fixed")
            else:
                self.add("warn", "daemon", "not running, but a stale control socket is left", "whim doctor --fix")
        else:
            self.add("info", "daemon", "not running", "whim run, or whim service install")

    def sandbox(self) -> None:
        from whim import sandbox

        python = self.cfg.plugins.python
        if not python.startswith("/usr/") or not os.access(python, os.X_OK):
            self.add("error", "sandbox", f"plugin interpreter {python} is missing or outside /usr",
                     "set [plugins] python in config.toml")
            return
        if shutil.which("bwrap") is None:
            self.add("warn", "sandbox", "bubblewrap is not installed; plugins cannot run", "install bubblewrap")
            return
        cmd = [shutil.which("bwrap"), "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
               *sandbox._system_binds()[3:], "--proc", "/proc", "--dev", "/dev", python, "-I", "-c", "pass"]
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        if r.returncode == 0:
            self.add("ok", "sandbox", "bubblewrap works (no network, isolated namespaces)")
        else:
            self.add("error", "sandbox", "bubblewrap failed to start a sandbox: "
                     + r.stderr.decode(errors="replace").strip()[-300:],
                     "check that unprivileged user namespaces are enabled")

    def plugins(self) -> None:
        from whim import plugins as plg

        rows = self._q("SELECT * FROM plugins WHERE status IN ('active', 'failed', 'validated', 'denied')")
        if not rows:
            self.add("info", "plugins", "none installed")
            return
        for r in rows:
            label = r["impl"]
            if r["status"] != "active":
                self.add("info", "plugins", f"{label}: {r['status']}")
                continue
            store = self.cfg.home / "plugins" / "store" / r["hash"]
            if not store.is_dir():
                self.add("error", "plugins", f"{label}: its stored code is missing", "reinstall the plugin")
                continue
            files = sorted(p for p in store.rglob("*") if p.is_file())
            digest = hashlib.sha256()
            for p in files:
                digest.update(p.relative_to(store).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
            if digest.hexdigest() != r["hash"]:
                self.add("error", "plugins", f"{label}: stored code no longer matches its identity",
                         "remove and reinstall the plugin")
                continue
            needed = plg.required_scopes(r)
            granted = {g["scope"] for g in self._q(
                "SELECT scope FROM grants WHERE revoked_at IS NULL AND (subject = ? OR (subject = '*' AND scope = "
                "'plugin.run'))", r["impl"])}
            missing = [s for s in needed if s not in granted]
            self.add("warn" if missing else "ok", "plugins",
                     f"{label}: active" + (f", but not authorized for {', '.join(missing)}" if missing else ""))

    def state(self) -> None:
        def count(sql, *a):
            rows = self._q(sql, *a)
            return rows[0][0] if rows else 0

        active = count("SELECT COUNT(*) FROM runs WHERE status = 'active'")
        if active and not self.daemon_running():
            self.add("info", "runs", f"{active} run(s) left active by an unclean stop; reconciled at next start")
        uncertain = count("SELECT COUNT(*) FROM operations WHERE status = 'uncertain'")
        if uncertain:
            self.add("info", "operations", f"{uncertain} tool call(s) with uncertain outcomes on record "
                                           "(never retried automatically)")
        for sql, what, level in [
            ("SELECT COUNT(*) FROM approvals WHERE status = 'pending'", "approval request(s) waiting", "info"),
            ("SELECT COUNT(*) FROM questions WHERE status = 'pending'", "unanswered question(s)", "info"),
            ("SELECT COUNT(*) FROM jobs WHERE status = 'failed'", "scheduled job(s) quarantined as invalid", "warn"),
            ("SELECT COUNT(*) FROM compactions WHERE status IN ('maintenance', 'maintained', 'summarized')",
             "compaction in progress", "info"),
            ("SELECT COUNT(*) FROM notes WHERE status = 'pending'", "host note(s) waiting for the next wake", "info"),
        ]:
            n = count(sql)
            if n:
                self.add(level, "state", f"{n} {what}", "whim approvals" if "approval" in what else
                         ("whim questions" if "question" in what else None))
        failed = self._q("SELECT error FROM compactions WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 1")
        if failed:
            self.add("info", "compaction", f"last failed compaction: {failed[0]['error'][:200]}")
        blocked = self._q("SELECT value FROM meta WHERE key = 'context_blocked'")
        if blocked:
            b = json.loads(blocked[0]["value"])
            waiting = count("SELECT COUNT(*) FROM inputs WHERE status = 'pending'")
            self.add("error", "context", f"working context full since {b['since']} and compaction unavailable; "
                                         f"model processing is paused ({waiting} message(s) waiting, all kept)",
                     "it resumes when compaction succeeds; check provider access (whim doctor --live)")
        backoff = self._q("SELECT value FROM meta WHERE key = 'compaction_backoff'")
        if backoff:
            b = json.loads(backoff[0]["value"])
            self.add("warn", "compaction", f"{b['failures']} failed compaction(s) in a row; no new attempt before "
                                           f"{b['until']} (context keeps growing meanwhile)",
                     "check provider access (whim doctor --live)")
        hb = self._q("SELECT value FROM meta WHERE key = 'heartbeat'")
        due = json.loads(hb[0]["value"]).get("next_due_at") if hb else None
        self.add("ok", "schedule", (f"heartbeats every {self.cfg.heartbeat.interval_minutes:g} min, next due {due}"
                                    if self.cfg.heartbeat.enabled and due else
                                    "heartbeats " + ("disabled" if not self.cfg.heartbeat.enabled else "not armed yet")))

    def service(self) -> None:
        from whim import service

        path = service.unit_path()
        if path.exists():
            self.add("ok" if str(self.home) in path.read_text() else "warn", "service",
                     f"user service installed ({path.name})" + ("" if str(self.home) in path.read_text()
                                                                else ", but for a different home"))
        else:
            self.add("info", "service", "no systemd user service installed", "whim service install")

    def provider(self) -> None:
        import asyncio

        import openai

        found = credentials.lookup("deepseek", self.cfg.provider.api_key_env)
        if found is None:
            return

        async def probe():
            client = openai.AsyncOpenAI(api_key=found[0].reveal(), base_url=self.cfg.provider.base_url, timeout=20)
            try:
                models = [m.id async for m in client.models.list()]
            finally:
                await client.close()
            return models

        try:
            models = asyncio.run(probe())
        except Exception as e:  # noqa: BLE001
            self.add("error", "provider", credentials.redact(f"cannot reach DeepSeek: {type(e).__name__}: {e}",
                                                             found[0]))
            return
        want = {self.cfg.provider.model, self.cfg.compaction.model}
        unknown = sorted(want - set(models))
        self.add("warn" if unknown else "ok", "provider",
                 "reachable" + (f"; configured model(s) not listed: {', '.join(unknown)}" if unknown else
                                f"; {', '.join(sorted(want))} available"))


def report(findings: list[Finding]) -> str:
    out = []
    for f in findings:
        line = f"{f.level.upper():5} {f.check}: {f.message}"
        if f.fix and f.fix != "fixed":
            line += f"  → {f.fix}"
        out.append(line)
    return "\n".join(out)
