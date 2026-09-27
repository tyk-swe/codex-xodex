import asyncio
from dataclasses import replace

import pytest

from conftest import create_task, make_engine


async def run(engine, task_id, cmd, key="run", **kwargs):
    result = await engine.call("exec_command",{"task_id":task_id,"request_id":key,"cmd":cmd,**kwargs})
    assert result["ok"], result
    return result


async def test_command_retry_once(engine):
    task_id = await create_task(engine)
    first = await run(engine,task_id,"printf x >> count")
    second = await run(engine,task_id,"printf x >> count")
    assert first["command_id"] == second["command_id"]
    assert (await engine.call("read_file",{"task_id":task_id,"path":"count"}))["text"] == "x"


async def test_one_writer(engine):
    task_id = await create_task(engine)
    job = await run(engine,task_id,"sleep .1",yield_time_ms=0)
    result = await engine.call("write_file",{"task_id":task_id,"request_id":"write","path":"x","content":"x","expected_sha256":""})
    assert result["error"]["code"] == "task_busy"
    await engine.jobs.poll(task_id,job["command_id"],wait_ms=1000)


async def test_command_bound_to_task(engine):
    a = await create_task(engine,"a")
    b = await create_task(engine,"b")
    result = await run(engine,a,"echo private")
    wrong = await engine.call("task_status",{"task_id":b,"command_id":result["command_id"]})
    assert wrong["error"]["code"] == "not_found"


async def test_cursor_repeat_and_bounds(engine):
    task_id = await create_task(engine)
    job = (await run(engine,task_id,"printf abcdef"))["command_id"]
    first = await engine.jobs.poll(task_id,job,limit=3)
    assert first == await engine.jobs.poll(task_id,job,limit=3)
    assert first["output"] == "abc" and first["has_more"]
    assert (await engine.jobs.poll(task_id,job,cursor=3))["output"] == "def"
    assert not (await engine.call("task_status",{"task_id":task_id,"command_id":job,"cursor":999}))["ok"]


async def test_exit_code_is_not_http_success(engine):
    task_id = await create_task(engine)
    result = await run(engine,task_id,"echo failure >&2; exit 23")
    assert result["command"]["exit_code"] == 23 and result["command"]["state"] == "failed"
    assert "failure" in result["command"]["output"]


async def test_timeout_retains(engine):
    task_id = await create_task(engine)
    result = await run(engine,task_id,"printf retained > note; sleep 20",timeout_seconds=1,yield_time_ms=2000)
    assert result["command"]["reason"] == "timeout"
    assert (await engine.call("read_file",{"task_id":task_id,"path":"note"}))["text"] == "retained"


async def test_pipe_input(engine):
    task_id = await create_task(engine)
    result = await run(engine,task_id,"cat",yield_time_ms=20)
    args = {"task_id":task_id,"request_id":"input","text":"hello\n","close_stdin":True}
    assert (await engine.call("command_input",args))["ok"]
    assert (await engine.call("command_input",args))["ok"]
    assert (await engine.jobs.poll(task_id,result["command_id"],wait_ms=1000))["output"] == "hello\n"


async def test_pty(engine):
    task_id = await create_task(engine)
    result = await run(
        engine, task_id, 'read -r line; printf "got:%s\\n" "$line"; stty size',
        tty=True, yield_time_ms=0,
    )
    live = engine.jobs.running[result["command_id"]]
    async with asyncio.timeout(10):
        await live.launched.wait()
        sent = await engine.call("command_input", {
            "task_id": task_id, "request_id": "input", "text": "hi\n"
        })
        assert sent["ok"], sent
        await live.done.wait()
    command = await engine.jobs.poll(task_id, result["command_id"])
    assert command["state"] == "succeeded", command
    assert "got:hi" in command["output"] and "40 120" in command["output"]


async def test_output_cap(config,remote):
    config = replace(config,output_limit_bytes=4096)
    engine = make_engine(config,remote)
    await engine.start()
    try:
        task_id = await create_task(engine)
        job = await run(engine,task_id,"yes flood",yield_time_ms=1000)
        result = await engine.jobs.poll(task_id,job["command_id"],wait_ms=2000)
        assert result["reason"] == "output_limit" and result["total_bytes"] == 4096
    finally:
        await engine.close()


