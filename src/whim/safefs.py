"""Confined file access inside a directory the host doesn't fully trust (the workspace,
which plugins with workspace.write can modify, and plugin source folders).

Every operation walks the relative path one component at a time from an open directory
descriptor, opening each component with O_NOFOLLOW (and O_DIRECTORY for folders). A
symbolic link anywhere in the path is refused at the moment of opening, so there is no
window between checking a path and using it. Files are only read if they are regular
files; writes go to a fresh randomly-named temporary created with O_EXCL and are renamed
into place (rename replaces a link itself, never what it points to).

Durability: every write returns only once it would survive a crash or power cut: the
data is fsynced, then the directory holding the new name (and every folder created on
the way to it). Tools call these before their result is committed to the ledger, so the
ledger never records a file change that a crash could still undo. (A crash between the
write and that commit leaves the change made but unrecorded; recovery reports such an
operation as uncertain, never as done.)
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(PermissionError):
    """The path leaves the root, goes through a symbolic link, or isn't a regular file."""


def parts(rel: str) -> list[str]:
    if not isinstance(rel, str) or "\0" in rel:
        raise UnsafePath("invalid path")
    p = [x for x in rel.replace("\\", "/").split("/") if x not in ("", ".")]
    if rel.startswith("/") or any(x == ".." for x in p):
        raise UnsafePath(f"{rel!r} is outside the workspace")
    return p


def _open_component(dir_fd: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o755, dir_fd=dir_fd)
            os.fsync(dir_fd)  # the new folder's entry is durable before anything goes in it
        except FileExistsError:
            pass
        return os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(f"{name!r} is a symbolic link or not a folder") from None
        raise


@contextmanager
def parent(root: Path, rel: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """(directory fd, final name) for `rel` under `root`, walked without following links."""
    p = parts(rel)
    if not p:
        raise UnsafePath("a file name is needed")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in p[:-1]:
            nxt = _open_component(fd, name, create=create)
            os.close(fd)
            fd = nxt
        yield fd, p[-1]
    finally:
        os.close(fd)


@contextmanager
def directory(root: Path, rel: str = "") -> Iterator[int]:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in parts(rel):
            nxt = _open_component(fd, name, create=False)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def _open_regular(dir_fd: int, name: str, flags: int, mode: int = 0o644) -> int:
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, mode, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise UnsafePath(f"{name!r} is a symbolic link") from None
        if e.errno == errno.EISDIR:
            raise IsADirectoryError(name) from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafePath(f"{name!r} is not a regular file")
    os.set_blocking(fd, True)
    return fd


def read_bytes(root: Path, rel: str, limit: int | None = None) -> bytes:
    with parent(root, rel) as (dfd, name):
        fd = _open_regular(dfd, name, os.O_RDONLY)
        with os.fdopen(fd, "rb") as f:
            return f.read() if limit is None else f.read(limit + 1)


def read_text(root: Path, rel: str) -> str | None:
    """Text of a regular file, or None if it doesn't exist or isn't safely readable."""
    try:
        return read_bytes(root, rel).decode("utf-8", errors="replace")
    except (FileNotFoundError, UnsafePath, IsADirectoryError, NotADirectoryError):
        return None


def kind(root: Path, rel: str) -> str:
    """'missing', 'file', 'dir', or 'other' (a link, device...), without following links."""
    try:
        with parent(root, rel) as (dfd, name):
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except (UnsafePath, NotADirectoryError):
        return "other"
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    return "other"


def write_atomic(root: Path, rel: str, data: bytes, *, mode: int = 0o644) -> None:
    with parent(root, rel, create=True) as (dfd, name):
        tmp = f".{name}.{secrets.token_hex(8)}.whim-tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    raise IsADirectoryError(name)
            except FileNotFoundError:
                pass
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)  # make the rename itself durable
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            raise


def create_exclusive(root: Path, rel: str, data: bytes) -> bool:
    """Create a new file; False if anything (including a link) already has that name."""
    with parent(root, rel, create=True) as (dfd, name):
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dfd)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.fsync(dfd)
        return True


def append(root: Path, rel: str, data: bytes) -> tuple[int, int]:
    """Append to a regular file (created if missing). Returns (offset before, after)."""
    with parent(root, rel, create=True) as (dfd, name):
        fd = _open_regular(dfd, name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            start = os.fstat(fd).st_size
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if start == 0:
            os.fsync(dfd)  # a new file: make its directory entry durable too
        return start, start + len(data)


def size(root: Path, rel: str) -> int | None:
    """Size of a regular file, None if missing; refuses links."""
    try:
        with parent(root, rel) as (dfd, name):
            fd = _open_regular(dfd, name, os.O_RDONLY)
            try:
                return os.fstat(fd).st_size
            finally:
                os.close(fd)
    except FileNotFoundError:
        return None


def listdir(root: Path, rel: str = "") -> list[tuple[str, str]]:
    """[(name, kind)] for a folder, without following links."""
    with directory(root, rel) as fd:
        out = []
        for name in sorted(os.listdir(fd)):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            k = "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
            out.append((name, k))
        return out


def walk_files(root: Path, rel: str = "", *, max_files: int = 10_000) -> list[tuple[str, bytes]]:
    """Every regular file under the folder `rel` as (path relative to it, bytes). Any link
    or special file is refused rather than skipped, so nothing is read through one."""
    out: list[tuple[str, bytes]] = []
    base = "/".join(parts(rel))

    def visit(sub: str) -> None:
        here = f"{base}/{sub}" if base and sub else (base or sub)
        for name, k in listdir(root, here):
            child = f"{sub}/{name}" if sub else name
            if name == "__pycache__":
                continue
            if k == "dir":
                visit(child)
            elif k == "file":
                out.append((child, read_bytes(root, f"{base}/{child}" if base else child)))
                if len(out) > max_files:
                    raise UnsafePath("too many files")
            else:
                raise UnsafePath(f"{child}: symbolic links and special files are not allowed")

    visit("")
    return out
