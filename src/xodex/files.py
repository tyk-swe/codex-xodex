from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import stat
import struct
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import XodexError

MAX_FILE = 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def relative(path: str, *, root_ok: bool = False) -> list[str]:
    if path == "." and root_ok:
        return []
    parts = path.split("/")
    if (not path or path.startswith("/") or "\x00" in path or "\\" in path
            or any(part in ("", ".", "..") for part in parts) or len(path.encode()) > 4096):
        raise XodexError("unsafe_path", "Use a non-empty workspace-relative POSIX path without traversal", path=path)
    return parts


class WorkspaceFS:
    """Directory-FD-relative access; never follow a model-controlled symlink."""

    def __init__(self, root: Path):
        self.fd = os.open(root, DIR_FLAGS)

    def __enter__(self) -> WorkspaceFS:
        return self

    def __exit__(self, *exc: Any) -> None:
        os.close(self.fd)

    @contextmanager
    def parent(self, path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = relative(path)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        except OSError as error:
            raise XodexError("filesystem", "Confined filesystem access failed", path=path,
                             errno=error.errno) from error
        finally:
            os.close(fd)

    def read(self, path: str, limit: int = MAX_FILE) -> tuple[bytes, int]:
        with self.parent(path) as (directory, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory)
            with os.fdopen(fd, "rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise XodexError("not_regular", "Only regular files can be read", path=path)
                if info.st_size > limit:
                    raise XodexError("file_too_large", "File exceeds this tool's byte budget; use bounded shell reads",
                                     path=path, bytes=info.st_size, limit=limit)
                data = file.read(limit + 1)
                if len(data) > limit:
                    raise XodexError("file_too_large", "File grew beyond this tool's byte budget", path=path)
                return data, stat.S_IMODE(info.st_mode) & 0o777

    def exists(self, path: str) -> bool:
        try:
            with self.parent(path) as (directory, name):
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            return True
        except XodexError as error:
            if error.details.get("errno") == 2:
                return False
            raise

    def write(self, path: str, data: bytes, mode: int = 0o644) -> None:
        with self.parent(path, create=True) as (directory, name):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise XodexError("not_regular", "Refusing to replace a symlink or special file", path=path)
            except FileNotFoundError:
                pass
            temporary = f".xodex-write-{uuid.uuid4().hex}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         mode & 0o777, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fchmod(file.fileno(), mode & 0o777)
                    os.fsync(file.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def delete(self, path: str) -> None:
        with self.parent(path) as (directory, name):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise XodexError("not_regular", "Refusing to delete a symlink or special file", path=path)
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)

    def directory(self, path: str) -> None:
        parts = relative(path, root_ok=True)
        fd = os.dup(self.fd)
        try:
            for part in parts:
                next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        except OSError as error:
            raise XodexError("unsafe_workdir", "Working directory must be an existing non-symlink directory",
                             path=path) from error
        finally:
            os.close(fd)

    def listing(self, path: str = ".", limit: int = 500, depth: int = 6,
                include_ignored: bool = False) -> dict[str, Any]:
        parts = relative(path, root_ok=True)
        start = os.dup(self.fd)
        try:
            for part in parts:
                next_fd = os.open(part, DIR_FLAGS, dir_fd=start)
                os.close(start)
                start = next_fd
            result: list[dict[str, Any]] = []
            truncated = False
            excluded = {".git", "node_modules", "target", ".venv", "__pycache__"}

            def walk(fd: int, prefix: str, remaining: int) -> None:
                nonlocal truncated
                # scandir is bounded before sorting; huge directories cannot allocate without limit.
                with os.scandir(fd) as entries:
                    names = []
                    for entry in entries:
                        try:
                            entry.name.encode("utf-8")
                        except UnicodeError as error:
                            raise XodexError("unsupported_filename", "File tools require UTF-8 filenames") from error
                        names.append(entry.name)
                        if len(names) > 20000:
                            truncated = True
                            break
                for name in sorted(names):
                    if len(result) >= limit:
                        truncated = True
                        return
                    if not include_ignored and name in excluded:
                        continue
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        kind = ("directory" if stat.S_ISDIR(info.st_mode) else
                                "file" if stat.S_ISREG(info.st_mode) else
                                "symlink" if stat.S_ISLNK(info.st_mode) else "special")
                        rel = f"{prefix}/{name}" if prefix else name
                        result.append({"path": rel, "type": kind, "bytes": info.st_size})
                        if kind == "directory" and remaining == 0:
                            truncated = True
                        if kind == "directory" and remaining > 0:
                            child = os.open(name, DIR_FLAGS, dir_fd=fd)
                            try:
                                walk(child, rel, remaining - 1)
                            finally:
                                os.close(child)
                    except FileNotFoundError:
                        continue
            walk(start, "/".join(parts), depth)
            return {"entries": result, "truncated": truncated}
        except OSError as error:
            raise XodexError("filesystem", "Cannot list this directory", path=path, errno=error.errno) from error
        finally:
            os.close(start)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(fs: WorkspaceFS, path: str, start_line: int = 1, max_lines: int = 250) -> dict[str, Any]:
    data, _ = fs.read(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise XodexError("not_text", "File is not UTF-8; inspect it with a bounded command", path=path) from error
    if "\x00" in text:
        raise XodexError("not_text", "File contains NUL bytes", path=path)
    lines = text.splitlines(keepends=True)
    chosen = lines[start_line - 1:start_line - 1 + max_lines]
    output = "".join(chosen)
    clipped = len(output) > 64000
    return {"path": path, "sha256": sha256(data), "bytes": len(data), "total_lines": len(lines),
            "start_line": start_line, "text": output[:64000],
            "truncated": clipped or start_line - 1 + max_lines < len(lines)}


def search(fs: WorkspaceFS, query: str, glob: str, case_sensitive: bool, limit: int) -> dict[str, Any]:
    listing = fs.listing(limit=3000, depth=12)
    matches: list[dict[str, Any]] = []
    needle = query if case_sensitive else query.casefold()
    scanned = 0
    skipped = 0
    for entry in listing["entries"]:
        if entry["type"] != "file" or not fnmatch.fnmatchcase(entry["path"], glob):
            continue
        try:
            data, _ = fs.read(entry["path"])
            scanned += len(data)
            if scanned > 32 * MAX_FILE:
                return {"matches": matches, "truncated": True, "skipped_files": skipped}
            text = data.decode("utf-8")
            if "\x00" in text:
                skipped += 1
                continue
        except (XodexError, UnicodeDecodeError):
            skipped += 1
            continue
        for number, line in enumerate(text.splitlines(), 1):
            haystack = line if case_sensitive else line.casefold()
            if needle in haystack:
                if len(matches) == limit:
                    return {"matches": matches, "truncated": True, "skipped_files": skipped}
                matches.append({"path": entry["path"], "line": number, "text": line[:1000]})
    return {"matches": matches, "truncated": listing["truncated"], "skipped_files": skipped}


def image(fs: WorkspaceFS, path: str) -> dict[str, Any]:
    data, _ = fs.read(path, 4 * MAX_FILE)
    width = height = 0
    mime = ""
    if len(data) >= 33 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        mime = "image/png"
    elif data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                break
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 7:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                mime = "image/jpeg"
                break
            offset += length
    if not mime or not (0 < width <= 16384 and 0 < height <= 16384) or width * height > 16_000_000:
        raise XodexError("unsupported_image", "Use a PNG or baseline/progressive JPEG under 4 MiB and 16 MP")
    return {"path": path, "mimeType": mime, "width": width, "height": height,
            "data": base64.b64encode(data).decode("ascii")}
