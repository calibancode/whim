"""Credential storage: owner-only, outside git, prompted not argv, never printed or recorded."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import events

from whim import credentials
from whim.credentials import CredentialError, Secret
from whim.provider.deepseek import DeepSeekProvider
from whim.provider.fake import ScriptedProvider

KEY = "sk-abcdefabcdefabcdefabcdefabcdef00"


def test_store_creates_owner_only_file_and_dir():
    path = credentials.store("deepseek", KEY)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    key, source = credentials.lookup("deepseek")
    assert key.reveal() == KEY and source == str(path)


def test_secret_never_shows_in_repr():
    s = Secret(KEY)
    assert KEY not in repr(s) and KEY not in str(s) and KEY not in f"{s}"


def test_group_or_world_readable_file_is_refused():
    path = credentials.store("deepseek", KEY)
    os.chmod(path, 0o644)
    with pytest.raises(CredentialError, match="chmod 600"):
        credentials.lookup("deepseek")


def test_refuses_location_inside_git_worktree(tmp_path, monkeypatch):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WHIM_CREDENTIALS", str(tmp_path / "repo" / "secrets" / "credentials.json"))
    with pytest.raises(CredentialError, match="git work tree"):
        credentials.store("deepseek", KEY)
    assert not (tmp_path / "repo" / "secrets").exists()


def test_remove_and_rejects_bad_keys():
    with pytest.raises(CredentialError):
        credentials.store("deepseek", "  ")
    with pytest.raises(CredentialError):
        credentials.store("deepseek", "sk-has space")
    credentials.store("deepseek", KEY)
    assert credentials.remove("deepseek") is True
    assert credentials.lookup("deepseek") is None


def test_redact_handles_values_and_keylike_text():
    assert credentials.redact(f"bad key {KEY}!", Secret(KEY)) == "bad key [redacted]!"
    assert "sk-zzzz" not in credentials.redact("Incorrect API key provided: sk-zzzz****1234")


def whim(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "whim", *args], input=stdin,
                          capture_output=True, text=True, timeout=20)


def test_cli_auth_set_reads_stdin_and_never_prints_key(tmp_path):
    r = whim("--home", str(tmp_path / "h"), "auth", "set", stdin=KEY + "\n")
    assert r.returncode == 0, r.stderr
    assert KEY not in r.stdout + r.stderr
    st = whim("--home", str(tmp_path / "h"), "auth", "status")
    assert "deepseek: configured" in st.stdout and KEY not in st.stdout + st.stderr
    data = json.loads(Path(os.environ["WHIM_CREDENTIALS"]).read_text())
    assert data == {"deepseek": {"api_key": KEY}}


def test_cli_auth_does_not_accept_key_as_argument(tmp_path):
    r = whim("auth", "set", KEY)
    assert r.returncode != 0  # argv would land in shell history and process listings


def test_init_writes_no_secret_to_config(tmp_path, monkeypatch):
    credentials.store("deepseek", KEY)
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    r = whim("--home", str(tmp_path / "h"), "init")
    assert r.returncode == 0
    for p in (tmp_path / "h").rglob("*"):
        if p.is_file():
            assert KEY.encode() not in p.read_bytes(), p


async def test_key_never_reaches_model_context_or_ledger(rig):
    credentials.store("deepseek", KEY)
    DeepSeekProvider(rig.cfg.provider)  # loads fine from the file
    from whim.provider.fake import finish

    p = ScriptedProvider([finish()])
    h = await rig.start(p)
    h.submit_input("hi")
    await h.wait_idle()
    assert KEY not in json.dumps(p.requests[0].messages)
    assert KEY not in json.dumps(events(h))
    assert KEY.encode() not in rig.cfg.db_path.read_bytes()
