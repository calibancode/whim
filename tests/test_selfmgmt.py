"""Self-management: settings, external folder access, the shell, restarts and the guide."""

from __future__ import annotations

import asyncio
import json
import os
import shutil

import pytest
from conftest import events, types

from whim import access, authz, restart, settings
from whim import config as cfgmod
from whim.cli import main as cli_main
from whim.config import ConfigError
from whim.provider.fake import call, finish

needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")


async def use_tools(h, rig, *steps, text="go"):
    """One wake that makes these tool calls (each a provider step), then finishes.
    Returns the parsed results in order."""
    before = h.ledger.q1("SELECT COUNT(*) c FROM operations")["c"]
    rig.provider.extend(*steps, finish())
    h.submit_input(text)
    await h.wait_idle(timeout=30)
    rows = h.ledger.q("SELECT tool, result FROM operations ORDER BY rowid")[before:]
    return [json.loads(r["result"]) for r in rows if r["tool"] != "turn.finish"]


def latest_opening(rig) -> str:
    """The opening user message of the most recent wake the provider saw."""
    msgs = rig.provider.requests[-1].messages
    return next(m["content"] for m in reversed(msgs) if m["role"] == "user")


def approve_pending(h):
    for a in h.ledger.q("SELECT id FROM approvals WHERE status = 'pending'"):
        h.decide_approval(a["id"], True, h.cfg.principal_id)


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "main.py").write_text("print('hello from project')\n")
    return d


# -- settings ------------------------------------------------------------------------------


def test_settings_validate_cross_field_and_save_atomically(cfg):
    with pytest.raises(ConfigError, match="tail_tokens < trigger_tokens"):
        settings.with_changes(cfg, {"compaction.trigger_tokens": 1000})
    with pytest.raises(ConfigError, match="needs a float"):
        settings.with_changes(cfg, {"heartbeat.interval_minutes": "soon"})
    with pytest.raises(ConfigError, match="fixed"):
        settings.with_changes(cfg, {"character.id": "other"})
    with pytest.raises(ConfigError, match="unknown setting"):
        settings.with_changes(cfg, {"heartbeat.nope": 1})
    _, after, diff = settings.update(cfg.home, {"heartbeat.interval_minutes": "30", "provider.model": "x"})
    assert diff == {"heartbeat.interval_minutes": {"old": 60.0, "new": 30.0},
                    "provider.model": {"old": "deepseek-v4-pro", "new": "x"}}
    assert cfgmod.load(cfg.home).heartbeat.interval_minutes == 30.0
    assert oct(cfg.config_path.stat().st_mode & 0o777) == "0o600"
    assert settings.pending_restart(cfg, after) == ["provider.model"]
    assert "direct.principal_id" not in settings.effective(cfg, for_character=True)


def test_config_edits_keep_comments_and_refuse_unsafe_structure(cfg):
    path = cfg.config_path
    path.write_text(path.read_text().replace("[heartbeat]", "# my note\n[heartbeat]  # inline\n# kept") + "\n[mine]\nx = 1\n")
    settings.update(cfg.home, {"heartbeat.interval_minutes": 20})
    text = path.read_text()
    assert "# my note" in text and "# inline" in text and "# kept" in text and "[mine]\nx = 1" in text
    assert "interval_minutes = 20" in text
    for odd in ('restart = { drain_seconds = 60 }\n', 'restart.drain_seconds = 60\n'):
        with pytest.raises(ConfigError, match="by hand"):
            settings.edit_text(odd, {"restart.drain_seconds": 30})


def test_cli_config_offline(cfg, capsys):
    home = str(cfg.home)
    cli_main(["--home", home, "config", "set", "heartbeat.interval_minutes", "45"])
    cli_main(["--home", home, "config", "get", "heartbeat.interval_minutes"])
    out = capsys.readouterr().out
    assert "changed: heartbeat.interval_minutes" in out and "= 45.0" in out and "applies: live" in out
    cli_main(["--home", home, "config", "validate"])
    assert "ok:" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="not saved"):
        cli_main(["--home", home, "config", "set", "exec.max_sessions=0"])


