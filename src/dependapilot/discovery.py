"""Dependabot PR discovery: chunked search across the fleet + REST hydration.

`DiscoveryService` is the dashboard's source of truth for every open Dependabot PR
across the fleet. It runs one `GET /search/issues` per chunk of repos -- queries are
packed to stay within GitHub's ~256-character search query budget -- then hydrates
each hit via `GET /repos/{owner}/{repo}/pulls/{number}` for fields the search API
doesn't carry (head sha, base ref, mergeable state). Results are cached in memory for
a TTL so the dashboard doesn't re-run discovery on every request.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from dependapilot.config import FleetConfig
from dependapilot.github import GitHubAPIError, GitHubClient

DEPENDABOT_LOGIN: Final = "dependabot[bot]"
DEFAULT_MAX_QUERY_LENGTH: Final = 256
DEFAULT_CACHE_TTL_SECONDS: Final = 300.0

_SEARCH_QUERY_PREFIX: Final = "is:pr is:open author:app/dependabot"

Clock = Callable[[], float]


def build_search_queries(
    repos: Sequence[str], *, max_query_length: int = DEFAULT_MAX_QUERY_LENGTH
) -> list[str]:
    """Chunk `repos` into `is:pr is:open author:app/dependabot repo:... ...` queries.

    Each query packs as many `repo:` qualifiers as fit within `max_query_length`
    characters (GitHub's search query budget) rather than a fixed repo count, so a
    fleet of short slugs gets more repos per query than one of long slugs. A single
    repo slug long enough to exceed the budget on its own is still emitted as its
    own (over-budget) query -- there's no way to split one repo further.

    Repos appear across the returned queries in the order given, each exactly once.
    """
    if not repos:
        return []

    queries: list[str] = []
    current: list[str] = []
    for repo in repos:
        candidate = [*current, repo]
        if current and len(_render_query(candidate)) > max_query_length:
            queries.append(_render_query(current))
            current = [repo]
        else:
            current = candidate
    queries.append(_render_query(current))
    return queries


def _render_query(repos: list[str]) -> str:
    return " ".join([_SEARCH_QUERY_PREFIX, *(f"repo:{repo}" for repo in repos)])


@dataclass(frozen=True, slots=True)
class PRRecord:
    """One fully-hydrated open Dependabot PR."""

    repo: str
    number: int
    title: str
    html_url: str
    author: str
    draft: bool
    head_sha: str
    head_ref: str
    base_ref: str
    mergeable: bool | None
    mergeable_state: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_pull(cls, repo: str, payload: dict[str, Any]) -> PRRecord:
        """Build a record from a `GET /repos/{owner}/{repo}/pulls/{number}` payload."""
        return cls(
            repo=repo,
            number=payload["number"],
            title=payload["title"],
            html_url=payload["html_url"],
            author=payload["user"]["login"],
            draft=payload.get("draft", False),
            head_sha=payload["head"]["sha"],
            head_ref=payload["head"]["ref"],
            base_ref=payload["base"]["ref"],
            mergeable=payload.get("mergeable"),
            mergeable_state=payload.get("mergeable_state"),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )


def _repo_from_search_hit(payload: dict[str, Any]) -> str:
    """Extract the `owner/repo` slug from a search hit's `repository_url`."""
    owner, repo = payload["repository_url"].rstrip("/").split("/")[-2:]
    return f"{owner}/{repo}"


async def _search_all(
    client: GitHubClient, query: str, *, per_page: int = 100
) -> list[dict[str, Any]]:
    """Run one search query, following `Link: rel="next"` across result pages.

    The search API wraps its results in `{"items": [...]}` rather than returning a
    bare list, so it can't reuse `GitHubClient.paginate` (which assumes the latter);
    this follows the same next-link-until-absent shape using httpx's own `Link`
    parsing (`response.links`) instead of duplicating the client's link-header logic.
    """
    items: list[dict[str, Any]] = []
    path: str | None = "/search/issues"
    params: dict[str, Any] | None = {"q": query, "per_page": per_page}
    while path is not None:
        response = await client.get(path, params=params)
        items.extend(response.json()["items"])
        path = response.links.get("next", {}).get("url")
        params = None  # the next URL already carries q/per_page/page
    return items


class DiscoveryService:
    """Discovers every open Dependabot PR across the fleet as hydrated `PRRecord`s.

    Results are cached in memory for `ttl_seconds`; pass `force_refresh=True` to
    `discover()` (or call `refresh()`) to bypass the cache and re-run discovery.
    """

    def __init__(
        self,
        client: GitHubClient,
        fleet: FleetConfig,
        *,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        max_query_length: int = DEFAULT_MAX_QUERY_LENGTH,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client = client
        self._fleet = fleet
        self._ttl_seconds = ttl_seconds
        self._max_query_length = max_query_length
        self._clock = clock
        self._cache: dict[str, list[PRRecord]] | None = None
        self._cached_at: float | None = None

    async def discover(self, *, force_refresh: bool = False) -> dict[str, list[PRRecord]]:
        """Return every open Dependabot PR across the fleet, keyed by repo.

        Serves the cached result when it's younger than `ttl_seconds`, unless
        `force_refresh` is set.
        """
        if not force_refresh and self._cache is not None and self._cached_at is not None:
            cache_age = self._clock() - self._cached_at
            if cache_age < self._ttl_seconds:
                return self._cache

        records = await self._discover_uncached()
        self._cache = records
        self._cached_at = self._clock()
        return records

    async def refresh(self) -> dict[str, list[PRRecord]]:
        """Explicit refresh hook: bypass the cache and re-run discovery."""
        return await self.discover(force_refresh=True)

    async def _discover_uncached(self) -> dict[str, list[PRRecord]]:
        repos = [entry.repo for entry in self._fleet.repos]
        records: dict[str, list[PRRecord]] = {repo: [] for repo in repos}
        if not repos:
            return records

        for query in build_search_queries(repos, max_query_length=self._max_query_length):
            hits = await _search_all(self._client, query)
            for hit in hits:
                if hit.get("user", {}).get("login") != DEPENDABOT_LOGIN:
                    # Defensive: the search `author:` qualifier should already
                    # guarantee this, but never trust it blindly downstream.
                    continue
                repo = _repo_from_search_hit(hit)
                try:
                    pull = await self._client.get(f"/repos/{repo}/pulls/{hit['number']}")
                except GitHubAPIError as exc:
                    if exc.status_code == 404:
                        continue  # closed/removed between search and hydration
                    raise
                records.setdefault(repo, []).append(PRRecord.from_pull(repo, pull.json()))
        return records
