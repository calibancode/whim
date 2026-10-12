"""Where the character may read, write and run commands: one access model shared by the
file tools and the shell sandbox.

Roots:
  workspace        the character's own folder, read-write, always
  implementation   the installed whim source (the checkout, for an editable install),
                   readable so the character can learn how it works
  granted roots    folders the designated human granted: read-only (fs.read) or
                   read-write (fs.write); fs.tool also puts one on PATH for commands

Grants live in the authz tables (subject `path:<real path>`), so they are requested,
approved, listed and revoked like plugin permissions. Running shell commands at all
(`shell.run`) and giving them the network (`shell.net`) are grants on subject `shell`.

The human may grant any existing folder except the filesystem root, /proc, /sys, /dev,
and anything inside the whim home or the credentials folder. Grants that are risky but
legitimate (writable places whose code runs outside the sandbox, credentials, control
sockets, the whole home folder) come with warnings, which the human confirms.

Inside every grant, masks hold however broad the grant is:
  hidden     the whim home (database, control socket; the workspace stays visible),
             the credentials folder, /run (session and system control sockets) and
             secret stores under the home folder. A grant at or inside a hidden place
             makes that part visible; a broader grant never does. Hidden means not
             mounted at all: a read-only mount doesn't stop connecting to a socket.
  read-only  "self": what the daemon loads when it starts (the implementation, the
             Python environments and library folders it runs from, its systemd unit).
             Only `self` write access makes these writable, so a broad write grant
             can't silently change the daemon.
These masks keep whim's own boundaries; they are not a list of everything dangerous.
Write access to a folder is write access to any code in it that something outside the
sandbox runs later (a project's scripts, ~/.local/bin, shell startup files, other
systemd units). Warnings name common cases; the grant itself is the decision.

A root is stored as its real path and must still be one when used. Inside a root, file
tools walk paths with whim.safefs (no symbolic links followed, checked at the moment of
opening); shell commands see only the roots, bind-mounted by bubblewrap with the masks
on top, so links that lead elsewhere simply don't resolve.
"""

from __future__ import annotations

import os
import site
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import whim
from whim import authz, credentials

if TYPE_CHECKING:
    from whim.harness import Harness

READ, WRITE, TOOL = "fs.read", "fs.write", "fs.tool"
SHELL = "shell"
SHELL_RUN, SHELL_NET = "shell.run", "shell.net"
# Under the user's home: other programs' credentials, hidden inside broader grants.
SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".pki",
               ".local/share/keyrings", ".mozilla", ".netrc", ".git-credentials")
# Provided by the sandbox itself; never granted (nor is the filesystem root).
UNGRANTABLE = ("/proc", "/sys", "/dev")
CONTROL_DIR = Path("/run")  # systemd, D-Bus and agent sockets: commands as the user, unsandboxed
# Under the user's home: common places whose contents run outside the sandbox. Used for
# warnings only; the daemon's PATH entries are added.
PROGRAM_DIRS = (".local/bin", "bin", ".cargo/bin", "go/bin", ".npm-global/bin", ".deno/bin", ".bun/bin",
                ".config", ".local/share/systemd")


INSTALL_PATHS = ("stdlib", "platstdlib", "purelib", "platlib")  # what the daemon imports from


class AccessError(PermissionError):
    pass


class ConfirmationNeeded(AccessError):
    def __init__(self, warnings: list[str]):
        super().__init__("this grant needs confirmation: " + " ".join(warnings))
        self.warnings = warnings


@dataclass(frozen=True)
class Root:
    path: Path
    writable: bool
    kind: str  # workspace | implementation | granted


def implementation_root() -> Path:
    """The source checkout for an editable install (src layout), else the package folder."""
    pkg = Path(whim.__file__).resolve().parent
    repo = pkg.parent.parent
    try:
        if pkg.parent.name == "src" and 'name = "whim"' in (repo / "pyproject.toml").read_text():
            return repo
    except OSError:
        pass
    return pkg


def subject(path: Path) -> str:
    return f"path:{path}"


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _real(p: str | Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(p))))


def own_dirs(h: "Harness") -> list[Path]:
    """The whim home and the credentials folder."""
    return [h.cfg.home.resolve(), _real(credentials.default_path().parent)]


def self_paths() -> list[Path]:
    """What the daemon loads when it starts: the implementation, its Python environments
    and import paths, and its systemd unit."""
    from whim import service

    out = {implementation_root(), _real(sys.prefix), _real(sys.base_prefix), _real(service.unit_path())}
    # The interpreter's install locations, not sys.path: that also holds the working
    # directory (`python -m`), which under systemd is the whole home folder.
    out |= {_real(p) for k, p in sysconfig.get_paths().items() if k in INSTALL_PATHS and os.path.isdir(p)}
    if site.ENABLE_USER_SITE and os.path.isdir(site.getusersitepackages()):
        out.add(_real(site.getusersitepackages()))
    return sorted(out)