async def test_host_config_live_change_and_approval_for_sensitive(rig):
    h = await rig.start()
    r = await use_tools(h, rig, call("host_config", op="set", changes={"heartbeat.interval_minutes": 15}),
                        call("host_config", op="set", changes={"limits.max_requests_per_run": 99}),
                        call("host_config", op="get", key="limits.max_requests_per_run"))
    assert r[0]["applied_now"] == ["heartbeat.interval_minutes"] and h.cfg.heartbeat.interval_minutes == 15
    assert r[1]["status"] == "waiting_for_approval" and h.cfg.limits.max_requests_per_run == 24
    assert r[2]["needs_approval"] is True and r[2]["value"] == 24
    rig.provider.extend(finish())
    approve_pending(h)
    await h.wait_idle()
    assert h.cfg.limits.max_requests_per_run == 99
    assert cfgmod.load(h.cfg.home).limits.max_requests_per_run == 99
    changed = events(h, "config.changed")
    assert [e["data"]["by"] for e in changed] == ["character", "character"]
    r = await use_tools(h, rig, call("host_config", op="set", changes={"provider.model": "deepseek-flash"}))
    assert r[0]["status"] == "waiting_for_approval"
    rig.provider.extend(finish())
    approve_pending(h)
    await h.wait_idle()
    r = await use_tools(h, rig, call("host_config", op="get"))
    assert r[0]["saved_but_waiting_for_restart"] == ["provider.model"]
    assert h.cfg.provider.model == "deepseek-v4-pro"


# -- file access ---------------------------------------------------------------------------


async def test_implementation_is_readable_not_writable(rig):
    h = await rig.start()
    impl = access.implementation_root()
    src = next(p for p in [impl / "src/whim/compaction.py", impl / "compaction.py"] if p.exists())
    r = await use_tools(h, rig, call("file_read", path=str(src), offset=1, limit=3),
                        call("file_write", path=str(src), content="oops"),
                        call("host_guide", topic="compaction"))
    assert r[0]["ok"] and r[0]["first_line"] == 1 and "Context compaction" in r[0]["content"]
    assert not r[1]["ok"] and "read-only" in r[1]["error"] and "explicit permission" in r[1]["error"]
    assert "checkpoint" in r[2]["text"]
    assert "Context compaction" in src.read_text()


async def test_granted_folder_read_write_and_boundaries(rig, project, tmp_path):
    h = await rig.start()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (project / "link").symlink_to(outside)
    r = await use_tools(h, rig, call("file_read", path=str(project / "main.py")),
                        call("host_access", op="request", target=str(project), write=True, reason="fix a bug"))
    assert not r[0]["ok"] and "outside every folder" in r[0]["error"]
    assert r[1]["status"] == "waiting_for_approval"
    summary = h.ledger.q1("SELECT summary FROM approvals WHERE status = 'pending'")["summary"]
    assert "read and write" in summary and "fix a bug" in summary
    rig.provider.extend(finish())
    approve_pending(h)
    await h.wait_idle()
    os.chmod(project / "main.py", 0o755)
    r = await use_tools(h, rig,
                        call("file_edit", path=str(project / "main.py"), old="hello", new="hi"),
                        call("file_read", path=str(project)),
                        call("file_read", path=str(project / "link")),
                        call("file_read", path=str(project / ".." / "secret.txt")),
                        call("file_write", path=str(project / "new/deep.txt"), content="x"))
    assert r[0]["ok"] and (project / "main.py").read_text() == "print('hi from project')\n"
    assert (project / "main.py").stat().st_mode & 0o777 == 0o755  # mode preserved
    assert "main.py" in r[1]["entries"]
    assert not r[2]["ok"] and "symbolic link" in r[2]["error"]
    assert not r[3]["ok"] and "outside every folder" in r[3]["error"]
    assert r[4]["ok"] and (project / "new/deep.txt").read_text() == "x"
    h.revoke_access(str(project), write_only=True, principal_id=h.cfg.principal_id)
    r = await use_tools(h, rig, call("file_read", path=str(project / "main.py")),
                        call("file_write", path=str(project / "main.py"), content="x"))
    assert r[0]["ok"] and not r[1]["ok"]
    h.revoke_access(str(project), write_only=False, principal_id=h.cfg.principal_id)
    r = await use_tools(h, rig, call("file_read", path=str(project / "main.py")))
    assert not r[0]["ok"]


