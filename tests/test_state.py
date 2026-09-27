import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import create_task, make_engine
from xodex.admin import administer
from xodex.config import Repository, load_config, repository_name
from xodex.errors import XodexError
from xodex.store import SCHEMA_VERSION, Store


@pytest.mark.parametrize("name", ["file:///etc/passwd", "https://evil.com/a/b", "https://github.com/a/b?x=1",
                                     "https://user:secret@github.com/a/b", "a/../b", "a/..", "-a/b",
                                     "https://github.com//a/b", "git@github.com:a/b", "a/b/extra"])
def test_repository_identity_is_exact(name):
    with pytest.raises(XodexError):
        repository_name(name)


def test_repository_normalization():
    assert repository_name("https://github.com/Tyk-Swe/PCR.git") == "tyk-swe/pcr"


@pytest.mark.parametrize("changes", [dict(max_jobs=0), dict(max_jobs=True), dict(memory="unlimited"),
                                     dict(timeout_seconds=50, max_timeout_seconds=10), dict(image="--privileged"),
                                     dict(repositories={"test/repo":Repository(checks="true")})])
def test_invalid_config_fails_closed(config, changes):
    with pytest.raises(XodexError):
        replace(config, **changes).validate()


def test_config_control_paths_not_mounted(config):
    for changes in ({"state_dir":config.workspace_dir / "state"}, {"engine_socket":config.workspace_dir / "socket"},
                    {"github_token_file":config.workspace_dir / "token"}, {"workspace_dir":Path("/tmp/path,rw")}):
        with pytest.raises(XodexError):
            replace(config, **changes).validate()


def test_example_config_parses():
    path = Path(__file__).parents[1] / "deploy/config.example.toml"
    config = load_config(path)
    assert config.workspace_dir == path.parent / "tasks"
    assert config.repositories["your-owner/your-repo"].branch_prefix == "xodex/"
    assert config.repositories["your-owner/your-repo"].checks == []


def test_initial_schema_is_one_and_reopens_without_losing_identity(tmp_path):
    root = tmp_path / "state"
    with closing(Store(root)) as store:
        assert SCHEMA_VERSION == 1
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
        instance = store.instance
        store.db.execute("INSERT INTO repositories VALUES('test/repo',1,'main',0)")
    with closing(Store(root)) as reopened:
        assert reopened.instance == instance
        assert reopened.db.execute("SELECT name FROM repositories").fetchone()[0] == "test/repo"


@pytest.mark.parametrize("version", [1, 2, 99])
def test_unidentified_or_unsupported_state_is_refused_without_mutation(tmp_path, version):
    root = tmp_path / "state"
    root.mkdir()
    path = root / "state.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version={version}")
        db.execute("CREATE TABLE unrelated(value TEXT)")
        db.execute("INSERT INTO unrelated VALUES('retained')")
        db.commit()
    before = path.read_bytes()
    with pytest.raises(XodexError) as result:
        Store(root)
    assert result.value.code == "schema_version"
    assert path.read_bytes() == before
    assert not (root / "state.sqlite3-wal").exists()
    # Rejection must release the supervisor lock.
    with pytest.raises(XodexError) as retry:
        Store(root)
    assert retry.value.code == "schema_version"


@pytest.mark.parametrize("instance", [None, "not-a-uuid", "0" * 31, "-" * 32])
def test_invalid_instance_is_refused_before_writing_state(tmp_path, instance):
    root = tmp_path / "state"
    with closing(Store(root)) as store:
        if instance is None:
            store.db.execute("DELETE FROM meta WHERE key='instance'")
        else:
            store.db.execute("UPDATE meta SET value=? WHERE key='instance'", (instance,))
    path = root / "state.sqlite3"
    before = path.read_bytes()
    with pytest.raises(XodexError) as result:
        Store(root)
    assert result.value.code == "schema_version"
    assert path.read_bytes() == before