def self_writable(h: "Harness") -> bool:
    return authz.granted(h, subject(implementation_root()), WRITE)


def check_grantable(h: "Harness", raw: str) -> Path:
    """The real path of a folder the human may grant, or AccessError."""
    if not raw or not os.path.isabs(os.path.expanduser(raw)):
        raise AccessError("give an absolute path")
    path = _real(raw)
    if not path.is_dir():
        raise AccessError(f"{path} is not an existing folder")
    if path == Path("/"):
        raise AccessError("the whole filesystem can't be granted; grant specific folders")
    for s in UNGRANTABLE:
        if _within(path, Path(s)):
            raise AccessError(f"{path} is provided by the sandbox itself and can't be granted")
    for d in own_dirs(h):
        if _within(path, d):
            raise AccessError(f"{path} is inside {d}, whim's own private state, which is never shared")
    return path


def _program_dirs() -> list[Path]:
    home = Path.home().resolve()
    out = {_real(home / d) for d in PROGRAM_DIRS}
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry.startswith("/") and _within(_real(entry), home):
            out.add(_real(entry))
    return sorted(out)


def warnings(h: "Harness", path: Path, *, write: bool) -> list[str]:
    """What the human should know before granting `path`. Empty for ordinary folders."""
    home = Path.home().resolve()
    out = []
    if _within(path, CONTROL_DIR):
        out.append(f"{path} is under /run, which holds control sockets (your systemd user manager, D-Bus, "
                   "agents). Even read-only, this lets commands act as you outside the sandbox.")
    if path == Path("/tmp"):
        out.append("/tmp holds other programs' sockets (X11, ssh-agent, ...); commands could connect to them.")
    if _within(home, path):
        out.append(f"{path} is your whole home folder. whim's own files, secret stores and /run stay hidden, "
                   "but everything else is visible, including app tokens under ~/.config.")
    secret = next((home / d for d in SECRET_DIRS if _within(path, home / d)), None)
    if secret:
        out.append(f"{path} holds credentials ({secret})"
                   + ("; shell commands also have network access." if network_allowed(h) else "."))
    if write:
        selfs = [p for p in self_paths() if _within(p, path) or _within(path, p)]
        if selfs:
            out.append(f"this includes code whim itself runs ({selfs[0]}), which stays read-only unless you also "
                       "grant `self --write`.")
        runs = [d for d in _program_dirs() if _within(d, path) or _within(path, d)]
        if runs:
            out.append(f"programs or settings here run outside the sandbox ({runs[0]}); writing to them changes "
                       "what runs as you later.")
    return out


def outside_links(path: Path, inside: list[Path], limit: int = 20) -> list[str]:
    """Folders that symbolic links directly in `path` point into but that aren't among
    `inside`: inside the sandbox those links (often a tool's real program) won't resolve."""
    out: set[str] = set()
    try:
        entries = list(os.scandir(path))
    except OSError:
        return []
    for e in entries:
        if not e.is_symlink():
            continue
        target = Path(os.path.realpath(e.path))
        if target.exists() and not any(_within(target, r) for r in inside):
            out.add(str(target.parent))
    return sorted(out)[:limit]


def tool_dirs(h: "Harness") -> list[Path]:
    """Granted tool folders, in grant order (earlier first on PATH)."""
    rows = h.ledger.q("SELECT subject FROM grants WHERE revoked_at IS NULL AND scope = ? AND subject LIKE 'path:%' "
                      "ORDER BY created_at", TOOL)
    return [Path(r["subject"][5:]) for r in rows]


def roots(h: "Harness") -> list[Root]:
    out = [Root(h.cfg.workspace.resolve(), True, "workspace")]
    impl = implementation_root()
    if not (_within(h.cfg.home.resolve(), impl)):
        out.append(Root(impl, self_writable(h), "implementation"))
    seen = {r.path for r in out}
    rows = h.ledger.q("SELECT subject, scope FROM grants WHERE revoked_at IS NULL AND subject LIKE 'path:%' "
                      "AND scope IN (?, ?) ORDER BY created_at", READ, WRITE)
    writable: dict[Path, bool] = {}
    for r in rows:
        p = Path(r["subject"][5:])
        writable[p] = writable.get(p, False) or r["scope"] == WRITE
    for p, w in writable.items():
        if p not in seen:
            out.append(Root(p, w, "granted"))
    return out


