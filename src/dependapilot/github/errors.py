"""Exception hierarchy for the GitHub client."""

from __future__ import annotations

import json
from typing import Any


class GitHubError(Exception):
    """Base class for all errors raised by the GitHub client."""


class GitHubAuthError(GitHubError):
    """Raised when the `gh` CLI has no usable token, or the API rejects it (401).

    The message is written to be shown to the user as-is: it always points at the
    `gh auth login` / `gh auth refresh` remedy.
    """


class GitHubRateLimitError(GitHubError):
    """Raised when the API reports a primary or secondary rate limit (403/429).

    Attributes:
        remaining: Requests left in the current window, if the server reported it.
        limit: The window's total request budget, if the server reported it.
        reset_at: Unix timestamp when the primary rate limit window resets, if reported.
        retry_after: Seconds to wait before retrying, if the server reported it
            (secondary rate limits use `Retry-After` instead of a reset timestamp).
    """

    def __init__(
        self,
        message: str,
        *,
        remaining: int | None = None,
        limit: int | None = None,
        reset_at: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.remaining = remaining
        self.limit = limit
        self.reset_at = reset_at
        self.retry_after = retry_after


class GitHubAPIError(GitHubError):
    """Raised for any other unsuccessful response, including exhausted 5xx retries."""

    def __init__(self, message: str, *, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def github_error_message(exc: GitHubAPIError) -> str:
    """GitHub's own `"message"` field from an error body, falling back to `str(exc)`.

    GitHub's JSON error bodies (405 branch-protection rejections, 422 validation
    failures, etc.) carry a human-readable `message` -- surface that verbatim to a
    dashboard rather than a generic "request failed" string.
    """
    try:
        payload: Any = json.loads(exc.body)
    except (json.JSONDecodeError, TypeError):
        return str(exc)
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message
    return str(exc)
