"""End-to-end tests for the fleet dashboard routes, against a fully mocked
`FleetService` (no GitHub client, no real async services) -- these tests
exercise only the FastAPI wiring and Jinja2 rendering.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from dependapilot.app import create_app
from dependapilot.ci import CIStatus, CIVerdict
from dependapilot.discovery import PRRecord
from dependapilot.fleet import DependencySummary, PRRow, RepoView
from dependapilot.metadata import MetadataStatus, PRUpdateMetadata
from dependapilot.scoring import SafetyBucket, SafetyScore, SignalBreakdown


def make_pr(*, repo: str = "acme/widgets", number: int = 1) -> PRRecord:
    return PRRecord(
        repo=repo,
        number=number,
        title="Bump foo from 1.0.0 to 1.1.0",
        html_url=f"https://github.com/{repo}/pull/{number}",
        author="dependabot[bot]",
        draft=False,
        head_sha="sha1",
        head_ref="dependabot/pip/foo-1.1.0",
        base_ref="main",
        mergeable=True,
        mergeable_state="clean",
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-20T00:00:00Z",
    )


def make_row(*, repo: str = "acme/widgets", number: int = 1, error: str | None = None) -> PRRow:
    if error is not None:
        return PRRow(pr=make_pr(repo=repo, number=number), error=error)
    return PRRow(
        pr=make_pr(repo=repo, number=number),
        summary=DependencySummary(
            names=("foo",),
            semver_labels=("patch",),
            dependency_type_labels=("direct:development",),
            old_version="1.0.0",
            new_version="1.1.0",
        ),
        metadata=PRUpdateMetadata(status=MetadataStatus.PARSED, updates=()),
        ci_status=CIStatus(verdict=CIVerdict.GREEN, checks=[]),
        safety=SafetyScore(
            score=95,
            bucket=SafetyBucket.SAFE,
            breakdown=(
                SignalBreakdown("ci_verdict", 10, "CI is green"),
                SignalBreakdown("semver", 15, "patch update"),
            ),
            stale=False,
        ),
    )


class FakeFleetService:
    def __init__(self, views: tuple[RepoView, ...]) -> None:
        self.views = views
        self.calls: list[bool] = []

    async def get_fleet_view(self, *, force_refresh: bool = False) -> tuple[RepoView, ...]:
        self.calls.append(force_refresh)
        return self.views


def client_for(fleet_service: object) -> TestClient:
    return TestClient(create_app(fleet_service))  # type: ignore[arg-type]


class TestIndexShell:
    def test_index_renders_without_touching_fleet_service(self) -> None:
        client = client_for(FakeFleetService(()))

        response = client.get("/")

        assert response.status_code == 200
        assert "DependaPilot" in response.text
        assert 'hx-get="/fleet"' in response.text

    def test_index_renders_even_when_fleet_service_is_none(self) -> None:
        client = TestClient(create_app())

        response = client.get("/")

        assert response.status_code == 200
        assert "DependaPilot" in response.text


class TestFleetPartial:
    def test_renders_all_repos_and_prs_with_badges(self) -> None:
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(),)),
            RepoView(repo="acme/gadgets", rows=(make_row(repo="acme/gadgets", number=2),)),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "acme/widgets" in response.text
        assert "acme/gadgets" in response.text
        assert "badge-ci-green" in response.text
        assert "badge-bucket-safe" in response.text
        assert "badge-semver-patch" in response.text
        assert "1.0.0" in response.text and "1.1.0" in response.text

    def test_score_breakdown_is_present_for_expansion(self) -> None:
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(make_row(),)),)))

        response = client.get("/fleet")

        assert "<details>" in response.text
        assert "<summary>" in response.text
        assert "CI is green" in response.text
        assert "patch update" in response.text

    def test_empty_repo_shows_empty_state(self) -> None:
        client = client_for(FakeFleetService((RepoView(repo="acme/quiet", rows=()),)))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "acme/quiet" in response.text
        assert "No open Dependabot pull requests" in response.text

    def test_no_repos_shows_fleet_empty_state(self) -> None:
        client = client_for(FakeFleetService(()))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "No repositories are configured" in response.text

    def test_repo_level_error_renders_inline_without_blanking_page(self) -> None:
        views = (
            RepoView(repo="acme/broken", error="GitHub API request failed: 404"),
            RepoView(repo="acme/fine", rows=(make_row(repo="acme/fine", number=2),)),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "acme/broken" in response.text
        assert "Could not load this repo" in response.text
        assert "acme/fine" in response.text
        assert "badge-bucket-safe" in response.text

    def test_row_level_error_renders_inline(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                rows=(make_row(), make_row(number=2, error="404 not found")),
            ),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "PR #2: 404 not found" in response.text
        assert "badge-bucket-safe" in response.text

    def test_stale_flag_rendered(self) -> None:
        row = make_row()
        stale_row = PRRow(
            pr=row.pr,
            summary=row.summary,
            metadata=row.metadata,
            ci_status=row.ci_status,
            safety=SafetyScore(
                score=60,
                bucket=SafetyBucket.CAUTION,
                breakdown=row.safety.breakdown,
                stale=True,  # type: ignore[union-attr]
            ),
        )
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(stale_row,)),)))

        response = client.get("/fleet")

        assert "Stale" in response.text

    def test_refresh_forwards_force_refresh_to_service(self) -> None:
        service = FakeFleetService(())
        client = client_for(service)

        client.get("/fleet")
        client.get("/fleet?refresh=true")

        assert service.calls == [False, True]

    def test_unconfigured_service_renders_inline_message(self) -> None:
        client = TestClient(create_app())

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "not configured" in response.text
