from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import XodexError
from .jsonutil import canonical, digest

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE repositories(
 name TEXT PRIMARY KEY, github_id INTEGER NOT NULL, base_ref TEXT NOT NULL, attached REAL NOT NULL
);
CREATE TABLE tasks(
 id TEXT PRIMARY KEY, repository TEXT NOT NULL REFERENCES repositories(name),
 prompt TEXT NOT NULL, title TEXT NOT NULL, phase TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL, branch TEXT NOT NULL, base_ref TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 1, base_sha TEXT, base_tree TEXT, final_tree TEXT,
 commit_sha TEXT, pr_url TEXT, pr_state TEXT,
 pr_attempted INTEGER NOT NULL DEFAULT 0, finish_spec TEXT, checks TEXT NOT NULL DEFAULT '[]',
 note TEXT NOT NULL DEFAULT '', error TEXT, reconciliation TEXT
);
CREATE TABLE jobs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
 kind TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL, tty INTEGER NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, finished REAL, exit_code INTEGER,
 reason TEXT, output_bytes INTEGER NOT NULL DEFAULT 0, timeout_seconds INTEGER NOT NULL
);
CREATE INDEX jobs_task ON jobs(task_id,id);
CREATE TABLE operations(
 key TEXT PRIMARY KEY, tool TEXT NOT NULL, task_id TEXT NOT NULL, digest TEXT NOT NULL,
 state TEXT NOT NULL, response TEXT, created REAL NOT NULL
);
CREATE TABLE audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, event TEXT NOT NULL,
 task_id TEXT, body TEXT NOT NULL
);
CREATE INDEX audit_task ON audit(task_id,id);
"""


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink():
            raise XodexError("configuration", "State directory must not be a symlink")
        os.chmod(root, 0o700)
        self.root = root
        self.lock = (root / "supervisor.lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            raise XodexError("already_running", "Stop the supervisor before offline administration") from error
        try:
            self.db = sqlite3.connect(root / "state.sqlite3", isolation_level=None)
            self.db.row_factory = sqlite3.Row
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            existing = self.db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            if version not in (0, SCHEMA_VERSION) or (version == 0 and existing):
                raise XodexError("schema_version", "Unsupported state format; use an empty state directory",
                                 version=version, supported=SCHEMA_VERSION)
            if version == SCHEMA_VERSION:
                try:
                    self.instance = self.db.execute("SELECT value FROM meta WHERE key='instance'").fetchone()[0]
                    if uuid.UUID(hex=self.instance).hex != self.instance:
                        raise ValueError("Invalid instance identity")
                except (sqlite3.DatabaseError, TypeError, ValueError, AttributeError) as error:
                    raise XodexError("schema_version", "State has no valid Xodex instance identity; use an empty state directory",
                                     version=version, supported=SCHEMA_VERSION) from error
            else:
                self.instance = uuid.uuid4().hex
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=5000")
            if version == 0:
                # Schema and instance identity must become durable together.
                self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                self.db.execute("INSERT INTO meta VALUES ('instance',?)", (self.instance,))
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.db.execute("COMMIT")
            for name in ("jobs", "patches", "tasks"):
                (root / name).mkdir(exist_ok=True, mode=0o700)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        self.lock.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def task(self, task_id: str) -> dict[str, Any]:
        try:
            if str(uuid.UUID(task_id)) != task_id:
                raise ValueError()
        except (ValueError, AttributeError, TypeError) as error:
            raise XodexError("invalid_task", "task_id must be a canonical UUID") from error
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise XodexError("not_found", "Task does not exist", task_id=task_id)
        return dict(row)

    def update(self, task_id: str, **fields: Any) -> None:
        allowed = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")} - {"id", "created"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown task fields")
        fields["updated"] = time.time()
        self.db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                        (*fields.values(), task_id))

    def job(self, task_id: str, job: int) -> dict[str, Any]:
        self.task(task_id)
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND task_id=?", (job, task_id)).fetchone()
        if not row:
            raise XodexError("not_found", "Command does not belong to this task", command_id=job)
        return dict(row)

    def audit(self, event: str, task_id: str | None, body: Any) -> None:
        self.db.execute("INSERT INTO audit(at,event,task_id,body) VALUES(?,?,?,?)", (time.time(), event, task_id, canonical(body)))

    def operation_key(self, tool: str, args: dict[str, Any]) -> str:
        return digest([tool, args.get("task_id", ""), args["request_id"]])

    def reserve(self, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        key, fingerprint = self.operation_key(tool, args), digest(args)
        row = self.db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
        if row:
            if row["digest"] != fingerprint:
                raise XodexError("idempotency_conflict", "This request_id was used with different arguments")
            if row["state"] == "done":
                return key, json.loads(row["response"])
            raise XodexError("operation_uncertain", "Inspect task status; this operation cannot safely be replayed", operation=key)
        self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                        (key, tool, args.get("task_id", ""), fingerprint, "pending", None, time.time()))
        return key, None

    def complete(self, key: str, response: Any) -> None:
        self.db.execute("UPDATE operations SET state='done',response=? WHERE key=?", (canonical(response), key))

    def log_path(self, job: int) -> Path:
        return self.root / "jobs" / f"{job}.log"
