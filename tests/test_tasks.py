import asyncio
from dataclasses import replace

import pytest

from conftest import create_task, finish, fix, git_at, make_engine, settle
from xodex.config import Repository
from xodex.errors import XodexError


async def test_task_to_pr(engine):
    task_id = await create_task(engine)
    fail = await engine.call("exec_command", {"task_id":task_id,"request_id":"reproduce","cmd":"python3 test_calc.py"})
    assert fail["command"]["state"] == "failed"
    await fix(engine, task_id)
    result = await finish(engine, task_id)
    assert result["phase"] == "completed", result
    assert result["pull_request"] == "https://github.com/test/repo/pull/1"
    record = engine.store.task(task_id)
    assert git_at(engine.github.remote, "show", record["branch"]+":calc.py") == "def add(a, b):\n    return a + b"
    assert git_at(engine.github.remote, "rev-parse", record["branch"] + "^{tree}") == record["final_tree"]
    assert git_at(engine.github.remote, "rev-parse", record["branch"] + "^") == record["base_sha"]
    assert result["checks"][0]["exit_code"] == 0
    assert engine.github.creates == 1
    assert not engine.jobs.active(task_id)
    assert engine.tasks.paths(record)[0].is_dir()


async def test_validation_blocks_then_corrects(engine):
    task_id = await create_task(engine)
    blocked = await finish(engine, task_id)
    assert blocked["phase"] == "blocked" and blocked["error"]["code"] == "validation_failed"
    assert not blocked["commit"] and engine.github.creates == 0
    assert (await engine.call("continue_task", {"task_id":task_id,"request_id":"resume"}))["phase"] == "working"
    await fix(engine, task_id)
    assert (await finish(engine, task_id, key="corrected"))["phase"] == "completed"


async def test_changed_tree_not_published(engine):
    task_id = await create_task(engine)
    await fix(engine, task_id)
    result = await finish(engine, task_id, ["python3 test_calc.py; printf changed >> README.md"])
    assert result["error"]["code"] == "validation_changed_tree"
    assert not result["commit"] and not engine.github.prs


async def test_owner_checks_cannot_be_skipped(config, remote):
    config = replace(config, repositories={"test/repo":Repository(checks=("exit 17",))})
    engine = make_engine(config, remote)
    await engine.start()
    try:
        task_id = await create_task(engine)
        await fix(engine, task_id)
        result = await finish(engine, task_id, ["true"])
        assert result["checks"][0]["exit_code"] == 17
        assert not engine.github.prs
    finally:
        await engine.close()


async def test_no_changes_completion(engine):
    task_id = await create_task(engine)
    result = await finish(engine, task_id, ["python3 -m py_compile calc.py"])
    assert result["phase"] == "completed" and result["pull_request"] is None
    assert engine.github.creates == 0


async def test_start_exact_retry(engine):
    await engine.call("attach_repository", {"request_id":"a","repository":"test/repo"})
    args = {"request_id":"same","repository":"test/repo","task":"Fix addition"}
    first, second = await asyncio.gather(engine.call("start_task",args), engine.call("start_task",args))
    assert first["task_id"] == second["task_id"]
    assert len((await engine.call("list_tasks",{}))["tasks"]) == 1
    conflict = await engine.call("start_task", {**args,"task":"A different task"})
    assert conflict["error"]["code"] == "idempotency_conflict"
    await settle(engine,first["task_id"])


async def test_finish_retry_creates_one_pr(engine):
    task_id = await create_task(engine)
    await fix(engine, task_id)
    first = await finish(engine, task_id)
    second = await finish(engine, task_id)
    assert first["commit"] == second["commit"] and engine.github.creates == 1
    assert second["phase"] == "completed"


async def test_lost_pr_response_reconciled(engine, monkeypatch):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    original = engine.github.create_pr
    async def lost(*args):
        await original(*args)
        raise XodexError("github_uncertain","Response lost")
    monkeypatch.setattr(engine.github,"create_pr",lost)
    result = await finish(engine, task_id)
    assert result["phase"] == "completed" and engine.github.creates == 1


