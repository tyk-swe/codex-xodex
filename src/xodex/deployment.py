"""Owner deployment operations; never instantiate an engine or mutate task state."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from .backend import PodmanBackend
from .config import Config, DEFAULT_IMAGE
from .control import run_control
from .errors import XodexError
from .git import GitControl


ASSETS = files("xodex").joinpath("assets")


def asset(name: str) -> str:
    return ASSETS.joinpath(name).read_text(encoding="utf-8")


def private_text(path: Path, limit: int = 65536) -> str:
    """Read credentials without following links, blocking on FIFOs, or echoing data."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise XodexError("credentials", "Credential must be an owner-only regular file (0600)", path=str(path))
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError()
        return data.decode("utf-8")
    except (OSError, ValueError) as error:
        raise XodexError("credentials", "Cannot read a valid private credential file", path=str(path)) from error


def check_generated(path: Path, content: str, mode: int = 0o600) -> bool:
    """True means identical and safe to reuse. Never adopt conflicting content."""
    for parent in path.parents:
        if parent.is_symlink():
            raise XodexError("setup_conflict", "Generated file parent may not be a symlink", path=str(parent))
    if not path.exists() and not path.is_symlink():
        return False
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & ~mode or path.read_bytes() != content.encode("utf-8")):
        raise XodexError("setup_conflict", "Existing file conflicts; review it and move it aside manually before retrying",
                         path=str(path))
    return True


def write_generated(path: Path, content: str, mode: int = 0o600) -> str:
    if check_generated(path, content, mode):
        return "reused"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish a complete file without ever replacing a file another process created.
    fd, temporary = tempfile.mkstemp(prefix=".xodex-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            check_generated(path, content, mode)
            return "reused"
    finally:
        Path(temporary).unlink(missing_ok=True)
    return "created"


def host_env() -> dict[str, str]:
    allowed = ("HOME", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


async def command(argv: list[str], *, timeout: int = 120, visible: bool = False) -> bytes:
    try:
        if visible:
            return await visible_command(argv, timeout)
        return await run_control(argv, host_env(), timeout=timeout)
    except (XodexError, OSError) as error:
        details = error.details if isinstance(error, XodexError) else {"errno": error.errno}
        reason = error.code if isinstance(error, XodexError) else type(error).__name__
        raise XodexError("deployment_command", "Deployment command failed", command=argv, reason=reason, **details) from error


async def visible_command(argv: list[str], timeout: int) -> bytes:
    """Stream long worker builds to the owner, with bounded lifetime and cleanup."""
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, env=host_env(), start_new_session=True, stdin=asyncio.subprocess.DEVNULL))
    process = None
    try:
        process = await asyncio.shield(spawn)
        code = await asyncio.wait_for(process.wait(), timeout)
        if code:
            raise XodexError("worker_build", "Worker build failed; inspect the build output", exit_code=code)
        return b""
    finally:
        if process is None and not spawn.cancelled():
            try:
                process = await spawn
            except OSError:
                pass
        if process is not None:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            await process.wait()


