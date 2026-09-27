"""Read-only local MCP acceptance probe. Never creates tasks, branches or PRs."""
from __future__ import annotations

from pathlib import Path

import httpx

from xodex import __version__


async def probe(socket: Path) -> dict:
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)),
                                base_url="http://localhost", timeout=15) as client:
        counter = 0
        async def rpc(method: str, params: dict | None = None) -> dict:
            nonlocal counter
            counter += 1
            reply = await client.post("/mcp", json={"jsonrpc":"2.0", "id":counter,
                                                  "method":method, "params":params or {}})
            reply.raise_for_status()
            body = reply.json()
            if "error" in body:
                raise RuntimeError(body["error"])
            return body["result"]
        initialized = await rpc("initialize", {"protocolVersion":"2025-06-18",
                               "capabilities":{}, "clientInfo":{"name":"xodex-smoke", "version":__version__}})
        client.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        notification = await client.post("/mcp", json={"jsonrpc":"2.0", "method":"notifications/initialized"})
        notification.raise_for_status()
        tools = (await rpc("tools/list"))["tools"]
        expected = {"attach_repository", "start_task", "task_status", "stop_task", "finish_task", "exec_command"}
        if not expected <= {t["name"] for t in tools}:
            raise RuntimeError("Task workflow tools missing")
        info = (await rpc("tools/call", {"name":"server_info", "arguments":{}}))["structuredContent"]
        if not info.get("ok") or info.get("storage_fault"):
            raise RuntimeError(info)
        tasks = (await rpc("tools/call", {"name":"list_tasks", "arguments":{"limit":1}}))["structuredContent"]
        if not tasks.get("ok"):
            raise RuntimeError(tasks)
        return {"result":"passed", "mutations":False, "server":initialized["serverInfo"],
                "tools":len(tools), "runtime":info["runtime"],
                "live_tunnel_and_chatgpt_tested":False}
