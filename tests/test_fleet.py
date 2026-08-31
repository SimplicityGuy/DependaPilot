"""Tests for the fleet dashboard service layer.

`DiscoveryService` and `CIVerdictService` are stood in for with lightweight
fakes (only `FleetService` needs their shape); metadata fetching goes through
the real `fetch_pr_update_metadata`, backed by a `GitHubClient` wired to an
`httpx.MockTransport` -- no real network or `gh` subprocess call anywhere.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from dependapilot.ci import CIStatus, CIVerdict
from dependapilot.discovery import PRRecord
from dependapilot.fleet import (
    DependencySummary,
    FleetService,
    PRRow,
    RepoView,
    compute_fleet_totals,
    parse_bump_title,
    summarize_metadata,
)
from dependapilot.metadata import (
    DependencyType,
    MetadataStatus,
    PRUpdateMetadata,
    SemverUpdateType,
)
from dependapilot.scoring import SafetyBucket, SafetyScore, SignalBreakdown
from tests.github.conftest import make_client

SINGLE_DEP_MESSAGE = """\
Bumps [foo](https://github.com/acme/foo) from 1.0.0 to 1.1.0.

---
updated-dependencies:
- dependency-name: foo
  dependency-type: direct:development
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""


def make_pr(
    *,
    repo: str = "acme/widgets",
    number: int = 1,
    title: str = "Bump foo from 1.0.0 to 1.1.0",
    head_sha: str = "sha1",
    mergeable: bool | None = True,
    mergeable_state: str | None = "clean",
    created_at: str = "2026-08-20T00:00:00Z",
) -> PRRecord:
    return PRRecord(
        repo=repo,
        number=number,
        title=title,
        html_url=f"https://github.com/{repo}/pull/{number}",
        author="dependabot[bot]",
        draft=False,
        head_sha=head_sha,
        head_ref="dependabot/pip/foo-1.1.0",
        base_ref="main",
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        created_at=created_at,
        updated_at=created_at,
    )


def commits_handler(message: str | None, *, status_code: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/commits")
        if status_code != 200:
            return httpx.Response(status_code, json={"message": "boom"})
        if message is None:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "sha": "abc123",
                    "commit": {"message": message},
                    "author": {"login": "dependabot[bot]"},
                }
            ],
        )

    return handler


class FakeDiscovery:
    """Stands in for `DiscoveryService`: only `.discover()` is used by `FleetService`."""

    def __init__(self, prs_by_repo: dict[str, list[PRRecord]]) -> None:
        self.prs_by_repo = prs_by_repo
        self.calls: list[bool] = []

    async def discover(self, *, force_refresh: bool = False) -> dict[str, list[PRRecord]]:
        self.calls.append(force_refresh)
        return self.prs_by_repo


class FakeCIService:
    """Stands in for `CIVerdictService`: only `.get_ci_status()` is used."""

    def __init__(
        self,
        verdict: CIVerdict = CIVerdict.GREEN,
        *,
        raise_for: set[str] | None = None,
    ) -> None:
        self.verdict = verdict
        self.raise_for = raise_for or set()

    async def get_ci_status(self, repo: str, ref: str) -> CIStatus:
        if ref in self.raise_for:
            raise RuntimeError(f"CI lookup failed for {ref}")
        return CIStatus(verdict=self.verdict, checks=[])


def make_service(
    *,
    prs_by_repo: dict[str, list[PRRecord]],
    ci_verdict: CIVerdict = CIVerdict.GREEN,
    ci_raise_for: set[str] | None = None,
    commit_message: str | None = SINGLE_DEP_MESSAGE,
    commits_status_code: int = 200,
) -> tuple[FleetService, FakeDiscovery]:
    client = make_client(commits_handler(commit_message, status_code=commits_status_code))
    discovery = FakeDiscovery(prs_by_repo)
    ci_service = FakeCIService(ci_verdict, raise_for=ci_raise_for)
    service = FleetService(client, discovery, ci_service)  # type: ignore[arg-type]
    return service, discovery


