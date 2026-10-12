"""Host configuration. Secrets never live here; see whim.credentials."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

from whim.timebase import detect_timezone, zone

CACHE_USER_ID_RE = re.compile(r"^[a-zA-Z0-9\-_]{1,512}$")
CHARACTER_FILES = ("IDENTITY.md", "SOUL.md", "USER.md", "MEMORY.md")


class ConfigError(Exception):
    pass


@dataclass
class ProviderConfig:
    kind: str = "deepseek"  # deepseek | script
    model: str = "deepseek-v4-pro"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"  # optional override; normally `whim auth set`
    cache_user_id: str = ""
    thinking: bool = True
    reasoning_effort: str = "high"
    timeout_seconds: float = 300.0
    script: str = ""  # path to a rule script, for kind = "script"


@dataclass
class Limits:
    max_requests_per_run: int = 24
    max_tool_calls_per_run: int = 64
    max_run_seconds: float = 900.0
    max_text_only_streak: int = 3
    max_recovery_attempts: int = 2
    shutdown_grace_seconds: float = 20.0


@dataclass
class HeartbeatConfig:
    enabled: bool = True
    interval_minutes: float = 60.0


@dataclass
class CompactionConfig:
    """Token figures are estimates of the working context the main model sees."""

    enabled: bool = True
    trigger_tokens: int = 128_000  # start memory maintenance at the next idle moment
    force_tokens: int = 192_000  # compact first, pausing runs; not during failure backoff (hard ceiling below applies)
    context_window_tokens: int = 1_000_000  # the model's real limit (DeepSeek V4: 1M)
    reserve_tokens: int = 64_000  # kept free below the window for replies, maintenance and summaries
    tail_tokens: int = 12_000  # recent conversation kept verbatim after compaction
    summary_max_tokens: int = 6_000  # output budget for the checkpoint
    min_growth_tokens: int = 32_000  # growth needed after a compaction before another
    model: str = "deepseek-flash"
    thinking: bool = False
    retry_minutes: float = 10.0  # wait after a failed summary before retrying
    max_summary_attempts: int = 3
    max_maintenance_attempts: int = 2


@dataclass
class PluginsConfig:
    python: str = "/usr/bin/python3"  # interpreter used inside the sandbox (must live under /usr)


@dataclass
class ExecConfig:
    """Shell commands the character runs under bubblewrap (see whim.shell)."""

    enabled: bool = True
    shell: str = "/bin/bash"  # must be under /usr or /bin (visible inside the sandbox)
    default_timeout_seconds: float = 120.0
    max_timeout_seconds: float = 3600.0
    max_sessions: int = 8
    output_buffer_bytes: int = 1024 * 1024  # retained per session; older output is dropped
    memory_mb: int = 4096  # data-segment limit per process
    file_size_mb: int = 1024


@dataclass
class RestartConfig:
    drain_seconds: float = 600.0  # how long a restart waits for the current wake to end


@dataclass
class Config:
    home: Path
    character_id: str
    thread_id: str
    principal_id: str
    allowed_uid: int
    seed_name: str = ""
    timezone: str = ""  # IANA name; empty = detect from the system at load
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    limits: Limits = field(default_factory=Limits)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)
    restart: RestartConfig = field(default_factory=RestartConfig)

    @property
    def db_path(self) -> Path:
        return self.home / "whim.db"

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def socket_path(self) -> Path:
        return self.run_dir / "whim.sock"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    def validate(self) -> None:
        if not CACHE_USER_ID_RE.match(self.provider.cache_user_id):
            raise ConfigError(
                "provider.cache_user_id must match [a-zA-Z0-9-_] and be 1-512 characters"
            )
        if self.provider.kind not in ("deepseek", "script"):
            raise ConfigError(f"unknown provider.kind {self.provider.kind!r}")
        try:
            zone(self.timezone)
        except ValueError as e:
            raise ConfigError(f"character.timezone: {e}") from None
        c = self.compaction
        if not 0 < c.tail_tokens < c.trigger_tokens <= c.force_tokens:
            raise ConfigError("compaction needs 0 < tail_tokens < trigger_tokens <= force_tokens")
        if not 0 < c.reserve_tokens < c.context_window_tokens or c.force_tokens > c.context_window_tokens - c.reserve_tokens:
            raise ConfigError("compaction needs force_tokens <= context_window_tokens - reserve_tokens")
        if self.heartbeat.interval_minutes <= 0:
            raise ConfigError("heartbeat.interval_minutes must be positive")
        if self.provider.kind == "script" and not self.provider.script:
            raise ConfigError("provider.kind = 'script' requires provider.script")
        e = self.exec
        if not 0 < e.default_timeout_seconds <= e.max_timeout_seconds:
            raise ConfigError("exec needs 0 < default_timeout_seconds <= max_timeout_seconds")
        if e.max_sessions < 1 or e.output_buffer_bytes < 4096 or e.memory_mb < 64 or e.file_size_mb < 1:
            raise ConfigError("exec limits are too small (max_sessions >= 1, output_buffer_bytes >= 4096, "
                              "memory_mb >= 64, file_size_mb >= 1)")
        if not e.shell.startswith(("/usr/", "/bin/")):
            raise ConfigError("exec.shell must be under /usr or /bin")
        if self.restart.drain_seconds < 0:
            raise ConfigError("restart.drain_seconds must not be negative")


def default_home() -> Path:
    env = os.environ.get("WHIM_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "whim"


def load(home: Path) -> Config:
    path = home / "config.toml"
    if not path.exists():
        raise ConfigError(f"no config at {path}; run `whim init` first")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None
    try:
        character = raw["character"]
        direct = raw["direct"]
        cfg = Config(
            home=home,
            character_id=character["id"],
            thread_id=character["thread_id"],
            seed_name=character.get("seed_name", ""),
            timezone=character.get("timezone") or detect_timezone(),
            principal_id=direct["principal_id"],
            allowed_uid=int(direct["allowed_uid"]),
            provider=ProviderConfig(**raw.get("provider", {})),
            limits=Limits(**raw.get("limits", {})),
            heartbeat=HeartbeatConfig(**raw.get("heartbeat", {})),
            compaction=CompactionConfig(**raw.get("compaction", {})),
            plugins=PluginsConfig(**raw.get("plugins", {})),
            exec=ExecConfig(**raw.get("exec", {})),
            restart=RestartConfig(**raw.get("restart", {})),
        )
    except (KeyError, TypeError) as e:
        raise ConfigError(f"invalid config {path}: {e}") from e
    cfg.validate()
    return cfg


def render(cfg: Config) -> str:
    p, lim, hb, c, e = cfg.provider, cfg.limits, cfg.heartbeat, cfg.compaction, cfg.exec

    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"""# whim host configuration. Secrets are never stored here: the API key is saved by
