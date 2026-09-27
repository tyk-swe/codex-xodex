import os
import shlex
from dataclasses import replace

import pytest

from conftest import create_task, finish, fix, git_at, make_engine
from xodex.errors import XodexError


async def test_worker_git_config_cannot_execute_on_host(engine, tmp_path):
    task_id = await create_task(engine)
    task = engine.store.task(task_id)
    root = engine.tasks.paths(task)[0]
    marker = tmp_path / "HOST_EXECUTED"
    (root / ".git/config").write_text("[core]\n\tfsmonitor = " + shlex.quote("touch " + str(marker)) + "\n[filter \"evil\"]\n\tclean = touch " + str(marker) + "\n")
    (root / ".gitattributes").write_text("*.py filter=evil\n")
    await fix(engine,task_id)
    result = await finish(engine,task_id)
    assert result["phase"] == "completed", result
    assert not marker.exists()


async def test_worker_index_cannot_hide_tracked_changes(engine):
    task_id = await create_task(engine)
    root = engine.tasks.paths(engine.store.task(task_id))[0]
    git_at(root,"update-index","--skip-worktree","calc.py")
    await fix(engine,task_id)
    result = await finish(engine,task_id)
    assert result["phase"] == "completed"
    assert "return a + b" in git_at(engine.github.remote,"show", result["branch"]+":calc.py")


async def test_worker_origin_cannot_redirect_publish(engine):
    task_id = await create_task(engine)
    root = engine.tasks.paths(engine.store.task(task_id))[0]
    git_at(root,"remote","set-url","origin","https://attacker.invalid/wrong/repo.git")
    await fix(engine,task_id)
    assert (await finish(engine,task_id))["phase"] == "completed"
    assert len(engine.github.prs) == 1


async def test_snapshot_tracks_binary_modes_symlinks_and_deletions(engine, tmp_path):
    task_id = await create_task(engine)
    record = engine.store.task(task_id)
    root = engine.tasks.paths(record)[0]
    (root / "README.md").unlink()
    (root / "binary.bin").write_bytes(b"\0\xff\r\n\0")
    (root / "script").write_text("#!/bin/sh\nexit 0\n")
    (root / "script").chmod(0o755)
    secret = tmp_path / "private_secret"
    secret.write_text("NEVER_COPY_THIS")
    (root / "link").symlink_to(secret)
    snapshot = await engine.git.snapshot(record, root)
    source = engine.git.directory(record) / "source.git"
    tree = snapshot["tree"]
    listing = git_at(source,"ls-tree","-r",tree)
    assert "100755 blob" in listing and "120000 blob" in listing and "README.md" not in listing
    assert git_at(source,"show",tree+":link") == str(secret)
    import subprocess
    raw = subprocess.check_output(["git","--git-dir",str(source),"show",tree+":binary.bin"])
    assert raw == b"\0\xff\r\n\0"
    assert "NEVER_COPY_THIS" not in git_at(source,"show",tree+":link")


async def test_ignored_untracked_excluded_but_tracked_not_hidden(engine):
    task_id = await create_task(engine)
    record = engine.store.task(task_id)
    root = engine.tasks.paths(record)[0]
    (root / "ignored.txt").write_text("do not publish")
    (root / ".gitignore").write_text("ignored.txt\ncalc.py\n")
    await fix(engine,task_id)
    snapshot = await engine.git.snapshot(record,root)
    source = engine.git.directory(record) / "source.git"
    paths = git_at(source,"ls-tree","-r","--name-only",snapshot["tree"])
    assert "ignored.txt" not in paths and "calc.py" in paths
    assert "return a + b" in git_at(source,"show",snapshot["tree"]+":calc.py")


async def test_directory_replaced_by_symlink_not_followed(engine, tmp_path):
    task_id = await create_task(engine)
    record = engine.store.task(task_id)
    root = engine.tasks.paths(record)[0]
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    snapshot = await engine.git.snapshot(record,root)
    source = engine.git.directory(record) / "source.git"
    paths = git_at(source,"ls-tree","-r",snapshot["tree"])
    assert "\tlinked\n" in paths+"\n" and "linked/" not in paths


