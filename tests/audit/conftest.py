"""Shared audit-test helpers. Every test drives a `MockTransport` — no network."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

import httpx

from dependapilot.github.client import GitHubClient
from tests.github.conftest import make_client

Route = tuple[int, Any]
"""An HTTP status plus a JSON body, or None for a bodiless response (204/404)."""


def make_routed_client(routes: Mapping[str, Route]) -> GitHubClient:
    """A client that answers each URL path from `routes`, and 404s anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        status, payload = routes.get(request.url.path, (404, {"message": "Not Found"}))
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, json=payload)

    return make_client(handler)


def contents_response(text: str) -> Route:
    """A Contents API payload carrying `text`, encoded the way GitHub encodes it."""
    return (
        200,
        {
            "name": "dependabot.yml",
            "path": ".github/dependabot.yml",
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
        },
    )


def tree_response(paths: list[str], *, truncated: bool = False) -> Route:
    """A git trees API payload listing `paths` as blobs."""
    return (
        200,
        {
            "sha": "deadbeef",
            "tree": [{"path": path, "type": "blob"} for path in paths],
            "truncated": truncated,
        },
    )
