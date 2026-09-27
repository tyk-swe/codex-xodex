from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config, repository_name
from .errors import XodexError
from .git import GitControl
from .github import GitHub
from .jobs import Jobs
from .jsonutil import canonical
from .store import Store

log = logging.getLogger(__name__)
ACTIVE_PHASES = {"preparing", "working", "validating", "publishing", "stopping"}


class Tasks:
    """The task owns its workspace, processes, evidence and publication lifecycle."""

    def __init__(self, config: Config, store: Store, jobs: Jobs, git: GitControl, github: GitHub):
        self.config, self.store, self.jobs, self.git, self.github = config, store, jobs, git, github
        self.flows: dict[str, asyncio.Task] = {}

    def paths(self, task: dict[str, Any]) -> tuple[Path, Path]:
        attempt = self.config.workspace_dir / task["id"] / f"attempt-{task['generation']}"
        return attempt / "repo", attempt / "home"

    def repository(self, task: dict[str, Any]):
        repo = self.config.repositories.get(task["repository"])
        if repo is None:
            raise XodexError("repository_revoked", "This repository is no longer allowed by the owner")
        return repo

    async def verify_repository(self, task: dict[str, Any]) -> None:
        self.repository(task)
        current = await self.github.repository(task["repository"])
        expected = self.store.db.execute("SELECT github_id FROM repositories WHERE name=?", (task["repository"],)).fetchone()
        if expected is None or current["github_id"] != expected["github_id"]:
            raise XodexError("repository_identity_changed", "Repository identity changed; do not publish to a replacement repository")

    async def attach(self, value: str) -> dict[str, Any]:
        name = repository_name(value)
        if name not in self.config.repositories:
            raise XodexError("repository_not_allowed", "The VPS owner must allow this exact GitHub repository first", repository=name)
        data = await self.github.repository(name)
        previous = self.store.db.execute("SELECT github_id FROM repositories WHERE name=?", (name,)).fetchone()
        if previous and previous["github_id"] != data["github_id"]:
            raise XodexError("repository_identity_changed", "An attached repository was replaced; owner review is required")
        self.store.db.execute("INSERT INTO repositories VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET base_ref=excluded.base_ref",
                              (name, data["github_id"], data["base_ref"], time.time()))
        return {"repository": name, "base_branch": data["base_ref"], "attached": True,
                "next_action": "Call start_task with the user's task. Manage execution details yourself."}

    def capacity(self) -> None:
        count = self.store.db.execute("SELECT count(*) FROM tasks WHERE phase IN ('preparing','working','validating','publishing','stopping')").fetchone()[0]
        if count >= self.config.max_tasks:
            raise XodexError("task_capacity", "Task limit reached; finish or explicitly stop an existing task")

    def phase(self, task_id: str, phase: str, message: str, **fields: Any) -> None:
        self.store.update(task_id, phase=phase, **fields)
        self.store.audit("progress", task_id, {"phase": phase, "message": message})

    def launch(self, task_id: str, operation: str) -> None:
        if task_id in self.flows and not self.flows[task_id].done():
            raise XodexError("task_busy", "This task already has an active lifecycle operation")
        flow = asyncio.create_task(self._drive(task_id, operation))
        self.flows[task_id] = flow
        def finished(done: asyncio.Task) -> None:
            if self.flows.get(task_id) is done:
                self.flows.pop(task_id, None)
        flow.add_done_callback(finished)

    def start(self, args: dict[str, Any], key: str) -> dict[str, Any]:
        self.capacity()
        if not args["task"].strip() or ("title" in args and not args["title"].strip()):
            raise XodexError("invalid_task", "Task and title must contain meaningful text")
        name = repository_name(args["repository"])
        repo = self.config.repositories.get(name)
        attached = self.store.db.execute("SELECT * FROM repositories WHERE name=?", (name,)).fetchone()
        if repo is None or attached is None:
            raise XodexError("repository_not_attached", "Call attach_repository for an allowed repository first")
        identifier = str(uuid.uuid5(uuid.UUID(hex=self.store.instance), key))
        now = time.time()
        self.store.db.execute("""INSERT INTO tasks(id,repository,prompt,title,phase,created,updated,branch,base_ref)
                                  VALUES(?,?,?,?,?,?,?,?,?)""",
                              (identifier, name, args["task"], args.get("title", args["task"].strip().splitlines()[0][:120]),
                               "preparing", now, now, repo.branch_prefix + identifier, attached["base_ref"]))
        self.store.audit("progress", identifier, {"phase": "preparing", "message": "Preparing an isolated task clone and branch"})
        self.launch(identifier, "prepare")
        return {"task_id": identifier, "accepted": True}

    async def _drive(self, task_id: str, operation: str) -> None:
        try:
            if operation == "prepare":
                record = self.store.task(task_id)
                await self.verify_repository(record)
                result = await self.git.prepare(record, *self.paths(record))
                self.phase(task_id, "working", "Repository ready; implement and test the task", **result, error=None)
            elif operation == "finish":
                await self._finish(task_id)
            else:
                await self._publish(task_id)
        except asyncio.CancelledError:
            raise
        except XodexError as error:
            if error.code == "reconciliation_required":
                self.store.update(task_id, reconciliation=str(error))
            self.phase(task_id, "blocked", str(error), error=canonical(error.result()["error"]))
        except Exception as error:
            log.exception("Task lifecycle failed: %s", task_id)
            failure = XodexError("task_failure", "Task operation stopped unexpectedly; inspect progress before continuing", exception=type(error).__name__)
            self.phase(task_id, "blocked", str(failure), error=canonical(failure.result()["error"]))

    def writable(self, task_id: str, *, idle: bool = True) -> dict[str, Any]:
        record = self.store.task(task_id)
        self.repository(record)
        if record["reconciliation"]:
            raise XodexError("reconciliation_required", "An uncertain filesystem or process boundary needs owner review",
                             reason=record["reconciliation"])
        if record["phase"] != "working" or record["commit_sha"]:
            raise XodexError("task_not_working", "Task is not accepting code changes", phase=record["phase"])
        if idle and self.jobs.active(task_id):
            raise XodexError("task_busy", "A command is still running; inspect task_status instead of starting a duplicate")
        return record

    def finish(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        record = self.writable(task_id)
        mandatory = list(self.repository(record).checks)
        checks = list(dict.fromkeys(mandatory + args["checks"]))
        if not checks or any(not check.strip() for check in checks):
            raise XodexError("validation_required", "Provide at least one meaningful validation command")
        if not args["title"].strip() or any(c in args["title"] for c in "\n\r\x00") or not args["summary"].strip():
            raise XodexError("invalid_publication", "Provide a single-line title and a nonempty summary")
        spec = {"title": args["title"], "summary": args["summary"], "checks": checks}
        self.phase(task_id, "validating", "Snapshotting changes and running validation before publication",
                   finish_spec=canonical(spec), checks="[]", error=None, final_tree=None)
        self.launch(task_id, "finish")
        return {"task_id": task_id, "accepted": True}

    async def _finish(self, task_id: str) -> None:
        record = self.store.task(task_id)
        spec = json.loads(record["finish_spec"])
        root, home = self.paths(record)
        snapshot = await self.git.snapshot(record, root)
        self.store.audit("candidate", task_id, snapshot)
        results = []
        for command in spec["checks"]:
            await self.jobs.wait_capacity()
            job = self.jobs.start(task_id, root, home, command, ".", False, self.config.timeout_seconds,
                                  self.repository(record).network, kind="validation")
            self.store.audit("progress", task_id, {"phase": "validating", "message": "Running a validation command", "command_id": job})
            live = self.jobs.running[job]
            await live.done.wait()
            if self.jobs.fault:
                raise XodexError("reconciliation_required", self.jobs.fault)
            result = self.store.job(task_id, job)
            results.append({"command_id": job, "command": command, "state": result["state"],
                            "exit_code": result["exit_code"], "reason": result["reason"], "tree": snapshot["tree"]})
            self.store.update(task_id, checks=canonical(results))
            if result["state"] != "succeeded" or result["exit_code"] != 0:
                raise XodexError("validation_failed", "Validation failed; no branch or PR was published", command_id=job)
            after = await self.git.snapshot(record, root)
            if snapshot["tree"] != after["tree"]:
                raise XodexError("validation_changed_tree", "Validation changed publishable files; inspect changes and rerun validation before publishing")
        self.store.update(task_id, final_tree=snapshot["tree"])
        if snapshot["tree"] == record["base_tree"]:
            self.phase(task_id, "completed", "Validation passed; there are no changes to publish", note=spec["summary"])
            return
        record = self.store.task(task_id)
        sha = await self.git.commit(record, spec["title"], spec["summary"])
        self.phase(task_id, "publishing", "Validated commit recorded; publishing task branch and pull request", commit_sha=sha)
        await self._publish(task_id)

    def _accept_pr(self, task: dict[str, Any], pr: dict[str, Any]) -> None:
        marker = "<!-- xodex-task:" + task["id"] + " -->"
        expected_url = "https://github.com/" + task["repository"] + "/pull/"
        number = pr.get("number")
        if (type(number) is not int or number < 1 or pr.get("html_url", "").lower() != expected_url + str(number)
                or marker not in (pr.get("body") or "") or pr.get("head", {}).get("sha") != task["commit_sha"]
                or pr.get("head", {}).get("ref") != task["branch"] or pr.get("base", {}).get("ref") != task["base_ref"]
                or pr.get("head", {}).get("repo", {}).get("full_name", "").lower() != task["repository"]
                or pr.get("base", {}).get("repo", {}).get("full_name", "").lower() != task["repository"]):
            raise XodexError("publication_conflict", "The matching pull request does not have this task's identity and validated commit")
        self.phase(task["id"], "completed", "Pull request ready", pr_url=pr["html_url"],
                   pr_state=pr.get("state", "unknown"), error=None)

    async def _publish(self, task_id: str) -> None:
        record = self.store.task(task_id)
        await self.verify_repository(record)
        # Reconcile the stable task identity before any remote mutation, including after a lost response.
        pr = await self.github.find_pr(record["repository"], record["branch"], record["base_ref"])
        if pr:
            self._accept_pr(record, pr)
            return
        if record["pr_attempted"]:
            raise XodexError("pr_outcome_unknown", "A PR request may have succeeded but lookup returned none. No second create request will be sent without owner reconciliation")
        current = await self.github.head(record["repository"], record["branch"])
        if current is not None and current != record["commit_sha"]:
            raise XodexError("publication_conflict", "Task branch points at a different commit; it will not be overwritten")
        if current is None:
            await self.git.push(record)
        current = await self.github.head(record["repository"], record["branch"])
        if current != record["commit_sha"]:
            raise XodexError("publication_unconfirmed", "Published branch could not be confirmed; no PR request was sent")
        spec = json.loads(record["finish_spec"])
        evidence = "\n".join(f"- `{check['command'].replace('`', '')}` — exit {check['exit_code']}" for check in json.loads(record["checks"]))
        body = (f"{spec['summary']}\n\n## Validation\n{evidence}\n\n"
                f"Validated tree: `{record['final_tree']}`\n\n<!-- xodex-task:{task_id} -->")
        self.store.update(task_id, pr_attempted=1)
        try:
            pr = await self.github.create_pr(record["repository"], record["branch"], record["base_ref"], spec["title"], body)
        except XodexError as error:
            if error.code == "github_http" and error.details.get("status") in {400, 401, 403, 404, 422, 429}:
                self.store.update(task_id, pr_attempted=0)
            # The POST may have reached GitHub. Read before considering any retry.
            pr = await self.github.find_pr(record["repository"], record["branch"], record["base_ref"])
            if pr is None:
                raise error
        self._accept_pr(record, pr)

    async def stop(self, task_id: str) -> dict[str, Any]:
        record = self.store.task(task_id)
        if record["phase"] in {"completed", "stopped", "deleted", "deleting", "delete_failed"}:
            return {"task_id": task_id, "stopped": record["phase"] == "stopped", "phase": record["phase"]}
        self.phase(task_id, "stopping", "Stopping task execution; preserving files and any existing remote effects")
        flow = self.flows.get(task_id)
        if flow is not None:
            flow.cancel()
            await asyncio.gather(flow, return_exceptions=True)
            self.flows.pop(task_id, None)
        await self.jobs.stop(task_id)
        self.phase(task_id, "stopped", "Task stopped; changes, logs and remote effects are retained")
        return {"task_id": task_id, "stopped": True,
                "remote_effects": "A branch or PR already sent to GitHub is not undone. Continue reconciles publication."}

    def resume(self, task_id: str) -> dict[str, Any]:
        record = self.store.task(task_id)
        self.repository(record)
        if record["phase"] in {"completed", "deleted", "deleting", "delete_failed"}:
            raise XodexError("task_finished", "This task is finished; start a new task for additional changes")
        if record["reconciliation"]:
            raise XodexError("reconciliation_required", "Owner review is required before resuming this task")
        if record["phase"] in ACTIVE_PHASES:
            return {"task_id": task_id, "already_active": True}
        self.capacity()
        if record["commit_sha"]:
            self.phase(task_id, "publishing", "Reconciling the existing validated commit and PR", error=None)
            self.launch(task_id, "publish")
        elif record["base_sha"] is None:
            self.phase(task_id, "preparing", "Creating a fresh setup attempt; retaining the previous partial clone",
                       generation=record["generation"] + 1, error=None)
            self.launch(task_id, "prepare")
        else:
            self.phase(task_id, "working", "Continuing in the existing task clone", error=None)
        return {"task_id": task_id, "continued": True}

    async def status(self, task_id: str, wait_ms: int = 0, command_id: int | None = None,
                     cursor: int | None = None) -> dict[str, Any]:
        self.store.task(task_id)
        flow = self.flows.get(task_id)
        if flow and wait_ms:
            try:
                await asyncio.wait_for(asyncio.shield(flow), wait_ms / 1000)
            except (TimeoutError, asyncio.CancelledError):
                if asyncio.current_task().cancelling():
                    raise
        elif wait_ms and self.jobs.active(task_id):
            await self.jobs.poll(task_id, self.jobs.active(task_id)[0], wait_ms=wait_ms)
        record = self.store.task(task_id)
        output = None
        if command_id is None:
            row = self.store.db.execute("SELECT id,output_bytes FROM jobs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            if row:
                command_id = row["id"]
                if cursor is None:
                    cursor = max(0, row["output_bytes"] - 16000)
        if command_id is not None:
            output = await self.jobs.poll(task_id, command_id, cursor or 0, 16000)
        events = self.store.db.execute("SELECT id,at,event,body FROM audit WHERE task_id=? ORDER BY id DESC LIMIT 20", (task_id,)).fetchall()
        return {**self.summary(record), "task": record["prompt"], "base_branch": record["base_ref"],
                "checks": json.loads(record["checks"]), "note": record["note"],
                "command": output, "events": [{**dict(event), "body": json.loads(event["body"])} for event in reversed(events)],
                "error": json.loads(record["error"]) if record["error"] else None,
                "reconciliation_required": bool(record["reconciliation"]),
                "next_action": self.next_action(record), "workspace": str(self.paths(record)[0])}

    def summary(self, record: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": record["id"], "repository": record["repository"], "title": record["title"],
                "phase": record["phase"], "created_at": record["created"], "updated_at": record["updated"],
                "branch": record["branch"], "commit": record["commit_sha"], "pull_request": record["pr_url"],
                "pr_state": record["pr_state"]}

    def next_action(self, record: dict[str, Any]) -> str:
        phase = record["phase"]
        if record["reconciliation"]:
            return "Owner inspection and offline reconciliation are required before further execution."
        if phase in {"preparing", "validating", "publishing", "stopping"}:
            return "Poll task_status; do not relaunch the task or repeat its side effects."
        if phase == "working":
            return "Read AGENTS.md, implement and validate the task. Call finish_task to validate, commit and open the PR."
        if phase == "blocked":
            return "Inspect the error and command output; continue_task resumes recoverable work. Do not mask failed validation."
        if phase == "stopped":
            return "The task was stopped. Resume only when the user asks to continue."
        return "Report the result and actual validation evidence. No session cleanup is required."

    async def recover(self) -> None:
        # An HTTP connection is not a task. A supervisor restart, however, interrupts execution.
        for row in self.store.db.execute("SELECT id FROM tasks WHERE phase IN ('preparing','working','validating','publishing','stopping')").fetchall():
            self.phase(row["id"], "blocked", "Supervisor restarted; no arbitrary command was replayed",
                       error=canonical({"code": "supervisor_restart", "message": "Continue the task to recover; publication uses its existing commit identity"}))

    async def close(self) -> None:
        flows = list(self.flows.items())
        for _, flow in flows:
            flow.cancel()
        await asyncio.gather(*(flow for _, flow in flows), return_exceptions=True)
        self.flows.clear()
        for task_id, _ in flows:
            record = self.store.task(task_id)
            if record["phase"] in ACTIVE_PHASES:
                self.phase(task_id, "blocked", "Supervisor shutdown interrupted task lifecycle",
                           error=canonical({"code": "supervisor_shutdown", "message": "Continue this retained task after restart"}))