class TestGetFleetView:
    async def test_renders_all_repos_and_prs_with_scores(self) -> None:
        pr = make_pr()
        service, _ = make_service(prs_by_repo={"acme/widgets": [pr]})

        views = await service.get_fleet_view()

        assert len(views) == 1
        view = views[0]
        assert view.repo == "acme/widgets"
        assert view.error is None
        assert len(view.rows) == 1
        row = view.rows[0]
        assert row.error is None
        assert row.metadata is not None
        assert row.metadata.status == MetadataStatus.PARSED
        assert row.ci_status is not None
        assert row.ci_status.verdict == CIVerdict.GREEN
        assert row.safety is not None
        assert row.safety.bucket == SafetyBucket.SAFE
        assert row.summary is not None
        assert row.summary.names == ("foo",)
        assert row.summary.old_version == "1.0.0"
        assert row.summary.new_version == "1.1.0"

    async def test_empty_repo_has_no_rows_and_no_error(self) -> None:
        service, _ = make_service(prs_by_repo={"acme/empty": []})

        views = await service.get_fleet_view()

        assert views[0].repo == "acme/empty"
        assert views[0].rows == ()
        assert views[0].error is None

    async def test_multiple_repos_all_render(self) -> None:
        service, _ = make_service(
            prs_by_repo={
                "acme/widgets": [make_pr(repo="acme/widgets", number=1)],
                "acme/gadgets": [make_pr(repo="acme/gadgets", number=2)],
                "acme/empty": [],
            }
        )

        views = await service.get_fleet_view()

        assert {view.repo for view in views} == {"acme/widgets", "acme/gadgets", "acme/empty"}
        assert all(view.error is None for view in views)

    async def test_one_pr_failure_isolated_within_repo(self) -> None:
        """One PR's CI lookup fails; its sibling in the same repo still renders."""
        good_pr = make_pr(number=1, head_sha="good-sha")
        bad_pr = make_pr(number=2, head_sha="bad-sha")
        client = make_client(commits_handler(SINGLE_DEP_MESSAGE))
        discovery = FakeDiscovery({"acme/widgets": [good_pr, bad_pr]})
        ci_service = FakeCIService(raise_for={"bad-sha"})
        service = FleetService(client, discovery, ci_service)  # type: ignore[arg-type]

        views = await service.get_fleet_view()

        rows_by_number = {row.pr.number: row for row in views[0].rows}
        assert rows_by_number[1].error is None
        assert rows_by_number[1].safety is not None
        assert rows_by_number[2].error is not None
        assert rows_by_number[2].safety is None

    async def test_per_pr_io_failure_does_not_blank_the_page(self) -> None:
        """When the commits endpoint 500s, every affected PR degrades to a
        row-level error -- the repo sections themselves still render (no
        `RepoView.error`, no missing rows) rather than the page going blank.

        `max_retries=0` keeps the test fast -- 5xx responses otherwise retry
        with backoff before the client gives up.
        """
        client = make_client(commits_handler(SINGLE_DEP_MESSAGE, status_code=500), max_retries=0)
        discovery = FakeDiscovery(
            {
                "acme/broken": [make_pr(repo="acme/broken")],
                "acme/also-broken": [make_pr(repo="acme/also-broken", number=9)],
            }
        )
        service = FleetService(client, discovery, FakeCIService())  # type: ignore[arg-type]

        views = await service.get_fleet_view()

        for view in views:
            assert view.error is None
            assert len(view.rows) == 1
            assert view.rows[0].error is not None
            assert view.rows[0].safety is None

    async def test_unexpected_repo_build_failure_is_caught_as_section_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bug in the pure scoring/summarizing step (not an I/O failure) is
        a safety net at the repo level -- it degrades that section without
        blanking the rest of the page."""
        import dependapilot.fleet as fleet_module

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("scoring blew up")

        monkeypatch.setattr(fleet_module, "score_pr", boom)
        service, _ = make_service(
            prs_by_repo={
                "acme/broken": [make_pr(repo="acme/broken")],
                "acme/fine": [],
            }
        )

        views = await service.get_fleet_view()

        by_repo = {view.repo: view for view in views}
        assert by_repo["acme/broken"].error is not None
        assert by_repo["acme/broken"].rows == ()
        assert by_repo["acme/fine"].error is None

    async def test_force_refresh_forwards_to_discovery(self) -> None:
        service, discovery = make_service(prs_by_repo={"acme/widgets": []})

        await service.get_fleet_view()
        await service.get_fleet_view(force_refresh=True)

        assert discovery.calls == [False, True]

    async def test_closes_open_alert_is_always_omitted(self) -> None:
        """No source of alert-closure facts exists yet -- the signal must
        never appear, matching `score_pr`'s "omit unless known" contract."""
        service, _ = make_service(prs_by_repo={"acme/widgets": [make_pr()]})

        views = await service.get_fleet_view()

        breakdown_signals = {entry.signal for entry in views[0].rows[0].safety.breakdown}  # type: ignore[union-attr]
        assert "closes_open_alert" not in breakdown_signals

    async def test_audit_enabled_flag_is_carried_through(self) -> None:
        client = make_client(commits_handler(SINGLE_DEP_MESSAGE))
        discovery = FakeDiscovery({"acme/widgets": [], "acme/other": []})
        service = FleetService(
            client,
            discovery,  # type: ignore[arg-type]
            FakeCIService(),  # type: ignore[arg-type]
            audit_enabled_repos=frozenset({"acme/widgets"}),
        )

        views = await service.get_fleet_view()

        by_repo = {view.repo: view.audit_enabled for view in views}
        assert by_repo == {"acme/widgets": True, "acme/other": False}

    async def test_actions_enabled_flag_is_carried_through(self) -> None:
        client = make_client(commits_handler(SINGLE_DEP_MESSAGE))
        discovery = FakeDiscovery({"acme/widgets": [], "acme/other": []})
        service = FleetService(
            client,
            discovery,  # type: ignore[arg-type]
            FakeCIService(),  # type: ignore[arg-type]
            actions_enabled_repos=frozenset({"acme/widgets"}),
        )

        views = await service.get_fleet_view()

        by_repo = {view.repo: view.actions_enabled for view in views}
        assert by_repo == {"acme/widgets": True, "acme/other": False}


