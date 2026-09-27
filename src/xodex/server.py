from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from . import __version__
from .config import Config
from .engine import Engine
from .errors import XodexError
from .http import body_json, response
from .store import canonical
from .tools import CATALOG, TOOLS

PROTOCOLS = ("2025-06-18", "2025-03-26")
INSTRUCTIONS = files("xodex").joinpath("instructions.md").read_text(encoding="utf-8")


def engine_app(engine: Engine) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            await engine.start()
            yield
        finally:
            await engine.close()

    async def call(request: Request) -> Response:
        if request.headers.get("origin"):
            return response({"error": "Origin is not accepted on private supervisor IPC"}, 403)
        try:
            args = await body_json(request)
            result = await engine.call(request.path_params["tool"], args)
            return response(result)
        except XodexError as error:
            return response(error.result(), 413 if error.code == "body_too_large" else 400)

    async def health(request: Request) -> Response:
        ready = bool(engine.capabilities) and not engine.stopping and not engine.jobs.fault
        return response({"ready": ready, "active_jobs": len(engine.jobs.running)}, 200 if ready else 503)

    return Starlette(routes=[Route("/v1/tools/{tool}", call, methods=["POST"]),
                             Route("/healthz", health)], lifespan=lifespan)


class EngineClient:
    def __init__(self, config: Config):
        self.client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(config.engine_socket)),
                                       base_url="http://localhost", timeout=httpx.Timeout(45, connect=3))

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.client.post(f"/v1/tools/{tool}", json=args)
            result.raise_for_status()
            return result.json()
        except (httpx.HTTPError, ValueError) as error:
            return XodexError("executor_unavailable",
                              "Supervisor could not be reached or the response was lost. An admitted mutation may still run; reuse its exact request_id.",
                              exception=type(error).__name__).result()

    async def close(self) -> None:
        await self.client.aclose()


def tool_result(result: dict[str, Any]) -> dict[str, Any]:
    public = dict(result)
    picture = public.pop("image", None)
    contents: list[dict[str, Any]] = []
    if picture and result.get("ok"):
        public["image"] = {key: value for key, value in picture.items() if key != "data"}
        contents.append({"type": "image", "data": picture["data"], "mimeType": picture["mimeType"]})
    contents.insert(0, {"type": "text", "text": canonical(public)})
    return {"content": contents, "structuredContent": public, "isError": not result.get("ok", False)}


def gateway_app(client: EngineClient) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.close()

    def rpc_error(identifier: Any, code: int, message: str, status: int = 200) -> Response:
        return response({"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}, status)

    async def mcp(request: Request) -> Response:
        # The production listener is a mode-0600 Unix socket, never a public unauthenticated endpoint.
        origin = request.headers.get("origin")
        if origin is not None and origin not in {"https://chatgpt.com", "https://chat.openai.com"}:
            return response({"error": "Origin rejected"}, 403)
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return response({"error": "application/json required"}, 415)
        version = request.headers.get("mcp-protocol-version")
        if version is not None and version not in PROTOCOLS:
            return response({"error": "Unsupported MCP-Protocol-Version", "supported": list(PROTOCOLS)}, 400)
        try:
            message = await body_json(request)
        except XodexError as error:
            return rpc_error(None, -32700, str(error), 413 if error.code == "body_too_large" else 400)
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str) or not message["method"]):
            return rpc_error(None, -32600, "Invalid JSON-RPC request", 400)
        identifier = message.get("id")
        if "id" in message and type(identifier) not in (str, int):
            return rpc_error(None, -32600, "Invalid JSON-RPC id", 400)
        if "id" not in message:
            # Notifications never execute tools. A cancelled HTTP request does not cancel a job.
            return Response(status_code=202)
        method, params = message["method"], message.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(identifier, -32602, "params must be an object")
        if method == "initialize":
            if not isinstance(params.get("protocolVersion"), str):
                return rpc_error(identifier, -32602, "protocolVersion is required")
            selected = params["protocolVersion"] if params["protocolVersion"] in PROTOCOLS else PROTOCOLS[0]
            result = {"protocolVersion": selected, "serverInfo": {"name": "chatgpt-xodex", "version": __version__},
                      "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False},
                                       "prompts": {"listChanged": False}}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [tool.wire() for tool in CATALOG]}
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOLS:
                return rpc_error(identifier, -32602, "Unknown tool")
            args = params.get("arguments", {})
            try:
                TOOLS[name].validate(args)
            except XodexError as error:
                return rpc_error(identifier, -32602, str(error))
            result = tool_result(await client.call(name, args))
        elif method == "resources/list":
            result = {"resources": [{"uri": "xodex://guide", "name": "Xodex working contract", "mimeType": "text/markdown"}]}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "resources/read":
            if params.get("uri") != "xodex://guide":
                return rpc_error(identifier, -32602, "Unknown resource")
            result = {"contents": [{"uri": "xodex://guide", "mimeType": "text/markdown", "text": INSTRUCTIONS}]}
        elif method == "prompts/list":
            result = {"prompts": [{"name": "coding-workflow", "description": "Take a repository and task through implementation, validation and PR publication",
                                   "arguments": []}]}
        elif method == "prompts/get":
            if params.get("name") != "coding-workflow":
                return rpc_error(identifier, -32602, "Unknown prompt")
            result = {"messages": [{"role": "user", "content": {"type": "text", "text": INSTRUCTIONS}}]}
        else:
            return rpc_error(identifier, -32601, "Method not found")
        return response({"jsonrpc": "2.0", "id": identifier, "result": result})

    async def health(request: Request) -> Response:
        result = await client.call("server_info", {})
        return response({"ready": bool(result.get("ok")) and not result.get("storage_fault"), "name": "chatgpt-xodex"}, 200 if result.get("ok") and not result.get("storage_fault") else 503)

    return Starlette(routes=[Route("/mcp", mcp, methods=["POST", "GET", "DELETE"]),
                             Route("/healthz", health)], lifespan=lifespan)
