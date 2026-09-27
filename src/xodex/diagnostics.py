from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .backend import PodmanBackend
from .config import Config
from .errors import XodexError
from .git import GitControl


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