class TestFleetScale:
    async def test_forty_repos_150_prs_completes_quickly_from_warm_cache(self) -> None:
        """~40 repos / ~150 PRs, per-repo and per-PR work fanned out with
        asyncio.gather, must render well under a second against a warm
        (already-mocked, zero-latency) backend."""
        prs_by_repo = {
            f"acme/repo-{repo_index}": [
                make_pr(repo=f"acme/repo-{repo_index}", number=pr_index)
                for pr_index in range(1, 5 if repo_index < 30 else 4)
            ]
            for repo_index in range(40)
        }
        total_prs = sum(len(prs) for prs in prs_by_repo.values())
        assert total_prs == 150

        service, _ = make_service(prs_by_repo=prs_by_repo)

        start = time.monotonic()
        views = await service.get_fleet_view()
        elapsed = time.monotonic() - start

        assert len(views) == 40
        assert sum(len(view.rows) for view in views) == total_prs
        assert all(view.error is None for view in views)
        assert elapsed < 1.0


class TestPRRowAgeDays:
    def test_age_in_whole_days_from_created_at(self) -> None:
        created_at = (datetime.now(UTC) - timedelta(days=5, hours=1)).isoformat()
        row = PRRow(pr=make_pr(created_at=created_at))

        assert row.age_days == 5

    def test_unparseable_created_at_degrades_to_none(self) -> None:
        row = PRRow(pr=make_pr(created_at="not-a-timestamp"))

        assert row.age_days is None


class TestPRRowClosesOpenAlert:
    def test_true_when_signal_present_with_positive_delta(self) -> None:
        safety = SafetyScore(
            score=90,
            bucket=SafetyBucket.SAFE,
            breakdown=(
                SignalBreakdown("closes_open_alert", 10, "closes an open Dependabot alert"),
            ),
            stale=False,
        )
        row = PRRow(pr=make_pr(), safety=safety)

        assert row.closes_open_alert is True

    def test_false_when_signal_present_with_zero_delta(self) -> None:
        safety = SafetyScore(
            score=80,
            bucket=SafetyBucket.SAFE,
            breakdown=(
                SignalBreakdown("closes_open_alert", 0, "does not close an open Dependabot alert"),
            ),
            stale=False,
        )
        row = PRRow(pr=make_pr(), safety=safety)

        assert row.closes_open_alert is False

    def test_false_when_signal_absent(self) -> None:
        safety = SafetyScore(score=80, bucket=SafetyBucket.SAFE, breakdown=(), stale=False)
        row = PRRow(pr=make_pr(), safety=safety)

        assert row.closes_open_alert is False

    def test_false_when_safety_is_none(self) -> None:
        row = PRRow(pr=make_pr(), error="boom")

        assert row.closes_open_alert is False


