"""End-to-end tests for the fleet dashboard routes, against a fully mocked
`FleetService` (no GitHub client, no real async services) -- these tests
exercise only the FastAPI wiring and Jinja2 rendering.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from dependapilot.app import create_app
from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.audit_service import RepoAuditView
from dependapilot.ci import CIStatus, CIVerdict
from dependapilot.discovery import PRRecord
from dependapilot.fleet import DependencySummary, PRRow, RepoView
from dependapilot.metadata import MetadataStatus, PRUpdateMetadata
from dependapilot.scoring import SafetyBucket, SafetyScore, SignalBreakdown


def days_ago(n: int) -> str:
    """An ISO-8601 timestamp `n` days before now -- used wherever a test cares
    about the derived `age_days`, so it stays correct regardless of when the
    suite runs (unlike a hard-coded calendar date)."""
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


def stat_numbers(html: str) -> list[int]:
    """Every stat-strip tile's number, in document order (Repos, Open PRs,
    Safe, Caution, Unsafe)."""
    return [int(n) for n in re.findall(r'class="stattile-number"[^>]*>\s*(\d+)\s*<', html)]


def make_pr(
    *, repo: str = "acme/widgets", number: int = 1, created_at: str | None = None
) -> PRRecord:
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
        created_at=created_at or days_ago(3),
        updated_at=created_at or days_ago(3),
    )


def make_row(
    *,
    repo: str = "acme/widgets",
    number: int = 1,
    error: str | None = None,
    created_at: str | None = None,
) -> PRRow:
    if error is not None:
        return PRRow(pr=make_pr(repo=repo, number=number, created_at=created_at), error=error)
    return PRRow(
        pr=make_pr(repo=repo, number=number, created_at=created_at),
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
    def test_renders_all_repos_and_prs_with_the_design_language(self) -> None:
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(),)),
            RepoView(repo="acme/gadgets", rows=(make_row(repo="acme/gadgets", number=2),)),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "acme/widgets" in response.text
        assert "acme/gadgets" in response.text
        assert "badge-" not in response.text
        assert "ci-cell-safe" in response.text and "passing" in response.text
        assert "chip-safe" in response.text and "SAFE" in response.text
        assert "patch" in response.text
        assert "1.0.0" in response.text and "1.1.0" in response.text

    def test_score_breakdown_is_present_for_expansion(self) -> None:
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(make_row(),)),)))

        response = client.get("/fleet")

        assert "<details>" in response.text
        assert "<summary" in response.text
        assert "ledger" in response.text
        assert "CI is green" in response.text
        assert "patch update" in response.text

    def test_empty_repo_shows_empty_state(self) -> None:
        client = client_for(FakeFleetService((RepoView(repo="acme/quiet", rows=()),)))

        response = client.get("/fleet")

        assert response.status_code == 200
        # the repo slug is split across two spans (muted owner / bright name),
        # so "acme/quiet" isn't a contiguous substring of the markup.
        assert "acme" in response.text and "quiet" in response.text
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
        assert "acme" in response.text and "broken" in response.text
        assert "error-banner" in response.text
        assert "Could not load this repo's PRs" in response.text
        assert "GitHub API request failed: 404" in response.text
        assert "fine" in response.text
        assert "SAFE" in response.text

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
        assert "SAFE" in response.text

    def test_stale_flag_rendered_as_a_caution_chip(self) -> None:
        row = make_row()
        stale_row = PRRow(
            pr=row.pr,
            summary=row.summary,
            metadata=row.metadata,
            ci_status=row.ci_status,
            safety=SafetyScore(
                score=60,
                bucket=SafetyBucket.CAUTION,
                breakdown=row.safety.breakdown,  # type: ignore[union-attr]
                stale=True,
            ),
        )
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(stale_row,)),)))

        response = client.get("/fleet")

        assert "stale" in response.text
        assert "chip-caution" in response.text

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


class TestFleetAge:
    def test_age_renders_in_days_from_created_at(self) -> None:
        row = make_row(created_at=days_ago(5))
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(row,)),)))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "5d" in response.text

    def test_a_fresh_pr_renders_zero_days(self) -> None:
        row = make_row(created_at=days_ago(0))
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(row,)),)))

        response = client.get("/fleet")

        assert "0d" in response.text


class TestFleetStatStrip:
    def test_totals_sum_from_the_rendered_rows(self) -> None:
        caution_row = PRRow(
            pr=make_pr(number=2),
            summary=DependencySummary(
                names=("bar",),
                semver_labels=("minor",),
                dependency_type_labels=("indirect",),
                old_version="1.0.0",
                new_version="1.1.0",
            ),
            metadata=PRUpdateMetadata(status=MetadataStatus.PARSED, updates=()),
            ci_status=CIStatus(verdict=CIVerdict.PENDING, checks=[]),
            safety=SafetyScore(score=60, bucket=SafetyBucket.CAUTION, breakdown=(), stale=False),
        )
        unsafe_row = PRRow(
            pr=make_pr(number=3),
            summary=DependencySummary(
                names=("baz",),
                semver_labels=("major",),
                dependency_type_labels=("direct:production",),
                old_version="1.0.0",
                new_version="2.0.0",
            ),
            metadata=PRUpdateMetadata(status=MetadataStatus.PARSED, updates=()),
            ci_status=CIStatus(verdict=CIVerdict.FAILING, checks=[]),
            safety=SafetyScore(score=10, bucket=SafetyBucket.UNSAFE, breakdown=(), stale=False),
        )
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(number=1), caution_row, unsafe_row)),
            RepoView(repo="acme/other", rows=()),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        # order: Repos, Open PRs, Safe, Caution, Unsafe
        assert stat_numbers(response.text) == [2, 3, 1, 1, 1]

    def test_a_repo_error_counts_toward_errored_but_contributes_no_prs(self) -> None:
        views = (
            RepoView(repo="acme/broken", error="boom"),
            RepoView(repo="acme/fine", rows=(make_row(),)),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert stat_numbers(response.text) == [2, 1, 1, 0, 0]

    def test_no_stat_strip_when_unconfigured(self) -> None:
        client = TestClient(create_app())

        response = client.get("/fleet")

        assert "stattile-number" not in response.text


class TestFleetMergeButton:
    def test_merge_disabled_exactly_when_ci_is_not_green(self) -> None:
        row = PRRow(
            pr=make_pr(),
            summary=DependencySummary(
                names=("foo",),
                semver_labels=("patch",),
                dependency_type_labels=("direct:development",),
                old_version="1.0.0",
                new_version="1.1.0",
            ),
            metadata=PRUpdateMetadata(status=MetadataStatus.PARSED, updates=()),
            ci_status=CIStatus(verdict=CIVerdict.FAILING, checks=[]),
            safety=SafetyScore(score=10, bucket=SafetyBucket.UNSAFE, breakdown=(), stale=False),
        )
        views = (RepoView(repo="acme/widgets", actions_enabled=True, rows=(row,)),)
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "CI must be green to merge" in response.text
        assert "btn-disabled" in response.text
        assert " disabled" in response.text

    def test_merge_enabled_when_ci_is_green(self) -> None:
        views = (RepoView(repo="acme/widgets", actions_enabled=True, rows=(make_row(),)),)
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "CI must be green to merge" not in response.text


class TestClosesAlertChip:
    def _row_with_alert_signal(self, delta: int, reason: str) -> PRRow:
        return PRRow(
            pr=make_pr(),
            summary=DependencySummary(
                names=("urllib3",),
                semver_labels=("minor",),
                dependency_type_labels=("indirect",),
                old_version="2.2.3",
                new_version="2.5.0",
            ),
            metadata=PRUpdateMetadata(status=MetadataStatus.PARSED, updates=()),
            ci_status=CIStatus(verdict=CIVerdict.GREEN, checks=[]),
            safety=SafetyScore(
                score=90,
                bucket=SafetyBucket.SAFE,
                breakdown=(SignalBreakdown("closes_open_alert", delta, reason),),
                stale=False,
            ),
        )

    def test_chip_appears_when_the_signal_has_a_positive_delta(self) -> None:
        row = self._row_with_alert_signal(10, "closes an open Dependabot alert")
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(row,)),)))

        response = client.get("/fleet")

        assert "closes alert" in response.text

    def test_chip_absent_when_the_signal_has_no_positive_delta(self) -> None:
        row = self._row_with_alert_signal(0, "does not close an open Dependabot alert")
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(row,)),)))

        response = client.get("/fleet")

        assert "closes alert" not in response.text

    def test_chip_absent_when_the_signal_is_omitted_entirely(self) -> None:
        client = client_for(FakeFleetService((RepoView(repo="acme/widgets", rows=(make_row(),)),)))

        response = client.get("/fleet")

        assert "closes alert" not in response.text


class FakeAuditService:
    """Stands in for `AuditService`: only `.get_audit_view()` is used by `/fleet`."""

    def __init__(self, views: tuple[RepoAuditView, ...]) -> None:
        self.views = views

    async def get_audit_view(self, *, force_refresh: bool = False) -> tuple[RepoAuditView, ...]:
        return self.views


class TestFleetAuditBadge:
    def test_repo_without_audit_enabled_shows_off(self) -> None:
        views = (RepoView(repo="acme/widgets", audit_enabled=False, rows=()),)
        client = TestClient(
            create_app(FakeFleetService(views), audit_service=FakeAuditService(()))  # type: ignore[arg-type]
        )

        response = client.get("/fleet")

        assert "chip-neutral" in response.text
        assert "audit off" in response.text

    def test_compliant_audited_repo_shows_ok(self) -> None:
        views = (RepoView(repo="acme/widgets", audit_enabled=True, rows=()),)
        audit_views = (RepoAuditView(repo="acme/widgets", findings=()),)
        client = TestClient(
            create_app(
                FakeFleetService(views),  # type: ignore[arg-type]
                audit_service=FakeAuditService(audit_views),  # type: ignore[arg-type]
            )
        )

        response = client.get("/fleet")

        assert "chip-safe" in response.text
        assert "audit ok" in response.text

    def test_repo_with_findings_shows_the_count(self) -> None:
        views = (RepoView(repo="acme/widgets", audit_enabled=True, rows=()),)
        audit_views = (
            RepoAuditView(
                repo="acme/widgets",
                findings=(
                    Finding(
                        repo="acme/widgets",
                        check=Check.MISSING_CONFIG,
                        severity=Severity.HIGH,
                        message="no config",
                    ),
                ),
            ),
        )
        client = TestClient(
            create_app(
                FakeFleetService(views),  # type: ignore[arg-type]
                audit_service=FakeAuditService(audit_views),  # type: ignore[arg-type]
            )
        )

        response = client.get("/fleet")

        assert "chip-caution" in response.text
        assert "1 finding" in response.text

    def test_degraded_scope_repo_shows_unknown(self) -> None:
        views = (RepoView(repo="acme/widgets", audit_enabled=True, rows=()),)
        audit_views = (
            RepoAuditView(
                repo="acme/widgets",
                findings=(
                    Finding(
                        repo="acme/widgets",
                        check=Check.ALERTS_UNKNOWN,
                        severity=Severity.INFO,
                        message="unknown",
                    ),
                ),
            ),
        )
        client = TestClient(
            create_app(
                FakeFleetService(views),  # type: ignore[arg-type]
                audit_service=FakeAuditService(audit_views),  # type: ignore[arg-type]
            )
        )

        response = client.get("/fleet")

        assert "chip-neutral" in response.text
        assert "audit unknown" in response.text

    def test_errored_audit_shows_error_badge(self) -> None:
        views = (RepoView(repo="acme/widgets", audit_enabled=True, rows=()),)
        audit_views = (RepoAuditView(repo="acme/widgets", error="boom"),)
        client = TestClient(
            create_app(
                FakeFleetService(views),  # type: ignore[arg-type]
                audit_service=FakeAuditService(audit_views),  # type: ignore[arg-type]
            )
        )

        response = client.get("/fleet")

        assert "chip-unsafe" in response.text
        assert "audit error" in response.text

    def test_audit_enabled_repo_without_a_configured_audit_service_shows_off(self) -> None:
        views = (RepoView(repo="acme/widgets", audit_enabled=True, rows=()),)
        client = TestClient(create_app(FakeFleetService(views)))  # type: ignore[arg-type]

        response = client.get("/fleet")

        assert "chip-neutral" in response.text
        assert "audit off" in response.text


class FakeActionsService:
    """Stands in for `ActionsService`: `/fleet` only checks its presence."""


class TestFleetActionsBadge:
    def test_repo_with_actions_disabled_shows_off_badge_with_remediation_tooltip(self) -> None:
        views = (RepoView(repo="acme/widgets", actions_enabled=False, rows=()),)
        client = TestClient(
            create_app(
                FakeFleetService(views),  # type: ignore[arg-type]
                actions_service=FakeActionsService(),  # type: ignore[arg-type]
            )
        )

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "actions off" in response.text
        assert "Enable actions: true for this repo in repos.yml" in response.text

    def test_repo_with_actions_enabled_shows_no_actions_badge(self) -> None:
        views = (RepoView(repo="acme/widgets", actions_enabled=True, rows=()),)
        client = TestClient(
            create_app(
                FakeFleetService(views),  # type: ignore[arg-type]
                actions_service=FakeActionsService(),  # type: ignore[arg-type]
            )
        )

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "actions off" not in response.text

    def test_actions_service_not_configured_renders_a_distinct_variant(self) -> None:
        views = (RepoView(repo="acme/widgets", actions_enabled=False, rows=()),)
        client = TestClient(create_app(FakeFleetService(views)))  # type: ignore[arg-type]

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "actions off" in response.text
        assert "Actions service is not configured" in response.text
        assert "Enable actions: true for this repo in repos.yml" not in response.text


class TestFleetBulkSelectCheckboxes:
    def test_actions_enabled_repo_rows_get_a_select_checkbox(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                actions_enabled=True,
                rows=(make_row(), make_row(number=2)),
            ),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert 'class="bulk-select-row"' in response.text
        assert 'value="acme/widgets#1"' in response.text
        assert 'value="acme/widgets#2"' in response.text
        assert 'class="bulk-select-all"' in response.text

    def test_actions_disabled_repo_rows_get_no_select_checkbox(self) -> None:
        views = (RepoView(repo="acme/widgets", actions_enabled=False, rows=(make_row(),)),)
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        assert response.status_code == 200
        assert "bulk-select-row" not in response.text
        assert "bulk-select-all" not in response.text

    def test_select_all_toggles_the_repos_row_checkboxes(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                actions_enabled=True,
                rows=(make_row(), make_row(number=2)),
            ),
        )
        client = client_for(FakeFleetService(views))

        response = client.get("/fleet")

        # Template-level check of the toggle wiring, not a browser JS run.
        assert "bulk-select-all" in response.text
        assert "querySelectorAll('.bulk-select-row')" in response.text
        assert "cb.checked = this.checked" in response.text
