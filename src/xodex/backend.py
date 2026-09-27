from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import XodexError


@dataclass(frozen=True)
class Launch:
    argv: list[str]
    env: dict[str, str]
    cwd: str | None = None


class PodmanBackend:
    """The only production execution backend. Never falls back to a host shell."""

    def __init__(self, config: Config):
        self.config = config
        self.image_id = ""
        # Podman needs the service user's rootless runtime, not the tunnel's credentials.
        allowed = ("HOME", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "LANG")
        self.host_env = {key: os.environ[key] for key in allowed if key in os.environ}
        self.host_env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")

    async def _run(self, *args: str, timeout: float = 20) -> tuple[int, bytes]:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            self.config.podman, *args, env=self.host_env, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT))
        process = None
        try:
            process = await asyncio.shield(spawn)
            output = bytearray()
            async with asyncio.timeout(timeout):
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > 1024 * 1024:
                        raise XodexError("sandbox_unavailable", "Podman response exceeded its output budget")
                await process.wait()
            return process.returncode, bytes(output)
        except OSError as error:
            raise XodexError("sandbox_unavailable", "Cannot start Podman", errno=error.errno) from error
        except TimeoutError as error:
            raise XodexError("sandbox_unavailable", "Podman control operation timed out") from error
        finally:
            # Cancellation during spawn must not orphan a host control process.
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
                await process.communicate()

    async def preflight(self) -> dict[str, Any]:
        if os.geteuid() == 0:
            raise XodexError("unsafe_host", "Run Xodex as a dedicated unprivileged Linux user, never root")
        code, output = await self._run("info", "--format", "json")
        if code:
            raise XodexError("sandbox_unavailable", "podman info failed", output=output.decode(errors="replace")[-2000:])
        try:
            info = json.loads(output)
            host = info["host"]
            if not host["security"]["rootless"] or host["cgroupVersion"] != "v2":
                raise ValueError("rootless Podman with cgroup v2 is required")
        except (KeyError, ValueError, TypeError) as error:
            raise XodexError("unsafe_host", "Rootless Podman and cgroup v2 are mandatory") from error
        code, output = await self._run("image", "inspect", "--format", "{{.Id}}", self.config.image)
        image_id = output.decode().strip()
        if code or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", image_id):
            raise XodexError("sandbox_unavailable", "Build the configured worker image before starting Xodex")
        self.image_id = image_id
        return {"backend": "rootless-podman", "image_id": image_id,
                "cgroup_version": "v2", "network_default": "none"}

    def launch(self, name: str, root: Path, home: Path, command: str, cwd: str,
               tty: bool, network: str) -> Launch:
        if not self.image_id:
            raise XodexError("sandbox_unavailable", "Backend was not verified")
        # No runtime socket, state tree, service home, keys, devices, or sibling workspaces are mounted.
        args = [self.config.podman, "run", "--name", name, "--label", "io.xodex.managed=1",
                "--pull=never", "--init", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--userns=keep-id",
                f"--user={os.getuid()}:{os.getgid()}", "--network", network,
                "--pids-limit", str(self.config.pids_limit), "--memory", self.config.memory,
                "--memory-swap", self.config.memory, "--cpus", str(self.config.cpus),
                "--ipc=private", "--pid=private", "--uts=private",
                "--http-proxy=false", "--log-driver=none",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
                "--mount", f"type=bind,src={root},dst=/workspace,rw",
                "--mount", f"type=bind,src={home},dst=/home/worker,rw",
                "--workdir", "/workspace" + ("/" + cwd if cwd != "." else ""),
                "--env", "HOME=/home/worker", "--env", "CARGO_HOME=/home/worker/.cargo",
                "--env", "RUSTUP_HOME=/usr/local/rustup", "--env", "GIT_CONFIG_NOSYSTEM=1",
                "--env", "GIT_TERMINAL_PROMPT=0", "--env", "LANG=C.UTF-8",
                "--env", "TERM=xterm-256color", "--entrypoint", "/bin/bash", "--interactive"]
        if tty:
            args.append("--tty")
        args.extend([self.image_id, "--noprofile", "--norc", "-c", command])
        return Launch(args, dict(self.host_env))

    async def cleanup(self, name: str) -> None:
        # Only ephemeral container metadata is removed. Bind-mounted workspace data is NEVER removed.
        code, output = await self._run("container", "exists", name)
        if code == 1:
            return
        if code != 0:
            raise XodexError("reconciliation_required", "Cannot determine whether a command container remains",
                             container=name, output=output.decode(errors="replace")[-1000:])
        code, output = await self._run("rm", "--force", "--time", "1", name)
        if code:
            raise XodexError("reconciliation_required", "Cannot remove the command boundary", container=name,
                             output=output.decode(errors="replace")[-1000:])
        code, _ = await self._run("container", "exists", name)
        if code != 1:
            raise XodexError("reconciliation_required", "Command boundary removal could not be verified", container=name)
