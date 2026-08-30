"""Tests for Dependabot PR discovery: query chunking, hydration, and the TTL cache.

Every test drives an `httpx.MockTransport` (via `tests.github.conftest.make_client`)
-- no real network or `gh` subprocess call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from dependapilot.config import Defaults, FleetConfig, RepoConfig
from dependapilot.discovery import (
    DEFAULT_MAX_QUERY_LENGTH,
    DiscoveryService,
    PRRecord,
    build_search_queries,
)
from dependapilot.github.client import GitHubClient
from tests.github.conftest import make_client

Handler = Callable[[httpx.Request], httpx.Response]

PREFIX = "is:pr is:open author:app/dependabot"


def fleet_of(*repos: str) -> FleetConfig:
    return FleetConfig(defaults=Defaults(), repos=[RepoConfig(repo=r) for r in repos])


def pull_payload(
    *,
    number: int,
    title: str = "Bump foo from 1.0.0 to 1.0.1",
    login: str = "dependabot[bot]",
    head_sha: str = "abc123",
    head_ref: str = "dependabot/pip/foo-1.0.1",
    base_ref: str = "main",
    mergeable: bool | None = True,
    mergeable_state: str | None = "clean",
    draft: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "user": {"login": login},
        "draft": draft,
        "head": {"sha": head_sha, "ref": head_ref},
        "base": {"ref": base_ref},
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }


def search_hit(*, repo: str, number: int, login: str = "dependabot[bot]") -> dict[str, Any]:
    owner, name = repo.split("/")
    return {
        "number": number,
        "title": "Bump foo",
        "repository_url": f"https://api.github.com/repos/{owner}/{name}",
        "user": {"login": login},
    }


# --- build_search_queries -----------------------------------------------------------


class TestBuildSearchQueries:
    def test_empty_repos_returns_no_queries(self) -> None:
        assert build_search_queries([]) == []

    def test_default_budget_is_256_chars(self) -> None:
        assert DEFAULT_MAX_QUERY_LENGTH == 256

    def test_single_repo_produces_one_query(self) -> None:
        queries = build_search_queries(["acme/widgets"])

        assert queries == [f"{PREFIX} repo:acme/widgets"]

    def test_repos_stay_together_when_they_fit_exactly_at_the_boundary(self) -> None:
        repos = ["acme/repo-one", "acme/repo-two"]
        query = f"{PREFIX} repo:acme/repo-one repo:acme/repo-two"

        queries = build_search_queries(repos, max_query_length=len(query))

        assert queries == [query]

    def test_repos_split_when_one_char_over_the_boundary(self) -> None:
        repos = ["acme/repo-one", "acme/repo-two"]
        query = f"{PREFIX} repo:acme/repo-one repo:acme/repo-two"

        queries = build_search_queries(repos, max_query_length=len(query) - 1)

        assert queries == [
            f"{PREFIX} repo:acme/repo-one",
            f"{PREFIX} repo:acme/repo-two",
        ]

    def test_three_repos_split_exactly_two_and_one_at_the_boundary(self) -> None:
        repos = ["acme/repo-one", "acme/repo-two", "acme/repo-three"]
        two_repo_query = f"{PREFIX} repo:acme/repo-one repo:acme/repo-two"

        queries = build_search_queries(repos, max_query_length=len(two_repo_query))

        assert queries == [two_repo_query, f"{PREFIX} repo:acme/repo-three"]

    def test_oversized_single_repo_is_still_emitted_alone(self) -> None:
        huge_repo = "acme/" + "x" * 300

        queries = build_search_queries(["small/one", huge_repo, "small/two"])

        assert queries == [
            f"{PREFIX} repo:small/one",
            f"{PREFIX} repo:{huge_repo}",
            f"{PREFIX} repo:small/two",
        ]

    def test_realistic_fleet_stays_within_default_budget_and_covers_every_repo(self) -> None:
        repos = [f"SimplicityGuy/service-{i:03d}" for i in range(40)]

        queries = build_search_queries(repos)

        assert all(len(query) <= DEFAULT_MAX_QUERY_LENGTH for query in queries)
        assert len(queries) > 1  # 40 repos can't fit in one 256-char query
        flattened = [repo for query in queries for repo in re.findall(r"repo:(\S+)", query)]
        assert flattened == repos  # every repo present, exactly once, in order


# --- PRRecord -------------------------------------------------------------------------


class TestPRRecordFromPull:
    def test_extracts_full_pr_fields(self) -> None:
        record = PRRecord.from_pull("acme/widgets", pull_payload(number=7))

        assert record == PRRecord(
            repo="acme/widgets",
            number=7,
            title="Bump foo from 1.0.0 to 1.0.1",
            html_url="https://github.com/acme/widgets/pull/7",
            author="dependabot[bot]",
            draft=False,
            head_sha="abc123",
            head_ref="dependabot/pip/foo-1.0.1",
            base_ref="main",
            mergeable=True,
            mergeable_state="clean",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

    def test_missing_optional_fields_default(self) -> None:
        payload = pull_payload(number=1)
        del payload["draft"]
        del payload["mergeable"]
        del payload["mergeable_state"]

        record = PRRecord.from_pull("acme/widgets", payload)

        assert record.draft is False
        assert record.mergeable is None
        assert record.mergeable_state is None


# --- DiscoveryService -------------------------------------------------------------------


class TestDiscoveryServiceDiscover:
    async def test_returns_hydrated_dependabot_prs_keyed_by_repo(self) -> None:
        fleet = fleet_of("acme/widgets", "acme/gadgets")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                return httpx.Response(
                    200,
                    json={"items": [search_hit(repo="acme/widgets", number=42)]},
                )
            assert request.url.path == "/repos/acme/widgets/pulls/42"
            return httpx.Response(200, json=pull_payload(number=42))

        client = make_client(handler)
        service = DiscoveryService(client, fleet)

        records = await service.discover()

        assert set(records) == {"acme/widgets", "acme/gadgets"}
        assert records["acme/gadgets"] == []
        assert len(records["acme/widgets"]) == 1
        record = records["acme/widgets"][0]
        assert record.number == 42
        assert record.repo == "acme/widgets"
        assert record.head_sha == "abc123"
        assert record.mergeable is True

    async def test_excludes_non_dependabot_hits_defensively(self) -> None:
        fleet = fleet_of("acme/widgets")
        hydrate_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hydrate_calls
            if request.url.path == "/search/issues":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            search_hit(repo="acme/widgets", number=1, login="dependabot[bot]"),
                            search_hit(repo="acme/widgets", number=2, login="some-human"),
                        ]
                    },
                )
            hydrate_calls += 1
            return httpx.Response(200, json=pull_payload(number=1))

        client = make_client(handler)
        service = DiscoveryService(client, fleet)

        records = await service.discover()

        assert len(records["acme/widgets"]) == 1
        assert records["acme/widgets"][0].number == 1
        assert hydrate_calls == 1  # the non-dependabot hit was never hydrated

    async def test_skips_a_pr_that_404s_on_hydration(self) -> None:
        fleet = fleet_of("acme/widgets")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                return httpx.Response(
                    200, json={"items": [search_hit(repo="acme/widgets", number=99)]}
                )
            return httpx.Response(404, json={"message": "Not Found"})

        client = make_client(handler)
        service = DiscoveryService(client, fleet)

        records = await service.discover()

        assert records["acme/widgets"] == []

    async def test_empty_fleet_returns_empty_mapping_without_any_requests(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"items": []})

        client = make_client(handler)
        service = DiscoveryService(client, fleet_of())

        records = await service.discover()

        assert records == {}
        assert calls == 0

    async def test_queries_are_chunked_across_multiple_search_requests(self) -> None:
        repos = [f"acme/repo-{i:03d}" for i in range(20)]
        fleet = fleet_of(*repos)
        search_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal search_calls
            if request.url.path == "/search/issues":
                search_calls += 1
                return httpx.Response(200, json={"items": []})
            return httpx.Response(404, json={"message": "Not Found"})

        client = make_client(handler)
        # Force a tiny budget so 20 repos can't possibly fit in one query.
        service = DiscoveryService(client, fleet, max_query_length=80)

        await service.discover()

        assert search_calls > 1

    async def test_search_pagination_follows_link_header(self) -> None:
        fleet = fleet_of("acme/widgets")
        page2_url = "https://api.github.com/search/issues?q=x&per_page=100&page=2"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues" and request.url.params.get("page") != "2":
                return httpx.Response(
                    200,
                    json={"items": [search_hit(repo="acme/widgets", number=1)]},
                    headers={"Link": f'<{page2_url}>; rel="next"'},
                )
            if request.url.path == "/search/issues":
                return httpx.Response(
                    200, json={"items": [search_hit(repo="acme/widgets", number=2)]}
                )
            number = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=pull_payload(number=number))

        client = make_client(handler)
        service = DiscoveryService(client, fleet)

        records = await service.discover()

        assert {record.number for record in records["acme/widgets"]} == {1, 2}


class TestDiscoveryServiceCache:
    async def _counting_client(self) -> tuple[GitHubClient, Callable[[], int]]:
        calls = {"search": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search/issues":
                calls["search"] += 1
                return httpx.Response(
                    200, json={"items": [search_hit(repo="acme/widgets", number=1)]}
                )
            return httpx.Response(200, json=pull_payload(number=1))

        return make_client(handler), lambda: calls["search"]

    async def test_second_call_within_ttl_is_served_from_cache(self) -> None:
        client, search_calls = await self._counting_client()
        clock = _FakeClock(start=0.0)
        service = DiscoveryService(client, fleet_of("acme/widgets"), ttl_seconds=60, clock=clock)

        await service.discover()
        clock.advance(10)
        await service.discover()

        assert search_calls() == 1

    async def test_call_after_ttl_expires_refreshes(self) -> None:
        client, search_calls = await self._counting_client()
        clock = _FakeClock(start=0.0)
        service = DiscoveryService(client, fleet_of("acme/widgets"), ttl_seconds=60, clock=clock)

        await service.discover()
        clock.advance(61)
        await service.discover()

        assert search_calls() == 2

    async def test_force_refresh_bypasses_a_still_valid_cache(self) -> None:
        client, search_calls = await self._counting_client()
        clock = _FakeClock(start=0.0)
        service = DiscoveryService(client, fleet_of("acme/widgets"), ttl_seconds=60, clock=clock)

        await service.discover()
        await service.discover(force_refresh=True)

        assert search_calls() == 2

    async def test_refresh_hook_forces_a_refetch(self) -> None:
        client, search_calls = await self._counting_client()
        clock = _FakeClock(start=0.0)
        service = DiscoveryService(client, fleet_of("acme/widgets"), ttl_seconds=60, clock=clock)

        await service.discover()
        await service.refresh()

        assert search_calls() == 2


class _FakeClock:
    """A monotonic-clock stand-in whose value only moves when `advance` is called."""

    def __init__(self, *, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now
