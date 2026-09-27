"""Interactive, resumable owner setup. No engine, task, Git branch, or PR mutations."""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import shlex
import shutil
import sys
import time
import warnings
from pathlib import Path

import httpx

from .config import Config, DEFAULT_IMAGE, load_config, parse_config, repository_name
from .deployment import (asset, build_worker, check_generated, check_host, command, executable,
                         image_available, outside_workspaces, private_text, service_files, write_generated)
from .diagnostics import doctor
from .errors import XodexError
from .github import GitHub, load_token
from .smoke import probe


class Console:
    def interactive(self) -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def say(self, text: str) -> None:
        print(text, flush=True)

    def ask(self, label: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        return input(label + suffix + ": ").strip() or default

    def confirm(self, label: str, default: bool = False) -> bool:
        while True:
            answer = self.ask(label + (" [Y/n]" if default else " [y/N]"))
            if not answer:
                return default
            if answer.lower() in {"y", "yes", "n", "no"}:
                return answer.lower() in {"y", "yes"}
            self.say("Enter yes or no.")

    def secret(self, label: str) -> str:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return getpass.getpass(label + ": ")
        except getpass.GetPassWarning as error:
            raise XodexError("credentials", "Cannot read masked input; provision an existing private credential file") from error


def absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def configure(path: Path, ui: Console) -> tuple[Config, str | None]:
    if path.is_symlink():
        raise XodexError("setup_conflict", "Configuration must not be a symlink", path=str(path))
    if path.exists():
        ui.say(f"Reusing configuration: {path}")
        return load_config(path), None
    ui.say("Allow exact GitHub owner/repo names. Workers default to offline and xodex/ branches.")
    names = [repository_name(name.strip()) for name in ui.ask("Allowed repositories (comma-separated)").split(",")]
    repositories = {}
    for name in dict.fromkeys(names):
        checks = []
        ui.say(f"Required checks for {name}; enter one command at a time, then a blank line.")
        while check := ui.ask("Check command (blank to finish)"):
            checks.append(check)
            if len(checks) > 20:
                raise XodexError("configuration", "At most 20 owner check commands are allowed")
        network = "slirp4netns:allow_host_loopback=false" if ui.confirm(f"Allow outbound worker networking for {name}?") else "none"
        repositories[name] = {"checks": checks, "network": network, "branch_prefix": "xodex/"}
    token_path = absolute(Path(ui.ask("GitHub credential file (existing or to create)", str(path.parent / "secrets/github-token"))))
    raw = {"github_token_file": str(token_path), "repositories": repositories}
    config = parse_config(raw, path)
    lines = ["# Created by xodex setup. Omitted runtime paths follow this file's directory.",
             "github_token_file = " + json.dumps(str(token_path), ensure_ascii=False), ""]
    for name, policy in repositories.items():
        lines.extend([f'[repositories.{json.dumps(name)}]',
                      "checks = " + json.dumps(policy["checks"], ensure_ascii=False),
                      "network = " + json.dumps(policy["network"]), 'branch_prefix = "xodex/"', ""])
    return config, "\n".join(lines)


def provision_github(config: Config, ui: Console) -> str:
    path = config.github_token_file
    if path is None:
        raise XodexError("github_credentials", "Set github_token_file in the configuration")
    if not path.exists() and not path.is_symlink():
        secret = ui.secret("GitHub token (masked; stored only in the private credential file)")
        if not re.fullmatch(r"[!-~]{1,4095}", secret):
            raise XodexError("github_credentials", "A nonempty ASCII GitHub token without whitespace is required")
        write_generated(path, secret + "\n")
    return load_token(path)


async def check_repositories(config: Config, token: str) -> None:
    github = GitHub(token)
    try:
        for name in config.repositories:
            await github.repository(name)
    finally:
        await github.close()


def tunnel_key(path: Path) -> str:
    text = private_text(path)
    values = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r'CONTROL_PLANE_API_KEY=(?:"([^"\\]*)"|\'([^\']*)\'|([^\s"\'\\]+))', line)
        if not match:
            raise XodexError("tunnel_credentials", "Expected only CONTROL_PLANE_API_KEY in the private tunnel environment file")
        values.append(next(value for value in match.groups() if value is not None))
    if len(values) != 1 or not re.fullmatch(r"[!-~]{1,4096}", values[0]) or "REPLACE_" in values[0]:
        raise XodexError("tunnel_credentials", "A tunnel runtime key is missing or invalid")
    return values[0]