async def test_restart_retains_without_replay(config,remote):
    engine = make_engine(config,remote)
    await engine.start()
    task_id = await create_task(engine)
    job = await run(engine,task_id,"printf retained > note; printf before; sleep 20",yield_time_ms=100)
    await engine.close()
    engine = make_engine(config,remote)
    await engine.start()
    try:
        status = await engine.call("task_status",{"task_id":task_id})
        assert status["phase"] == "blocked"
        assert status["command"]["state"] == "interrupted"
        assert "before" in status["command"]["output"]
        assert (await engine.call("read_file",{"task_id":task_id,"path":"note"}))["text"] == "retained"
        assert len((await engine.call("list_tasks",{}))["tasks"]) == 1
        await engine.call("continue_task",{"task_id":task_id,"request_id":"resume"})
        assert not engine.jobs.active(task_id)
    finally:
        await engine.close()


async def test_descendant_keeps_writer_ownership(engine):
    task_id = await create_task(engine)
    result = await run(engine,task_id,"(sleep .2; printf child) &",yield_time_ms=20)
    assert result["command"]["state"] == "running"
    assert engine.jobs.active(task_id)
    assert (await engine.jobs.poll(task_id,result["command_id"],wait_ms=1000))["output"] == "child"


async def test_environment_does_not_inherit_credentials(engine,monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY","TUNNEL_SECRET")
    monkeypatch.setenv("GITHUB_TOKEN","GITHUB_SECRET")
    monkeypatch.setenv("SSH_AUTH_SOCK","/host/agent.sock")
    task_id = await create_task(engine)
    output = (await run(engine,task_id,"env"))["command"]["output"]
    assert "TUNNEL_SECRET" not in output and "GITHUB_SECRET" not in output and "SSH_AUTH_SOCK" not in output


async def test_bad_workdir_and_timeout_rejected(engine):
    task_id = await create_task(engine)
    for fields in ({"workdir":"../"},{"timeout_seconds":31}):
        result = await engine.call("exec_command",{"task_id":task_id,"request_id":str(fields),"cmd":"touch NEVER",**fields})
        assert not result["ok"]


async def test_persistence_failure_releases_waiters_and_blocks_new_writes(engine, monkeypatch):
    import sqlite3
    task_id = await create_task(engine)
    original = engine.store.audit
    def fail(event, *args):
        if event == "job_finished":
            raise sqlite3.OperationalError("simulated disk full")
        return original(event, *args)
    monkeypatch.setattr(engine.store, "audit", fail)
    run = await engine.call("exec_command", {"task_id":task_id,"request_id":"run","cmd":"true","yield_time_ms":1000})
    assert not engine.jobs.active(task_id) and engine.jobs.fault
    result = await engine.call("exec_command", {"task_id":task_id,"request_id":"another","cmd":"touch MUST_NOT_RUN"})
    assert result["error"]["code"] == "storage_fault"


async def test_uncertain_cleanup_quarantines_task(engine, monkeypatch):
    from xodex.errors import XodexError
    task_id = await create_task(engine)
    original = engine.backend.cleanup
    async def uncertain(name):
        await original(name)
        raise XodexError("reconciliation_required", "simulated lost boundary response")
    monkeypatch.setattr(engine.backend, "cleanup", uncertain)
    run = await engine.call("exec_command", {"task_id":task_id,"request_id":"run","cmd":"true","yield_time_ms":1000})
    assert run["command"]["state"] == "uncertain"
    assert engine.store.task(task_id)["reconciliation"]
    result = await engine.call("continue_task", {"task_id":task_id,"request_id":"continue"})
    assert result["error"]["code"] == "reconciliation_required"


@pytest.mark.parametrize("tty", [False, True])
async def test_cancel_during_spawn_keeps_process_owned_until_reaped(engine, monkeypatch, tty):
    task_id = await create_task(engine)
    original = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def slow_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    result = await run(engine, task_id, "sleep 30", tty=tty, yield_time_ms=0)
    live = engine.jobs.running[result["command_id"]]
    try:
        async with asyncio.timeout(10):
            await spawned.wait()
            live.task.cancel()
            await asyncio.sleep(0)
            release.set()
            await live.done.wait()
        assert processes[0].returncode is not None
        assert not engine.jobs.active(task_id)
        terminal = await engine.jobs.poll(task_id, result["command_id"])
        assert terminal["state"] == "interrupted"
    finally:
        release.set()