class TestComputeFleetTotals:
    def _row(self, bucket: SafetyBucket, *, number: int = 1) -> PRRow:
        return PRRow(
            pr=make_pr(number=number),
            summary=DependencySummary(
                names=("foo",),
                semver_labels=("patch",),
                dependency_type_labels=("direct:development",),
                old_version="1.0.0",
                new_version="1.1.0",
            ),
            metadata=PRUpdateMetadata(status=MetadataStatus.PARSED, updates=()),
            ci_status=CIStatus(verdict=CIVerdict.GREEN, checks=[]),
            safety=SafetyScore(score=90, bucket=bucket, breakdown=(), stale=False),
        )

    def test_sums_reachability_prs_and_buckets(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                rows=(
                    self._row(SafetyBucket.SAFE, number=1),
                    self._row(SafetyBucket.CAUTION, number=2),
                    self._row(SafetyBucket.UNSAFE, number=3),
                ),
            ),
            RepoView(repo="acme/broken", error="boom"),
            RepoView(repo="acme/empty", rows=()),
        )

        totals = compute_fleet_totals(views)

        assert totals.repos == 3
        assert totals.repos_reachable == 2
        assert totals.repos_errored == 1
        assert totals.open_prs == 3
        assert totals.safe == 1
        assert totals.caution == 1
        assert totals.unsafe == 1

    def test_row_level_errors_and_unscored_rows_are_excluded_from_buckets(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                rows=(
                    self._row(SafetyBucket.SAFE, number=1),
                    PRRow(pr=make_pr(number=2), error="404"),
                ),
            ),
        )

        totals = compute_fleet_totals(views)

        assert totals.open_prs == 2
        assert totals.safe == 1
        assert totals.caution == 0
        assert totals.unsafe == 0

    def test_empty_fleet(self) -> None:
        totals = compute_fleet_totals(())

        assert totals == compute_fleet_totals([])
        assert totals.repos == 0
        assert totals.open_prs == 0


class TestParseBumpTitle:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Bump foo from 1.0.0 to 1.1.0", ("1.0.0", "1.1.0")),
            ("Bump foo from 1.0.0 to 1.1.0 in /app", ("1.0.0", "1.1.0")),
            ("Bump the npm-deps group with 2 updates", (None, None)),
            ("", (None, None)),
        ],
    )
    def test_extracts_versions_or_degrades_to_none(
        self, title: str, expected: tuple[str | None, str | None]
    ) -> None:
        assert parse_bump_title(title) == expected


class TestSummarizeMetadata:
    def test_empty_updates_degrades_to_empty_names(self) -> None:
        metadata = PRUpdateMetadata(status=MetadataStatus.TRAILER_MISSING, updates=())

        summary = summarize_metadata(metadata, "Bump foo from 1.0.0 to 1.1.0")

        assert summary.names == ()
        assert summary.semver_labels == ()
        assert summary.old_version == "1.0.0"
        assert summary.new_version == "1.1.0"

    def test_deduplicates_labels_across_grouped_updates(self) -> None:
        from dependapilot.metadata import DependencyUpdate

        metadata = PRUpdateMetadata(
            status=MetadataStatus.PARSED,
            updates=(
                DependencyUpdate("foo", DependencyType.DIRECT_PRODUCTION, SemverUpdateType.PATCH),
                DependencyUpdate("bar", DependencyType.DIRECT_PRODUCTION, SemverUpdateType.PATCH),
            ),
        )

        summary = summarize_metadata(metadata, "Bump the group with 2 updates")

        assert summary.names == ("foo", "bar")
        assert summary.semver_labels == ("patch",)
        assert summary.dependency_type_labels == ("direct:production",)
