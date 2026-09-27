"""Opt-in acceptance: real containers, but no real GitHub writes or OpenAI calls."""
import os

import pytest

from conftest import FakeGitHub, LocalGit, create_task
from xodex.cli import doctor
from xodex.engine import Engine

pytestmark = [pytest.mark.podman,
              pytest.mark.skipif(os.environ.get("XODEX_PODMAN_TESTS") != "1",
                                 reason="Requires actual rootless Podman and the configured worker image")]


async def test_real_sandbox(config):
    report = await doctor(config, True)
    assert report["sandbox_probe"] == "passed"


async def test_real_task_isolation(config, remote):
    engine = Engine(config, github=FakeGitHub(remote), git=LocalGit(config, remote))
    await engine.start()
    try:
        task_id = await create_task(engine)
        run = await engine.call("exec_command", {"task_id":task_id, "request_id":"isolation",
              "cmd":"set -eu; printf '%s\\n' \"$HOME\"; test ! -S /run/podman/podman.sock; test ! -e /run/host; test ! -e /root/.ssh; touch /workspace/retained; ! touch /etc/must-not-write",
              "yield_time_ms":1000})
        assert run["ok"], run
        status = await engine.jobs.poll(task_id, run["command_id"], wait_ms=10000)
        assert status["state"] == "succeeded", status
        assert "/home/worker" in status["output"]
        assert (engine.tasks.paths(engine.store.task(task_id))[0] / "retained").exists()
    finally:
        await engine.close()