def tunnel_profile(path: Path) -> None:
    # Inspect the shipped v1 profile contract without adding a YAML dependency.
    # Full configuration and account authorization remain the client's responsibility.
    text = private_text(path)
    required = (r"(?m)^config_version: *1 *$", r"(?m)^  tunnel_id: *[\"']?tunnel_[a-f0-9]{32}[\"']? *$",
                r"(?m)^  api_key: *env:CONTROL_PLANE_API_KEY *$",
                r"(?m)^      unix_socket: *env:MCP_UNIX_SOCKET_PATH *$")
    if not all(re.search(pattern, text) for pattern in required):
        raise XodexError("tunnel_profile", "Profile must use the bundled v1 tunnel ID and environment-key/socket format; review the existing file")


def configure_tunnel(config: Config, client: Path, profile: Path, env: Path, ui: Console) -> dict:
    issues = []
    outside_workspaces(config, [client, profile, env])
    if not ui.confirm("Configure/check the separately installed tunnel client now?", profile.exists() or env.exists()):
        return {"ready": False, "issues": ["Tunnel setup deferred; local services can run."]}
    try:
        if not profile.exists() and not profile.is_symlink():
            identifier = ui.ask("Authorized tunnel ID (blank to defer)")
            if not identifier:
                return {"ready": False, "issues": ["Account authorization and a tunnel ID are still required."]}
            if not re.fullmatch(r"tunnel_[a-f0-9]{32}", identifier):
                raise XodexError("tunnel_profile", "Expected tunnel_ followed by 32 lowercase hexadecimal digits")
            write_generated(profile, asset("tunnel-client.yaml").replace("@TUNNEL_ID@", identifier))
        tunnel_profile(profile)
        if not env.exists() and not env.is_symlink():
            secret = ui.secret("Tunnel runtime key (masked; blank to defer)")
            if not secret:
                return {"ready": False, "issues": ["Tunnel runtime credential is still required."]}
            if not re.fullmatch(r"[!-~]{1,4096}", secret) or any(c in secret for c in "\"'\\"):
                raise XodexError("tunnel_credentials", "Invalid tunnel runtime key")
            content = asset("tunnel.env.example").replace("REPLACE_WITH_YOUR_RUNTIME_KEY", '"' + secret + '"')
            write_generated(env, content)
        tunnel_key(env)
        if not shutil.which(str(client)):
            issues.append(f"Install the verified tunnel-client release bundle at {client}, or select --tunnel-client.")
    except XodexError as error:
        issues.append(str(error))
    return {"ready": not issues, "issues": issues, "live_connection_tested": False}


async def startup_smoke(socket: Path) -> dict:
    deadline = time.monotonic() + 30
    while True:
        try:
            return await probe(socket)
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as error:
            if time.monotonic() >= deadline:
                raise XodexError("smoke_failed", "Local MCP smoke failed after startup; inspect the engine and gateway journals") from error
            await asyncio.sleep(0.5)


