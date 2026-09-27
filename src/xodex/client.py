from __future__ import annotations

from typing import Any

import httpx

from .config import Config
from .errors import XodexError


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