def test_schema_initialization_is_atomic(tmp_path, monkeypatch):
    import xodex.store as storage

    root = tmp_path / "state"
    with monkeypatch.context() as context:
        context.setattr(storage, "SCHEMA", storage.SCHEMA + "\nTHIS IS NOT SQL;")
        with pytest.raises(sqlite3.OperationalError):
            Store(root)
    with closing(sqlite3.connect(root / "state.sqlite3")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
    with closing(Store(root)) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


async def test_offline_admin_cannot_run_with_supervisor(engine):
    task_id = await create_task(engine)
    with pytest.raises(XodexError) as result:
        await administer(engine.config, "delete", task_id, task_id)
    assert result.value.code == "already_running"


async def test_explicit_admin_delete_retains_evidence_and_siblings(config, remote, monkeypatch):
    engine = make_engine(config, remote)
    await engine.start()
    first = await create_task(engine)
    second = await create_task(engine, "second")
    root = engine.tasks.paths(engine.store.task(first))[0]
    sibling = engine.tasks.paths(engine.store.task(second))[0]
    protected = engine.git.directory(engine.store.task(first))
    backend = engine.backend
    await engine.close()
    monkeypatch.setattr("xodex.admin.PodmanBackend", lambda _: backend)
    with pytest.raises(XodexError):
        await administer(config, "delete", first, second)
    result = await administer(config, "delete", first, first)
    assert result["phase"] == "deleted" and not root.exists()
    assert sibling.is_dir() and protected.is_dir()
    assert (await administer(config, "delete", first, first))["already_deleted"]


async def test_owner_reconcile_does_not_replay_unknown_write(config, remote, monkeypatch):
    engine = make_engine(config, remote)
    await engine.start()
    task_id = await create_task(engine)
    args = {"task_id":task_id, "request_id":"unknown", "path":"unsafe", "content":"x", "expected_sha256":""}
    key, _ = engine.store.reserve("write_file", args)
    engine.store.update(task_id, reconciliation="manual inspection needed")
    backend = engine.backend
    await engine.close()
    monkeypatch.setattr("xodex.admin.PodmanBackend", lambda _: backend)
    await administer(config, "reconcile", task_id, task_id)
    store = Store(config.state_dir)
    try:
        assert store.task(task_id)["reconciliation"] is None
        with pytest.raises(XodexError, match="cannot safely be replayed"):
            store.reserve("write_file", args)
        assert store.db.execute("SELECT state FROM operations WHERE key=?", (key,)).fetchone()[0] == "reconciled"
    finally:
        store.close()


@pytest.mark.parametrize("changes", [
    {"image": 23}, {"image": "worker\x00image"}, {"podman": "/usr/bin/podman\x00"},
    {"git": None}, {"memory": 2}, {"repositories": []},
    {"repositories": {"test/repo": None}},
    {"repositories": {"test/repo": Repository(branch_prefix=1)}},
    {"repositories": {"test/repo": Repository(network=[])}},
    {"repositories": {"test/repo": Repository(checks=("true\x00",))}},
    {"commit_name": 1}, {"commit_email": None},
])
def test_invalid_configuration_types_are_structured_errors(config, changes):
    with pytest.raises(XodexError) as result:
        replace(config, **changes).validate()
    assert result.value.code == "configuration"


@pytest.mark.parametrize("name", ["https://github.com/a/b\n", "https://github.com/a/\tb", "a/b\x00"])
def test_repository_does_not_normalize_away_control_characters(name):
    with pytest.raises(XodexError):
        repository_name(name)


@pytest.mark.parametrize("definition", [
    "CREATE TABLE unrelated(value TEXT)",
    "CREATE VIEW unrelated AS SELECT 'retained' AS value",
])
def test_unversioned_nonempty_database_is_not_adopted(tmp_path, definition):
    root = tmp_path / "state"
    root.mkdir()
    with closing(sqlite3.connect(root / "state.sqlite3")) as db:
        db.execute(definition)
        if definition.startswith("CREATE TABLE"):
            db.execute("INSERT INTO unrelated VALUES('retained')")
        db.commit()
    with pytest.raises(XodexError) as result:
        Store(root)
    assert result.value.code == "schema_version"
    with closing(sqlite3.connect(root / "state.sqlite3")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT value FROM unrelated").fetchone()[0] == "retained"
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == [("unrelated",)]


def test_configuration_checks_resolved_storage_boundaries(config, tmp_path):
    link = tmp_path / "alias"
    config.state_dir.mkdir()
    link.symlink_to(config.state_dir, target_is_directory=True)
    with pytest.raises(XodexError) as result:
        replace(config, workspace_dir=link / "work").validate()
    assert result.value.code == "configuration"


def test_branch_prefix_reserves_room_for_the_task_uuid(config):
    with pytest.raises(XodexError):
        replace(config, repositories={"test/repo": Repository(branch_prefix="x" * 170)}).validate()
