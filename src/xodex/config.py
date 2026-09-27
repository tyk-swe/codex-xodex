from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .errors import XodexError


PATH_FIELDS = ("state_dir", "workspace_dir", "engine_socket", "mcp_socket", "github_token_file")
DEFAULT_PATHS = dict(zip(PATH_FIELDS, ("state", "tasks", "run/engine.sock", "run/mcp.sock", "secrets/github-token")))
DEFAULT_IMAGE = f"localhost/chatgpt-xodex-worker:{__version__}"


def repository_name(value: str) -> str:
    """Accept an exact GitHub repository, never an arbitrary clone/credential URL."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise XodexError("invalid_repository", "Use a GitHub owner/repo name without whitespace")
    if value.startswith("https://"):
        try:
            url = urlsplit(value)
        except ValueError as error:
            raise XodexError("invalid_repository", "Use a valid GitHub repository URL") from error
        if (url.netloc != "github.com" or url.query or url.fragment or url.username
                or url.password or url.path.endswith("/")):
            raise XodexError("invalid_repository", "Use owner/repo or its exact https://github.com/owner/repo URL")
        value = url.path[1:]
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise XodexError("invalid_repository", "Use a GitHub owner/repo name")
    if value.split("/")[1] in {".", ".."}:
        raise XodexError("invalid_repository", "Invalid repository name")
    return value.lower()


def validate_ref(ref: str) -> None:
    if (not isinstance(ref, str) or not ref or len(ref) > 200 or ref == "@"
            or ref.startswith(("-", "/"))
            or any(c in ref for c in ("..", "@{", "//", "\\", " ", "~", "^", ":", "?", "*", "["))
            or any(ord(c) < 33 or ord(c) == 127 for c in ref)
            or any(p.startswith(".") or p.endswith((".", ".lock")) or not p for p in ref.split("/"))):
        raise XodexError("invalid_ref", "Invalid Git branch name")


@dataclass(frozen=True)
class Repository:
    checks: tuple[str, ...] = ()
    network: str = "none"
    branch_prefix: str = "xodex/"


@dataclass(frozen=True)
class Config:
    state_dir: Path
    workspace_dir: Path
    engine_socket: Path
    mcp_socket: Path
    github_token_file: Path | None = None
    repositories: dict[str, Repository] = field(default_factory=dict)
    image: str = DEFAULT_IMAGE
    podman: str = "/usr/bin/podman"
    git: str = "/usr/bin/git"
    max_jobs: int = 4
    max_tasks: int = 8
    timeout_seconds: int = 1800
    max_timeout_seconds: int = 86400
    output_limit_bytes: int = 64 * 1024 * 1024
    min_free_bytes: int = 512 * 1024 * 1024
    memory: str = "2g"
    cpus: int = 2
    pids_limit: int = 256
    max_files: int = 20000
    max_file_bytes: int = 10 * 1024 * 1024
    max_tree_bytes: int = 128 * 1024 * 1024
    commit_name: str = "Xodex"
    commit_email: str = "xodex@users.noreply.github.com"

    def validate(self) -> None:
        self._validate_paths()
        self._validate_execution()
        self._validate_repositories()
        for value in (self.commit_name, self.commit_email):
            if (not isinstance(value, str) or not value.strip() or len(value) > 200
                    or any(c in value for c in "\n\r<>\x00")):
                raise XodexError("configuration", "Invalid commit identity")

    def _validate_paths(self) -> None:
        resolved = {}
        for name in PATH_FIELDS:
            path = getattr(self, name)
            if name == "github_token_file" and path is None:
                continue
            if (not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
                    or re.search(r"\$(?:\w|\{)", str(path))
                    or any(c in str(path) for c in ",\n\r\x00")):
                raise XodexError("configuration", "Paths must be absolute and fully expanded", field=name)
            try:
                resolved[name] = path.resolve()
            except (OSError, RuntimeError) as error:
                raise XodexError("configuration", "Cannot resolve configured path", field=name) from error
        workspace = resolved["workspace_dir"]
        state = resolved["state_dir"]
        if state == workspace or state in workspace.parents or workspace in state.parents:
            raise XodexError("configuration", "State and workspaces must be disjoint trees")
        for name in ("engine_socket", "mcp_socket"):
            if len(os.fsencode(getattr(self, name))) > 100:
                raise XodexError("configuration", "Unix socket path exceeds 100 bytes", field=name)
            if workspace == resolved[name] or workspace in resolved[name].parents:
                raise XodexError("configuration", "Control sockets must not be inside workspaces")
        if resolved["engine_socket"] == resolved["mcp_socket"]:
            raise XodexError("configuration", "Engine and MCP sockets must differ")
        token = resolved.get("github_token_file")
        if token is not None and (workspace == token or workspace in token.parents):
            raise XodexError("configuration", "GitHub credential must not be in the workspace tree")

    def _validate_execution(self) -> None:
        for executable in (self.podman, self.git):
            if (not isinstance(executable, str) or not executable.startswith("/")
                    or any(c in executable for c in "\n\r\x00")):
                raise XodexError("configuration", "Absolute control executable paths are required")
        if (not isinstance(self.image, str) or not self.image or self.image.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in self.image)):
            raise XodexError("configuration", "A valid worker image reference is required")
        limits = (self.max_jobs, self.max_tasks, self.timeout_seconds, self.max_timeout_seconds,
                  self.output_limit_bytes, self.cpus, self.pids_limit, self.max_files,
                  self.max_file_bytes, self.max_tree_bytes)
        if (any(type(value) is not int or value < 1 for value in limits)
                or type(self.min_free_bytes) is not int or self.min_free_bytes < 0):
            raise XodexError("configuration", "Resource limits must be positive integers")
        if self.timeout_seconds > self.max_timeout_seconds or self.max_jobs > 64 or self.max_tasks > 64:
            raise XodexError("configuration", "Invalid timeout or concurrency limits")
        if not isinstance(self.memory, str) or not re.fullmatch(r"[1-9][0-9]*[kmg]", self.memory):
            raise XodexError("configuration", "memory must be a Podman size, e.g. 2g")

    def _validate_repositories(self) -> None:
        if not isinstance(self.repositories, dict) or not self.repositories:
            raise XodexError("configuration", "Allow at least one GitHub owner/repo on the host")
        for name, repo in self.repositories.items():
            try:
                if repository_name(name) != name:
                    raise XodexError("configuration", "Repository keys must be lowercase owner/repo names")
                if not isinstance(repo, Repository):
                    raise XodexError("configuration", "Each repository must have a repository policy")
                if not isinstance(repo.network, str) or repo.network not in {"none", "slirp4netns:allow_host_loopback=false"}:
                    raise XodexError("configuration", "Unsupported network policy")
                if not isinstance(repo.branch_prefix, str):
                    raise XodexError("configuration", "Branch prefixes must be strings")
                validate_ref(repo.branch_prefix + "0" * 36)
                if (not isinstance(repo.checks, (list, tuple)) or len(repo.checks) > 20
                        or any(not isinstance(c, str) or not c.strip() or len(c) > 65536 or "\x00" in c
                               for c in repo.checks)):
                    raise XodexError("configuration", "Repository checks must be nonempty bounded commands without NUL")
            except XodexError as error:
                if error.code == "configuration":
                    raise
                raise XodexError("configuration", str(error)) from error


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except ValueError as error:
        raise XodexError("configuration", "Invalid configuration value or TOML syntax") from error
    return parse_config(raw, path)


def parse_config(raw: dict, path: Path) -> Config:
    """Validate configuration before writing it; omitted paths follow the config file."""
    raw = dict(raw)
    base = Path(os.path.abspath(path.expanduser())).parent
    try:
        unknown = set(raw) - set(Config.__dataclass_fields__)
        if unknown:
            raise XodexError("configuration", "Unknown configuration fields", fields=sorted(unknown))
        for key in PATH_FIELDS:
            if key not in raw:
                raw[key] = str(base / DEFAULT_PATHS[key])
            if not isinstance(raw[key], str):
                raise XodexError("configuration", "Configured paths must be strings", field=key)
            raw[key] = Path(os.path.expandvars(os.path.expanduser(raw[key])))
        raw["repositories"] = {name: Repository(**repo) for name, repo in raw.get("repositories", {}).items()}
        config = Config(**raw)
        config.validate()
    except (TypeError, ValueError, AttributeError) as error:
        raise XodexError("configuration", "Invalid configuration value or TOML syntax") from error
    return config
