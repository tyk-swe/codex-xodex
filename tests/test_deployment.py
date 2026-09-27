import asyncio
import json
import os
import pty
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from xodex import deployment
from xodex.cli import dispatch, parser
from xodex.config import DEFAULT_IMAGE
from xodex.errors import XodexError

ROOT = Path(__file__).resolve().parents[1]


def test_units_use_installed_paths(config, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "units"))
    monkeypatch.setattr(sys, "executable", '/opt/owner space/$runtime%/bin/python')
    config_path = tmp_path / "owner $config.toml"
    files = deployment.service_files(config, config_path, tmp_path / "client", tmp_path / "profile.yaml", tmp_path / "private.env")
    engine = next(text for path, text in files.items() if path.name == "xodex-engine.service")
    tunnel = next(text for path, text in files.items() if path.name == "xodex-tunnel.service")
    assert 'ExecStart="/opt/owner space/$runtime%%/bin/python" -m xodex --config ' in engine
    assert f'"{str(config_path).replace("$", "$$")}" supervise' in engine
    assert f'Environment="MCP_UNIX_SOCKET_PATH={config.mcp_socket}"' in tunnel
    assert f'EnvironmentFile={tmp_path}/private.env' in tunnel
    assert "@PYTHON@" not in engine and "%h/.xodex" not in str(files)
    assert 'NoNewPrivileges=yes' not in engine


@pytest.mark.parametrize("kind", ["different", "public", "symlink", "directory", "fifo"])
def test_generated_conflicts_preserved(tmp_path, kind):
    path = tmp_path / "file"
    if kind == "different":
        path.write_text("original")
    elif kind == "public":
        path.write_text("wanted")
        path.chmod(0o666)
    elif kind == "symlink":
        path.symlink_to(tmp_path / "target")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = path.lstat()
    with pytest.raises(XodexError, match="conflicts"):
        deployment.write_generated(path, "wanted")
    assert path.lstat() == before


def test_generated_reuses_without_touch(tmp_path):
    path = tmp_path / "private/file"
    assert deployment.write_generated(path, "contents") == "created"
    before = path.stat()
    assert deployment.write_generated(path, "contents") == "reused"
    assert before == path.stat()
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".xodex-*"))


def test_generated_refuses_symlink_parent(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "target", target_is_directory=True)
    with pytest.raises(XodexError, match="symlink"):
        deployment.write_generated(tmp_path / "link/file", "contents")
    assert not list((tmp_path / "target").iterdir())