async def test_grant_policy_refuses_only_whims_own_boundaries(rig, project, tmp_path, monkeypatch):
    fh = tmp_path / "fakehome"
    for d in (".ssh", ".config/app", ".local/bin"):
        (fh / d).mkdir(parents=True)
    (fh / ".ssh/id").write_text("KEY")
    (fh / ".config/app/settings").write_text("ok")
    monkeypatch.setenv("HOME", str(fh))
    h = await rig.start()
    pid = h.cfg.principal_id
    creds = access.own_dirs(h)[1]
    for bad in [h.cfg.home, h.cfg.home / "workspace", creds, "/", "/proc", "relative/path"]:
        with pytest.raises(authz.AuthzError):
            h.grant_access(str(bad), write=False, principal_id=pid)
    with pytest.raises(authz.AuthzError):
        h.grant_access(str(project), write=False, principal_id="someone-else")
    out = h.grant_access(str(project), write=False, principal_id=pid)  # ordinary: no confirmation
    assert {"path": str(project.resolve()), "access": "read-only", "kind": "granted"} in out["roots"]
    assert "grant.created" in types(h)
    # Risky but legitimate: warned, granted once confirmed.
    for target, write, warning in [(fh, False, "whole home folder"), (fh / ".ssh", False, "credentials"),
                                   (fh / ".local/bin", True, "outside the sandbox")]:
        with pytest.raises(access.ConfirmationNeeded) as e:
            h.grant_access(str(target), write=write, principal_id=pid)
        assert warning in " ".join(e.value.warnings)
    run_user = f"/run/user/{os.getuid()}"
    if os.path.isdir(run_user):
        assert "control sockets" in " ".join(access.warnings(h, access._real(run_user), write=False))
    # A broad grant never shows whim's own state, credentials or secret stores.
    h.grant_access(str(tmp_path), write=False, principal_id=pid, confirm=True)
    h.grant_access(str(fh), write=True, principal_id=pid, confirm=True)
    r = await use_tools(h, rig, call("file_read", path=str(h.cfg.db_path)),
                        call("file_read", path=str(fh / ".ssh/id")),
                        call("file_read", path=str(fh / ".config/app/settings")),
                        call("file_write", path=str(fh / ".local/bin/tool"), content="#!/bin/sh\n"),
                        call("file_read", path=str(h.cfg.workspace)))
    assert not r[0]["ok"] and "isn't visible" in r[0]["error"]
    assert not r[1]["ok"] and "isn't visible" in r[1]["error"]
    assert r[2]["ok"] and r[3]["ok"] and r[4]["ok"]
    # ...unless that place is granted itself.
    h.grant_access(str(fh / ".ssh"), write=False, principal_id=pid, confirm=True)
    r = await use_tools(h, rig, call("file_read", path=str(fh / ".ssh/id")))
    assert r[0]["content"] == "KEY"


async def test_self_is_the_installed_code_not_the_working_directory(rig, project, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "path", [str(project), *sys.path])  # `python -m` run from there
    h = await rig.start()
    assert not any(access._within(project.resolve(), p) for p in access.self_paths())
    assert access.implementation_root() in access.self_paths()
    assert not any("self" in w for w in access.warnings(h, project.resolve(), write=True))
    h.grant_access(str(project), write=True, principal_id=h.cfg.principal_id)
    r = await use_tools(h, rig, call("file_write", path=str(project / "main.py"), content="x"))
    assert r[0]["ok"]


