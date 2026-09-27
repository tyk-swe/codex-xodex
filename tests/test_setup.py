import asyncio
import importlib
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from xodex.config import load_config
from xodex.deployment import asset
from xodex.errors import XodexError
from xodex.github import GitHub

guided = importlib.import_module("xodex.setup")


class Answers(guided.Console):
    def __init__(self, answers=(), secrets=(), confirms=(), interactive=True):
        self.answers = iter(answers)
        self.secrets = iter(secrets)
        self.confirms = iter(confirms)
        self.messages = []
        self.questions = []
        self.is_interactive = interactive

    def interactive(self):
        return self.is_interactive

    def say(self, text):
        self.messages.append(text)

    def ask(self, label, default=""):
        answer = next(self.answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer or default

    def secret(self, label):
        answer = next(self.secrets)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def confirm(self, label, default=False):
        self.questions.append((label, default))
        return next(self.confirms, default)


@pytest.fixture
def host(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="xodex-setup-") as temporary:
        root = Path(temporary)
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(root / ".config"))
        for name in ("check_host", "check_repositories", "build_worker", "command"):
            monkeypatch.setattr(guided, name, AsyncMock())
        monkeypatch.setattr(guided, "image_available", AsyncMock(return_value=True))
        monkeypatch.setattr(guided, "doctor", AsyncMock(return_value={"sandbox_probe": "passed"}))
        monkeypatch.setattr(guided, "startup_smoke", AsyncMock(return_value={"result": "passed", "mutations": False}))
        monkeypatch.setattr(guided, "executable", lambda value: "/usr/bin/" + value)
        yield root


def fresh(*, confirms=(False, False, False), secrets=("github-test-secret",)):
    return Answers(["Test/Repo", "python3 test_calc.py", "", ""], secrets, confirms)


async def initial(host):
    path = host / "config.toml"
    await guided.setup(path, ui=fresh())
    return path


async def test_fresh_setup(host):
    path = host / "config.toml"
    ui = fresh()
    report = await guided.setup(path, ui=ui)
    config = load_config(path)
    assert config.repositories["test/repo"].checks == ["python3 test_calc.py"]
    assert config.repositories["test/repo"].network == "none"
    assert config.repositories["test/repo"].branch_prefix == "xodex/"
    assert config.state_dir == host / "state"
    assert config.github_token_file.read_text() == "github-test-secret\n"
    assert stat.S_IMODE(config.github_token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert report["local_ready"] and not report["tunnel"]["ready"]
    assert not report["services_started"]
    assert not config.state_dir.exists() and not config.workspace_dir.exists()
    assert not list(host.rglob("*.sqlite3"))
    assert guided.command.call_args_list[0].args[0][-1] == "daemon-reload"
    assert guided.command.await_count == 1
    assert all(default is False for _, default in ui.questions)
    guided.doctor.assert_awaited_once_with(config, True)
    guided.check_repositories.assert_awaited_once_with(config, "github-test-secret")
    assert "github-test-secret" not in json.dumps(report) + str(ui.messages)


async def test_rerun_retains_files(host):
    path = await initial(host)
    retained = host / "state/state.sqlite3"
    retained.parent.mkdir()
    retained.write_bytes(b"retained evidence")
    snapshots = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in host.rglob("*") if file.is_file()}
    report = await guided.setup(path, ui=Answers(confirms=[False, False]))
    assert all(status == "reused" for status in report["service_files"].values())
    assert snapshots == {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in snapshots}


async def test_noninteractive_writes_nothing(host):
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=Answers(interactive=False))
    assert failure.value.details["cause"] == "interactive_required"
    assert "setup" in failure.value.details["resume"]
    assert not list(host.iterdir())
    guided.check_host.assert_not_awaited()


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt(), asyncio.CancelledError()])
async def test_cancel_before_writes(host, error):
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=Answers([error]))
    assert failure.value.code == "setup_cancelled"
    assert not list(host.iterdir())


async def test_cancel_at_credentials_can_resume(host):
    path = host / "config.toml"
    with pytest.raises(XodexError) as failure:
        await guided.setup(path, ui=fresh(secrets=[EOFError()]))
    assert failure.value.details["stage"] == "GitHub credentials"
    assert path.exists()
    report = await guided.setup(path, ui=Answers(secrets=["new-secret"], confirms=[False, False]))
    assert report["local_ready"]


