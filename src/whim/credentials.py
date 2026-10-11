"""Persistent provider credentials, kept out of config, the repository, logs, the model
and the event ledger.

Location: $WHIM_CREDENTIALS, else $XDG_CONFIG_HOME/whim/credentials.json, else
~/.config/whim/credentials.json. The directory is 0700 and the file 0600, owned by the
current user; a file readable by anyone else is refused rather than used (like ssh).
A credentials path inside a git work tree is refused, so a key cannot be committed.

Lookup order for a provider key: the configured environment variable (optional
override), then this file.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

PROVIDERS = ("deepseek",)
# Matches common API-key shapes (e.g. sk-…) for defensive redaction of error text.
_KEYLIKE = re.compile(r"\bsk-[A-Za-z0-9_\-*.]{4,}")


class CredentialError(Exception):
    pass


class Secret:
    """A string that never shows itself in repr/str (tracebacks, logs, pytest output)."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__


def default_path() -> Path:
    env = os.environ.get("WHIM_CREDENTIALS")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "whim" / "credentials.json"


def _inside_git_worktree(path: Path) -> Path | None:
    for parent in path.resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


def _check_location(path: Path) -> None:
    repo = _inside_git_worktree(path)
    if repo is not None:
        raise CredentialError(f"refusing to keep credentials inside the git work tree {repo}")


def _check_file(path: Path) -> None:
    st = path.stat()
    if st.st_uid != os.getuid():
        raise CredentialError(f"{path} is not owned by the current user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise CredentialError(f"{path} is accessible by other users; run: chmod 600 {path}")


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    _check_file(path)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise CredentialError(f"{path} is not valid JSON") from None
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    _check_location(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def store(provider: str, api_key: str, path: Path | None = None) -> Path:
    if provider not in PROVIDERS:
        raise CredentialError(f"unknown provider {provider!r}")
    key = api_key.strip()
    if not key or any(c.isspace() for c in key):
        raise CredentialError("the API key is empty or contains whitespace")
    path = path or default_path()
    data = _read(path)
    data[provider] = {"api_key": key}
    _write(path, data)
    return path


def remove(provider: str, path: Path | None = None) -> bool:
    path = path or default_path()
    data = _read(path)
    if provider not in data:
        return False
    del data[provider]
    _write(path, data)
    return True


def lookup(provider: str, env_var: str | None = None, path: Path | None = None) -> tuple[Secret, str] | None:
    """Return (key, source) where source is 'env' or the file path; None if absent."""
    if env_var and os.environ.get(env_var):
        return Secret(os.environ[env_var]), f"${env_var}"
    path = path or default_path()
    entry = _read(path).get(provider)
    if isinstance(entry, dict) and entry.get("api_key"):
        return Secret(entry["api_key"]), str(path)
    return None


def redact(text: str, *secrets: Secret | str | None) -> str:
    """Remove known secret values and anything key-shaped from text bound for logs/ledger."""
    for s in secrets:
        value = s.reveal() if isinstance(s, Secret) else s
        if value:
            text = text.replace(value, "[redacted]")
    return _KEYLIKE.sub("[redacted]", text)
