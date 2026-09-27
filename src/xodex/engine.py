from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from . import __version__
from .backend import PodmanBackend
from .config import Config
from .errors import XodexError
from .files import WorkspaceFS, image, read_text, search, sha256
from .git import GitControl
from .github import GitHub, load_token
from .jobs import Jobs
from .patch import Change, commit, prepare
from .jsonutil import canonical
from .store import Store
from .tasks import Tasks
from .tools import TOOLS

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, config: Config, backend: PodmanBackend | None = None,
                 github: GitHub | None = None, git: GitControl | None = None):
        config.validate()
        self.config = config
        config.workspace_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if config.workspace_dir.is_symlink():
            raise XodexError("configuration", "Workspace root must not be a symlink")
        self.github = github or GitHub(load_token(config.github_token_file))
        self.git = git or GitControl(config, self.github.token)
        self.store = Store(config.state_dir)
        self.backend = backend or PodmanBackend(config)
        self.jobs = Jobs(config, self.store, self.backend, self._job_changed)
        self.tasks = Tasks(config, self.store, self.jobs, self.git, self.github)
        self.locks: dict[str, asyncio.Lock] = {}
        self.capabilities: dict[str, Any] = {}
        self.calls: set[asyncio.Task] = set()
        self.stopping = False

    async def start(self) -> None:
        self.capabilities = await self.backend.preflight()
        self.capabilities["git"] = await self.git.preflight()
        await self.jobs.recover()
        await self.tasks.recover()

    async def close(self) -> None:
        self.stopping = True
        if self.calls:
            await asyncio.gather(*list(self.calls), return_exceptions=True)
        await self.tasks.close()
        await self.jobs.shutdown()
        await self.github.close()
        self.store.close()

    def _job_changed(self, job: dict[str, Any]) -> None:
        if job["state"] == "uncertain":
            self.tasks.phase(job["task_id"], "blocked", "Process boundary could not be cleaned up safely",
                             error=canonical({"code": "reconciliation_required", "message": "Owner must inspect the process boundary"}))

    def check_space(self) -> None:
        for path in (self.config.state_dir, self.config.workspace_dir):
            free = shutil.disk_usage(path).free
            if free < self.config.min_free_bytes:
                raise XodexError("disk_pressure", "Insufficient free space; retained tasks will NOT be evicted", free_bytes=free)

    async def call(self, tool: str, args: Any) -> dict[str, Any]:
        if self.stopping:
            return XodexError("shutting_down", "Supervisor is stopping").result()
        task = asyncio.create_task(self._call(tool, args))
        self.calls.add(task)
        task.add_done_callback(self.calls.discard)
        return await asyncio.shield(task)

    async def _call(self, tool: str, args: Any) -> dict[str, Any]:
        key = None
        try:
            definition = TOOLS.get(tool)
            if definition is None:
                raise XodexError("unknown_tool", "Unknown tool", tool=tool)
            definition.validate(args)
            if definition.mutates:
                if self.jobs.fault and tool != "stop_task":
                    raise XodexError("storage_fault", self.jobs.fault + "; owner inspection and restart required")
                scope = args.get("task_id", "admission")
                async with self.locks.setdefault(scope, asyncio.Lock()):
                    key, cached = self.store.reserve(tool, args)
                    if cached is not None:
                        result = cached
                    else:
                        result = {"ok": True, **await self._dispatch(tool, args, key)}
                        self.store.complete(key, result)
                        self.store.audit("tool_completed", args.get("task_id") or result.get("task_id"), {"tool": tool, "operation": key})
            else:
                result = {"ok": True, **await self._dispatch(tool, args, None)}
            if result.get("ok") and tool == "exec_command":
                return {**result, "command": await self.jobs.poll(args["task_id"], result["command_id"],
                                                        wait_ms=args.get("yield_time_ms", 1000))}
            if result.get("ok") and tool in {"start_task", "finish_task", "continue_task", "stop_task"}:
                return {**result, **await self.tasks.status(result["task_id"])}
            return result
        except XodexError as error:
            result = error.result()
            if error.code == "reconciliation_required" and isinstance(args, dict) and args.get("task_id"):
                self.store.update(args["task_id"], reconciliation=str(error))
            if key:
                self.store.complete(key, result)
            return result
        except Exception as error:
            log.exception("Tool failed: %s", tool)
            result = XodexError("operation_uncertain" if key else "internal_error",
                                "Operation did not finish cleanly; inspect task progress before retrying",
                                exception=type(error).__name__).result()
            if key:
                self.store.db.execute("UPDATE operations SET state='uncertain' WHERE key=?", (key,))
                if tool in {"apply_patch", "write_file"}:
                    self.store.update(args["task_id"], reconciliation="Uncertain file mutation " + key)
            return result

    async def _dispatch(self, tool: str, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        task_id = args.get("task_id")
        if tool == "server_info":
            return {"name": "chatgpt-xodex", "version": __version__, "runtime": self.capabilities, "storage_fault": self.jobs.fault,
                    "repositories": list(self.config.repositories), "retention": "No automatic task directory deletion",
                    "agent_runtime": "ChatGPT supplies the coding loop. Server owns setup, checks, commits and PR publication.",
                    "client_support": "ChatGPT account/client availability is a platform gate, not implemented by this server"}
        if tool == "attach_repository":
            return await self.tasks.attach(args["repository"])
        if tool == "start_task":
            self.check_space()
            return self.tasks.start(args, key)
        if tool == "list_tasks":
            limit, offset = args.get("limit", 20), args.get("offset", 0)
            rows = self.store.db.execute("SELECT * FROM tasks ORDER BY created DESC,id LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            return {"tasks": [self.tasks.summary(dict(row)) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}
        if tool == "task_status":
            return await self.tasks.status(task_id, args.get("wait_ms", 0), args.get("command_id"), args.get("cursor"))
        if tool == "stop_task":
            return await self.tasks.stop(task_id)
        if tool == "continue_task":
            self.check_space()
            return self.tasks.resume(task_id)
        if tool == "finish_task":
            self.check_space()
            return self.tasks.finish(args)
        record = self.store.task(task_id)
        if record["phase"] in {"deleted", "deleting", "delete_failed"}:
            raise XodexError("task_deleted", "The owner explicitly deleted this task directory")
        root, home = self.tasks.paths(record)
        if tool == "task_note":
            self.store.update(task_id, note=args["note"])
            return {"saved": True}
        if tool == "command_input":
            self.tasks.writable(task_id, idle=False)
            active = self.jobs.active(task_id)
            if not active:
                raise XodexError("not_running", "No command is waiting for input")
            return await self.jobs.stdin(task_id, active[0], args["text"], args.get("close_stdin", False))
        if tool == "exec_command":
            self.tasks.writable(task_id)
            self.check_space()
            workdir = args.get("workdir", ".")
            with WorkspaceFS(root) as fs:
                fs.directory(workdir)
            timeout = args.get("timeout_seconds", self.config.timeout_seconds)
            if timeout > self.config.max_timeout_seconds:
                raise XodexError("invalid_timeout", "Command timeout exceeds the owner-configured ceiling")
            job = self.jobs.start(task_id, root, home, args["cmd"], workdir, args.get("tty", False), timeout,
                                  self.tasks.repository(record).network)
            return {"task_id": task_id, "command_id": job}
        if TOOLS[tool].mutates:
            self.tasks.writable(task_id)
            self.check_space()
        if not record["base_sha"]:
            raise XodexError("task_preparing", "Wait for task preparation before reading files")
        with WorkspaceFS(root) as fs:
            return self._filesystem(tool, fs, args, key)

    def _filesystem(self, tool: str, fs: WorkspaceFS, args: dict[str, Any], key: str | None) -> dict[str, Any]:
        if tool == "read_file":
            return read_text(fs, args["path"], args.get("start_line", 1), args.get("max_lines", 250))
        if tool == "list_files":
            return fs.listing(args.get("path", "."), args.get("limit", 500), args.get("depth", 6), False)
        if tool == "search_files":
            return search(fs, args["query"], args.get("glob", "*"), args.get("case_sensitive", True), args.get("limit", 50))
        if tool == "view_image":
            return {"image": image(fs, args["path"])}
        if tool == "apply_patch":
            changes = prepare(fs, args["patch"], args.get("expected_sha256", {}))
            return commit(fs, changes, self.store.root / "patches" / key)
        if tool == "write_file":
            before, mode = fs.read(args["path"]) if fs.exists(args["path"]) else (None, 0o644)
            observed = sha256(before) if before is not None else ""
            if observed != args["expected_sha256"]:
                raise XodexError("file_conflict", "File changed or create target already exists", observed_sha256=observed)
            content = args["content"].encode()
            if len(content) > 1024 * 1024:
                raise XodexError("file_too_large", "write_file is limited to 1 MiB of UTF-8")
            return commit(fs, [Change(args["path"], before, content, mode)], self.store.root / "patches" / key)
        raise XodexError("unknown_tool", "No filesystem handler", tool=tool)
