from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx

from . import __version__
from .admin import administer
from .config import DEFAULT_IMAGE, load_config
from .deployment import build_worker, executable
from .diagnostics import doctor
from .errors import XodexError
from .services import listener, serve  # listener retained as a compatibility import
from .setup import setup
from .smoke import probe


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="xodex", description="Task-to-pull-request coding for ChatGPT over private MCP")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, default=Path.home() / ".xodex/config.toml")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("supervise", help="Own tasks, Git publication and rootless command containers")
    sub.add_parser("serve", help="Serve MCP on a private Unix socket")
    check = sub.add_parser("doctor", help="Validate host configuration and rootless execution")
    check.add_argument("--probe", action="store_true", help="Actually run a bounded isolation probe")
    guided = sub.add_parser("setup", help="Guide owner configuration, checks and optional service startup")
    for name in ("client", "config", "env"):
        guided.add_argument("--tunnel-" + name, type=Path)
    sub.add_parser("build-worker", help="Build the packaged default worker (BASE_IMAGE/RUST_TOOLCHAIN supported)")
    smoke = sub.add_parser("smoke", help="Run the read-only local MCP acceptance probe")
    smoke.add_argument("--socket", type=Path, help="Override the configured MCP socket")
    admin = sub.add_parser("admin", help="Offline owner administration; never exposed over MCP")
    admin_sub = admin.add_subparsers(dest="action", required=True)
    for action in ("delete", "reconcile", "retry-pr"):
        item = admin_sub.add_parser(action)
        item.add_argument("--task", required=True)
        item.add_argument("--confirm", required=True, help="Exact task UUID; explicit owner acknowledgement")
    return root


async def dispatch(args: argparse.Namespace) -> dict | None:
    if args.command == "setup":
        return await setup(args.config, tunnel_client=args.tunnel_client,
                           tunnel_config=args.tunnel_config, tunnel_env=args.tunnel_env)
    if args.command == "build-worker":
        config = load_config(args.config) if args.config.exists() else None
        return await build_worker(podman=config.podman if config else executable("podman"),
                                  image=config.image if config else DEFAULT_IMAGE)
    if args.command == "smoke":
        socket = args.socket
        if socket is None:
            socket = load_config(args.config).mcp_socket if args.config.exists() else args.config.expanduser().absolute().parent / "run/mcp.sock"
        return await probe(socket)
    config = load_config(args.config)
    if args.command in {"serve", "supervise"}:
        await serve(config, args.command)
        return None
    if args.command == "doctor":
        return await doctor(config, args.probe)
    return await administer(config, args.action, args.task, args.confirm)


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parser().parse_args()
    try:
        result = asyncio.run(dispatch(args))
        if result is not None:
            print(json.dumps(result, indent=2))
    except (XodexError, OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        result = error.result() if isinstance(error, XodexError) else {"error": str(error)}
        print(json.dumps(result, indent=2), file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
