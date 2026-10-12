"""Workspace files: the character's file tools never escape the workspace (paths,
symbolic links, temp files), and writes are durable before they're reported."""

from __future__ import annotations

import os

import pytest
from conftest import SECRET, Clock, sent_to_model, tool_result

from whim import compaction
from whim.provider.fake import ScriptedProvider, call, calls, finish, tool_call


async def test_planted_temp_symlink_cannot_redirect_file_write(rig, secret):
    os.symlink(secret, rig.cfg.workspace / "note.txt.whim-tmp")
    p = ScriptedProvider([call("file_write", path="note.txt", content="hello"), finish()])
    h = await rig.start(p)
    h.submit_input("write")
    await h.wait_idle()
    assert secret.read_text() == SECRET
    assert (rig.cfg.workspace / "note.txt").read_text() == "hello"


@pytest.mark.parametrize("planted", ["file", "dir"])
async def test_workspace_symlinks_are_refused_for_read_and_write(rig, secret, planted):
    ws = rig.cfg.workspace
    if planted == "file":
        os.symlink(secret, ws / "link.txt")
        read, write = "link.txt", "link.txt"
    else:
        os.symlink(secret.parent, ws / "linkdir")
        read, write = "linkdir/private.txt", "linkdir/new.txt"
    p = ScriptedProvider([calls(tool_call("file_read", path=read), tool_call("file_write", path=write, content="x")),
                          finish()])
    h = await rig.start(p)
    h.submit_input("go")
    await h.wait_idle()
    r, w = (tool_result(m) for m in p.requests[1].messages[-2:])
    assert not r["ok"]
    assert SECRET not in sent_to_model(p) and secret.read_text() == SECRET
    assert not (secret.parent / "new.txt").exists()
    if planted == "file":  # writing replaces the link itself, inside the workspace
        assert w["ok"] and not (ws / "link.txt").is_symlink() and (ws / "link.txt").read_text() == "x"
    else:
        assert not w["ok"]


async def test_journal_folder_symlink_cannot_redirect_appends(rig, secret):
    os.symlink(secret.parent, rig.cfg.workspace / "journal")
    p = ScriptedProvider([call("journal_write", entry="hello"), call("history_search", query="topsecret"), finish()])
    h = await rig.start(p, clock=Clock())
    h.submit_input("go")
    await h.wait_idle()
    assert list(secret.parent.iterdir()) == [secret]
    assert SECRET not in sent_to_model(p)


async def test_character_file_symlinks_never_reach_system_prompt_or_compactor(rig, secret):
    (rig.cfg.workspace / "USER.md").unlink()
    os.symlink(secret, rig.cfg.workspace / "USER.md")
    (rig.cfg.workspace / "BOOTSTRAP.md").unlink()
    os.symlink(secret, rig.cfg.workspace / "BOOTSTRAP.md")
    compactor = ScriptedProvider([])
    p = ScriptedProvider([finish()])
    h = await rig.start(p, compactor=compactor)
    h.submit_input("hi")
    await h.wait_idle()
    assert SECRET not in sent_to_model(p)
    assert SECRET not in compaction._files_block(h)


def test_starter_files_are_never_written_through_a_dangling_symlink(tmp_path):
    from whim import onboard

    home = tmp_path / "home"
    onboard.init(home)
    target = tmp_path / "outside" / "created.txt"
    target.parent.mkdir()
    (home / "workspace" / "SOUL.md").unlink()
    os.symlink(target, home / "workspace" / "SOUL.md")  # dangling: target doesn't exist
    onboard.init(home)
    assert not target.exists()


async def test_file_tools_stay_inside_workspace(rig):
    p = ScriptedProvider([
        calls(tool_call("file_write", path="../escape.txt", content="x"),
              tool_call("file_write", path="notes/a.md", content="hello")),
        finish(),
    ])
    h = await rig.start(p)
    h.submit_input("hi")
    await h.wait_idle()
    esc, ok = p.requests[1].messages[-2:]
    assert "outside the workspace" in tool_result(esc)["error"]
    assert tool_result(ok)["ok"]
    assert (rig.cfg.workspace / "notes/a.md").read_text() == "hello"
    assert not (rig.cfg.home / "escape.txt").exists()


# -- durability: data before names, names before the ledger says ok ------------------


class FsyncSpy:
    """Records durability-relevant syscalls as (call, inode, is_dir)."""

    def __init__(self, monkeypatch, on_dir_sync=None):
        import os

        self.log = []
        real_fsync, real_replace, real_mkdir = os.fsync, os.replace, os.mkdir

        def fsync(fd):
            st = os.fstat(fd)
            import stat as s
            self.log.append(("fsync", st.st_ino, s.S_ISDIR(st.st_mode)))
            out = real_fsync(fd)
            if s.S_ISDIR(st.st_mode) and on_dir_sync is not None:
                on_dir_sync()
            return out

        def replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            self.log.append(("replace", os.fstat(dst_dir_fd).st_ino if dst_dir_fd is not None else None, True))
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def mkdir(path, mode=0o777, *, dir_fd=None):
            self.log.append(("mkdir", os.fstat(dir_fd).st_ino if dir_fd is not None else None, True))
            return real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "mkdir", mkdir)

    def synced_after(self, call: str, ino: int) -> bool:
        """Some fsync of directory `ino` follows the last `call` made in it."""
        idx = max(i for i, e in enumerate(self.log) if e[0] == call and e[1] == ino)
        return any(e == ("fsync", ino, True) for e in self.log[idx + 1:])


def test_atomic_write_makes_the_rename_and_new_folders_durable(tmp_path, monkeypatch):
    from whim import safefs

    spy = FsyncSpy(monkeypatch)
    safefs.write_atomic(tmp_path, "notes/deep/plan.md", b"v1")
    root, notes, deep = (tmp_path.stat().st_ino, (tmp_path / "notes").stat().st_ino,
                         (tmp_path / "notes/deep").stat().st_ino)
    assert spy.synced_after("replace", deep)  # the new name survives a power cut
    assert spy.synced_after("mkdir", root) and spy.synced_after("mkdir", notes)  # and the folders holding it
    file_syncs = [i for i, e in enumerate(spy.log) if e[0] == "fsync" and not e[2]]
    assert file_syncs and file_syncs[0] < spy.log.index(("replace", deep, True))  # data before the name


async def test_file_write_is_durable_before_the_ledger_says_ok(rig, monkeypatch):
    p = ScriptedProvider([call("file_write", path="notes/plan.md", content="v1"), finish()])
    h = await rig.start(p)
    statuses = []

    def op_status():
        row = h.ledger.q1("SELECT status FROM operations WHERE tool = 'file.write'")
        statuses.append(row["status"] if row else None)

    FsyncSpy(monkeypatch, on_dir_sync=op_status)
    h.submit_input("write it")
    await h.wait_idle()
    assert statuses == ["dispatched"] * len(statuses) and statuses  # all synced while still in flight
    assert h.ledger.q1("SELECT status FROM operations WHERE tool = 'file.write'")["status"] == "ok"
