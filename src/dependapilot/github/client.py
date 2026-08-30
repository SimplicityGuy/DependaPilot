"""Async GitHub REST API client.

The single chokepoint every other service goes through: it owns auth (via the `gh`
CLI), pagination, rate-limit surfacing, and retry/backoff, so callers issue plain
`await client.get(...)` / `await client.paginate(...)` calls without re-solving any
of that themselves.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final

import httpx

from dependapilot.github.auth import get_gh_cli_token
from dependapilot.github.errors import GitHubAPIError, GitHubAuthError, GitHubRateLimitError

GITHUB_API_URL: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"

_REAUTH_HINT = "Run `gh auth login` (or `gh auth refresh` if already logged in) and retry."

# Scopes that a classic OAuth token needs in order to read Dependabot alerts.
# Fine-grained tokens don't report scopes at all (see `Identity.scopes_known`); for
# those, `probe_security_events_scope` is the only reliable signal.
_SECURITY_EVENTS_SCOPE = "security_events"


@dataclass(frozen=True, slots=True)
class Identity:
    """The authenticated user, as reported by `GET /user`."""

    login: str
    scopes: frozenset[str]
    scopes_known: bool
    """False for fine-grained PATs, which never report an `X-OAuth-Scopes` header."""


@dataclass(slots=True)
class Capabilities:
    """Feature flags callers can branch on instead of guessing and raising.

    `security_events` starts `None` (unknown) until either the token's OAuth scopes
    are read at startup or `probe_security_events_scope` spot-checks a repo.
    """

    security_events: bool | None = None


def _parse_link_header(link_header: str | None) -> dict[str, str]:
    """Parse a `Link` response header into a `{rel: url}` mapping."""
    links: dict[str, str] = {}
    if not link_header:
        return links
    for part in link_header.split(","):
        segment = part.strip()
        if not segment or ";" not in segment:
            continue
        url_part, *params = segment.split(";")
        url = url_part.strip().removeprefix("<").removesuffix(">")
        for param in params:
            key, _, value = param.strip().partition("=")
            if key.strip() == "rel":
                links[value.strip().strip('"')] = url
    return links


def _parse_oauth_scopes(header_value: str | None) -> tuple[frozenset[str], bool]:
    """Split the `X-OAuth-Scopes` header into (scopes, was_the_header_present)."""
    if header_value is None:
        return frozenset(), False
    scopes = {scope.strip() for scope in header_value.split(",") if scope.strip()}
    return frozenset(scopes), True


class GitHubClient:
    """Async GitHub REST API client, authenticated via the `gh` CLI.

    Construct with `await GitHubClient.create()` for normal use; the `client=`
    constructor argument exists so tests can inject an `httpx.AsyncClient` wired to
    a `MockTransport` instead of talking to a real server.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep or asyncio.sleep
        self.identity: Identity | None = None
        self.capabilities = Capabilities()

    @classmethod
    async def create(cls, *, timeout: float = 30.0) -> GitHubClient:
        """Build a client authenticated from the local `gh` CLI state and verify it.

        Raises:
            GitHubAuthError: `gh` has no usable token, or the API rejects it.
        """
        token = await get_gh_cli_token()
        http_client = httpx.AsyncClient(
            base_url=GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {token}",
            },
            timeout=timeout,
        )
        instance = cls(http_client)
        await instance.identify()
        return instance

    async def identify(self) -> Identity:
        """`GET /user` to verify the token and record which login is active.

        Also opportunistically reads the `X-OAuth-Scopes` header (present for
        classic OAuth tokens, absent for fine-grained PATs and GitHub Apps) to seed
        `capabilities.security_events` without an extra request.
        """
        response = await self.get("/user")
        payload = response.json()
        scopes, scopes_known = _parse_oauth_scopes(response.headers.get("X-OAuth-Scopes"))
        identity = Identity(login=payload["login"], scopes=scopes, scopes_known=scopes_known)
        self.identity = identity
        if scopes_known:
            self.capabilities.security_events = _SECURITY_EVENTS_SCOPE in scopes
        return identity

    async def probe_security_events_scope(self, owner: str, repo: str) -> bool:
        """Spot-check whether the token can read `repo`'s Dependabot alerts.

        Updates and returns `capabilities.security_events`. Never raises for the
        expected "no access" outcomes (403 missing-scope, 404 alerts disabled) —
        those are answered as `False` so callers can branch on the flag instead of
        wrapping every audit call in a try/except.
        """
        try:
            await self.get(f"/repos/{owner}/{repo}/dependabot/alerts", params={"per_page": 1})
        except GitHubAPIError as exc:
            if exc.status_code in (403, 404):
                self.capabilities.security_events = False
                return False
            raise
        else:
            self.capabilities.security_events = True
            return True

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue one request with 401/rate-limit surfacing and 5xx retry/backoff."""
        attempt = 0
        while True:
            response = await self._client.request(method, path, **kwargs)

            if response.status_code == 401:
                raise GitHubAuthError(
                    "GitHub rejected the token (401 Unauthorized). "
                    f"It may be expired or revoked. {_REAUTH_HINT}"
                )

            if _is_rate_limited(response):
                raise _rate_limit_error(response)

            if response.status_code >= 500 and attempt < self._max_retries:
                await self._sleep(self._backoff_base * (2**attempt) + random.uniform(0, 0.1))
                attempt += 1
                continue

            if response.status_code >= 400:
                raise GitHubAPIError(
                    f"GitHub API request failed: {method} {path} -> {response.status_code}",
                    status_code=response.status_code,
                    body=response.text,
                )

            return response

    async def paginate(self, path: str, **kwargs: Any) -> AsyncIterator[Any]:
        """Yield every item across all pages of a paginated list endpoint.

        Follows the `Link: rel="next"` header until it's absent, so callers don't
        need to know page size or count up front.
        """
        next_url: str | None = path
        first = True
        while next_url is not None:
            response = await self.get(next_url, **(kwargs if first else {}))
            first = False
            for item in response.json():
                yield item
            next_url = _parse_link_header(response.headers.get("Link")).get("next")

    async def paginate_all(self, path: str, **kwargs: Any) -> list[Any]:
        """Convenience wrapper over `paginate` that collects every item into a list."""
        return [item async for item in self.paginate(path, **kwargs)]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code == 403:
        remaining = response.headers.get("X-RateLimit-Remaining")
        return remaining == "0" or "Retry-After" in response.headers
    return False


def _rate_limit_error(response: httpx.Response) -> GitHubRateLimitError:
    headers = response.headers
    remaining = _int_or_none(headers.get("X-RateLimit-Remaining"))
    limit = _int_or_none(headers.get("X-RateLimit-Limit"))
    reset_at = _int_or_none(headers.get("X-RateLimit-Reset"))
    retry_after = _float_or_none(headers.get("Retry-After"))

    if retry_after is not None:
        message = f"GitHub secondary rate limit hit; retry after {retry_after:.0f}s."
    elif reset_at is not None:
        message = f"GitHub rate limit exhausted ({remaining}/{limit}); resets at {reset_at}."
    else:
        message = "GitHub rate limit exhausted."

    return GitHubRateLimitError(
        message,
        remaining=remaining,
        limit=limit,
        reset_at=reset_at,
        retry_after=retry_after,
    )


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _float_or_none(value: str | None) -> float | None:
    return float(value) if value is not None else None
