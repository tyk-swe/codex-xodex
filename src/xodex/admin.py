from __future__ import annotations

import shutil
import time
from typing import Any

from .backend import PodmanBackend
from .config import Config
from .errors import XodexError
from .github import GitHub, load_token
from .store import Store


async def administer(config: Config, action: str, task_id: str, confirm: str) -> dict[str, Any]:
    if confirm != task_id:
        raise XodexError("confirmation_required", "--confirm must exactly match the task UUID")
    if action not in {"delete", "reconcile", "retry-pr"}:
        raise XodexError("invalid_admin_action", "Unknown administration action")
    # The exclusive Store lock refuses administration while the supervisor is live.
    store = Store(config.state_dir)
    try:
        record = store.task(task_id)
        directory = config.workspace_dir / task_id
        if directory.is_symlink():
            raise XodexError("unsafe_path", "Refusing a symlink task root")
        if record["phase"] == "deleted":
            if action == "delete":
                return {"task_id": task_id, "already_deleted": True}
            raise XodexError("task_deleted", "A deleted task cannot be resumed")
        backend = PodmanBackend(config)
        await backend.preflight()
        for row in store.db.execute("SELECT id FROM jobs WHERE task_id=?", (task_id,)).fetchall():
            await backend.cleanup(f"xodex-{store.instance[:12]}-{row['id']}")
        if action == "retry-pr":
            if not record["commit_sha"] or not record["pr_attempted"]:
                raise XodexError("invalid_admin_action", "This task has no ambiguous PR request")
            github = GitHub(load_token(config.github_token_file))
            try:
                existing = await github.find_pr(record["repository"], record["branch"], record["base_ref"])
                if existing:
                    raise XodexError("pr_exists", "Continue the task to reconcile its existing pull request")
            finally:
                await github.close()
            store.update(task_id, pr_attempted=0, phase="blocked")
            store.audit("owner_authorized_pr_retry", task_id, {"duplicate_risk_acknowledged": True})
            return {"task_id": task_id, "retry_authorized": True,
                    "warning": "Owner authorizes a future create despite an earlier ambiguous outcome. No PR was created here."}
        if action == "reconcile":
            with store.transaction():
                store.update(task_id, reconciliation=None, phase="blocked")
                store.db.execute("UPDATE jobs SET state='interrupted',reason='owner_reconciled',finished=? WHERE task_id=? AND state IN ('starting','running','stopping','uncertain')",
                                 (time.time(), task_id))
                store.db.execute("UPDATE operations SET state='reconciled' WHERE task_id=? AND state IN ('pending','uncertain')", (task_id,))
                store.audit("owner_reconciled", task_id, {"acknowledged": True})
            return {"task_id": task_id, "reconciled": True,
                    "warning": "No commands replayed or files restored. Historical uncertain request IDs remain non-replayable."}
        store.update(task_id, phase="deleting")
        try:
            if directory.exists():
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise XodexError("unsafe_platform", "Safe fd-relative rmtree is required")
                shutil.rmtree(directory)
            store.update(task_id, phase="deleted", reconciliation=None)
            store.audit("owner_deleted_task", task_id, {"path": str(directory), "evidence_retained": True})
        except BaseException:
            store.update(task_id, phase="delete_failed")
            raise
        return {"task_id": task_id, "phase": "deleted", "directory": str(directory),
                "retained": "metadata, logs, protected Git objects and patch journals; remote branches and PRs untouched"}
    finally:
        store.close()