def executable(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise XodexError("host_prerequisite", "Required executable is missing; provision it as the administrator", executable=name)
    return found


async def check_host(config: Config) -> None:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise XodexError("unsafe_host", "Setup requires a dedicated unprivileged Linux service account")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        raise XodexError("host_prerequisite", "Log in as the service user so XDG_RUNTIME_DIR and user systemd are available")
    for name in (config.podman, config.git, "newuidmap", "newgidmap", "systemctl"):
        executable(name)
    if any(repo.network != "none" for repo in config.repositories.values()):
        executable("slirp4netns")
    await command([executable("systemctl"), "--user", "show-environment"])
    await GitControl(config, "").preflight()
    # The full rootless/cgroup/image validation is repeated by doctor --probe.
    code, output = await PodmanBackend(config)._run("info", "--format", "json")
    if code:
        raise XodexError("host_prerequisite", "podman info failed; check rootless storage and subordinate UID/GID mappings")
    try:
        host = json.loads(output)["host"]
        if not host["security"]["rootless"] or host["cgroupVersion"] != "v2":
            raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise XodexError("host_prerequisite", "Rootless Podman with cgroup v2 is required") from error


async def image_available(config: Config) -> bool:
    code, _ = await PodmanBackend(config)._run("image", "exists", config.image)
    if code not in (0, 1):
        raise XodexError("sandbox_unavailable", "Cannot inspect the configured worker image")
    return code == 0


async def build_worker(*, podman: str = "/usr/bin/podman", image: str = DEFAULT_IMAGE) -> dict:
    if os.geteuid() == 0:
        raise XodexError("unsafe_host", "Build in the same rootless Podman account that runs Xodex")
    if image != DEFAULT_IMAGE:
        raise XodexError("custom_image", "Provision the custom worker image using its owner-maintained build recipe", image=image)
    args = [executable(podman), "build"]
    for name in ("BASE_IMAGE", "RUST_TOOLCHAIN"):
        if os.environ.get(name):
            args.extend(["--build-arg", f"{name}={os.environ[name]}"])
    # A private, minimal context works from wheels and never uploads the checkout.
    with tempfile.TemporaryDirectory(prefix="xodex-worker-") as temporary:
        containerfile = Path(temporary) / "Containerfile"
        containerfile.write_text(asset("worker/Containerfile"), encoding="utf-8")
        await command([*args, "-t", image, "-f", str(containerfile), temporary], timeout=1800, visible=True)
    output = await command([podman, "image", "inspect", "--format", "{{.Id}}", image])
    image_id = output.decode().strip()
    if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", image_id):
        raise XodexError("worker_build", "Build completed but the worker image ID could not be verified")
    return {"image": image, "image_id": image_id}


def systemd_quote(value: str, *, argument: bool = False) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise XodexError("configuration", "Service paths must not contain control characters")
    value = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    # Only ExecStart arguments expand environment variables, not the executable.
    if argument:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def outside_workspaces(config: Config, paths: list[Path]) -> None:
    workspace = config.workspace_dir.resolve()
    for path in paths:
        resolved = path.resolve()
        if resolved == workspace or workspace in resolved.parents:
            raise XodexError("configuration", "Setup control files must stay outside the workspace tree", path=str(path))


def environment_file_path(path: Path) -> str:
    # EnvironmentFile consumes a raw path/glob, unlike ExecStart and Environment.
    value = str(path)
    if (not path.is_absolute() or value != value.rstrip()
            or any(ord(c) < 32 or ord(c) == 127 or c in "*?[]\\" for c in value)):
        raise XodexError("configuration", "Tunnel environment path must be absolute without glob characters, backslashes, controls or trailing whitespace")
    return value.replace("%", "%%")


def service_files(config: Config, path: Path, tunnel_client: Path, tunnel_config: Path,
                  tunnel_env: Path) -> dict[Path, str]:
    directory = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
    if not directory.is_absolute():
        raise XodexError("configuration", "XDG_CONFIG_HOME must be absolute")
    replacements = {
        "PYTHON": systemd_quote(sys.executable),
        "CONFIG": systemd_quote(str(path), argument=True),
        "TUNNEL_CLIENT": systemd_quote(str(tunnel_client)),
        "TUNNEL_CONFIG": systemd_quote(str(tunnel_config), argument=True),
        "TUNNEL_ENV": environment_file_path(tunnel_env),
        "MCP_ENV": systemd_quote("MCP_UNIX_SOCKET_PATH=" + str(config.mcp_socket)),
    }
    result = {}
    for service in ("engine", "mcp", "tunnel"):
        name = f"xodex-{service}.service"
        content = asset("systemd/" + name)
        for key, value in replacements.items():
            content = content.replace("@" + key + "@", value)
        result[directory / name] = content
    return result
