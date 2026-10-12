"""Structured configuration management: one implementation behind `whim config`, the
daemon's /v1/config endpoint and the character's host_config tool.

Keys are `section.field`, as in config.toml. The schema is the Config dataclasses
themselves (types and defaults) plus the policy table below, which says for each key:

  apply      live     takes effect at once in a running daemon
             restart  saved now, used after the next restart
             fixed    identity; not changeable here (edit config.toml by hand, offline)
  sensitive  the character may propose it, but it takes effect only once the designated
             human approves (anything that changes cost, trust or what runs)
  hidden     never shown to the character

Every change is validated against the whole configuration (Config.validate, so cross-field
constraints hold) before it is saved, and saved atomically. Secrets never live here; API
keys go through `whim auth` (whim.credentials). A new built-in service adds a dataclass
section to whim.config and its keys to POLICY; nothing else changes.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import tomllib

import tomlkit
import tomlkit.exceptions
import tomlkit.items
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whim import config as cfgmod
from whim.config import Config, ConfigError

# Section -> (attribute on Config, or None for fields that live on Config itself).
SECTIONS = {"character": None, "direct": None, "provider": "provider", "limits": "limits",
            "heartbeat": "heartbeat", "compaction": "compaction", "plugins": "plugins", "exec": "exec",
            "restart": "restart"}
TOP_LEVEL = {"character.id": "character_id", "character.thread_id": "thread_id",
             "character.seed_name": "seed_name", "character.timezone": "timezone",
             "direct.principal_id": "principal_id", "direct.allowed_uid": "allowed_uid"}


@dataclass(frozen=True)
class Policy:
    apply: str = "live"  # live | restart | fixed
    sensitive: bool = False
    hidden: bool = False


# Per-section defaults, then per-key overrides.
SECTION_POLICY = {
    "character": Policy("restart"),
    "direct": Policy("fixed", sensitive=True),
    "provider": Policy("restart", sensitive=True),
    "limits": Policy("live", sensitive=True),
    "heartbeat": Policy("live"),
    "compaction": Policy("live"),
    "plugins": Policy("live", sensitive=True),
    "exec": Policy("live", sensitive=True),
    "restart": Policy("live"),
}
POLICY = {
    "character.id": Policy("fixed"),
    "character.thread_id": Policy("fixed"),
    "direct.principal_id": Policy("fixed", sensitive=True, hidden=True),
    "compaction.model": Policy("restart", sensitive=True),
    "compaction.thinking": Policy("restart"),
}


def policy(key: str) -> Policy:
    return POLICY.get(key) or SECTION_POLICY[key.split(".", 1)[0]]


def _fields(cfg: Config) -> dict[str, tuple[Any, dataclasses.Field]]:
    """key -> (owner object, dataclass field)."""
    out = {}
    top = {f.name: f for f in dataclasses.fields(Config)}
    for key, attr in TOP_LEVEL.items():
        out[key] = (cfg, top[attr])
    for section, attr in SECTIONS.items():
        if attr is None:
            continue
        obj = getattr(cfg, attr)
        for f in dataclasses.fields(obj):
            out[f"{section}.{f.name}"] = (obj, f)
    return out


def keys(cfg: Config) -> list[str]:
    return list(_fields(cfg))


def get(cfg: Config, key: str) -> Any:
    fields = _fields(cfg)
    if key not in fields:
        raise ConfigError(f"unknown setting {key!r}")
    obj, f = fields[key]
    return getattr(obj, f.name)


def describe(cfg: Config, key: str, *, for_character: bool = False) -> dict:
    fields = _fields(cfg)
    if key not in fields or (for_character and policy(key).hidden):
        raise ConfigError(f"unknown setting {key!r}")
    obj, f = fields[key]
    p = policy(key)
    default = f.default if f.default is not dataclasses.MISSING else None
    return {"key": key, "value": getattr(obj, f.name), "type": _type_name(f), "default": default,
            "applies": p.apply, "needs_approval": p.sensitive}


def effective(cfg: Config, *, for_character: bool = False) -> dict[str, Any]:
    return {k: getattr(obj, f.name) for k, (obj, f) in _fields(cfg).items()
            if not (for_character and policy(k).hidden)}


def _type_name(f: dataclasses.Field) -> str:
    t = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
    return {"Path": "str"}.get(t, t)


def coerce(cfg: Config, key: str, value: Any) -> Any:
    """Convert a value (a native value, or a string from the command line) to the key's type."""
    fields = _fields(cfg)
    if key not in fields:
        raise ConfigError(f"unknown setting {key!r}")
    t = _type_name(fields[key][1])
    if isinstance(value, str) and t != "str":
        try:
            value = tomllib.loads(f"v = {value}")["v"]
        except tomllib.TOMLDecodeError:
            raise ConfigError(f"{key} needs a {t}, not {value!r}") from None
    if t == "bool" and isinstance(value, bool):
        return value
    if t == "int" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if t == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if t == "str" and isinstance(value, str):
        return value
    raise ConfigError(f"{key} needs a {t}, not {value!r}")