async def test_unknown_pr_outcome_never_blindly_retried(engine, monkeypatch):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    count = 0
    async def lost(*args):
        nonlocal count
        count += 1
        raise XodexError("github_uncertain","Response lost")
    monkeypatch.setattr(engine.github,"create_pr",lost)
    result = await finish(engine,task_id)
    assert result["phase"] == "blocked"
    await engine.call("continue_task",{"task_id":task_id,"request_id":"retry"})
    result = await settle(engine,task_id)
    assert result["error"]["code"] == "pr_outcome_unknown"
    assert count == 1


async def test_publish_error_reuses_recorded_commit(engine, monkeypatch):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    original = engine.git.push
    async def once(task):
        await original(task)
        raise XodexError("git_timeout","Lost push response")
    monkeypatch.setattr(engine.git,"push",once)
    result = await finish(engine,task_id)
    assert result["phase"] == "blocked" and result["commit"]
    monkeypatch.setattr(engine.git,"push",original)
    await engine.call("continue_task",{"task_id":task_id,"request_id":"resume"})
    resumed = await settle(engine,task_id)
    assert resumed["commit"] == result["commit"] and resumed["phase"] == "completed"
    assert engine.github.creates == 1


async def test_stop_job_retains_files_and_prevents_finish(engine):
    task_id = await create_task(engine)
    await engine.call("exec_command",{"task_id":task_id,"request_id":"run","cmd":"printf saved > saved; sleep 20","yield_time_ms":100})
    stop = await engine.call("stop_task",{"task_id":task_id,"request_id":"stop"})
    assert stop["phase"] == "stopped" and not engine.jobs.active(task_id)
    assert (await engine.call("read_file",{"task_id":task_id,"path":"saved"}))["text"] == "saved"
    denied = await engine.call("exec_command",{"task_id":task_id,"request_id":"denied","cmd":"true"})
    assert denied["error"]["code"] == "task_not_working"
    root = engine.tasks.paths(engine.store.task(task_id))[0]
    await engine.call("continue_task",{"task_id":task_id,"request_id":"resume"})
    assert engine.tasks.paths(engine.store.task(task_id))[0] == root


async def test_stop_before_launch(engine):
    task_id = await create_task(engine)
    # Start directly without yielding, then stop before the runner coroutine starts.
    root, home = engine.tasks.paths(engine.store.task(task_id))
    job = engine.jobs.start(task_id,root,home,"touch NEVER", ".",False,5,"none")
    await asyncio.wait_for(engine.jobs.stop(task_id),2)
    assert not engine.jobs.active(task_id)
    assert not (root / "NEVER").exists()
    assert engine.store.job(task_id,job)["state"] == "terminated"


async def test_stop_during_validation(engine):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    await engine.call("finish_task",{"task_id":task_id,"request_id":"finish","title":"fix","summary":"fixed","checks":["sleep 20"]})
    for _ in range(500):
        if engine.jobs.active(task_id):
            break
        await asyncio.sleep(.01)
    result = await engine.call("stop_task",{"task_id":task_id,"request_id":"stop"})
    assert result["phase"] == "stopped" and not engine.jobs.active(task_id) and not engine.github.prs


async def test_failed_setup_uses_new_attempt_without_deleting(engine, monkeypatch):
    original = engine.git.prepare
    async def fail(task,root,home):
        root.mkdir(parents=True)
        (root / "retained").write_text("partial")
        raise XodexError("git_failed","clone failed")
    monkeypatch.setattr(engine.git,"prepare",fail)
    await engine.call("attach_repository",{"repository":"test/repo","request_id":"a"})
    result = await engine.call("start_task",{"repository":"test/repo","task":"Fix","request_id":"s"})
    task_id = result["task_id"]
    await settle(engine,task_id)
    old = engine.tasks.paths(engine.store.task(task_id))[0]
    monkeypatch.setattr(engine.git,"prepare",original)
    await engine.call("continue_task",{"task_id":task_id,"request_id":"continue"})
    result = await settle(engine,task_id)
    assert result["phase"] == "working" and (old / "retained").read_text() == "partial"
    assert engine.tasks.paths(engine.store.task(task_id))[0] != old


async def test_repo_replacement_blocks_publish(engine):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    engine.github.github_id = 999
    result = await finish(engine,task_id)
    assert result["error"]["code"] == "repository_identity_changed" and not engine.github.prs


