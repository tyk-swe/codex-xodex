"""These checks can be copied out of the checkout for installed-wheel acceptance."""
import json
import os
import shlex
import subprocess
import sys
from importlib.resources import files

import pytest


@pytest.mark.parametrize("name", ["worker/Containerfile", "systemd/xodex-engine.service", "systemd/xodex-mcp.service",
                                  "systemd/xodex-tunnel.service", "tunnel-client.yaml", "tunnel.env.example"])
def test_packaged_assets(name):
    resource = files("xodex").joinpath("assets", name)
    assert resource.is_file() and resource.read_text(encoding="utf-8")


@pytest.mark.parametrize("command", ["setup", "build-worker", "smoke", "doctor", "supervise", "serve", "admin"])
def test_installed_command_help(tmp_path, command):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-m", "xodex", command, "--help"], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "usage: xodex" in result.stdout


def test_noninteractive_installed_setup(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "PATH": ""}
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-m", "xodex", "setup"], cwd=tmp_path, env=env,
                            input="", capture_output=True, text=True)
    assert result.returncode == 1
    assert '"cause": "interactive_required"' in result.stderr
    resume = shlex.split(json.loads(result.stderr)["error"]["details"]["resume"])
    # An absolute installation launcher works even before ~/.local/bin is on PATH.
    help_result = subprocess.run([*resume, "--help"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert help_result.returncode == 0, help_result.stderr
    assert "usage: xodex setup" in help_result.stdout
    assert not list(tmp_path.iterdir())