async def test_nested_git_is_explicitly_rejected(engine):
    task_id = await create_task(engine)
    record = engine.store.task(task_id)
    root = engine.tasks.paths(record)[0]
    (root / "nested").mkdir()
    git_at(root / "nested","init")
    with pytest.raises(XodexError,match="Nested Git"):
        await engine.git.snapshot(record,root)


async def test_special_file_refused_without_blocking(engine):
    task_id = await create_task(engine)
    record = engine.store.task(task_id)
    root = engine.tasks.paths(record)[0]
    os.mkfifo(root / "pipe")
    # Git itself may omit untracked FIFOs; tracked file replaced by a FIFO must be rejected.
    (root / "calc.py").unlink()
    os.mkfifo(root / "calc.py")
    with pytest.raises(XodexError,match="special files"):
        await engine.git.snapshot(record,root)


async def test_tree_budgets(config, remote):
    config = replace(config,max_tree_bytes=8)
    engine = make_engine(config,remote)
    await engine.start()
    try:
        task_id = await create_task(engine)
        record = engine.store.task(task_id)
        with pytest.raises(XodexError,match="byte budget"):
            await engine.git.snapshot(record,engine.tasks.paths(record)[0])
    finally:
        await engine.close()


async def test_create_only_push_lease_closes_ref_race(engine, monkeypatch):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    original = engine.git.push
    async def race(record):
        # A different actor creates our future branch at an ancestor after the preflight read.
        git_at(engine.github.remote,"update-ref","refs/heads/"+record["branch"],record["base_sha"])
        await original(record)
    monkeypatch.setattr(engine.git,"push",race)
    result = await finish(engine,task_id)
    assert result["phase"] == "blocked"
    record = engine.store.task(task_id)
    assert await engine.github.head("test/repo",record["branch"]) == record["base_sha"]
    assert not engine.github.prs


async def test_snapshot_repeat_is_same_tree(engine):
    task_id = await create_task(engine)
    record = engine.store.task(task_id)
    root = engine.tasks.paths(record)[0]
    first = await engine.git.snapshot(record,root)
    second = await engine.git.snapshot(record,root)
    assert first["tree"] == second["tree"] == record["base_tree"]


async def test_truncated_spool_is_repaired_before_hashing(engine):
    import hashlib
    task_id = await create_task(engine)
    record = engine.store.task(task_id)
    root = engine.tasks.paths(record)[0]
    blobs = engine.git.directory(record) / "blobs"
    blobs.mkdir()
    content = (root / "calc.py").read_bytes()
    blob = blobs / hashlib.sha256(content).hexdigest()
    blob.write_bytes(b"truncated")
    result = await engine.git.snapshot(record, root)
    assert result["tree"] == record["base_tree"]
    assert blob.read_bytes() == content


async def test_commit_has_protected_gc_root(engine):
    task_id = await create_task(engine)
    await fix(engine, task_id)
    result = await finish(engine, task_id)
    source = engine.git.directory(engine.store.task(task_id)) / "source.git"
    assert git_at(source, "rev-parse", "refs/xodex/result") == result["commit"]


async def test_initial_checkout_preserves_git_object_bytes_with_eol_attributes(config, remote):
    seed = remote.parent / "seed"
    (seed / ".gitattributes").write_text("*.txt text eol=crlf\n")
    (seed / "sample.txt").write_bytes(b"line\n")
    git_at(seed, "add", ".")
    git_at(seed, "commit", "-m", "eol fixture")
    git_at(seed, "push", str(remote), "main")
    engine = make_engine(config, remote)
    await engine.start()
    try:
        task_id = await create_task(engine)
        record = engine.store.task(task_id)
        root = engine.tasks.paths(record)[0]
        assert (root / "sample.txt").read_bytes() == b"line\n"
        assert (await engine.git.snapshot(record, root))["tree"] == record["base_tree"]
    finally:
        await engine.close()