async def test_missing_prerequisites_do_not_write(host):
    guided.check_host.side_effect = XodexError("host_prerequisite", "Missing Podman")
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh())
    assert failure.value.details["stage"] == "host prerequisites"
    assert not list(host.iterdir())


async def test_invalid_secret_not_saved(host):
    ui = fresh(secrets=["secret with whitespace"])
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=ui)
    assert "secret with whitespace" not in str(failure.value.result()) + str(ui.messages)
    assert not (host / "secrets").exists()


@pytest.mark.parametrize("kind", ["public", "symlink", "directory", "fifo"])
async def test_bad_credentials_refused(host, kind):
    path = await initial(host)
    token = load_config(path).github_token_file
    token.unlink()
    if kind == "public":
        token.write_text("unlogged-secret")
        token.chmod(0o644)
    elif kind == "symlink":
        token.symlink_to(host / "missing")
    elif kind == "directory":
        token.mkdir()
    else:
        os.mkfifo(token, 0o600)
    with pytest.raises(XodexError) as failure:
        await guided.setup(path, ui=Answers())
    assert failure.value.details["stage"] == "GitHub credentials"
    assert "unlogged-secret" not in str(failure.value.result())


async def test_existing_external_token(host):
    token = host / "private-token"
    token.write_text("existing-token")
    token.chmod(0o600)
    before = token.stat().st_mtime_ns
    ui = Answers(["test/repo", "", str(token)], confirms=[False, False, False])
    await guided.setup(host / "config.toml", ui=ui)
    assert token.stat().st_mtime_ns == before
    assert load_config(host / "config.toml").repositories["test/repo"].checks == []


async def test_repository_failure_stops_startup(host):
    guided.check_repositories.side_effect = XodexError("repository_readonly", "Cannot publish")
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh())
    assert failure.value.details["stage"] == "GitHub repository access"
    guided.command.assert_not_awaited()
    guided.doctor.assert_not_awaited()


async def test_build_missing_default(host):
    guided.image_available.return_value = False
    report = await guided.setup(host / "config.toml", ui=fresh(confirms=[False, True, False, False]))
    assert report["local_ready"]
    guided.build_worker.assert_awaited_once()


async def test_build_failure_is_resumable(host):
    guided.image_available.return_value = False
    guided.build_worker.side_effect = XodexError("deployment_command", "Build failed")
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh(confirms=[False, True]))
    assert failure.value.details["stage"] == "worker image"
    assert "--config" in failure.value.details["resume"]
    guided.doctor.assert_not_awaited()
    guided.command.assert_not_awaited()


async def test_custom_image_needs_owner(host):
    path = await initial(host)
    path.write_text('image = "registry.example/worker:custom"\n' + path.read_text())
    guided.image_available.return_value = False
    guided.command.reset_mock()
    with pytest.raises(XodexError) as failure:
        await guided.setup(path, ui=Answers())
    assert failure.value.details["cause"] == "custom_image"
    guided.build_worker.assert_not_awaited()
    guided.command.assert_not_awaited()


async def test_probe_failure_blocks_units(host):
    guided.doctor.side_effect = XodexError("sandbox_probe_failed", "Probe failed")
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh())
    assert failure.value.details["stage"] == "sandbox probe"
    assert not (host / ".config").exists()
    guided.command.assert_not_awaited()


async def test_conflicting_units_preserved(host):
    unit = host / ".config/systemd/user/xodex-mcp.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("owner custom unit")
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh())
    assert failure.value.details["stage"] == "service files"
    assert failure.value.details["path"] == str(unit)
    assert unit.read_text() == "owner custom unit"
    assert list(unit.parent.iterdir()) == [unit]
    guided.command.assert_not_awaited()