@needs_bwrap
async def test_broad_grants_mask_whim_state_secrets_sockets_and_self(rig, project, tmp_path, monkeypatch):
    import socket

    fh = tmp_path / "fakehome"
    (fh / ".gnupg").mkdir(parents=True)
    (fh / ".gnupg/key").write_text("SECRET")
    agent = socket.socket(socket.AF_UNIX)
    agent.bind(str(fh / ".gnupg/S.agent"))
    agent.listen()
    selfdir = tmp_path / "selfenv"
    selfdir.mkdir()
    monkeypatch.setenv("HOME", str(fh))
    real_self = access.self_paths
    monkeypatch.setattr(access, "self_paths", lambda: real_self() + [selfdir])
    h = await rig.start()
    pid = h.cfg.principal_id
    h.grant_access("shell", write=False, principal_id=pid)
    h.grant_access(str(tmp_path), write=True, principal_id=pid, confirm=True)  # contains all of the above
    connect = (f"python3 -c \"import socket; socket.socket(socket.AF_UNIX).connect('{fh}/.gnupg/S.agent')\" "
               "2>/dev/null && echo CONNECTED")
    script = (f"ls -A {h.cfg.home}; cat {fh}/.gnupg/key 2>/dev/null; {connect}; "
              f"touch {selfdir}/x 2>/dev/null && echo WROTE_SELF; touch {project}/ok && echo WROTE_PROJECT")
    r = await use_tools(h, rig, call("shell_exec", command=script),
                        call("file_write", path=str(selfdir / "y"), content="x"))
    out = r[0]["output"]
    assert out.split() == ["workspace", "WROTE_PROJECT"]  # the home shows only the workspace
    assert not r[1]["ok"] and "explicit permission" in r[1]["error"]
    # `self` write access lifts the read-only mask; revoking it ends sessions that had it.
    h.grant_access("self", write=True, principal_id=pid)
    r = await use_tools(h, rig, call("shell_exec", command=f"touch {selfdir}/x && echo WROTE_SELF; sleep 60",
                                     wait_seconds=1))
    assert "WROTE_SELF" in r[0]["output"]
    out = h.revoke_access("self", write_only=True, principal_id=pid)
    assert out["killed_sessions"] == [r[0]["session_id"]]
    agent.close()


# -- shell ---------------------------------------------------------------------------------


@needs_bwrap
async def test_shell_needs_permission_then_runs_sandboxed(rig, project, tmp_path):
    h = await rig.start()
    r = await use_tools(h, rig, call("shell_exec", command="echo hi"))
    assert not r[0]["ok"] and "permission" in r[0]["error"]
    pid = h.cfg.principal_id
    h.grant_access("shell", write=False, principal_id=pid)
    h.grant_access(str(project), write=False, principal_id=pid)
    (tmp_path / "unrelated.txt").write_text("x")
    script = (f"echo out; echo err >&2; ls {project}; test -e {h.cfg.db_path} && echo SAW_DB; "
              f"test -e {h.cfg.socket_path} && echo SAW_SOCKET; test -e {tmp_path}/unrelated.txt && echo SAW_OTHER; "
              f"touch {project}/x 2>/dev/null && echo WROTE_RO; touch $WHIM_WORKSPACE/made && echo WROTE_WS; "
              "(exec 3<>/dev/tcp/1.1.1.1/53) 2>/dev/null && echo NET; exit 3")
    r = await use_tools(h, rig, call("shell_exec", command=script, cwd=str(project)))
    out = r[0]["output"]
    assert r[0]["status"] == "exited" and r[0]["exit_code"] == 3
    assert "out" in out and "err" in out and "main.py" in out and "WROTE_WS" in out
    for leak in ("SAW_DB", "SAW_SOCKET", "SAW_OTHER", "WROTE_RO", "NET"):
        assert leak not in out
    assert (h.cfg.workspace / "made").exists()
    assert [e["data"]["status"] for e in events(h, "exec.exited")] == ["exited"]


