"""Tests for bulk approve/merge-all: eligibility, preview, and sequential
execution -- against fully faked `FleetService`/`ActionsService` collaborators
(each already covered by its own unit tests), no real network anywhere.
"""

from __future__ import annotations

import pytest

from dependapilot.actions import ActionOutcome, ActionResult
from dependapilot.bulk import BulkOutcome, execute_bulk, is_eligible, preview_bulk
from dependapilot.ci import CIStatus, CIVerdict
from dependapilot.discovery import PRRecord
from dependapilot.fleet import PRRow, RepoView
from dependapilot.scoring import SafetyBucket, SafetyScore


def make_pr(*, repo: str = "acme/widgets", number: int = 1, head_sha: str = "sha1") -> PRRecord:
    return PRRecord(
        repo=repo,
        number=number,
        title=f"Bump foo to {number}",
        html_url=f"https://github.com/{repo}/pull/{number}",
        author="dependabot[bot]",
        draft=False,
        head_sha=head_sha,
        head_ref="dependabot/pip/foo",
        base_ref="main",
        mergeable=True,
        mergeable_state="clean",
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-20T00:00:00Z",
    )


def make_row(
    *,
    repo: str = "acme/widgets",
    number: int = 1,
    head_sha: str = "sha1",
    verdict: CIVerdict | None = CIVerdict.GREEN,
    bucket: SafetyBucket | None = SafetyBucket.SAFE,
    error: str | None = None,
) -> PRRow:
    if error is not None:
        return PRRow(pr=make_pr(repo=repo, number=number, head_sha=head_sha), error=error)
    ci_status = CIStatus(verdict=verdict, checks=[]) if verdict is not None else None
    safety = (
        SafetyScore(score=90, bucket=bucket, breakdown=(), stale=False)
        if bucket is not None
        else None
    )
    return PRRow(
        pr=make_pr(repo=repo, number=number, head_sha=head_sha),
        ci_status=ci_status,
        safety=safety,
    )


class FakeFleetService:
    """Stands in for `FleetService`: `get_fleet_view` replays a scripted
    sequence of results, one per call -- so a test can simulate CI regressing
    between a "preview" call and the fresh re-check `execute_bulk` performs.
    """

    def __init__(self, *views_sequence: tuple[RepoView, ...]) -> None:
        self._sequence = list(views_sequence)
        self.calls = 0

    async def get_fleet_view(self, *, force_refresh: bool = False) -> tuple[RepoView, ...]:
        self.calls += 1
        if len(self._sequence) > 1:
            return self._sequence.pop(0)
        return self._sequence[0]


class FakeActionsService:
    """Stands in for `ActionsService`: records calls and returns canned outcomes."""

    def __init__(self, *, fail_for: set[tuple[str, int]] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.fail_for = fail_for or set()

    async def approve(self, repo: str, number: int) -> ActionResult:
        self.calls.append(("approve", repo, number))
        if (repo, number) in self.fail_for:
            return ActionResult(repo, number, "approve", ActionOutcome.FAILED, "boom")
        return ActionResult(repo, number, "approve", ActionOutcome.APPROVED)

    async def merge(self, repo: str, number: int, head_sha: str) -> ActionResult:
        self.calls.append(("merge", repo, number))
        if (repo, number) in self.fail_for:
            return ActionResult(repo, number, "merge", ActionOutcome.FAILED, "boom")
        return ActionResult(repo, number, "merge", ActionOutcome.MERGED)


class TestIsEligible:
    def test_green_and_safe_is_eligible(self) -> None:
        decision = is_eligible(make_row())
        assert decision.eligible is True
        assert decision.reason is None

    def test_non_green_ci_is_ineligible(self) -> None:
        decision = is_eligible(make_row(verdict=CIVerdict.PENDING))
        assert decision.eligible is False
        assert "CI is not green" in decision.reason  # type: ignore[arg-type]

    def test_caution_bucket_ineligible_at_default_safe_threshold(self) -> None:
        decision = is_eligible(make_row(bucket=SafetyBucket.CAUTION))
        assert decision.eligible is False
        assert "caution" in decision.reason  # type: ignore[operator]

    def test_caution_bucket_eligible_when_threshold_widened(self) -> None:
        decision = is_eligible(
            make_row(bucket=SafetyBucket.CAUTION), min_bucket=SafetyBucket.CAUTION
        )
        assert decision.eligible is True

    def test_unsafe_bucket_ineligible_even_when_widened_to_caution(self) -> None:
        decision = is_eligible(
            make_row(bucket=SafetyBucket.UNSAFE), min_bucket=SafetyBucket.CAUTION
        )
        assert decision.eligible is False

    def test_row_error_is_ineligible(self) -> None:
        decision = is_eligible(make_row(error="404 not found"))
        assert decision.eligible is False
        assert "failed to load" in decision.reason  # type: ignore[operator]


class TestPreviewBulk:
    async def test_splits_eligible_and_skipped_with_reasons(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                rows=(
                    make_row(number=1),
                    make_row(number=2, verdict=CIVerdict.FAILING),
                ),
            ),
        )
        service = FakeFleetService(views)

        preview = await preview_bulk(service, action="approve")

        assert len(preview.eligible) == 1
        assert preview.eligible[0].pr.number == 1
        assert len(preview.skipped) == 1
        assert preview.skipped[0].number == 2
        assert preview.skipped[0].reason is not None

    async def test_scoped_to_one_repo_excludes_other_repos(self) -> None:
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(repo="acme/widgets", number=1),)),
            RepoView(repo="acme/gadgets", rows=(make_row(repo="acme/gadgets", number=2),)),
        )
        service = FakeFleetService(views)

        preview = await preview_bulk(service, action="approve", repo="acme/widgets")

        assert len(preview.eligible) == 1
        assert preview.eligible[0].pr.repo == "acme/widgets"

    async def test_fleet_wide_spans_every_repo(self) -> None:
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(repo="acme/widgets", number=1),)),
            RepoView(repo="acme/gadgets", rows=(make_row(repo="acme/gadgets", number=2),)),
        )
        service = FakeFleetService(views)

        preview = await preview_bulk(service, action="merge")

        assert {row.pr.repo for row in preview.eligible} == {"acme/widgets", "acme/gadgets"}

    async def test_caution_included_only_when_explicitly_selected(self) -> None:
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(number=1, bucket=SafetyBucket.CAUTION),)),
        )
        service = FakeFleetService(views)

        default_preview = await preview_bulk(service, action="approve")
        widened_preview = await preview_bulk(
            service, action="approve", min_bucket=SafetyBucket.CAUTION
        )

        assert default_preview.eligible == ()
        assert len(widened_preview.eligible) == 1

    async def test_unknown_action_raises(self) -> None:
        service = FakeFleetService((RepoView(repo="acme/widgets", rows=()),))

        with pytest.raises(ValueError, match="unknown bulk action"):
            await preview_bulk(service, action="delete")


