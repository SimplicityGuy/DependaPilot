"""Tests for the async GitHub client: identity, pagination, 401, rate limits, retries.

Every test drives an `httpx.MockTransport` — no real network or `gh` subprocess call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from dependapilot.github.errors import GitHubAPIError, GitHubAuthError, GitHubRateLimitError
from tests.github.conftest import make_client

Sleep = Callable[[float], Awaitable[None]]


async def test_identify_reads_login_and_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user"
        return httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-OAuth-Scopes": "repo, security_events, read:org"},
        )

    client = make_client(handler)

    identity = await client.identify()

    assert identity.login == "octocat"
    assert identity.scopes_known is True
    assert "security_events" in identity.scopes
    assert client.capabilities.security_events is True


async def test_identify_without_security_events_scope() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-OAuth-Scopes": "repo, read:org"},
        )

    client = make_client(handler)

    await client.identify()

    assert client.capabilities.security_events is False


async def test_identify_leaves_capability_unknown_for_fine_grained_token() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # Fine-grained PATs never send an X-OAuth-Scopes header at all.
        return httpx.Response(200, json={"login": "octocat"})

    client = make_client(handler)

    identity = await client.identify()

    assert identity.scopes_known is False
    assert client.capabilities.security_events is None


async def test_401_raises_actionable_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = make_client(handler)

    with pytest.raises(GitHubAuthError, match="gh auth login"):
        await client.get("/user")


async def test_primary_rate_limit_raises_with_remaining_and_reset() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Reset": "1700000000",
            },
        )

    client = make_client(handler)

    with pytest.raises(GitHubRateLimitError) as exc_info:
        await client.get("/repos/o/r")

    assert exc_info.value.remaining == 0
    assert exc_info.value.limit == 5000
    assert exc_info.value.reset_at == 1700000000


async def test_secondary_rate_limit_raises_with_retry_after() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit"},
            headers={"Retry-After": "30"},
        )

    client = make_client(handler)

    with pytest.raises(GitHubRateLimitError) as exc_info:
        await client.get("/repos/o/r")

    assert exc_info.value.retry_after == 30.0


async def test_429_raises_rate_limit_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "rate limited"})

    client = make_client(handler)

    with pytest.raises(GitHubRateLimitError):
        await client.get("/repos/o/r")


async def test_plain_403_without_rate_limit_headers_raises_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    client = make_client(handler)

    with pytest.raises(GitHubAPIError) as exc_info:
        await client.get("/repos/o/r")

    assert exc_info.value.status_code == 403


async def test_404_raises_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = make_client(handler)

    with pytest.raises(GitHubAPIError) as exc_info:
        await client.get("/repos/o/missing")

    assert exc_info.value.status_code == 404


async def test_5xx_retries_then_succeeds(no_op_sleep: Sleep) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"message": "Service Unavailable"})
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler, sleep=no_op_sleep)

    response = await client.get("/repos/o/r")

    assert response.json() == {"ok": True}
    assert attempts == 3


async def test_5xx_exhausts_retries_and_raises(no_op_sleep: Sleep) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502, json={"message": "Bad Gateway"})

    client = make_client(handler, max_retries=2, sleep=no_op_sleep)

    with pytest.raises(GitHubAPIError) as exc_info:
        await client.get("/repos/o/r")

    assert exc_info.value.status_code == 502
    assert attempts == 3  # initial attempt + 2 retries


async def test_paginate_follows_link_header_across_pages() -> None:
    page1_url = "/repositories/1/issues?per_page=2"
    page2_url = "https://api.github.com/repositories/1/issues?per_page=2&page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 3}])
        return httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}],
            headers={"Link": f'<{page2_url}>; rel="next", <{page2_url}>; rel="last"'},
        )

    client = make_client(handler)

    items = await client.paginate_all(page1_url)

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]


async def test_paginate_stops_when_no_next_link() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 1}])

    client = make_client(handler)

    items = await client.paginate_all("/repositories/1/issues")

    assert items == [{"id": 1}]


async def test_probe_security_events_scope_true_on_200() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = make_client(handler)

    result = await client.probe_security_events_scope("acme", "widgets")

    assert result is True
    assert client.capabilities.security_events is True


async def test_probe_security_events_scope_false_on_missing_scope_without_raising() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "Resource not accessible: missing security_events scope"}
        )

    client = make_client(handler)

    result = await client.probe_security_events_scope("acme", "widgets")

    assert result is False
    assert client.capabilities.security_events is False


async def test_probe_security_events_scope_false_when_alerts_disabled() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Dependabot alerts are disabled"})

    client = make_client(handler)

    result = await client.probe_security_events_scope("acme", "widgets")

    assert result is False
    assert client.capabilities.security_events is False


async def test_probe_security_events_scope_reraises_other_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = make_client(handler, sleep=lambda _s: _immediate())

    with pytest.raises(GitHubAPIError):
        await client.probe_security_events_scope("acme", "widgets")


async def _immediate() -> None:
    return None
