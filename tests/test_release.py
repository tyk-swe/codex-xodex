import hashlib
import re
import runpy
import shutil
import stat
import tomllib
import zipfile
from pathlib import Path

import pytest

from xodex import __version__
from xodex.config import Config

ROOT = Path(__file__).resolve().parents[1]


def test_release_version_has_one_runtime_authority():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    example = tomllib.loads((ROOT / "deploy/config.example.toml").read_text())
    assert __version__ == "0.1.0"
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "xodex.__version__"
    assert Config.__dataclass_fields__["image"].default == example["image"]
    assert example["image"].endswith(":" + __version__)


def test_documentation_links_resolve():
    for document in [ROOT / "README.md", ROOT / "CHANGELOG.md", *(ROOT / "docs").glob("*.md")]:
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text()):
            if "://" in link or link.startswith("#"):
                continue
            target = document.parent / link.split("#", 1)[0]
            assert target.exists(), f"Broken link in {document.relative_to(ROOT)}: {link}"


def test_release_excludes_generated_files_and_credentials(tmp_path):
    release = runpy.run_path(str(ROOT / "scripts/release.py"))
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.egg-info", "build", "dist", ".venv"))
    excluded = ("docs/results.json", "docs/results.txt", "docs/.env", "deploy/tunnel.env",
                "tests/state.sqlite3", "src/xodex/private.key", "scripts/scratch.zip",
                "tests/.tox/fixture.py", "src/xodex/__pycache__/fixture.py", "src/xodex/private.yaml")
    for name in excluded:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not a release input")
    names = {path.relative_to(source).as_posix() for path in release["source_files"](source)}
    assert names.isdisjoint(excluded)
    assert {"src/xodex/assets/worker/Containerfile", "src/xodex/assets/tunnel.env.example", "src/xodex/instructions.md"} <= names


@pytest.mark.parametrize("name", ["README.md", "src/xodex/assets/worker", "docs", "docs/linked.md"])
def test_release_refuses_symlink_inputs(tmp_path, name):
    release = runpy.run_path(str(ROOT / "scripts/release.py"))
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.egg-info", "build", "dist", ".venv"))
    path = source / name
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlinks"):
        release["source_files"](source)


def test_source_release_is_reproducible_complete_and_clean(tmp_path):
    release = runpy.run_path(str(ROOT / "scripts/release.py"))
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    checksum = release["build_archive"](ROOT, first)
    assert release["build_archive"](ROOT, second) == checksum
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        prefix = "chatgpt-xodex/"
        names = archive.namelist()
        assert all(name.startswith(prefix) for name in names)
        assert not any("__pycache__" in name or ".egg-info" in name or "/dist/" in name for name in names)
        manifest = archive.read(prefix + "SHA256SUMS").decode()
        tracked = set()
        for line in manifest.splitlines():
            expected, name = line.split("  ", 1)
            tracked.add(prefix + name)
            assert hashlib.sha256(archive.read(prefix + name)).hexdigest() == expected
        assert tracked == set(names) - {prefix + "SHA256SUMS"}
        for name in ("install-user.sh", "build-worker.sh", "smoke-mcp.py", "release.py"):
            mode = archive.getinfo(prefix + "scripts/" + name).external_attr >> 16
            assert stat.S_ISREG(mode) and mode & 0o111 == 0o111
        assert archive.read(prefix + "src/xodex/instructions.md")
