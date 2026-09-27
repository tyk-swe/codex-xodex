from __future__ import annotations

from typing import Any


class XodexError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details

    def result(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self),
                                       "details": self.details}}
