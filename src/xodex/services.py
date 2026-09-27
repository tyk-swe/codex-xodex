from __future__ import annotations

import fcntl
import os
import socket
import stat
from pathlib import Path
from typing import Any

import uvicorn

from .client import EngineClient
from .config import Config
from .engine import Engine
from .errors import XodexError
from .gateway import gateway_app
from .supervisor import engine_app


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
