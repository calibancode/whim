"""Running whim as a `systemd --user` service.

systemd supervises the process; whim has no watchdog of its own. The unit:
- runs the same `whim run` as the foreground command, against one home, as the user
  (so it uses the same owner-only credentials file and the same character state);
- restarts on failure, but not when another daemon already owns the home (exit 75) or
  the configuration/credentials are unusable (exit 78), which restarting can't fix;
- restarts on EXIT_RESTART (76), which whim uses for its own checked restarts
  (whim.restart); WHIM_SUPERVISOR tells whim it is supervised and may do that;
- gives a stop request longer than whim's own shutdown grace, so a run in progress can
  finish; anything still active is reconciled at the next start like any crash.
The home's flock still guarantees one daemon even if a foreground `whim run` is started
alongside the service.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from whim.config import Config
from whim.restart import EXIT_RESTART

EXIT_ALREADY_RUNNING = 75  # EX_TEMPFAIL
EXIT_CONFIG = 78  # EX_CONFIG
UNIT_NAME = "whim.service"


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT_NAME


def unit_text(cfg: Config, python: str | None = None) -> str:
    python = python or sys.executable
    exec_start = shlex.join([python, "-m", "whim", "--home", str(cfg.home), "run"])
    stop_timeout = int(cfg.limits.shutdown_grace_seconds) + 15
    return f"""[Unit]
Description=whim character harness ({cfg.home})
Documentation=file://{Path(__file__).resolve().parents[2] / "README.md"}
After=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=10
RestartPreventExitStatus={EXIT_ALREADY_RUNNING} {EXIT_CONFIG}
RestartForceExitStatus={EXIT_RESTART}
# whim drains a running wake for up to shutdown_grace_seconds on SIGTERM.
TimeoutStopSec={stop_timeout}
KillMode=mixed
UMask=0077
Environment=PYTHONUNBUFFERED=1
Environment=WHIM_SUPERVISOR=systemd

[Install]
WantedBy=default.target
"""


def install(cfg: Config, *, enable: bool = False) -> list[str]:
    """Write the unit (replacing an older one); optionally enable and start it."""
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(cfg))
    done = [f"wrote {path}"]
    commands = [["systemctl", "--user", "daemon-reload"]]
    if enable:
        commands.append(["systemctl", "--user", "enable", "--now", UNIT_NAME])
    for c in commands:
        subprocess.run(c, check=True)
        done.append("ran " + " ".join(c))
    return done


def uninstall() -> list[str]:
    path = unit_path()
    done = []
    if path.exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
        path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        done.append(f"stopped, disabled and removed {path}")
    else:
        done.append("no whim user service installed")
    return done