async def setup(path: Path, *, tunnel_client: Path | None = None, tunnel_config: Path | None = None,
                tunnel_env: Path | None = None, ui: Console | None = None) -> dict:
    ui = ui or Console()
    path = absolute(path)
    invocation = [sys.executable, "-m", "xodex", "--config", str(path)]
    resume = [*invocation, "setup"]
    for flag, value in (("--tunnel-client", tunnel_client), ("--tunnel-config", tunnel_config), ("--tunnel-env", tunnel_env)):
        if value is not None:
            resume.extend([flag, str(absolute(value))])
    resume_command = shlex.join(resume)
    stage = "interactive terminal"
    try:
        if not ui.interactive():
            raise XodexError("interactive_required", "Setup requires an interactive terminal; bootstrap with scripts/install-user.sh --install-only, then run setup in a terminal")
        stage = "configuration"
        config, content = configure(path, ui)
        outside_workspaces(config, [path])
        stage = "host prerequisites"
        ui.say("Checking host prerequisites and user systemd...")
        await check_host(config)
        stage = "configuration"
        if content is not None:
            write_generated(path, content)
        stage = "GitHub credentials"
        token = provision_github(config, ui)
        stage = "GitHub repository access"
        ui.say("Checking GitHub access to each allowed repository (read-only)...")
        await check_repositories(config, token)
        del token
        stage = "worker image"
        if not await image_available(config):
            if config.image != DEFAULT_IMAGE:
                raise XodexError("custom_image", "Provision the configured custom image with its owner-maintained build recipe", image=config.image)
            if not ui.confirm("Build the missing bundled worker image? (requires registry/package network access)"):
                build_command = shlex.join([*invocation, "build-worker"])
                raise XodexError("worker_missing", f"Worker image is required; run {build_command}, then resume setup")
            ui.say("Building the bundled worker image; this can take several minutes...")
            await build_worker(podman=config.podman, image=config.image)
        stage = "sandbox probe"
        ui.say("Running the disposable sandbox probe...")
        diagnostics = await doctor(config, True)
        stage = "tunnel configuration"
        client = absolute(tunnel_client or Path(shutil.which("tunnel-client") or Path.home() / ".local/opt/openai-tunnel/tunnel-client"))
        profile = absolute(tunnel_config or path.parent / "tunnel-client.yaml")
        env = absolute(tunnel_env or path.parent / "tunnel.env")
        tunnel = configure_tunnel(config, client, profile, env, ui)
        if not tunnel["ready"]:
            ui.say("Tunnel pending: " + " ".join(tunnel["issues"]))
            tunnel["resume"] = resume_command
        stage = "service files"
        generated = service_files(config, path, client, profile, env)
        outside_workspaces(config, list(generated))
        for destination, text in generated.items():
            check_generated(destination, text, 0o644)
        changes = {str(destination): write_generated(destination, text, 0o644) for destination, text in generated.items()}
        stage = "systemd reload"
        await command([executable("systemctl"), "--user", "daemon-reload"])
        report = {"local_ready": True, "configuration": str(path), "sandbox_probe": diagnostics["sandbox_probe"],
                  "tunnel": tunnel, "service_files": changes, "services_started": False}
        services = ["xodex-engine.service", "xodex-mcp.service"]
        if tunnel["ready"]:
            services.append("xodex-tunnel.service")
        stage = "service startup"
        # Enabling by filename links units outside the manager's search path.
        unit_paths = {destination.name: str(destination) for destination in generated}
        start = [executable("systemctl"), "--user", "enable", "--now", *(unit_paths[name] for name in services)]
        if ui.confirm("Enable and start " + ", ".join(services) + "?"):
            await command(start)
            report["services_started"] = True
            stage = "MCP smoke"
            report["smoke"] = await startup_smoke(config.mcp_socket)
            stage = "service status"
            for service in services:
                await command([executable("systemctl"), "--user", "is-active", "--quiet", service])
        else:
            report["start_command"] = shlex.join(start)
            report["smoke_command"] = shlex.join([*invocation, "smoke"])
        return report
    except (EOFError, KeyboardInterrupt, asyncio.CancelledError) as error:
        raise XodexError("setup_cancelled", "Setup cancelled; completed files and retained state are preserved",
                         stage=stage, resume=resume_command) from error
    except (XodexError, OSError, ValueError) as error:
        details = error.details if isinstance(error, XodexError) else {}
        raise XodexError("setup_failed", str(error), stage=stage, resume=resume_command,
                         cause=error.code if isinstance(error, XodexError) else type(error).__name__, **details) from error
