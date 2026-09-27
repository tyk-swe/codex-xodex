from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .errors import XodexError

MAX_BODY = 2 * 1024 * 1024


def strict_json(data: bytes) -> Any:
    def reject(value: str) -> None:
        raise ValueError(f"Invalid JSON constant: {value}")
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    value = json.loads(data.decode("utf-8"), parse_constant=reject, object_pairs_hook=unique_object)
    # parse_constant alone does not reject numeric overflow or lone surrogates.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


async def body_json(request: Request) -> Any:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) < 0 or int(length) > MAX_BODY:
                raise XodexError("body_too_large", "Request exceeds 2 MiB")
        except ValueError as error:
            raise XodexError("invalid_request", "Invalid Content-Length") from error
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            raise XodexError("body_too_large", "Request exceeds 2 MiB")
    try:
        return strict_json(body)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise XodexError("invalid_json", "Request is not valid JSON") from error


def response(value: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store",
                                                           "X-Content-Type-Options": "nosniff"})
