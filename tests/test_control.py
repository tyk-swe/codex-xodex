import asyncio
import os
import shlex
import sys
from dataclasses import replace

import pytest

from xodex.backend import PodmanBackend
from xodex.control import run_control
from xodex.errors import XodexError


async def test_control_streams_stdin_and_stdout():
    output = await run_control([sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"],
                               {"PATH":"/usr/bin:/bin"}, data=b"data")
    assert output == b"atad"


async def test_control_nonzero_and_timeout_are_distinct():
    with pytest.raises(XodexError) as result:
        await run_control([sys.executable,"-c","raise SystemExit(17)"],{})
    assert result.value.details["exit_code"] == 17
    with pytest.raises(XodexError) as result:
        await run_control([sys.executable,"-c","import time;time.sleep(10)"],{},timeout=.05)
    assert result.value.code == "git_timeout"


async def test_control_output_limit_does_not_deadlock():
    script = "import os\nwhile True: os.write(1,b'x'*65536)"
    with pytest.raises(XodexError) as result:
        async with asyncio.timeout(3):
            await run_control([sys.executable,"-c",script],{},limit=1024)
    assert result.value.code == "control_output_limit"


async def test_control_cancellation_reaps_process(tmp_path):
    pid = tmp_path / "pid"
    script = f"import os,time;open({str(pid)!r},'w').write(str(os.getpid()));time.sleep(10)"
    task = asyncio.create_task(run_control([sys.executable,"-c",script],{}))
    async with asyncio.timeout(2):
        while not pid.exists():
            await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


async def test_podman_control_limits_are_bounded(config):
    backend = PodmanBackend(replace(config, podman=sys.executable))
    with pytest.raises(XodexError) as result:
        async with asyncio.timeout(3):
            await backend._run("-c", "import os\nwhile True: os.write(1,b'x'*65536)")
    assert result.value.code == "sandbox_unavailable"


def test_production_container_has_only_task_mounts(config, tmp_path):
    backend = PodmanBackend(config)
    backend.image_id = "a"*64
    root, home = tmp_path / "repo", tmp_path / "home"
    launch = backend.launch("test", root, home, "true", ".", False, "none")
    mounts = [launch.argv[i+1] for i,a in enumerate(launch.argv) if a == "--mount"]
    assert mounts == [f"type=bind,src={root},dst=/workspace,rw", f"type=bind,src={home},dst=/home/worker,rw"]
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never"):
        assert flag in launch.argv
    assert "RUSTUP_HOME=/usr/local/rustup" in launch.argv
    assert "OPENAI_API_KEY" not in launch.env and "GITHUB_TOKEN" not in launch.env


@pytest.mark.parametrize("backend", ["git", "podman"])
async def test_exited_control_parent_does_not_leave_pipe_holding_descendant(config, tmp_path, backend):
    marker = tmp_path / "child.pid"
    script = f'sleep 30 & printf "%s" "$!" > {shlex.quote(str(marker))}'
    if backend == "git":
        operation = run_control(["/bin/sh", "-c", script], {}, timeout=1)
    else:
        operation = PodmanBackend(replace(config, podman="/bin/sh"))._run("-c", script, timeout=1)
    try:
        with pytest.raises(XodexError) as result:
            async with asyncio.timeout(5):
                await operation
        assert marker.exists(), "The test must exercise a surviving descendant"
        assert result.value.code == ("git_timeout" if backend == "git" else "sandbox_unavailable")
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), 9)
            except ProcessLookupError:
                pass
