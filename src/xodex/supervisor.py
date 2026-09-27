from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .engine import Engine
from .errors import XodexError
from .http import body_json, response


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
