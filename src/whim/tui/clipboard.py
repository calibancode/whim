"""Copying text out of the TUI to the system clipboard.

A terminal app can reach the clipboard in three ways, none reliable on its own:

- a native tool on the machine running the TUI (wl-copy, xclip, xsel, pbcopy). It is
  confirmed, but over SSH it is the remote machine's clipboard;
- tmux, with `tmux load-buffer -w -`: a tmux paste buffer, forwarded to the attached
  terminal's clipboard when tmux can. tmux's default `set-clipboard external` ignores an
  OSC 52 written by an application, so it is the way out of tmux;
- OSC 52, a request to the terminal itself. It is unacknowledged and some terminals refuse it.

As in Codex's TUI: native first, then also the terminal route in tmux or over SSH (the
person may be attached from elsewhere), or only when native copying failed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable

MAX_TERMINAL_BYTES = 100_000  # larger OSC 52 / tmux payloads can overwhelm a terminal
TIMEOUT = 3.0


def _run(argv: list[str], text: str) -> bool:
    try:
        # Output goes nowhere: xclip keeps serving the selection from a background child,
        # which would otherwise hold our pipes open.
        return subprocess.run(argv, input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=TIMEOUT, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _native_commands(primary: bool) -> list[list[str]]:
    out: list[list[str]] = []
    if sys.platform == "darwin" and not primary and shutil.which("pbcopy"):
        out.append(["pbcopy"])
    if os.environ.get("WAYLAND_DISPLAY") and (tool := shutil.which("wl-copy")):
        out.append([tool, "--primary"] if primary else [tool])
    if os.environ.get("DISPLAY"):
        if tool := shutil.which("xclip"):
            out.append([tool, "-selection", "primary" if primary else "clipboard"])
        if tool := shutil.which("xsel"):
            out.append([tool, "--primary" if primary else "--clipboard", "--input"])
    return out


def native(text: str, primary: bool = False) -> bool:
    return any(_run(cmd, text) for cmd in _native_commands(primary))


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX") or os.environ.get("TMUX_PANE"))


def over_ssh() -> bool:
    return bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))


def copy(text: str, osc52: Callable[[str], None]) -> str:
    """Copy `text`; returns "copied" (a native clipboard took it), "requested" (only
    asked the terminal, unconfirmed) or "failed". `osc52` writes the escape sequence."""
    if not text:
        return "failed"
    copied = native(text)
    requested = False
    if (in_tmux() or over_ssh() or not copied) and len(text.encode()) <= MAX_TERMINAL_BYTES:
        if in_tmux() and (tmux := shutil.which("tmux")) and _run([tmux, "load-buffer", "-w", "-"], text):
            requested = True
        else:
            osc52(text)
            requested = True
    return "copied" if copied else "requested" if requested else "failed"


def publish_primary(text: str) -> None:
    """The X11/Wayland PRIMARY selection, so middle-click paste works as it does for text
    selected in the terminal itself. Silent; nothing happens without a native tool."""
    if text:
        native(text, primary=True)