async def test_bundled_build_and_overrides(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(deployment, "executable", lambda value: value)
    monkeypatch.setenv("BASE_IMAGE", "registry.example/base@sha256:abc")
    monkeypatch.setenv("RUST_TOOLCHAIN", "1.89.0")
    commands = []
    async def run(argv, **kwargs):
        commands.append(argv)
        if "build" in argv:
            context = Path(argv[-1])
            assert list(context.iterdir()) == [context / "Containerfile"]
            assert (context / "Containerfile").read_text() == deployment.asset("worker/Containerfile")
            assert argv[argv.index("-f") + 1] == str(context / "Containerfile")
            return b""
        return b"sha256:" + b"a" * 64
    monkeypatch.setattr(deployment, "command", run)
    result = await deployment.build_worker(podman="/rootless/podman")
    assert result["image"] == DEFAULT_IMAGE
    assert commands[0][:2] == ["/rootless/podman", "build"]
    assert "BASE_IMAGE=registry.example/base@sha256:abc" in commands[0]
    assert "RUST_TOOLCHAIN=1.89.0" in commands[0]
    assert not Path(commands[0][-1]).exists()


async def test_build_failure_cleans_context(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(deployment, "executable", lambda value: value)
    contexts = []
    async def fail(argv, **kwargs):
        contexts.append(Path(argv[-1]))
        raise XodexError("deployment_command", "failed")
    monkeypatch.setattr(deployment, "command", fail)
    with pytest.raises(XodexError):
        await deployment.build_worker()
    assert not contexts[0].exists()


async def test_custom_build_refused(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(XodexError) as error:
        await deployment.build_worker(image="custom:latest")
    assert error.value.code == "custom_image"


async def test_commands_redact_child_output(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "must-not-be-inherited")
    with pytest.raises(XodexError) as error:
        await deployment.command([sys.executable, "-c", "import sys; print('sensitive-child-output'); sys.exit(19)"])
    assert error.value.details["exit_code"] == 19
    # Output is never embedded in deployment failures.
    assert "output" not in error.value.details
    output = await deployment.command([sys.executable, "-c", "import os; print(os.environ.get('CONTROL_PLANE_API_KEY', 'absent'))"])
    assert output == b"absent\n"


@pytest.mark.parametrize("reason", ["root", "runtime", "executable", "systemd"])
async def test_host_preflight_failure(config, monkeypatch, tmp_path, reason):
    monkeypatch.setattr(os, "geteuid", lambda: 0 if reason == "root" else 1000)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing" if reason == "runtime" else tmp_path))
    def executable(value):
        if reason == "executable":
            raise XodexError("host_prerequisite", "missing")
        return value
    monkeypatch.setattr(deployment, "executable", executable)
    monkeypatch.setattr(deployment, "command", AsyncMock(side_effect=XodexError("deployment_command", "no user manager")))
    with pytest.raises(XodexError):
        await deployment.check_host(config)


async def test_cli_socket_override_needs_no_config(monkeypatch, tmp_path):
    import xodex.cli as cli
    call = AsyncMock(return_value={"result": "passed"})
    monkeypatch.setattr(cli, "probe", call)
    socket = tmp_path / "private.sock"
    args = parser().parse_args(["--config", str(tmp_path / "absent.toml"), "smoke", "--socket", str(socket)])
    await dispatch(args)
    call.assert_awaited_once_with(socket)


def test_build_wrapper_forwards_environment(tmp_path):
    spy = tmp_path / "xodex"
    record = tmp_path / "record"
    spy.write_text(f'#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\nPath(os.environ["RECORD"]).write_text(json.dumps([sys.argv[1:], os.environ["BASE_IMAGE"], os.environ["RUST_TOOLCHAIN"]]))\n')
    spy.chmod(0o700)
    env = {**os.environ, "XODEX_BIN": str(spy), "RECORD": str(record), "BASE_IMAGE": "base", "RUST_TOOLCHAIN": "toolchain"}
    subprocess.run(["bash", str(ROOT / "scripts/build-worker.sh"), "--help"], env=env, check=True)
    assert json.loads(record.read_text()) == [["build-worker", "--help"], "base", "toolchain"]


@pytest.fixture
def bootstrap(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
if sys.argv[1:3] == ["-m", "venv"]:
    bin_dir = Path(sys.argv[3]) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("python", "xodex"):
        path = bin_dir / name
        path.write_text('#!/bin/sh\\nprintf "%s\\\\n" "$*" >> "$HOME/calls"\\n')
        path.chmod(0o700)
''')
    python.chmod(0o700)
    # The installer refuses root; use a test-only id to exercise bootstrap mechanics.
    identity = bin_dir / "id"
    identity.write_text("#!/bin/sh\necho 1000\n")
    identity.chmod(0o700)
    return home, {**os.environ, "HOME": str(home), "PATH": str(bin_dir) + ":/usr/bin:/bin"}


def test_install_only_bootstraps_without_setup(bootstrap):
    home, env = bootstrap
    result = subprocess.run(["bash", str(ROOT / "scripts/install-user.sh"), "--install-only"], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (home / ".local/bin/xodex").is_symlink()
    assert "setup" not in (home / "calls").read_text()
    assert not (home / ".xodex/config.toml").exists()
    assert not (home / ".config/systemd").exists()


def test_bootstrap_interactive_runs_setup(bootstrap):
    home, env = bootstrap
    master, slave = pty.openpty()
    try:
        result = subprocess.run(["bash", str(ROOT / "scripts/install-user.sh")], env=env, stdin=slave, stdout=slave, stderr=slave, timeout=10)
        assert result.returncode == 0
    finally:
        os.close(master)
        os.close(slave)
    assert (home / "calls").read_text().splitlines()[-1] == "setup"


def test_bootstrap_noninteractive_is_readonly(bootstrap):
    home, env = bootstrap
    result = subprocess.run(["bash", str(ROOT / "scripts/install-user.sh")], env=env, capture_output=True)
    assert result.returncode == 1 and b"--install-only" in result.stderr
    assert not list(home.iterdir())


def test_bootstrap_keeps_conflicting_launcher(bootstrap):
    home, env = bootstrap
    launcher = home / ".local/bin/xodex"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("owner launcher")
    result = subprocess.run(["bash", str(ROOT / "scripts/install-user.sh"), "--install-only"], env=env, capture_output=True)
    assert result.returncode == 1 and b"conflicts" in result.stderr
    assert launcher.read_text() == "owner launcher"
    assert not (home / ".xodex").exists()


async def test_visible_command_times_out_and_reaps(tmp_path):
    marker = tmp_path / "pid"
    script = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(marker)
    with pytest.raises(XodexError):
        await deployment.command([sys.executable, "-c", script], timeout=0.2, visible=True)
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


async def test_host_checks_rootless_cgroups(config, monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(deployment, "executable", lambda value: value)
    monkeypatch.setattr(deployment, "command", AsyncMock())
    monkeypatch.setattr(deployment.GitControl, "preflight", AsyncMock())
    monkeypatch.setattr(deployment.PodmanBackend, "_run", AsyncMock(return_value=(0, b'{"host":{"security":{"rootless":false},"cgroupVersion":"v2"}}')))
    with pytest.raises(XodexError, match="Rootless"):
        await deployment.check_host(config)


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze is required to parse generated units")
@pytest.mark.parametrize("binary_directory", ["bin", "bin $literal % space"])
def test_systemd_accepts_generated_units(config, tmp_path, monkeypatch, binary_directory):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    binaries = tmp_path / binary_directory
    binaries.mkdir()
    python, client = binaries / "python", binaries / "tunnel-client"
    python.symlink_to(sys.executable)
    client.symlink_to("/usr/bin/true")
    monkeypatch.setattr(sys, "executable", str(python))
    units = deployment.service_files(config, tmp_path / "config with $space.toml", client,
                                     tmp_path / "$profile.yaml", tmp_path / "private env%file")
    for path, content in units.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    result = subprocess.run(["systemd-analyze", "verify", "--man=no", *map(str, units)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    # verify can exit successfully even when it ignores an invalid EnvironmentFile.
    assert not any(str(path) in result.stderr for path in units), result.stderr


@pytest.mark.parametrize("value", ["/tmp/*.env", "/tmp/env\\file", "/tmp/env\nfile", "/tmp/env "])
def test_ambiguous_environment_paths_refused(value):
    with pytest.raises(XodexError):
        deployment.environment_file_path(Path(value))
