#!/usr/bin/env python3
"""Build a deterministic source ZIP and its per-file SHA-256 manifest."""
from __future__ import annotations

import argparse
import hashlib
import runpy
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (".editorconfig", ".gitignore", "CHANGELOG.md", "LICENSE", "MANIFEST.in", "README.md", "pyproject.toml", "worker/Containerfile")
DIRECTORIES = {
    ".github": {".yml", ".yaml"},
    "deploy": {".toml", ".yaml", ".example", ".service"},
    "docs": {".md"},
    "scripts": {".sh", ".py"},
    "src": {".py", ".md"},
    "tests": {".py"},
}
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", ".tox", "build", "dist", ".git"}


def source_files(root: Path) -> list[Path]:
    for name in (*FILES, *DIRECTORIES):
        path = root / name
        if path.is_symlink() or any((root / parent).is_symlink() for parent in Path(name).parents):
            raise ValueError(f"Release inputs must not be symlinks: {name}")
    paths = [root / name for name in FILES]
    for directory, suffixes in DIRECTORIES.items():
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if any(part in EXCLUDED or part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Release inputs must not be symlinks: {relative}")
            if path.is_file() and path.suffix in suffixes:
                paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def build_archive(root: Path, destination: Path) -> str:
    contents = {path.relative_to(root).as_posix(): path.read_bytes() for path in source_files(root)}
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(contents.items())
    )
    contents["SHA256SUMS"] = manifest.encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(contents.items()):
                entry = zipfile.ZipInfo("chatgpt-xodex/" + name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                mode = 0o755 if name.startswith("scripts/") and data.startswith(b"#!") else 0o644
                entry.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def main() -> None:
    version = runpy.run_path(str(ROOT / "src/xodex/__init__.py"))["__version__"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"chatgpt-xodex-v{version}.zip")
    args = parser.parse_args()
    try:
        checksum = build_archive(ROOT, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Release failed: {error}\n")
    print(f"{checksum}  {args.output}")


if __name__ == "__main__":
    main()
