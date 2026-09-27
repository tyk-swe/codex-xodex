import asyncio

import httpx
import pytest
import uvicorn

from conftest import create_task, settle
from xodex.cli import listener
from xodex.errors import XodexError
from xodex.server import EngineClient, engine_app, gateway_app
from xodex.tools import CATALOG


class DirectClient:
    def __init__(self, engine):
        self.engine = engine
    async def call(self, name, args):
        return await self.engine.call(name, args)
    async def close(self):
        pass


def rpc(method, params=None, identifier=1):
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}


@pytest.fixture
def app(engine):
    return gateway_app(DirectClient(engine))


async def test_protocol_discovery(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        init = (await client.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18"}))).json()
        assert init["result"]["serverInfo"]["name"] == "chatgpt-xodex"
        tools = (await client.post("/mcp", json=rpc("tools/list"))).json()["result"]["tools"]
        assert tools == [tool.wire() for tool in CATALOG]
        assert len({tool["name"] for tool in tools}) == len(CATALOG) == 17
        for tool in tools:
            if tool["name"] in {"exec_command", "apply_patch", "command_input"}:
                assert tool["annotations"]["destructiveHint"]
                assert not tool["annotations"]["readOnlyHint"]
        assert (await client.post("/mcp", json=rpc("resources/read", {"uri": "xodex://guide"}))).json()["result"]["contents"]
        assert (await client.post("/mcp", json=rpc("prompts/get", {"name": "coding-workflow"}))).json()["result"]["messages"]


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_no_stream_or_teardown(app, method):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.request(method, "/mcp")
        assert result.status_code == 405


async def test_reject_origin_and_version(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.post("/mcp", json=rpc("ping"), headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.post("/mcp", json=rpc("ping"), headers={"MCP-Protocol-Version": "2099-01-01"})).status_code == 400
        assert (await client.post("/mcp", content="plain")).status_code == 415


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"jsonrpc":"2.0","method":"ping","id":true}', b'{"x":NaN}'])
async def test_bad_json(app, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/mcp", content=body, headers={"Content-Type": "application/json"})
        assert result.status_code == 400


async def test_body_limit(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/mcp", content=b"x" * (2 * 1024 * 1024 + 1), headers={"Content-Type": "application/json"})
        assert result.status_code == 413


async def test_notification_cannot_execute(app, engine):
    workspace = await create_task(engine)
    message = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "exec_command",
               "arguments": {"task_id": workspace, "request_id": "bad", "cmd": "touch SHOULD_NOT_EXIST"}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/mcp", json=message)
        assert result.status_code == 202
    assert not (engine.tasks.paths(engine.store.task(workspace))[0] / "SHOULD_NOT_EXIST").exists()


async def test_tool_error_not_transport_success(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        result = await client.post("/mcp", json=rpc("tools/call", {"name": "attach_repository", "arguments": {"repository": "other/repo", "request_id": "x"}}))
        body = result.json()["result"]
        assert body["isError"] and not body["structuredContent"]["ok"]
        assert (await client.post("/mcp", json=rpc("tools/call", {"name": "invented"}))).json()["error"]["code"] == -32602
        assert (await client.post("/mcp", json=rpc("imaginary/method"))).json()["error"]["code"] == -32601


async def test_cancel_does_not_abort(engine, monkeypatch):
    workspace = await create_task(engine)
    original = engine._dispatch
    admitted = asyncio.Event()
    release = asyncio.Event()
    async def slow(tool, args, key):
        if tool == "write_file":
            admitted.set()
            await release.wait()
        return await original(tool, args, key)
    monkeypatch.setattr(engine, "_dispatch", slow)
    args = {"task_id": workspace, "request_id": "write", "path": "survived", "content": "yes", "expected_sha256": ""}
    task = asyncio.create_task(engine.call("write_file", args))
    await admitted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.02)
    retry = await engine.call("write_file", args)
    assert retry["ok"], retry
    assert (await engine.call("read_file", {"task_id": workspace, "path": "survived"}))["text"] == "yes"


async def run_uvicorn(app, path):
    sock, lock = listener(path)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", access_log=False, ws="none"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            return server, task, sock, lock
        await asyncio.sleep(0.01)
    raise AssertionError("uvicorn did not start")


async def stop_uvicorn(state, path):
    server, task, sock, lock = state
    server.should_exit = True
    await asyncio.wait_for(task, 5)
    sock.close()
    lock.close()
    path.unlink(missing_ok=True)


async def test_real_uds_gateway_restart(engine, config):
    executor = await run_uvicorn(engine_app(engine), config.engine_socket)
    remote = EngineClient(config)
    first = await run_uvicorn(gateway_app(remote), config.mcp_socket)
    transport = httpx.AsyncHTTPTransport(uds=str(config.mcp_socket))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            await client.post("/mcp", json=rpc("tools/call", {"name":"attach_repository", "arguments":{"repository":"test/repo", "request_id":"uds-attach"}}))
            init = (await client.post("/mcp", json=rpc("tools/call", {"name":"start_task", "arguments":{"repository":"test/repo", "request_id":"uds-init", "task":"Fix addition"}}))).json()["result"]["structuredContent"]
            workspace = init["task_id"]
            await settle(engine, workspace)
            run = (await client.post("/mcp", json=rpc("tools/call", {"name": "exec_command", "arguments": {"task_id": workspace, "request_id": "uds-run", "cmd": "sleep 0.5; printf restart-survived", "yield_time_ms": 0}}))).json()["result"]["structuredContent"]
        await stop_uvicorn(first, config.mcp_socket)
        first = None
        second = await run_uvicorn(gateway_app(remote), config.mcp_socket)
        try:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(config.mcp_socket)), base_url="http://localhost") as client:
                result = (await client.post("/mcp", json=rpc("tools/call", {"name": "task_status", "arguments": {"task_id": workspace, "command_id": run["command_id"], "wait_ms": 3000}}))).json()["result"]["structuredContent"]
                assert result["command"]["state"] == "succeeded", result
                assert result["command"]["output"] == "restart-survived"
        finally:
            await stop_uvicorn(second, config.mcp_socket)
    finally:
        if first is not None:
            await stop_uvicorn(first, config.mcp_socket)
        await remote.close()
        await stop_uvicorn(executor, config.engine_socket)


def test_socket_permissions(tmp_path):
    path = tmp_path / "private/mcp.sock"
    sock, lock = listener(path)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        with pytest.raises(XodexError):
            listener(path)
    finally:
        sock.close()
        lock.close()


async def test_ipc_has_no_admin(engine):
    app = engine_app(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.post("/admin/delete", json={})).status_code == 404
        assert (await client.post("/v1/tools/server_info", json={}, headers={"Origin": "https://chatgpt.com"})).status_code == 403


async def test_shipped_smoke_script_over_real_socket(engine, config):
    import os
    import sys
    import xodex
    from pathlib import Path
    executor = await run_uvicorn(engine_app(engine), config.engine_socket)
    remote = EngineClient(config)
    gateway = await run_uvicorn(gateway_app(remote), config.mcp_socket)
    try:
        script = Path(__file__).parents[1] / "scripts/smoke-mcp.py"
        env = {**os.environ, "PYTHONPATH": str(Path(xodex.__file__).resolve().parent.parent)}
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), "--socket", str(config.mcp_socket), env=env,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0, output.decode()
        assert b'"result": "passed"' in output
        assert b'"mutations": false' in output
        assert len((await engine.call("list_tasks", {}))["tasks"]) == 0
    finally:
        await stop_uvicorn(gateway, config.mcp_socket)
        await remote.close()
        await stop_uvicorn(executor, config.engine_socket)


@pytest.mark.parametrize("body", [
    b'{"jsonrpc":"2.0","method":"ping","id":"\\ud800"}',
    b'{"jsonrpc":"2.0","method":"ping","id":1,"id":2}',
    b'{"jsonrpc":"2.0","method":"ping","id":1,"params":{"x":1e400}}',
])
async def test_invalid_json_envelopes_never_reach_response_encoding(app, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        reply = await client.post("/mcp", content=body, headers={"Content-Type": "application/json"})
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == -32700


def test_failed_socket_probe_releases_listener_lock(tmp_path, monkeypatch):
    import socket

    path = tmp_path / "mcp.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    original = socket.socket.connect
    with monkeypatch.context() as patch:
        def fail(*args):
            raise TimeoutError("simulated socket probe timeout")
        patch.setattr(socket.socket, "connect", fail)
        with pytest.raises(TimeoutError):
            listener(path)
    assert socket.socket.connect is original
    sock, lock = listener(path)
    sock.close()
    lock.close()


async def test_gateway_closes_client_on_exceptional_lifespan_exit():
    class Client:
        closed = False

        async def close(self):
            self.closed = True

    client = Client()
    app = gateway_app(client)
    with pytest.raises(RuntimeError, match="lifespan failure"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("lifespan failure")
    assert client.closed