# `whim auth set` in an owner-only credentials file; ${p.api_key_env}, if set, overrides it.

[character]
id = {q(cfg.character_id)}
thread_id = {q(cfg.thread_id)}
seed_name = {q(cfg.seed_name)}
# IANA time zone for journal dates, local times shown to the character, and schedules.
timezone = {q(cfg.timezone)}

[direct]
# Opaque designated-human principal; never shown to the model.
principal_id = {q(cfg.principal_id)}
# Only this local uid may connect to the control socket.
allowed_uid = {cfg.allowed_uid}

[provider]
kind = {q(p.kind)}
model = {q(p.model)}
base_url = {q(p.base_url)}
api_key_env = {q(p.api_key_env)}
# Stable, non-identifying cache namespace sent as DeepSeek `user_id` on every request.
cache_user_id = {q(p.cache_user_id)}
thinking = {str(p.thinking).lower()}
reasoning_effort = {q(p.reasoning_effort)}
timeout_seconds = {p.timeout_seconds}
script = {q(p.script)}

[limits]
max_requests_per_run = {lim.max_requests_per_run}
max_tool_calls_per_run = {lim.max_tool_calls_per_run}
max_run_seconds = {lim.max_run_seconds}
max_text_only_streak = {lim.max_text_only_streak}
max_recovery_attempts = {lim.max_recovery_attempts}
shutdown_grace_seconds = {lim.shutdown_grace_seconds}

[heartbeat]
# Recurring free time for the character once its first meeting is complete.
# Disable here, or for one session with `whim run --no-heartbeat`. Scheduled jobs and
# question expiry keep running either way.
enabled = {str(hb.enabled).lower()}
interval_minutes = {hb.interval_minutes}

[compaction]
# When the working context (estimated tokens) passes trigger_tokens, the character gets
# a memory-maintenance wake at the next idle moment, then older context is condensed
# into a checkpoint by `model`, keeping about tail_tokens of recent conversation as is.
# At force_tokens, compaction goes ahead of human input and runs are paused for it,
# except while compaction is backing off after failures (an hour, doubling, at most a
# day): then work continues normally until the hard ceiling, context_window_tokens -
# reserve_tokens. At that ceiling no oversized request is sent: messages are still
# accepted and wait, and processing resumes once compaction succeeds.
enabled = {str(c.enabled).lower()}
trigger_tokens = {c.trigger_tokens}
force_tokens = {c.force_tokens}
context_window_tokens = {c.context_window_tokens}
reserve_tokens = {c.reserve_tokens}
tail_tokens = {c.tail_tokens}
summary_max_tokens = {c.summary_max_tokens}
min_growth_tokens = {c.min_growth_tokens}
model = {q(c.model)}
thinking = {str(c.thinking).lower()}
retry_minutes = {c.retry_minutes}
max_summary_attempts = {c.max_summary_attempts}
max_maintenance_attempts = {c.max_maintenance_attempts}

[plugins]
# Plugins run sandboxed under bubblewrap with this interpreter (it must be under /usr).
python = {q(cfg.plugins.python)}

[exec]
# Shell commands the character runs (shell_exec), sandboxed under bubblewrap: they see
# the workspace, directories you granted (`whim access`), and the system's /usr and /etc,
# with network only if granted. Running commands at all needs a grant too.
enabled = {str(e.enabled).lower()}
shell = {q(e.shell)}
default_timeout_seconds = {e.default_timeout_seconds}
max_timeout_seconds = {e.max_timeout_seconds}
max_sessions = {e.max_sessions}
output_buffer_bytes = {e.output_buffer_bytes}
memory_mb = {e.memory_mb}
file_size_mb = {e.file_size_mb}

[restart]
# A restart (`whim restart`, or the character's host_restart) waits this long for the
# current wake to end before stopping anyway.
drain_seconds = {cfg.restart.drain_seconds}
"""
