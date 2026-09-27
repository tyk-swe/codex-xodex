"""Shared wire/storage encoding. Changes here affect durable operation identities."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


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
