from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from xodex.backend import Launch
from xodex.config import Config, Repository
from xodex.engine import Engine
from xodex.git import GitControl


def git_at(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                            env={"PATH":"/usr/bin:/bin", "HOME":str(path), "GIT_CONFIG_NOSYSTEM":"1",
                                 "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_AUTHOR_NAME":"Test", "GIT_AUTHOR_EMAIL":"test@example.invalid",
                                 "GIT_COMMITTER_NAME":"Test", "GIT_COMMITTER_EMAIL":"test@example.invalid"})
    return result.stdout.decode().strip()


class LocalTestBackend:
    """TEST ONLY. Never installed or selectable by the production CLI."""
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    async def preflight(self):
        return {"backend": "TEST_ONLY_UNSANDBOXED", "rootless_podman_tested": False}

    def launch(self, name, root, home, command, cwd, tty, network):
        marker = self.root / (name + ".pid")
        wrapper = f"printf '%s' $$ > {shlex.quote(str(marker))}; exec /bin/bash --noprofile --norc -c {shlex.quote(command)}"
        return Launch(["/bin/bash", "-c", wrapper],
                      {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "TERM": "xterm-256color",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL":"/dev/null", "GIT_TERMINAL_PROMPT": "0"}, str(root / cwd))

    async def cleanup(self, name):
        marker = self.root / (name + ".pid")
        if marker.exists():
            try:
                os.killpg(int(marker.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
            marker.unlink(missing_ok=True)


class LocalGit(GitControl):
    """Local transport for real Git integration tests; not shipped as a runtime option."""
    def __init__(self, config, remote):
        super().__init__(config, "TEST_ONLY_CREDENTIAL")
        self.fixture_remote = remote

    def remote(self, task):
        return self.fixture_remote.as_uri()


class FakeGitHub:
    """In-memory GitHub HTTP contract stand-in; Git objects/ref updates are real."""
    token = "TEST_ONLY_CREDENTIAL"

    def __init__(self, remote):
        self.remote = remote
        self.prs = []
        self.creates = 0
        self.github_id = 42

    async def repository(self, name):
        return {"name":name, "github_id":self.github_id, "base_ref":"main"}

    async def head(self, name, branch):
        try:
            return git_at(self.remote, "rev-parse", "--verify", "refs/heads/" + branch)
        except subprocess.CalledProcessError:
            return None

    async def find_pr(self, name, branch, base):
        return next((pr for pr in self.prs if pr["head"]["ref"] == branch and pr["base"]["ref"] == base), None)

    async def create_pr(self, name, branch, base, title, body):
        self.creates += 1
        pr = {"number":self.creates, "html_url":f"https://github.com/{name}/pull/{self.creates}", "state":"open",
              "title":title, "body":body, "head":{"ref":branch,"sha":await self.head(name,branch),"repo":{"full_name":name}},
              "base":{"ref":base,"repo":{"full_name":name}}}
        self.prs.append(pr)
        return pr

    async def close(self):
        pass


@pytest.fixture
def remote(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    git_at(seed, "init", "-b", "main")
    (seed / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (seed / "test_calc.py").write_text("import sys\nsys.dont_write_bytecode = True\nfrom calc import add\nassert add(2, 3) == 5\n")
    (seed / "README.md").write_text("Test fixture\n")
    (seed / ".gitignore").write_text("__pycache__/\nignored.txt\n")
    git_at(seed, "add", ".")
    git_at(seed, "commit", "-m", "seed")
    destination = tmp_path / "remote.git"
    git_at(seed, "clone", "--bare", str(seed), str(destination))
    return destination


@pytest.fixture
def config(tmp_path):
    return Config(state_dir=tmp_path / "state", workspace_dir=tmp_path / "work",
                  engine_socket=tmp_path / "e.sock", mcp_socket=tmp_path / "m.sock",
                  min_free_bytes=0, timeout_seconds=5, max_timeout_seconds=30,
                  repositories={"test/repo": Repository()})


def make_engine(config, remote, github=None):
    return Engine(config, backend=LocalTestBackend(config.state_dir / "test-pids"),
                  github=github or FakeGitHub(remote), git=LocalGit(config, remote))


@pytest_asyncio.fixture
async def engine(config, remote):
    instance = make_engine(config, remote)
    await instance.start()
    yield instance
    await instance.close()


async def settle(engine, task_id):
    async with asyncio.timeout(15):
        while flow := engine.tasks.flows.get(task_id):
            await asyncio.shield(flow)
            await asyncio.sleep(0)
    return await engine.call("task_status", {"task_id":task_id})


async def create_task(engine, request_id="start"):
    attached = await engine.call("attach_repository", {"request_id":"attach", "repository":"test/repo"})
    assert attached["ok"], attached
    result = await engine.call("start_task", {"request_id":request_id, "repository":"test/repo", "task":"Fix addition"})
    assert result["ok"], result
    status = await settle(engine, result["task_id"])
    assert status["phase"] == "working", status
    return result["task_id"]


async def fix(engine, task_id):
    result = await engine.call("apply_patch", {"task_id":task_id,"request_id":"fix",
         "patch":"*** Begin Patch\n*** Update File: calc.py\n@@\n-    return a - b\n+    return a + b\n*** End Patch"})
    assert result["ok"], result


async def finish(engine, task_id, checks=None, key="finish"):
    result = await engine.call("finish_task", {"task_id":task_id,"request_id":key,"title":"fix: correct addition",
                                    "summary":"Fix addition and verify its regression test.","checks":checks or ["python3 test_calc.py"]})
    assert result["ok"], result
    return await settle(engine, task_id)
