from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from . import __version__
from .config import repository_name, validate_ref
from .errors import XodexError
from .jsonutil import strict_json


def load_token(path: Path | None) -> str:
    if path is None:
        raise XodexError("github_credentials", "Set github_token_file on the VPS; never send credentials in chat")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise XodexError("github_credentials", "GitHub token must be an owner-only regular file (0600)")
            data = file.read(4097)
            if len(data) > 4096:
                raise XodexError("github_credentials", "GitHub credential file exceeds 4096 bytes")
            token = data.decode("ascii").strip()
    except (OSError, UnicodeError) as error:
        raise XodexError("github_credentials", "Cannot read a valid owner-only GitHub credential file") from error
    if not re.fullmatch(r"[!-~]{1,4096}", token):
        raise XodexError("github_credentials", "Invalid GitHub credential file")
    return token


class GitHub:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.client = httpx.AsyncClient(base_url="https://api.github.com", follow_redirects=False,
            transport=transport, timeout=httpx.Timeout(30, connect=5), trust_env=False,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": f"chatgpt-xodex/{__version__}"})

    async def request(self, method: str, path: str, *, missing: bool = False, **kwargs: Any) -> Any:
        try:
            async with self.client.stream(method, path, **kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise XodexError("github_response", "GitHub response exceeded its byte budget")
                if response.status_code == 404 and missing:
                    return None
                if response.status_code >= 300:
                    raise XodexError("github_http", "GitHub rejected the request; check credential permissions, repository rules, or rate limits",
                                     status=response.status_code)
                return strict_json(bytes(body))
        except (httpx.HTTPError, ValueError, UnicodeError, RecursionError) as error:
            raise XodexError("github_uncertain", "GitHub response was lost or invalid; publication will reconcile before retrying") from error

    async def repository(self, name: str) -> dict[str, Any]:
        data = await self.request("GET", "/repos/" + name)
        try:
            canonical_name = repository_name(data["full_name"])
            branch = data["default_branch"]
            validate_ref(branch)
            if canonical_name != name or type(data["id"]) is not int:
                raise ValueError()
            if data.get("archived") or data.get("disabled") or not data.get("permissions", {}).get("push"):
                raise XodexError("repository_readonly", "This credential cannot publish to this repository")
            return {"name": name, "github_id": data["id"], "base_ref": branch}
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise XodexError("github_response", "Repository metadata did not match the requested repository") from error

    async def head(self, name: str, branch: str) -> str | None:
        data = await self.request("GET", f"/repos/{name}/git/ref/heads/{quote(branch, safe='')}", missing=True)
        if data is None:
            return None
        obj = data.get("object") if isinstance(data, dict) else None
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if not isinstance(sha, str) or obj.get("type") != "commit" or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise XodexError("github_response", "Invalid branch commit response")
        return sha

    async def find_pr(self, name: str, branch: str, base: str) -> dict[str, Any] | None:
        owner = name.split("/")[0]
        data = await self.request("GET", f"/repos/{name}/pulls", params={"state": "all", "head": owner + ":" + branch,
                                                                                 "base": base, "per_page": 100})
        if not isinstance(data, list):
            raise XodexError("github_response", "Invalid pull request lookup response")
        matches = []
        for item in data:
            try:
                if (item["head"]["ref"] == branch and item["head"]["repo"]["full_name"].lower() == name
                        and item["base"]["ref"] == base and item["base"]["repo"]["full_name"].lower() == name):
                    matches.append(item)
            except (KeyError, TypeError, AttributeError) as error:
                raise XodexError("github_response", "Incomplete pull request identity in lookup response") from error
        if len(matches) > 1:
            raise XodexError("publication_conflict", "Multiple pull requests match this task branch")
        return matches[0] if matches else None

    async def create_pr(self, name: str, branch: str, base: str, title: str, body: str) -> dict[str, Any]:
        return await self.request("POST", f"/repos/{name}/pulls", json={"head": branch, "base": base,
                                                                      "title": title, "body": body,
                                                                      "maintainer_can_modify": True})

    async def close(self) -> None:
        await self.client.aclose()
