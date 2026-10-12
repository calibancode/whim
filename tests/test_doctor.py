"""whim doctor and the systemd user service."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from whim import credentials, doctor, service
from whim.server import acquire_lock

KEY = "sk-doctortest0123456789abcdef0123"


def run_doctor(cfg, **kw):
    findings = doctor.Doctor(cfg.home, **kw).run()
    return {(f.check, f.level) for f in findings}, findings, doctor.report(findings)


def test_healthy_home_with_credentials(cfg):
    credentials.store("deepseek", KEY)
    cfg.config_path.write_text(cfg.config_path.read_text())  # unchanged config is valid
    levels, findings, text = run_doctor(cfg)
    assert not [f for f in findings if f.level == "error"], text
    assert ("credentials", "ok") in levels and ("database", "ok") in levels
    assert KEY not in text and "sk-" not in text
    for name in ("IDENTITY.md", "MEMORY.md"):
        assert (cfg.workspace / name).read_text()[:30] not in text  # no character content


def test_missing_key_is_an_error_with_a_next_step(cfg):
    levels, findings, text = run_doctor(cfg)
    assert ("credentials", "error") in levels and "whim auth set" in text


def test_fix_repairs_only_safe_things(cfg):
    credentials.store("deepseek", KEY)
    cfg.socket_path.touch()
    cfg.config_path.chmod(0o644)
    cfg.home.chmod(0o755)
    credentials.default_path().chmod(0o644)
    (cfg.workspace / "SOUL.md").unlink()
    soul_before = (cfg.workspace / "IDENTITY.md").read_text()
    levels, findings, text = run_doctor(cfg)
    assert ("daemon", "warn") in levels and ("workspace", "warn") in levels and ("credentials", "error") in levels
    assert cfg.socket_path.exists()  # read-only by default
    levels, findings, text = run_doctor(cfg, fix=True)
    assert not cfg.socket_path.exists()
    assert oct(cfg.config_path.stat().st_mode & 0o777) == "0o600"
    assert oct(cfg.home.stat().st_mode & 0o777) == "0o700"
    assert oct(credentials.default_path().stat().st_mode & 0o777) == "0o600"
    assert (cfg.workspace / "SOUL.md").exists()
    assert (cfg.workspace / "IDENTITY.md").read_text() == soul_before  # existing files untouched
    assert not [f for f in run_doctor(cfg)[1] if f.level in ("warn", "error")]


async def test_leftover_bootstrap_note_after_completion(rig):
    h = await rig.start()
    h.ledger.set_meta("bootstrap", {"status": "complete"})
    h.close()
    rig.h = None
    levels, _, _ = run_doctor(rig.cfg)
    assert ("bootstrap", "warn") in levels
    run_doctor(rig.cfg, fix=True)
    assert not (rig.cfg.workspace / "BOOTSTRAP.md").exists()


def test_running_daemon_is_detected_and_its_socket_left_alone(cfg):
    lock = acquire_lock(cfg)
    try:
        cfg.socket_path.touch()
        levels, findings, _ = run_doctor(cfg, fix=True)
        assert any(f.check == "daemon" and "running" in f.message for f in findings)
        assert cfg.socket_path.exists()
    finally:
        lock.close()


def test_corrupted_database_is_reported(cfg):
    data = bytearray(cfg.db_path.read_bytes())
    data[100:4096] = b"\xff" * (4096 - 100)
    cfg.db_path.write_bytes(bytes(data))
    for p in cfg.home.glob("whim.db-*"):
        p.unlink()
    levels, _, text = run_doctor(cfg)
    assert ("database", "error") in levels, text


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap not installed")
async def test_tampered_or_unauthorized_plugins_are_flagged(rig):
    from test_plugins import WORDCOUNT, activated

    await rig.start()
    row = await activated(rig, "plugins/wc", "wordcount", WORDCOUNT)
    await rig.close()
    levels, findings, _ = run_doctor(rig.cfg)
    assert ("plugins", "ok") in levels
    store = rig.cfg.home / "plugins" / "store" / row["hash"]
    (store / "server.py").chmod(0o644)
    (store / "server.py").write_text("tampered")
    levels, findings, text = run_doctor(rig.cfg)
    assert ("plugins", "error") in levels and "no longer matches" in text


def test_live_provider_check_is_opt_in(cfg, monkeypatch):
    credentials.store("deepseek", KEY)
    called = []
    monkeypatch.setattr(doctor.Doctor, "provider", lambda self: called.append(1))
    run_doctor(cfg)
    assert called == []
    run_doctor(cfg, live=True)
    assert called == [1]


def test_unit_file(cfg):
    text = service.unit_text(cfg, python="/opt/py/bin/python3")
    assert f"ExecStart=/opt/py/bin/python3 -P -m whim --home {cfg.home} run" in text
    assert "Restart=on-failure" in text and "RestartPreventExitStatus=75 78" in text
    assert f"TimeoutStopSec={int(cfg.limits.shutdown_grace_seconds) + 15}" in text
    assert "KillMode=mixed" in text and "WantedBy=default.target" in text
    assert "DEEPSEEK_API_KEY" not in text and "Environment=PYTHONUNBUFFERED=1" in text


def test_service_install_writes_the_unit_without_touching_systemd_unless_asked(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    ran = []
    monkeypatch.setattr(service.subprocess, "run", lambda cmd, check=False: ran.append(cmd))
    out = service.install(cfg)
    assert service.unit_path().read_text() == service.unit_text(cfg)
    assert ran == [["systemctl", "--user", "daemon-reload"]] and "wrote" in out[0]
    service.install(cfg, enable=True)
    assert ran[-1] == ["systemctl", "--user", "enable", "--now", "whim.service"]
    service.uninstall()
    assert not service.unit_path().exists()


def whim(home, *args, env=None):
    return subprocess.run([sys.executable, "-m", "whim", "--home", str(home), *args], capture_output=True,
                          text=True, timeout=30, env=env)


def test_exit_codes_tell_a_supervisor_not_to_retry(cfg):
    lock = acquire_lock(cfg)  # another daemon owns this home
    try:
        r = whim(cfg.home, "run")
    finally:
        lock.close()
    assert r.returncode == service.EXIT_ALREADY_RUNNING and "another whim daemon" in r.stderr
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    r = whim(cfg.home, "run", env=env)  # no credentials (isolated by the conftest fixture)
    assert r.returncode == service.EXIT_CONFIG and "whim auth set" in r.stderr
    cfg.config_path.write_text("not = [valid")
    r = whim(cfg.home, "run", env=env)
    assert r.returncode == service.EXIT_CONFIG


def test_doctor_cli_exit_status(cfg):
    r = whim(cfg.home, "doctor")
    assert r.returncode == 1 and "credentials" in r.stdout  # no key in this isolated home
    credentials.store("deepseek", KEY)
    r = whim(cfg.home, "doctor")
    assert r.returncode == 0, r.stdout
    assert KEY not in r.stdout
    assert json.dumps is not None


def test_fix_rebuilds_a_removed_workspace_without_touching_onboarding(cfg):
    from whim.ledger import Ledger

    credentials.store("deepseek", KEY)
    led = Ledger(cfg.db_path, character_id=cfg.character_id, thread_id=cfg.thread_id)
    led.set_meta("bootstrap", {"status": "complete"})
    led.close()
    (cfg.workspace / "BOOTSTRAP.md").unlink()
    shutil.rmtree(cfg.workspace)
    levels, findings, text = run_doctor(cfg)
    assert ("workspace", "error") in levels and "cannot recover the originals" in text
    levels, findings, text = run_doctor(cfg, fix=True)
    assert sorted(p.name for p in cfg.workspace.iterdir()) == ["IDENTITY.md", "MEMORY.md", "SOUL.md", "USER.md"]
    assert "NOT recovered" in text  # scaffolds, clearly not the original contents
    led = Ledger(cfg.db_path, character_id=cfg.character_id, thread_id=cfg.thread_id)
    assert led.get_meta("bootstrap")["status"] == "complete"  # onboarding not reset
    led.close()
    assert not [f for f in run_doctor(cfg)[1] if f.level == "error"]


def test_fix_restores_the_note_for_a_meeting_still_pending(cfg):
    shutil.rmtree(cfg.workspace)
    levels, findings, text = run_doctor(cfg, fix=True)
    assert (cfg.workspace / "BOOTSTRAP.md").exists() and (cfg.workspace / "MEMORY.md").exists()
    assert "recreated starter files" in text