def masks(h: "Harness", roots_: list[Root]) -> list[tuple[Path, str]]:
    """(path, "hidden" | "readonly") for places inside broader roots that must not be
    visible, or not writable, shallowest first."""
    home = Path.home().resolve()
    hidden = own_dirs(h) + [CONTROL_DIR] + [home / d for d in SECRET_DIRS]
    out = [(m, "hidden") for m in hidden if os.path.lexists(m) and any(_within(m, r.path) for r in roots_)
           and m not in {r.path for r in roots_}]
    if not self_writable(h):
        out += [(m, "readonly") for m in self_paths() if m.exists() and any(_within(m, r.path) and r.writable for r in roots_)]
    return sorted(out, key=lambda m: len(m[0].parts))


def resolve(h: "Harness", raw: str, *, write: bool = False) -> tuple[Root, str]:
    """(root, path relative to it) for an absolute path; relative paths are in the
    workspace. Raises AccessError if no root covers it, a mask hides it, or it isn't
    writable."""
    if not raw.startswith("/") and not raw.startswith("~"):
        return roots(h)[0], raw
    path = Path(os.path.normpath(os.path.expanduser(raw)))
    rs = roots(h)
    best = None
    for r in rs:
        if _within(path, r.path) and (best is None or len(str(r.path)) > len(str(best.path))):
            best = r
    if best is None:
        raise AccessError(f"{raw} is outside every folder you can use; ask with host_access "
                          "(op request) if you need it")
    if os.path.realpath(best.path) != str(best.path):
        raise AccessError(f"{best.path} has been moved or replaced by a link since it was granted")
    for m, how in masks(h, rs):
        if not _within(path, m) or (_within(best.path, m) and (how == "hidden" or best.path != m)):
            continue  # a grant of the hidden place itself, or of something inside it, governs it
        if how == "hidden":
            raise AccessError(f"{raw} is inside {m}, which isn't visible through a broader grant")
        if write:
            raise AccessError(f"{m} is part of how you run (edits take effect after a restart), so it is read-only "
                              "for you. Editing it needs your human's explicit permission (host_access op request, "
                              "target self, write).")
    if write and not best.writable:
        hint = (" Editing your own implementation needs your human's explicit permission (host_access op "
                "request with write)." if best.kind == "implementation" else " Ask with host_access for write access.")
        raise AccessError(f"{best.path} is read-only for you.{hint}")
    return best, os.path.relpath(path, best.path) if path != best.path else ""


def shell_allowed(h: "Harness") -> bool:
    return authz.granted(h, SHELL, SHELL_RUN)


def network_allowed(h: "Harness") -> bool:
    return authz.granted(h, SHELL, SHELL_NET)


def describe(h: "Harness") -> dict:
    return {
        "roots": [{"path": str(r.path), "access": "read-write" if r.writable else "read-only", "kind": r.kind}
                  for r in roots(h)],
        "tools_on_path": [str(p) for p in tool_dirs(h)],
        "shell": shell_allowed(h),
        "shell_network": network_allowed(h),
    }


def request_summary(path: Path | None, scopes: list[str], reason: str, h: "Harness") -> str:
    warn: list[str] = []
    if path is None:
        what = "run shell commands (sandboxed)" if SHELL_RUN in scopes else "network access for shell commands"
    else:
        what = f"{'read and write' if WRITE in scopes else 'read'} {path}"
        if TOOL in scopes:
            what += " (its programs on PATH for shell commands)"
        if path == implementation_root():
            what += (" — this is whim's own implementation (and the environment it runs from): edits change how "
                     "the daemon behaves after its next restart")
        else:
            warn = warnings(h, path, write=WRITE in scopes)
    return (f"Access: {what}. Reason given: {reason.strip() or '(none)'}"
            + "".join(f"\nWARNING: {w}" for w in warn))


def grant(h: "Harness", subj: str, scopes: list[str], principal_id: str) -> list[dict]:
    """The designated human grants access directly (authenticated control path). Inside a tx."""
    if principal_id != h.cfg.principal_id:
        raise authz.AuthzError("only the designated human can grant access")
    return [authz.grant(h, subj, s, principal_id) for s in scopes if not authz.granted(h, subj, s)]


def revoke_subject(h: "Harness", subj: str, principal_id: str, scopes: list[str] | None = None) -> list[dict]:
    rows = h.ledger.q("SELECT id, scope FROM grants WHERE subject = ? AND revoked_at IS NULL", subj)
    return [authz.revoke(h, r["id"], principal_id) for r in rows if scopes is None or r["scope"] in scopes]