class TestExecuteBulk:
    async def test_executes_only_eligible_prs_and_skips_the_rest_with_reasons(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                rows=(make_row(number=1), make_row(number=2, verdict=CIVerdict.FAILING)),
            ),
        )
        fleet_service = FakeFleetService(views)
        actions_service = FakeActionsService()

        outcome = await execute_bulk(fleet_service, actions_service, action="approve")

        assert isinstance(outcome, BulkOutcome)
        assert [c for c in actions_service.calls] == [("approve", "acme/widgets", 1)]
        assert len(outcome.acted_on) == 1
        assert outcome.acted_on[0].outcome == ActionOutcome.APPROVED
        assert len(outcome.skipped) == 1
        assert outcome.skipped[0].number == 2

    async def test_mid_batch_failure_does_not_stop_the_batch(self) -> None:
        views = (
            RepoView(
                repo="acme/widgets",
                rows=(make_row(number=1), make_row(number=2), make_row(number=3)),
            ),
        )
        fleet_service = FakeFleetService(views)
        actions_service = FakeActionsService(fail_for={("acme/widgets", 2)})

        outcome = await execute_bulk(fleet_service, actions_service, action="merge")

        assert len(actions_service.calls) == 3  # every eligible PR was attempted
        outcomes_by_number = {r.number: r.outcome for r in outcome.acted_on}
        assert outcomes_by_number == {
            1: ActionOutcome.MERGED,
            2: ActionOutcome.FAILED,
            3: ActionOutcome.MERGED,
        }

    async def test_fleet_level_execution_spans_repos(self) -> None:
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(repo="acme/widgets", number=1),)),
            RepoView(repo="acme/gadgets", rows=(make_row(repo="acme/gadgets", number=2),)),
        )
        fleet_service = FakeFleetService(views)
        actions_service = FakeActionsService()

        outcome = await execute_bulk(fleet_service, actions_service, action="approve")

        acted_repos = {r.repo for r in outcome.acted_on}
        assert acted_repos == {"acme/widgets", "acme/gadgets"}

    async def test_recheck_demotes_a_pr_whose_ci_regressed_between_preview_and_confirm(
        self,
    ) -> None:
        preview_views = (RepoView(repo="acme/widgets", rows=(make_row(number=1),)),)
        confirm_views = (
            RepoView(repo="acme/widgets", rows=(make_row(number=1, verdict=CIVerdict.FAILING),)),
        )
        fleet_service = FakeFleetService(preview_views, confirm_views)
        actions_service = FakeActionsService()

        preview = await preview_bulk(fleet_service, action="merge")
        assert len(preview.eligible) == 1  # green at preview time

        outcome = await execute_bulk(fleet_service, actions_service, action="merge")

        assert actions_service.calls == []  # never attempted -- demoted by the fresh re-check
        assert len(outcome.skipped) == 1
        assert outcome.skipped[0].number == 1
        assert "not green" in outcome.skipped[0].reason  # type: ignore[operator]

    async def test_widened_threshold_is_forwarded_to_execution(self) -> None:
        views = (
            RepoView(repo="acme/widgets", rows=(make_row(number=1, bucket=SafetyBucket.CAUTION),)),
        )
        fleet_service = FakeFleetService(views)
        actions_service = FakeActionsService()

        default_outcome = await execute_bulk(fleet_service, actions_service, action="approve")
        widened_outcome = await execute_bulk(
            fleet_service, actions_service, action="approve", min_bucket=SafetyBucket.CAUTION
        )

        assert default_outcome.acted_on == ()
        assert len(widened_outcome.acted_on) == 1