async def test_mutation_locked_during_finish(engine):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    await engine.call("finish_task",{"task_id":task_id,"request_id":"finish","title":"fix","summary":"fixed","checks":["sleep .2"]})
    result = await engine.call("write_file",{"task_id":task_id,"request_id":"late","path":"late","content":"x","expected_sha256":""})
    assert result["error"]["code"] == "task_not_working"
    assert (await settle(engine,task_id))["phase"] == "completed"


async def test_closed_pr_is_not_recreated(engine):
    task_id = await create_task(engine)
    await fix(engine,task_id)
    result = await finish(engine,task_id)
    engine.github.prs[0]["state"] = "closed"
    engine.store.update(task_id,phase="blocked",pr_url=None)
    await engine.call("continue_task",{"task_id":task_id,"request_id":"recover"})
    result = await settle(engine,task_id)
    assert result["phase"] == "completed" and result["pr_state"] == "closed"
    assert engine.github.creates == 1


async def test_task_note_handoff(engine):
    task_id = await create_task(engine)
    await engine.call("task_note",{"task_id":task_id,"request_id":"note","note":"Reproduced. Next: fix calc.py."})
    assert (await engine.call("task_status",{"task_id":task_id}))["note"] == "Reproduced. Next: fix calc.py."


async def test_no_session_or_deletion_tools(engine):
    for name in ("session_init","session_delete","task_delete","snapshot_create"):
        assert (await engine.call(name,{}))["error"]["code"] == "unknown_tool"


async def test_each_check_is_bound_to_unchanged_tree(engine):
    task_id = await create_task(engine)
    await fix(engine, task_id)
    result = await finish(engine, task_id, ["printf modified > README.md", "git checkout -- README.md"])
    assert result["error"]["code"] == "validation_changed_tree"
    assert len(result["checks"]) == 1 and not engine.github.prs


async def test_stop_after_pr_post_reconciles_without_duplicate(engine, monkeypatch):
    task_id = await create_task(engine)
    await fix(engine, task_id)
    sent = asyncio.Event()
    original = engine.github.create_pr
    async def slow(*args):
        result = await original(*args)
        sent.set()
        await asyncio.sleep(20)
        return result
    monkeypatch.setattr(engine.github, "create_pr", slow)
    await engine.call("finish_task", {"task_id":task_id,"request_id":"finish","title":"fix","summary":"fixed","checks":["python3 test_calc.py"]})
    await asyncio.wait_for(sent.wait(), 10)
    result = await engine.call("stop_task", {"task_id":task_id,"request_id":"stop"})
    assert result["phase"] == "stopped" and engine.github.creates == 1
    await engine.call("continue_task", {"task_id":task_id,"request_id":"resume"})
    result = await settle(engine, task_id)
    assert result["phase"] == "completed" and engine.github.creates == 1


async def test_blank_task_is_rejected(engine):
    await engine.call("attach_repository", {"request_id":"a","repository":"test/repo"})
    result = await engine.call("start_task", {"request_id":"blank","repository":"test/repo","task":"\n\t"})
    assert result["error"]["code"] == "invalid_task"


@pytest.mark.parametrize("phase", ["deleted", "deleting", "delete_failed"])
async def test_stop_cannot_reopen_owner_deleted_or_partial_tasks(engine, phase):
    task_id = await create_task(engine)
    engine.store.update(task_id, phase=phase)
    stopped = await engine.call("stop_task", {"task_id": task_id, "request_id": "stop-deleted"})
    assert stopped["phase"] == phase
    resumed = await engine.call("continue_task", {"task_id": task_id, "request_id": "resume-deleted"})
    assert resumed["error"]["code"] == "task_finished"


async def test_quarantined_working_task_requires_owner_before_resume(engine):
    task_id = await create_task(engine)
    engine.store.update(task_id, reconciliation="uncertain file mutation")
    status = await engine.call("task_status", {"task_id": task_id})
    assert "owner" in status["next_action"].lower()
    resumed = await engine.call("continue_task", {"task_id": task_id, "request_id": "quarantine"})
    assert resumed["error"]["code"] == "reconciliation_required"


async def test_task_title_uses_first_nonblank_line(engine):
    await engine.call("attach_repository", {"repository": "test/repo", "request_id": "attach"})
    started = await engine.call("start_task", {
        "repository": "test/repo", "request_id": "leading-newline", "task": "\n\nFix addition\nWith tests"
    })
    assert started["title"] == "Fix addition"