async def test_local_start_with_tunnel_pending(host):
    report = await guided.setup(host / "config.toml", ui=fresh(confirms=[False, False, True]))
    assert report["services_started"] and report["smoke"]["result"] == "passed"
    assert not report["tunnel"]["ready"]
    startup = guided.command.call_args_list[1].args[0]
    assert startup[-2:] == [str(host / ".config/systemd/user" / name)
                            for name in ("xodex-engine.service", "xodex-mcp.service")]
    assert "restart" not in str(guided.command.call_args_list)
    guided.startup_smoke.assert_awaited_once_with(host / "run/mcp.sock")


async def test_start_failure_reports_stage(host):
    guided.command.side_effect = [b"", XodexError("deployment_command", "Start failed")]
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh(confirms=[False, False, True]))
    assert failure.value.details["stage"] == "service startup"
    guided.startup_smoke.assert_not_awaited()
    assert (host / "config.toml").exists()


async def test_smoke_failure_reports_resume(host):
    guided.startup_smoke.side_effect = XodexError("smoke_failed", "Smoke failed")
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh(confirms=[False, False, True]))
    assert failure.value.details["stage"] == "MCP smoke"
    assert "setup" in failure.value.details["resume"]


async def test_complete_tunnel_and_rerun(host):
    client = host / "client"
    client.write_text("#!/bin/sh\nexit 0\n")
    client.chmod(0o700)
    ui = Answers(["test/repo", "", "", "tunnel_" + "a" * 32],
                 ["github-secret", "runtime-secret"], [False, True, True])
    report = await guided.setup(host / "config.toml", tunnel_client=client, ui=ui)
    assert report["tunnel"]["ready"] and not report["tunnel"]["live_connection_tested"]
    assert guided.command.call_args_list[1].args[0][-1] == str(host / ".config/systemd/user/xodex-tunnel.service")
    assert stat.S_IMODE((host / "tunnel.env").stat().st_mode) == 0o600
    assert "runtime-secret" not in str(report) + str(ui.messages)
    report = await guided.setup(host / "config.toml", tunnel_client=client, ui=Answers(confirms=[True, False]))
    assert report["tunnel"]["ready"]


async def test_tunnel_bad_file_allows_local(host):
    env = host / "external.env"
    env.write_text("CONTROL_PLANE_API_KEY=private-value\n")
    env.chmod(0o644)
    ui = Answers(["test/repo", "", "", "tunnel_" + "1" * 32], ["github-secret"], [False, True, True])
    report = await guided.setup(host / "config.toml", tunnel_env=env, ui=ui)
    assert report["local_ready"] and report["services_started"] and not report["tunnel"]["ready"]
    assert "owner-only" in str(report["tunnel"]["issues"])
    assert "--tunnel-env" in report["tunnel"]["resume"]
    assert env.read_text() == "CONTROL_PLANE_API_KEY=private-value\n"
    assert "private-value" not in str(report) + str(ui.messages)


async def test_existing_tunnel_profile_not_overwritten(host):
    profile = host / "tunnel-client.yaml"
    profile.write_text("owner custom profile")
    profile.chmod(0o600)
    ui = fresh(confirms=[False, True, False])
    report = await guided.setup(host / "config.toml", ui=ui)
    assert report["local_ready"] and not report["tunnel"]["ready"]
    assert profile.read_text() == "owner custom profile"


async def test_repository_check_only_reads(monkeypatch, config):
    requests = []
    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"full_name": "test/repo", "id": 1, "default_branch": "main", "permissions": {"push": True}})
    monkeypatch.setattr(guided, "GitHub", lambda token: GitHub(token, transport=httpx.MockTransport(respond)))
    await guided.check_repositories(config, "secret")
    assert requests == [("GET", "/repos/test/repo")]


@pytest.mark.parametrize("contents", ["CONTROL_PLANE_API_KEY=REPLACE_WITH_YOUR_RUNTIME_KEY", "OTHER=secret",
    "CONTROL_PLANE_API_KEY=\"unterminated", "CONTROL_PLANE_API_KEY=a\nCONTROL_PLANE_API_KEY=b"])
def test_invalid_tunnel_env(tmp_path, contents):
    path = tmp_path / "tunnel.env"
    path.write_text(contents)
    path.chmod(0o600)
    with pytest.raises(XodexError):
        guided.tunnel_key(path)


