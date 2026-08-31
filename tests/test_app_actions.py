"""End-to-end tests for the single-PR action FastAPI routes, against a fully
faked `ActionsService` -- exercises only the FastAPI wiring, form handling,
and Jinja2 rendering (`ActionsService`'s own logic is covered by
`test_actions.py`).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from dependapilot.actions import ActionOutcome, ActionResult
from dependapilot.app import create_app

REPO = "acme/widgets"
REPO_NAME = REPO.split("/")[1]


class FakeActionsService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, str | None]] = []
        self.next_result: ActionResult | None = None

    async def approve(self, repo: str, number: int) -> ActionResult:
        self.calls.append(("approve", repo, number, None))
        return self.next_result or ActionResult(repo, number, "approve", ActionOutcome.APPROVED)

    async def merge(self, repo: str, number: int, head_sha: str) -> ActionResult:
        self.calls.append(("merge", repo, number, head_sha))
        return self.next_result or ActionResult(repo, number, "merge", ActionOutcome.MERGED)

    async def rebase(self, repo: str, number: int) -> ActionResult:
        self.calls.append(("rebase", repo, number, None))
        return self.next_result or ActionResult(repo, number, "rebase", ActionOutcome.REBASED)


class TestSinglePrActionRoutes:
    def test_approve_returns_actions_cell_with_outcome(self) -> None:
        actions = FakeActionsService()
        client = TestClient(create_app(actions_service=actions))  # type: ignore[arg-type]

        response = client.post(f"/repos/acme/{REPO_NAME}/pulls/1/approve", data={"sha": "sha1"})

        assert response.status_code == 200
        assert actions.calls == [("approve", REPO, 1, None)]
        assert "approved" in response.text.lower()

    def test_merge_sends_form_sha_through_to_the_service(self) -> None:
        actions = FakeActionsService()
        client = TestClient(create_app(actions_service=actions))  # type: ignore[arg-type]

        response = client.post(f"/repos/acme/{REPO_NAME}/pulls/9/merge", data={"sha": "abc123"})

        assert response.status_code == 200
        assert actions.calls == [("merge", REPO, 9, "abc123")]
        assert "merged" in response.text.lower()

    def test_merge_missing_sha_is_rejected(self) -> None:
        actions = FakeActionsService()
        client = TestClient(create_app(actions_service=actions))  # type: ignore[arg-type]

        response = client.post(f"/repos/acme/{REPO_NAME}/pulls/9/merge", data={})

        assert response.status_code == 422

    def test_rebase_posts_and_renders_outcome(self) -> None:
        actions = FakeActionsService()
        client = TestClient(create_app(actions_service=actions))  # type: ignore[arg-type]

        response = client.post(f"/repos/acme/{REPO_NAME}/pulls/1/rebase", data={"sha": "sha1"})

        assert response.status_code == 200
        assert actions.calls == [("rebase", REPO, 1, None)]
        assert "rebased" in response.text.lower()

    def test_failure_outcome_surfaces_github_message(self) -> None:
        actions = FakeActionsService()
        actions.next_result = ActionResult(
            REPO, 9, "merge", ActionOutcome.FAILED, "Pull Request is not mergeable"
        )
        client = TestClient(create_app(actions_service=actions))  # type: ignore[arg-type]

        response = client.post(f"/repos/acme/{REPO_NAME}/pulls/9/merge", data={"sha": "sha1"})

        assert response.status_code == 200
        assert "Pull Request is not mergeable" in response.text

    def test_action_without_configured_service_degrades_inline(self) -> None:
        client = TestClient(create_app())

        response = client.post(f"/repos/acme/{REPO_NAME}/pulls/1/approve", data={"sha": "sha1"})

        assert response.status_code == 200
        assert "not configured" in response.text.lower()
