from __future__ import annotations

import base64
import errno
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .control import run_control
from .errors import XodexError
from .files import WorkspaceFS, relative


class GitControl:
    """Git credentials and publication objects never enter the coding container.

    After setup, host Git uses only a protected bare repository/index, NEVER the
    worker's .git. Snapshot reads are fd-relative, bounded, and do not follow links.
    """

    def __init__(self, config: Config, token: str):
        self.config, self.token = config, token

    async def preflight(self) -> str:
        version = await run_control([self.config.git, "--attr-source=HEAD", "--version"],
                                    {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        return version.decode().strip()

    def directory(self, task: dict[str, Any]) -> Path:
        return self.config.state_dir / "tasks" / task["id"] / f"attempt-{task['generation']}"

    def env(self, task: dict[str, Any], authenticated: bool = False) -> dict[str, str]:
        control = self.directory(task)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(control), "LANG": "C.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
               "GIT_TERMINAL_PROMPT": "0", "GIT_INDEX_FILE": str(control / "index"),
               "GIT_AUTHOR_NAME": self.config.commit_name, "GIT_AUTHOR_EMAIL": self.config.commit_email,
               "GIT_COMMITTER_NAME": self.config.commit_name, "GIT_COMMITTER_EMAIL": self.config.commit_email}
        if authenticated:
            auth = base64.b64encode(("x-access-token:" + self.token).encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
                       GIT_CONFIG_VALUE_0="Authorization: Basic " + auth)
        return env

    async def run(self, task: dict[str, Any], *args: str, data: bytes = b"", authenticated: bool = False,
                  bare: bool = True) -> bytes:
        prefix = [self.config.git, "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
                  "-c", "protocol.ext.allow=never", "-c", "http.followRedirects=false",
                  "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false"]
        if bare:
            prefix += ["--git-dir=" + str(self.directory(task) / "source.git")]
        return await run_control([*prefix, *args], self.env(task, authenticated), data=data)

    async def prepare(self, task: dict[str, Any], root: Path, home: Path) -> dict[str, str]:
        control = self.directory(task)
        control.mkdir(parents=True, mode=0o700, exist_ok=False)
        root.parent.mkdir(parents=True, mode=0o700, exist_ok=False)
        home.mkdir(mode=0o700)
        source = control / "source.git"
        await self.run(task, "init", "--bare", "--object-format=sha1", "--template=", str(source), bare=False)
        await self.run(task, "fetch", "--no-tags", "--depth=1", "--", self.remote(task),
                       "+refs/heads/" + task["base_ref"] + ":refs/heads/xodex-base", authenticated=True)
        base = (await self.run(task, "rev-parse", "refs/heads/xodex-base")).decode().strip()
        tree = (await self.run(task, "rev-parse", base + "^{tree}")).decode().strip()
        listing = await self.run(task, "ls-tree", "-r", "-z", base)
        if any(entry.startswith(b"160000 ") for entry in listing.split(b"\0")):
            raise XodexError("unsupported_repository", "Git submodules require a separate authorization model; this release does not publish submodule repositories")
        # Initial checkout is the ONLY host Git access to worker .git. No worker exists yet.
        # Config, templates, hooks and filters are controlled here, not taken from the cloned repository.
        env = self.env(task)
        env.pop("GIT_INDEX_FILE")
        args = [self.config.git, "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=always",
                "clone", "--template=", "--no-local", "--no-hardlinks", "--no-checkout", "--branch", "xodex-base",
                "--", source.as_uri(), str(root)]
        await run_control(args, env)
        # Checkout raw Git-object bytes: repository EOL/encoding/smudge attributes
        # must not silently manufacture a diff before any task edit exists.
        empty_tree = (await run_control([self.config.git, "-C", str(root), "hash-object", "-t", "tree", "-w", "--stdin"], env)).decode().strip()
        env["GIT_ATTR_SOURCE"] = empty_tree
        for tail in (("remote", "set-url", "origin", self.remote(task)),
                     ("config", "core.hooksPath", "/dev/null"),
                     ("config", "user.name", self.config.commit_name),
                     ("config", "user.email", self.config.commit_email),
                     ("checkout", "-b", task["branch"], base)):
            await run_control([self.config.git, "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false",
                               "-C", str(root), *tail], env)
        return {"base_sha": base, "base_tree": tree}

    def remote(self, task: dict[str, Any]) -> str:
        return "https://github.com/" + task["repository"] + ".git"

    def _read(self, fs: WorkspaceFS, path: str) -> tuple[bytes, str] | None:
        try:
            with fs.parent(path) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return os.fsencode(os.readlink(name, dir_fd=parent)), "120000"
                if stat.S_ISDIR(info.st_mode):
                    return None
                if not stat.S_ISREG(info.st_mode):
                    raise XodexError("unsupported_file", "Publication refuses special files", path=path)
                content, mode = fs.read(path, self.config.max_file_bytes)
                after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise XodexError("tree_changed", "File changed during snapshot", path=path)
                return content, "100755" if mode & 0o111 else "100644"
        except (FileNotFoundError, NotADirectoryError):
            return None
        except XodexError as error:
            if error.details.get("errno") in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                return None
            raise

    async def snapshot(self, task: dict[str, Any], root: Path) -> dict[str, Any]:
        await self.run(task, "read-tree", task["base_sha"])
        listing = await self.run(task, "--work-tree=" + str(root), "ls-files", "--cached", "--others",
                                 "--exclude-standard", "--deduplicate", "-z")
        try:
            paths = sorted(set(p.decode("utf-8") for p in listing.split(b"\0") if p))
        except UnicodeError as error:
            raise XodexError("unsupported_filename", "Publication requires UTF-8 filenames") from error
        if len(paths) > self.config.max_files:
            raise XodexError("tree_budget", "Repository exceeds the configured publication file budget")
        blobs = self.directory(task) / "blobs"
        blobs.mkdir(mode=0o700, exist_ok=True)
        files: list[tuple[str, str, Path]] = []
        size = 0
        with WorkspaceFS(root) as fs:
            for path in paths:
                if path.endswith("/"):
                    raise XodexError("unsupported_repository", "Nested Git repositories are not published", path=path)
                parts = relative(path)
                if any(p.lower() == ".git" for p in parts):
                    raise XodexError("unsafe_path", "Git administration paths cannot be published")
                item = self._read(fs, path)
                if item is None:
                    continue
                content, mode = item
                size += len(content)
                if size > self.config.max_tree_bytes:
                    raise XodexError("tree_budget", "Repository exceeds the configured publication byte budget")
                blob = blobs / hashlib.sha256(content).hexdigest()
                # Atomic replacement repairs an interrupted spool write as well as
                # preventing future readers from observing a partially written blob.
                if not blob.is_file() or blob.read_bytes() != content:
                    fd, temporary = tempfile.mkstemp(prefix=".blob-", dir=blobs)
                    try:
                        with os.fdopen(fd, "wb") as out:
                            out.write(content)
                            out.flush()
                            os.fsync(out.fileno())
                        os.replace(temporary, blob)
                        directory = os.open(blobs, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                files.append((path, mode, blob))
        # Only protected, hash-named spool files are handed to host Git; no filters run.
        hashes = (await self.run(task, "hash-object", "-w", "--no-filters", "--stdin-paths",
                  data=b"".join(os.fsencode(blob) + b"\n" for _, _, blob in files))).splitlines()
        if len(hashes) != len(files) or any(not re.fullmatch(b"[a-f0-9]{40}", value) for value in hashes):
            raise XodexError("git_response", "Unexpected snapshot object response")
        await self.run(task, "read-tree", "--empty")
        index = b"".join(mode.encode() + b" " + oid + b"\t" + path.encode() + b"\0"
                         for (path, mode, _), oid in zip(files, hashes, strict=True))
        await self.run(task, "update-index", "-z", "--index-info", data=index)
        tree = (await self.run(task, "write-tree")).decode().strip()
        stat_text = (await self.run(task, "diff", "--no-ext-diff", "--no-textconv", "--stat",
                                     task["base_tree"], tree)).decode("utf-8", errors="replace")
        return {"tree": tree, "files": len(files), "bytes": size, "diff_stat": stat_text[:16000]}

    async def commit(self, task: dict[str, Any], title: str, summary: str) -> str:
        message = f"{title}\n\n{summary}\n\nXodex-Task: {task['id']}\n".encode()
        sha = (await self.run(task, "commit-tree", task["final_tree"], "-p", task["base_sha"], data=message)).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise XodexError("git_response", "Invalid commit response")
        await self.run(task, "update-ref", "refs/xodex/result", sha)
        return sha

    async def push(self, task: dict[str, Any]) -> None:
        # Fixed destination from attached repository; never use the worker's origin/config/refspec.
        await self.run(task, "push", "--porcelain", "--force-with-lease=refs/heads/" + task["branch"] + ":",
                       "--", self.remote(task),
                       task["commit_sha"] + ":refs/heads/" + task["branch"], authenticated=True)
