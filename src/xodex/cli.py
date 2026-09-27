from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import uvicorn

from . import __version__
from .admin import administer
from .backend import PodmanBackend
from .config import Config, load_config
from .engine import Engine
from .errors import XodexError
from .git import GitControl
from .server import EngineClient, engine_app, gateway_app


def listener(path: Path) -> tuple[socket.socket, Any]:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink():
        raise XodexError("unsafe_socket", "Socket parent may not be a symlink")
    os.chmod(path.parent, 0o700)
    lock = path.with_suffix(path.suffix + ".lock").open("a+b")
    sock = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise XodexError("already_running", "Another process owns this listener", path=str(path)) from error
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise XodexError("unsafe_socket", "Refusing to replace a non-socket path")
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.settimeout(0.2)
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    path.unlink()
                else:
                    raise XodexError("already_running", "Socket is live")
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.listen(128)
        sock.setblocking(False)
        return sock, lock
    except BaseException:
        if sock is not None:
            sock.close()
        lock.close()
        raise


async def serve(config: Config, mode: str) -> None:
    if os.geteuid() == 0:
        raise XodexError("unsafe_host", "Run the services as a dedicated non-root user")
    path = config.engine_socket if mode == "supervise" else config.mcp_socket
    sock, lock = listener(path)
    try:
        if mode == "supervise":
            engine = Engine(config)
            app = engine_app(engine)
        else:
            app = gateway_app(EngineClient(config))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="info", lifespan="on", ws="none",
                                               limit_concurrency=64, timeout_keep_alive=10,
                                               timeout_graceful_shutdown=45))
        await server.serve(sockets=[sock])
    finally:
        sock.close()
        path.unlink(missing_ok=True)
        lock.close()


async def doctor(config: Config, probe: bool) -> dict[str, Any]:
    backend = PodmanBackend(config)
    runtime = await backend.preflight()
    runtime["git"] = await GitControl(config, "").preflight()
    result: dict[str, Any] = {"configuration": "valid", "runtime": runtime,
                              "chatgpt_tunnel": "not tested; requires your tunnel ID/runtime key and account",
                              "native_mobile": "not promised; verify platform support"}
    if not probe:
        return result
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # These are disposable diagnostic fixtures, not retained user tasks.
    with tempfile.TemporaryDirectory(prefix="diagnostic-", dir=config.state_dir) as temporary:
        root = Path(temporary) / "repo"
        home = Path(temporary) / "home"
        root.mkdir()
        home.mkdir()
        name = "xodex-probe-" + uuid.uuid4().hex
        script = """set -eu
id
[ "$(id -u)" != 0 ]
[ "$(awk '/^CapEff:/ {print $2}' /proc/self/status)" = 0000000000000000 ]
[ "$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)" = 1 ]
! touch /etc/xodex-should-not-write 2>/dev/null
[ -f /sys/fs/cgroup/memory.max ]
[ "$(cat /sys/fs/cgroup/memory.max)" != max ]
[ "$(cat /sys/fs/cgroup/pids.max)" != max ]
[ -z "${CONTROL_PLANE_API_KEY:-}" ]
[ -z "${OPENAI_API_KEY:-}" ]
[ -z "${SSH_AUTH_SOCK:-}" ]
printf probe-ok > /workspace/probe.txt
printf '\nXODEX_SANDBOX_PROBE_OK\n'
"""
        launch = backend.launch(name, root, home, script, ".", False, "none")
        process = None
        try:
            process = await asyncio.create_subprocess_exec(*launch.argv, env=launch.env,
                                                          stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await asyncio.wait_for(process.communicate(), 60)
            if process.returncode or not (root / "probe.txt").exists() or b"XODEX_SANDBOX_PROBE_OK" not in output:
                raise XodexError("sandbox_probe_failed", "Behavioral sandbox probe failed", output=output.decode(errors="replace"))
            result["sandbox_probe"] = "passed"
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await backend.cleanup(name)
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="xodex", description="Task-to-pull-request coding for ChatGPT over private MCP")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, default=Path.home() / ".xodex/config.toml")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("supervise", help="Own tasks, Git publication and rootless command containers")
    sub.add_parser("serve", help="Serve MCP on a private Unix socket")
    check = sub.add_parser("doctor", help="Validate host configuration and rootless execution")
    check.add_argument("--probe", action="store_true", help="Actually run a bounded isolation probe")
    admin = sub.add_parser("admin", help="Offline owner administration; never exposed over MCP")
    admin_sub = admin.add_subparsers(dest="action", required=True)
    for action in ("delete", "reconcile", "retry-pr"):
        item = admin_sub.add_parser(action)
        item.add_argument("--task", required=True)
        item.add_argument("--confirm", required=True, help="Exact task UUID; explicit owner acknowledgement")
    return root


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parser().parse_args()
    try:
        config = load_config(args.config)
        if args.command in {"serve", "supervise"}:
            asyncio.run(serve(config, args.command))
        elif args.command == "doctor":
            print(json.dumps(asyncio.run(doctor(config, args.probe)), indent=2))
        else:
            print(json.dumps(asyncio.run(administer(config, args.action, args.task, args.confirm)), indent=2))
    except (XodexError, OSError, ValueError) as error:
        result = error.result() if isinstance(error, XodexError) else {"error": str(error)}
        print(json.dumps(result, indent=2), file=sys.stderr)
        raise SystemExit(1) from error
