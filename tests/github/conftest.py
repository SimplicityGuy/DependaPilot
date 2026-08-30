"""Shared fixtures for GitHub client tests. All tests mock the transport — no network."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from dependapilot.github.client import GITHUB_API_URL, GitHubClient

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler,
    *,
    max_retries: int = 3,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> GitHubClient:
    """Build a `GitHubClient` backed by a `MockTransport` instead of a real socket."""
    http_client = httpx.AsyncClient(
        base_url=GITHUB_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": "Bearer test-token",
        },
        transport=httpx.MockTransport(handler),
    )
    return GitHubClient(http_client, max_retries=max_retries, sleep=sleep)


@pytest.fixture
def no_op_sleep() -> Callable[[float], Awaitable[None]]:
    """A `sleep` stand-in that returns instantly, so retry tests don't wait for real backoff."""

    async def _sleep(_seconds: float) -> None:
        return None

    return _sleep