@needs_bwrap
async def test_shell_sessions_timeout_tty_kill_and_survive_wakes(rig):
    h = await rig.start()
    h.grant_access("shell", write=False, principal_id=h.cfg.principal_id)
    r = await use_tools(h, rig, call("shell_exec", command="sleep 30", timeout_seconds=0.5, wait_seconds=3),
                        call("shell_exec", command="python3 -i -q", tty=True, wait_seconds=1))
    assert r[0]["status"] == "timed_out"
    sid = r[1]["session_id"]
    assert r[1]["status"] == "running"
    # A later wake can still talk to it.
    r = await use_tools(h, rig, call("shell_write", session_id=sid, input="print(6*7)\n", wait_seconds=3),
                        call("shell_list"))
    assert "42" in r[0]["output"]
    assert r[1]["sessions"][0]["session_id"] == sid
    r = await use_tools(h, rig, call("shell_write", session_id=sid, input="import time; time.sleep(60)\n",
                                     wait_seconds=0.5),
                        call("shell_write", session_id=sid, input="\u0003", wait_seconds=2),
                        call("shell_kill", session_id=sid, signal="KILL"))
    assert "KeyboardInterrupt" in r[1]["output"]
    assert r[2]["status"] in ("killed", "exited")
    # Large output is bounded.
    r = await use_tools(h, rig, call("shell_exec", command="head -c 200000 /dev/zero | tr '\\0' a; echo END"))
    assert "omitted" in r[0]["output"] and r[0]["output"].rstrip().endswith("END")
    assert len(r[0]["output"]) < 30000


@needs_bwrap
async def test_shell_sessions_are_reported_lost_after_a_restart(rig):
    h = await rig.start()
    h.grant_access("shell", write=False, principal_id=h.cfg.principal_id)
    r = await use_tools(h, rig, call("shell_exec", command="sleep 60", wait_seconds=0))
    pid = h.shell.sessions[r[0]["session_id"]].proc.pid
    h = await rig.restart()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    rows = h.ledger.q("SELECT status FROM exec_sessions")
    assert [r["status"] for r in rows] == ["lost"]
    rig.provider.extend(finish())
    h.submit_input("hello")
    await h.wait_idle()
    first = latest_opening(rig)
    assert "shell sessions ended" in first and "sleep 60" in first


