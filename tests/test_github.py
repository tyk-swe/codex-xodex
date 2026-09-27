import os

import httpx
import pytest

from xodex.errors import XodexError
from xodex.github import GitHub, load_token


def test_token_file_permissions_and_symlinks(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-secret\n")
    token.chmod(0o600)
    assert load_token(token) == "test-secret"
    token.chmod(0o644)
    with pytest.raises(XodexError):
        load_token(token)
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(XodexError):
        load_token(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(XodexError):
        load_token(fifo)


async def test_headers_identity_and_branch_escaping():
    seen = []
    async def handler(request):
        seen.append(request)
        if request.url.path == "/repos/test/repo":
            return httpx.Response(200, json={"id":42, "full_name":"Test/Repo", "default_branch":"main",
                                             "permissions":{"push":True}})
        return httpx.Response(200, json={"object":{"type":"commit", "sha":"a"*40}})
    github = GitHub("test-token", transport=httpx.MockTransport(handler))
    try:
        assert (await github.repository("test/repo"))["github_id"] == 42
        assert await github.head("test/repo", "xodex/task") == "a"*40
        assert b"xodex%2Ftask" in seen[-1].url.raw_path
        assert seen[0].headers["authorization"] == "Bearer test-token"
        assert seen[0].headers["x-github-api-version"] == "2022-11-28"
    finally:
        await github.close()


@pytest.mark.parametrize("status", [301,302,307,401,403,422,429,500])
async def test_http_errors_are_not_followed_or_leak_response(status):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location":"https://evil.example/"}, text="sensitive response")
    github = GitHub("secret", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(XodexError) as result:
            await github.request("POST", "/repos/test/repo/pulls", json={})
        assert result.value.code == "github_http"
        assert result.value.details["status"] == status
        assert "sensitive" not in str(result.value) and len(seen) == 1
    finally:
        await github.close()


@pytest.mark.parametrize("payload", [None, [], {}, {"object":None}, {"object":{"type":"tree", "sha":"a"*40}}])
async def test_malformed_ref_response_fails_closed(payload):
    github = GitHub("secret", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    try:
        with pytest.raises(XodexError):
            await github.head("test/repo", "main")
    finally:
        await github.close()


async def test_large_response_rejected():
    github = GitHub("secret", transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x"*(2*1024*1024+1))))
    try:
        with pytest.raises(XodexError, match="byte budget"):
            await github.request("GET", "/repos/test/repo")
    finally:
        await github.close()


async def test_lost_response_is_uncertain():
    def broken(request):
        raise httpx.ReadError("lost", request=request)
    github = GitHub("secret", transport=httpx.MockTransport(broken))
    try:
        with pytest.raises(XodexError) as result:
            await github.create_pr("test/repo", "branch", "main", "title", "body")
        assert result.value.code == "github_uncertain"
    finally:
        await github.close()


async def test_matching_closed_pull_request_and_duplicate_rejection():
    pr = {"state":"closed", "head":{"ref":"xodex/task", "repo":{"full_name":"test/repo"}},
          "base":{"ref":"main", "repo":{"full_name":"test/repo"}}}
    replies = [[pr], [pr, pr], [None]]
    def handler(request):
        assert request.url.params["state"] == "all"
        assert request.url.params["head"] == "test:xodex/task"
        return httpx.Response(200, json=replies.pop(0))
    github = GitHub("secret", transport=httpx.MockTransport(handler))
    try:
        assert (await github.find_pr("test/repo", "xodex/task", "main"))["state"] == "closed"
        with pytest.raises(XodexError) as result:
            await github.find_pr("test/repo", "xodex/task", "main")
        assert result.value.code == "publication_conflict"
        with pytest.raises(XodexError):
            await github.find_pr("test/repo", "xodex/task", "main")
    finally:
        await github.close()


@pytest.mark.parametrize("contents", [b"secret\x00", b"secret\x7f", b"\xff", b"", b"x" * 4097, b"x" * 4096 + b"\nextra"])
def test_invalid_credential_file_is_a_structured_error(tmp_path, contents):
    path = tmp_path / "token"
    path.write_bytes(contents)
    path.chmod(0o600)
    with pytest.raises(XodexError) as result:
        load_token(path)
    assert result.value.code == "github_credentials"
    assert "secret" not in str(result.value)
