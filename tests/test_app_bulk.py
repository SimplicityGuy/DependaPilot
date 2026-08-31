"""End-to-end tests for the bulk-action FastAPI routes, against fully faked
`ActionsService`/`FleetService` collaborators -- exercises only the FastAPI
wiring, form handling, and Jinja2 rendering (`bulk.py`'s own eligibility and
sequencing logic is covered by `test_bulk.py`).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from dependapilot.actions import ActionOutcome, ActionResult
from dependapilot.app import create_app
from dependapilot.ci import CIStatus, CIVerdict
from dependapilot.discovery import PRRecord
from dependapilot.fleet import PRRow, RepoView
from dependapilot.scoring import SafetyBucket, SafetyScore

REPO = "acme/widgets"


def make_pr(*, number: int = 1, head_sha: str = "sha1") -> PRRecord:
    return PRRecord(
        repo=REPO,
        number=number,
        title="Bump foo from 1.0.0 to 1.1.0",
        html_url=f"https://github.com/{REPO}/pull/{number}",
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


def make_row(*, number: int = 1, verdict: CIVerdict = CIVerdict.GREEN) -> PRRow:
    return PRRow(
        pr=make_pr(number=number),
        ci_status=CIStatus(verdict=verdict, checks=[]),
        safety=SafetyScore(score=90, bucket=SafetyBucket.SAFE, breakdown=(), stale=False),
    )


class FakeActionsService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, str | None]] = []

    async def approve(self, repo: str, number: int) -> ActionResult:
        self.calls.append(("approve", repo, number, None))
        return ActionResult(repo, number, "approve", ActionOutcome.APPROVED)

    async def merge(self, repo: str, number: int, head_sha: str) -> ActionResult:
        self.calls.append(("merge", repo, number, head_sha))
        return ActionResult(repo, number, "merge", ActionOutcome.MERGED)

    async def rebase(self, repo: str, number: int) -> ActionResult:
        self.calls.append(("rebase", repo, number, None))
        return ActionResult(repo, number, "rebase", ActionOutcome.REBASED)


class FakeFleetService:
    def __init__(self, views: tuple[RepoView, ...]) -> None:
        self.views = views

    async def get_fleet_view(self, *, force_refresh: bool = False) -> tuple[RepoView, ...]:
        return self.views


class TestBulkRoutes:
    def test_preview_lists_eligible_and_skipped(self) -> None:
        views = (
            RepoView(
                repo=REPO,
                rows=(make_row(number=1), make_row(number=2, verdict=CIVerdict.FAILING)),
            ),
        )
        client = TestClient(create_app(fleet_service=FakeFleetService(views)))  # type: ignore[arg-type]

        response = client.post("/fleet/bulk/preview", data={"action": "approve"})

        assert response.status_code == 200
        assert f"{REPO}#1" in response.text
        assert "skipped" in response.text.lower()

    def test_preview_without_fleet_service_degrades_inline(self) -> None:
        client = TestClient(create_app())

        response = client.post("/fleet/bulk/preview", data={"action": "approve"})

        assert response.status_code == 200
        assert "not configured" in response.text.lower()

    def test_execute_runs_actions_and_renders_results(self) -> None:
        views = (RepoView(repo=REPO, rows=(make_row(number=1),)),)
        actions = FakeActionsService()
        client = TestClient(
            create_app(fleet_service=FakeFleetService(views), actions_service=actions)  # type: ignore[arg-type]
        )

        response = client.post("/fleet/bulk/execute", data={"action": "approve"})

        assert response.status_code == 200
        assert actions.calls == [("approve", REPO, 1, None)]
        assert "approved" in response.text.lower()

    def test_execute_repo_scoped(self) -> None:
        views = (
            RepoView(repo=REPO, rows=(make_row(number=1),)),
            RepoView(repo="acme/gadgets", rows=(make_row(number=2),)),
        )
        actions = FakeActionsService()
        client = TestClient(
            create_app(fleet_service=FakeFleetService(views), actions_service=actions)  # type: ignore[arg-type]
        )

        response = client.post("/fleet/bulk/execute", data={"action": "merge", "repo": REPO})

        assert response.status_code == 200
        assert actions.calls == [("merge", REPO, 1, "sha1")]

    def test_invalid_action_is_rejected(self) -> None:
        views = (RepoView(repo=REPO, rows=()),)
        client = TestClient(create_app(fleet_service=FakeFleetService(views)))  # type: ignore[arg-type]

        response = client.post("/fleet/bulk/preview", data={"action": "delete"})

        assert response.status_code == 422

    def test_preview_with_selection_narrows_to_exactly_the_chosen_prs(self) -> None:
        views = (
            RepoView(
                repo=REPO,
                rows=(make_row(number=1), make_row(number=2), make_row(number=3)),
            ),
        )
        client = TestClient(create_app(fleet_service=FakeFleetService(views)))  # type: ignore[arg-type]

        response = client.post(
            "/fleet/bulk/preview",
            data={"action": "approve", "selected": [f"{REPO}#1", f"{REPO}#2"]},
        )

        assert response.status_code == 200
        assert f"{REPO}#1" in response.text
        assert f"{REPO}#2" in response.text
        assert f"{REPO}#3" not in response.text

    def test_preview_selection_carries_a_selected_but_ineligible_pr_into_the_confirm_form(
        self,
    ) -> None:
        views = (
            RepoView(
                repo=REPO,
                rows=(make_row(number=1), make_row(number=2, verdict=CIVerdict.FAILING)),
            ),
        )
        client = TestClient(create_app(fleet_service=FakeFleetService(views)))  # type: ignore[arg-type]

        response = client.post(
            "/fleet/bulk/preview",
            data={"action": "approve", "selected": [f"{REPO}#1", f"{REPO}#2"]},
        )

        assert response.status_code == 200
        assert "skipped" in response.text.lower()
        # the confirm form must re-carry the selection through to execute
        assert response.text.count(f'name="selected" value="{REPO}#1"') == 1
        assert response.text.count(f'name="selected" value="{REPO}#2"') == 1

    def test_execute_acts_only_on_selected_eligible_prs(self) -> None:
        views = (
            RepoView(
                repo=REPO,
                rows=(make_row(number=1), make_row(number=2), make_row(number=3)),
            ),
        )
        actions = FakeActionsService()
        client = TestClient(
            create_app(fleet_service=FakeFleetService(views), actions_service=actions)  # type: ignore[arg-type]
        )

        response = client.post(
            "/fleet/bulk/execute",
            data={"action": "approve", "selected": [f"{REPO}#1", f"{REPO}#3"]},
        )

        assert response.status_code == 200
        assert sorted(c[2] for c in actions.calls) == [1, 3]

    def test_execute_reports_a_selected_ineligible_pr_as_skipped_with_reason(self) -> None:
        views = (
            RepoView(
                repo=REPO,
                rows=(make_row(number=1, verdict=CIVerdict.FAILING),),
            ),
        )
        actions = FakeActionsService()
        client = TestClient(
            create_app(fleet_service=FakeFleetService(views), actions_service=actions)  # type: ignore[arg-type]
        )

        response = client.post(
            "/fleet/bulk/execute",
            data={"action": "approve", "selected": [f"{REPO}#1"]},
        )

        assert response.status_code == 200
        assert actions.calls == []
        assert "skipped" in response.text.lower()

    def test_no_selection_preserves_current_all_eligible_behavior(self) -> None:
        views = (RepoView(repo=REPO, rows=(make_row(number=1), make_row(number=2))),)
        actions = FakeActionsService()
        client = TestClient(
            create_app(fleet_service=FakeFleetService(views), actions_service=actions)  # type: ignore[arg-type]
        )

        response = client.post("/fleet/bulk/execute", data={"action": "approve"})

        assert response.status_code == 200
        assert sorted(c[2] for c in actions.calls) == [1, 2]

    def test_malformed_selection_token_is_rejected(self) -> None:
        views = (RepoView(repo=REPO, rows=(make_row(number=1),)),)
        client = TestClient(create_app(fleet_service=FakeFleetService(views)))  # type: ignore[arg-type]

        response = client.post(
            "/fleet/bulk/preview", data={"action": "approve", "selected": ["garbage"]}
        )

        assert response.status_code == 422