def with_changes(cfg: Config, changes: dict[str, Any], *, allow_fixed: bool = False) -> Config:
    """A validated copy of `cfg` with `changes` applied. Raises ConfigError."""
    if not changes:
        raise ConfigError("no changes given")
    new = dataclasses.replace(cfg, **{attr: dataclasses.replace(getattr(cfg, attr))
                                      for attr in SECTIONS.values() if attr})
    fields = _fields(new)
    for key, value in changes.items():
        if key not in fields:
            raise ConfigError(f"unknown setting {key!r}")
        if policy(key).apply == "fixed" and not allow_fixed:
            raise ConfigError(f"{key} is fixed for this home and can't be changed here")
        obj, f = fields[key]
        setattr(obj, f.name, coerce(new, key, value))
    new.validate()
    return new


def edit_text(text: str, changes: dict[str, Any]) -> str:
    """Set `changes` ({"section.field": value}, already coerced) in config.toml's text,
    keeping comments, formatting and everything else as written. Raises ConfigError for
    a structure it can't edit safely (a dotted or inline section, a non-table value)."""
    try:
        doc = tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as e:
        raise ConfigError(f"config.toml is not valid TOML: {e}") from None
    for key, value in changes.items():
        section, name = key.split(".", 1)
        table = doc.get(section)
        if table is None:
            table = tomlkit.table()
            doc.add(tomlkit.nl())
            doc.add(section, table)
        elif not isinstance(table, tomlkit.items.Table) or table.is_super_table():
            raise ConfigError(f"[{section}] in config.toml isn't a plain table; edit {key} by hand")
        if name in table and isinstance(table[name], (tomlkit.items.Table, tomlkit.items.AoT)):
            raise ConfigError(f"{key} in config.toml isn't a plain value; edit it by hand")
        table[name] = value
    out = tomlkit.dumps(doc)
    if tomllib.loads(out) != _expected(tomllib.loads(text), changes):
        raise ConfigError("config.toml couldn't be edited without changing other settings; edit it by hand")
    return out


def _expected(raw: dict, changes: dict[str, Any]) -> dict:
    for key, value in changes.items():
        section, name = key.split(".", 1)
        raw.setdefault(section, {})[name] = value
    return raw


def save(cfg: Config, changes: dict[str, Any]) -> None:
    """Apply `changes` to config.toml in place, atomically (owner-only, fsynced, renamed)."""
    path = cfg.config_path
    text = edit_text(path.read_text(), {k: get(cfg, k) for k in changes})
    tmp = path.with_name(f".config.toml.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def update(home: Path, changes: dict[str, Any], *, allow_fixed: bool = False) -> tuple[Config, Config, dict]:
    """Load the saved configuration, apply and validate `changes`, save. Returns
    (before, after, {key: {"old", "new"}} for the keys that actually changed)."""
    before = cfgmod.load(home)
    after = with_changes(before, changes, allow_fixed=allow_fixed)
    diff = {k: {"old": get(before, k), "new": get(after, k)} for k in changes if get(before, k) != get(after, k)}
    if diff:
        save(after, diff)
    return before, after, diff


def pending_restart(running: Config, saved: Config) -> list[str]:
    """Keys whose saved value differs from what the running daemon uses and that only
    take effect after a restart."""
    return [k for k in keys(saved) if policy(k).apply != "live" and get(running, k) != get(saved, k)]


SCOPE = "config.write"  # an approval for one exact proposed change (subject: config:<changes>)


def subject(changes: dict[str, Any]) -> str:
    from whim.ledger import dumps

    return "config:" + dumps(dict(sorted(changes.items())))


def load_saved(h) -> Config:
    return cfgmod.load(h.cfg.home)