@needs_bwrap
async def test_tool_folders_are_on_path_and_link_targets_reported(rig, project, tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    cargo_bin, local_bin = home / ".cargo/bin", home / ".local/bin"
    venv_bin = home / ".local/share/pipx/venvs/pytool/bin"
    for d in (cargo_bin, local_bin, venv_bin):
        d.mkdir(parents=True)
    (home / ".cargo/credentials.toml").write_text("token = 'SECRET'")
    (cargo_bin / "mytool").write_text("#!/bin/sh\necho TOOL-OK > \"$1\"\n")
    (venv_bin / "pytool").write_text("#!/bin/sh\necho PYTOOL-OK\n")
    for f in (cargo_bin / "mytool", venv_bin / "pytool"):
        f.chmod(0o755)
    (local_bin / "pytool").symlink_to(venv_bin / "pytool")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{local_bin}:{os.environ['PATH']}")
    h = await rig.start()
    pid = h.cfg.principal_id
    for d in (cargo_bin, local_bin, home / ".local"):  # writable is allowed, but warned about
        with pytest.raises(access.ConfirmationNeeded, match="outside the sandbox"):
            h.grant_access(str(d), write=True, principal_id=pid)
    h.grant_access("shell", write=False, principal_id=pid)
    h.grant_access(str(project), write=True, principal_id=pid)
    h.grant_access(str(cargo_bin), write=False, tool=True, principal_id=pid)
    out = h.grant_access(str(local_bin), write=False, tool=True, principal_id=pid)
    assert out["unreachable_link_targets"] == [str(venv_bin)]
    assert out["tools_on_path"] == [str(cargo_bin), str(local_bin)]
    script = (f"mytool {project}/built.txt; pytool || echo NO_PYTOOL; "
              f"cat {home}/.cargo/credentials.toml 2>/dev/null; touch {cargo_bin}/x 2>/dev/null && echo WROTE_BIN")
    r = await use_tools(h, rig, call("shell_exec", command=script, cwd=str(project)),
                        call("file_write", path=str(cargo_bin / "mytool"), content="x"))
    assert (project / "built.txt").read_text() == "TOOL-OK\n"
    assert "NO_PYTOOL" in r[0]["output"] and "SECRET" not in r[0]["output"] and "WROTE_BIN" not in r[0]["output"]
    assert not r[1]["ok"] and "read-only" in r[1]["error"]
    h.grant_access(str(venv_bin.parents[1]), write=False, principal_id=pid)  # the link's dependency
    r = await use_tools(h, rig, call("shell_exec", command="pytool"))
    assert "PYTOOL-OK" in r[0]["output"]


@needs_bwrap
async def test_killing_a_session_leaves_no_processes(rig):
    h = await rig.start()
    h.grant_access("shell", write=False, principal_id=h.cfg.principal_id)
    r = await use_tools(h, rig, call("shell_exec", command="setsid sleep 300 & nohup sleep 301 & sleep 302",
                                     wait_seconds=1))
    sid = r[0]["session_id"]
    await use_tools(h, rig, call("shell_kill", session_id=sid))
    await asyncio.sleep(0.5)
    left = [p for p in os.listdir("/proc") if p.isdigit() and _cmdline(p).startswith("sleep 30")]
    assert left == []


def _cmdline(pid: str) -> str:
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
    except OSError:
        return ""


@needs_bwrap
async def test_revoking_access_kills_sessions_that_use_it(rig, project):
    h = await rig.start()
    pid = h.cfg.principal_id
    h.grant_access("shell", write=False, principal_id=pid)
    h.grant_access(str(project), write=True, principal_id=pid)
    r = await use_tools(h, rig, call("shell_exec", command="sleep 60", cwd=str(project), wait_seconds=0))
    out = h.revoke_access(str(project), write_only=True, principal_id=pid)
    assert out["killed_sessions"] == [r[0]["session_id"]]


# -- restart -------------------------------------------------------------------------------


async def test_restart_refused_without_a_supervisor(rig, monkeypatch):
    monkeypatch.delenv("WHIM_SUPERVISOR", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    h = await rig.start()
    r = await use_tools(h, rig, call("host_restart", reason="apply settings"))
    assert not r[0]["ok"] and "systemd" in r[0]["error"]


async def test_restart_drains_checks_and_continues(rig, monkeypatch):
    monkeypatch.setenv("WHIM_SUPERVISOR", "systemd")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")  # the startup check sets up the provider
    h = await rig.start()
    r = await use_tools(h, rig, call("host_restart", reason="apply settings", note="ask about compaction"))
    assert r[0]["status"] == "scheduled"
    await asyncio.wait_for(h.restart_requested.wait(), 30)  # the real preflight passed
    # New input while draining is kept, not run.
    h.submit_input("are you back?")
    assert h.draining and h.active_run is None
    rig.provider.extend(finish())
    h = await rig.restart()
    await h.wait_idle()
    assert "restart.completed" in types(h)
    opening = latest_opening(rig)
    assert "Whim restarted" in opening and "as requested by you" in opening
    assert "ask about compaction" in opening and "are you back?" in opening
    assert h.ledger.get_meta(restart.META_KEY) is None


async def test_failed_preflight_cancels_the_restart(rig, monkeypatch):
    monkeypatch.setenv("WHIM_SUPERVISOR", "systemd")

    async def broken(home):
        return False, "SyntaxError: invalid syntax (compaction.py, line 1)"

    monkeypatch.setattr(restart, "preflight", broken)
    h = await rig.start()
    await use_tools(h, rig, call("host_restart", reason="try new code"))
    rig.provider.extend(finish())  # the wake that reports the cancellation
    for _ in range(100):
        if "restart.cancelled" in types(h):
            break
        await asyncio.sleep(0.05)
    await h.wait_idle()
    assert not h.draining and not h.restart_requested.is_set()
    opening = latest_opening(rig)
    assert "restart cancelled" in opening and "SyntaxError" in opening


def test_service_unit_restarts_on_the_restart_exit_code(cfg):
    from whim import service

    text = service.unit_text(cfg)
    assert f"RestartForceExitStatus={restart.EXIT_RESTART}" in text and "WHIM_SUPERVISOR=systemd" in text
    assert str(restart.EXIT_RESTART) not in text.split("RestartPreventExitStatus=")[1].split("\n")[0]
