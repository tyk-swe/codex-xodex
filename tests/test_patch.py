import json

import pytest

from xodex.errors import XodexError
from xodex.files import WorkspaceFS
from xodex.patch import commit, prepare


def apply(root, journal, text, expected=None):
    with WorkspaceFS(root) as fs:
        return commit(fs, prepare(fs, text, expected or {}), journal)


def test_add_update_move_delete(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    apply(root, tmp_path / "j1", "*** Begin Patch\n*** Add File: a\n+one\n+two\n*** End Patch")
    assert (root / "a").read_text() == "one\ntwo\n"
    apply(root, tmp_path / "j2", "*** Begin Patch\n*** Update File: a\n*** Move to: src/b\n@@\n one\n-two\n+three\n*** End Patch")
    assert not (root / "a").exists()
    assert (root / "src/b").read_text() == "one\nthree\n"
    apply(root, tmp_path / "j3", "*** Begin Patch\n*** Delete File: src/b\n*** End Patch")
    assert not (root / "src/b").exists()
    assert json.loads((tmp_path / "j3/before.json").read_text())[0]["before"]


def test_ambiguous_context(tmp_path):
    (tmp_path / "a").write_text("x\nx\n")
    with WorkspaceFS(tmp_path) as fs:
        with pytest.raises(XodexError, match="one exact"):
            prepare(fs, "*** Begin Patch\n*** Update File: a\n@@\n-x\n+y\n*** End Patch", {})
    assert (tmp_path / "a").read_text() == "x\nx\n"


def test_eof_and_no_newline(tmp_path):
    (tmp_path / "a").write_bytes(b"x\nx")
    patch = "*** Begin Patch\n*** Update File: a\n@@\n-x\n+y\n*** End of File\n*** End Patch"
    apply(tmp_path, tmp_path / "journal", patch)
    assert (tmp_path / "a").read_bytes() == b"x\ny"


def test_crlf(tmp_path):
    (tmp_path / "a").write_bytes(b"one\r\ntwo\r\n")
    apply(tmp_path, tmp_path / "journal", "*** Begin Patch\n*** Update File: a\n@@\n one\n-two\n+three\n*** End Patch")
    assert (tmp_path / "a").read_bytes() == b"one\r\nthree\r\n"


def test_prevalidation(tmp_path):
    (tmp_path / "a").write_text("before\n")
    patch = "*** Begin Patch\n*** Update File: a\n@@\n-before\n+after\n*** Update File: missing\n@@\n-x\n+y\n*** End Patch"
    with WorkspaceFS(tmp_path) as fs:
        with pytest.raises(XodexError):
            prepare(fs, patch, {})
    assert (tmp_path / "a").read_text() == "before\n"


def test_hash_conflict(tmp_path):
    (tmp_path / "a").write_text("before\n")
    with WorkspaceFS(tmp_path) as fs:
        with pytest.raises(XodexError):
            prepare(fs, "*** Begin Patch\n*** Delete File: a\n*** End Patch", {"a": "0" * 64})


@pytest.mark.parametrize("patch", [
    "garbage", "*** Begin Patch\n*** End Patch", "*** Begin Patch\n*** Add File: ../bad\n+x\n*** End Patch",
    "*** Begin Patch\n*** Add File: a\nno-prefix\n*** End Patch",
    "*** Begin Patch\n*** Add File: a\n+x\n*** Add File: a\n+y\n*** End Patch",
])
def test_invalid_patch(tmp_path, patch):
    with WorkspaceFS(tmp_path) as fs:
        with pytest.raises(XodexError):
            prepare(fs, patch, {})


def test_rollback(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a").write_text("a\n")
    (root / "b").write_text("b\n")
    patch = "*** Begin Patch\n*** Update File: a\n@@\n-a\n+A\n*** Update File: b\n@@\n-b\n+B\n*** End Patch"
    with WorkspaceFS(root) as fs:
        changes = prepare(fs, patch, {})
        original = fs.write
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected disk error")
            return original(*args, **kwargs)
        monkeypatch.setattr(fs, "write", fail_second)
        with pytest.raises(OSError):
            commit(fs, changes, tmp_path / "journal")
    assert (root / "a").read_text() == "a\n"
    assert (root / "b").read_text() == "b\n"


def test_failure_after_rename_restores_preimage(tmp_path, monkeypatch):
    from xodex.files import WorkspaceFS
    from xodex.patch import Change, commit
    root = tmp_path / "work"
    root.mkdir()
    (root / "x").write_bytes(b"before")
    original = WorkspaceFS.write
    fired = False
    def post_rename_failure(fs, path, data, mode=0o644):
        nonlocal fired
        original(fs, path, data, mode)
        if not fired:
            fired = True
            raise OSError("injected failure after durable rename")
    monkeypatch.setattr(WorkspaceFS, "write", post_rename_failure)
    with WorkspaceFS(root) as fs:
        with pytest.raises(OSError):
            commit(fs, [Change("x", b"before", b"after", 0o644)], tmp_path / "journal")
    assert (root / "x").read_bytes() == b"before"
