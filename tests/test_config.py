import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from xodex.config import DEFAULT_PATHS, Repository, load_config, parse_config
from xodex.errors import XodexError
from xodex.jsonutil import canonical, digest


def test_minimal_config_follows_its_directory(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[repositories."owner/repo"]\n')
    config = load_config(path)
    for field, relative in DEFAULT_PATHS.items():
        assert getattr(config, field) == tmp_path / relative
    assert config.repositories == {"owner/repo": Repository()}


@pytest.mark.parametrize("selected", ["config.toml", "child/../config.toml"])
def test_relative_config_path_uses_absolute_directory(tmp_path, monkeypatch, selected):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "child").mkdir()
    Path("config.toml").write_text('[repositories."owner/repo"]\n')
    assert load_config(Path(selected)).state_dir == tmp_path / "state"


def test_explicit_paths_keep_precedence(config, tmp_path, monkeypatch):
    raw = asdict(config)
    raw["repositories"] = {"test/repo": {}}
    for field in DEFAULT_PATHS:
        if getattr(config, field) is not None:
            raw[field] = str(getattr(config, field))
        else:
            raw.pop(field)
    monkeypatch.setenv("TEST_XODEX_ROOT", str(tmp_path))
    raw["github_token_file"] = "${TEST_XODEX_ROOT}/private/token"
    result = parse_config(raw, tmp_path / "elsewhere/config.toml")
    assert result.state_dir == config.state_dir
    assert result.mcp_socket == config.mcp_socket
    assert result.github_token_file == tmp_path / "private/token"


@pytest.mark.parametrize("field,value", [("state_dir", "tasks/state"), ("engine_socket", "tasks/control.sock"),
    ("github_token_file", "tasks/token"), ("mcp_socket", "run/engine.sock")])
def test_derived_path_confinement(tmp_path, field, value):
    with pytest.raises(XodexError):
        parse_config({"repositories": {"test/repo": {}}, field: str(tmp_path / value)}, tmp_path / "config.toml")


def test_derived_paths_check_resolved_boundaries(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "run").symlink_to(tmp_path / "tasks", target_is_directory=True)
    with pytest.raises(XodexError):
        parse_config({"repositories": {"test/repo": {}}}, tmp_path / "config.toml")


def test_no_first_run_repository(tmp_path):
    with pytest.raises(XodexError, match="Allow at least one"):
        parse_config({}, tmp_path / "config.toml")


def test_operation_encoding_unchanged():
    value = {"request_id": "é", "nested": {"b": True, "a": None}, "number": 1.5}
    expected = '{"nested":{"a":null,"b":true},"number":1.5,"request_id":"é"}'
    assert canonical(value) == expected
    assert digest(value) == hashlib.sha256(expected.encode("utf-8")).hexdigest()