def test_console_defaults_and_masking(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    masked = []
    monkeypatch.setattr(guided.getpass, "getpass", lambda prompt: masked.append(prompt) or "secret")
    ui = guided.Console()
    assert not ui.confirm("Start?")
    assert ui.confirm("Reuse?", True)
    assert ui.secret("Key") == "secret"
    assert masked == ["Key: "]


async def test_custom_image_reused(host):
    path = await initial(host)
    path.write_text('image = "custom:installed"\n' + path.read_text())
    report = await guided.setup(path, ui=Answers(confirms=[False, False]))
    assert report["local_ready"]
    guided.build_worker.assert_not_awaited()


async def test_units_cannot_live_in_workspaces(host, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(host / "tasks/user-config"))
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh())
    assert failure.value.details["cause"] == "configuration"
    assert not (host / "tasks").exists()


async def test_missing_tunnel_client_allows_local(host):
    ui = Answers(["test/repo", "", "", "tunnel_" + "a" * 32],
                 ["github-secret", "runtime-secret"], [False, True, True])
    report = await guided.setup(host / "config.toml", tunnel_client=host / "not-installed", ui=ui)
    assert report["local_ready"] and report["services_started"] and not report["tunnel"]["ready"]
    assert "Install" in report["tunnel"]["issues"][0]


async def test_inactive_service_reported(host):
    guided.command.side_effect = [b"", b"", XodexError("deployment_command", "Inactive")]
    with pytest.raises(XodexError) as failure:
        await guided.setup(host / "config.toml", ui=fresh(confirms=[False, False, True]))
    assert failure.value.details["stage"] == "service status"


def test_secret_never_falls_back_to_echo(monkeypatch):
    import warnings
    def unmasked(prompt):
        warnings.warn("Cannot mask", guided.getpass.GetPassWarning)
        pytest.fail("Must refuse before reading a visible credential")
    monkeypatch.setattr(guided.getpass, "getpass", unmasked)
    with pytest.raises(XodexError, match="masked input"):
        guided.Console().secret("Key")


async def test_each_started_service_checked(host):
    await guided.setup(host / "config.toml", ui=fresh(confirms=[False, False, True]))
    checks = [call.args[0] for call in guided.command.call_args_list if "is-active" in call.args[0]]
    assert [args[-1] for args in checks] == ["xodex-engine.service", "xodex-mcp.service"]
    assert all(len(args) == 5 for args in checks)


async def test_selected_config_dot_segments(host):
    (host / "child").mkdir()
    report = await guided.setup(host / "child/../config.toml", ui=fresh())
    assert report["configuration"] == str(host / "config.toml")
    assert load_config(host / "config.toml").state_dir == host / "state"
    assert all("/../" not in Path(path).read_text() for path in report["service_files"])


@pytest.mark.parametrize("start", [False, True])
async def test_custom_unit_directory_is_enabled_by_path(host, monkeypatch, start):
    # The setup shell's XDG_CONFIG_HOME need not match the running user manager.
    config_home = host / "custom config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    report = await guided.setup(host / "config.toml", ui=fresh(confirms=[False, False, start]))
    if start:
        argv = next(call.args[0] for call in guided.command.call_args_list if "enable" in call.args[0])
    else:
        argv = shlex.split(report["start_command"])
    assert argv == ["/usr/bin/systemctl", "--user", "enable", "--now",
                    str(config_home / "systemd/user/xodex-engine.service"),
                    str(config_home / "systemd/user/xodex-mcp.service")]
    assert all(Path(filename).is_file() for filename in argv[4:])


async def test_report_commands_use_installed_interpreter(host):
    path = host / "config.toml"
    client, profile, env = (host / name for name in ("client path", "profile path", "env path"))
    report = await guided.setup(path, tunnel_client=client, tunnel_config=profile, tunnel_env=env, ui=fresh())
    invocation = [sys.executable, "-m", "xodex", "--config", str(path)]
    assert shlex.split(report["tunnel"]["resume"]) == [*invocation, "setup", "--tunnel-client", str(client),
        "--tunnel-config", str(profile), "--tunnel-env", str(env)]
    assert shlex.split(report["smoke_command"]) == [*invocation, "smoke"]
