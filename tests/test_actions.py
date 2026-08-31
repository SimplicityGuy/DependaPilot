"""Tests for single-PR actions (approve/merge/rebase), against a fully mocked
GitHub transport -- no real network or `gh` subprocess call anywhere.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from dependapilot.actions import (
    REBASE_COMMENT_BODY,
    ActionOutcome,
    ActionsService,
)
from dependapilot.ci import CIVerdictService
from dependapilot.config import Defaults, FleetConfig, MergeMethod, RepoConfig
from tests.github.conftest import make_client

Handler = Callable[[httpx.Request], httpx.Response]

REPO = "acme/widgets"
HEAD_SHA = "deadbeef"


def green_ci_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/check-runs"):
        return httpx.Response(
            200,
            json={
                "check_runs": [{"name": "build", "status": "completed", "conclusion": "success"}]
            },
        )
    if request.url.path.endswith("/status"):
        return httpx.Response(200, json={"statuses": []})
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def failing_ci_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/check-runs"):
        return httpx.Response(
            200,
            json={
                "check_runs": [{"name": "build", "status": "completed", "conclusion": "failure"}]
            },
        )
    if request.url.path.endswith("/status"):
        return httpx.Response(200, json={"statuses": []})
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


class RecordingHandler:
    """Serves CI as green (or a caller-chosen verdict) and records write calls,
    replying with a caller-controlled status for the write endpoint under test.
    """

    def __init__(
        self,
        *,
        ci_handler: Handler = green_ci_handler,
        write_status: int = 200,
        write_body: dict[str, object] | None = None,
    ) -> None:
        self.ci_handler = ci_handler
        self.write_status = write_status
        self.write_body = write_body if write_body is not None else {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/check-runs") or request.url.path.endswith("/status"):
            return self.ci_handler(request)
        return httpx.Response(self.write_status, json=self.write_body)


def make_fleet(*, actions: bool = True, merge_method: MergeMethod | None = None) -> FleetConfig:
    return FleetConfig(
        defaults=Defaults(merge_method=MergeMethod.MERGE),
        repos=[RepoConfig(repo=REPO, actions=actions, merge_method=merge_method)],
    )


def make_service(handler: RecordingHandler, *, fleet: FleetConfig | None = None) -> ActionsService:
    client = make_client(handler)
    ci_service = CIVerdictService(client)
    return ActionsService(client, fleet or make_fleet(), ci_service)


class TestApprove:
    async def test_approve_success(self) -> None:
        handler = RecordingHandler(write_status=200)
        service = make_service(handler)

        result = await service.approve(REPO, 42)

        assert result.outcome == ActionOutcome.APPROVED
        assert result.ok is True
        write_requests = [r for r in handler.requests if r.url.path.endswith("/reviews")]
        assert len(write_requests) == 1
        assert write_requests[0].method == "POST"
        assert json.loads(write_requests[0].content) == {"event": "APPROVE"}

    async def test_approve_error_is_surfaced(self) -> None:
        handler = RecordingHandler(
            write_status=422,
            write_body={"message": "Review cannot be requested from pull request author."},
        )
        service = make_service(handler)

        result = await service.approve(REPO, 42)

        assert result.outcome == ActionOutcome.FAILED
        assert result.ok is False
        assert result.message == "Review cannot be requested from pull request author."

    async def test_approve_blocked_when_repo_actions_disabled(self) -> None:
        handler = RecordingHandler()
        service = make_service(handler, fleet=make_fleet(actions=False))

        result = await service.approve(REPO, 42)

        assert result.outcome == ActionOutcome.SKIPPED
        assert not any(r.url.path.endswith("/reviews") for r in handler.requests)


class TestRebase:
    async def test_rebase_posts_exact_comment_body(self) -> None:
        handler = RecordingHandler(write_status=201)
        service = make_service(handler)

        result = await service.rebase(REPO, 7)

        assert result.outcome == ActionOutcome.REBASED
        comment_requests = [r for r in handler.requests if r.url.path.endswith("/comments")]
        assert len(comment_requests) == 1
        assert json.loads(comment_requests[0].content) == {"body": "@dependabot rebase"}
        assert REBASE_COMMENT_BODY == "@dependabot rebase"

    async def test_rebase_error_is_surfaced(self) -> None:
        handler = RecordingHandler(write_status=404, write_body={"message": "Not Found"})
        service = make_service(handler)

        result = await service.rebase(REPO, 7)

        assert result.outcome == ActionOutcome.FAILED
        assert result.message == "Not Found"


class TestMerge:
    async def test_merge_success_sends_resolved_merge_method_and_sha(self) -> None:
        handler = RecordingHandler(write_status=200, write_body={"merged": True})
        service = make_service(handler, fleet=make_fleet(merge_method=MergeMethod.SQUASH))

        result = await service.merge(REPO, 9, HEAD_SHA)

        assert result.outcome == ActionOutcome.MERGED
        merge_requests = [r for r in handler.requests if r.url.path.endswith("/merge")]
        assert len(merge_requests) == 1
        assert merge_requests[0].method == "PUT"
        payload = json.loads(merge_requests[0].content)
        assert payload == {"merge_method": "squash", "sha": HEAD_SHA}

    async def test_merge_uses_fleet_default_merge_method_when_no_override(self) -> None:
        handler = RecordingHandler(write_status=200)
        service = make_service(handler, fleet=make_fleet(merge_method=None))

        await service.merge(REPO, 9, HEAD_SHA)

        merge_requests = [r for r in handler.requests if r.url.path.endswith("/merge")]
        assert json.loads(merge_requests[0].content)["merge_method"] == "merge"

    async def test_merge_blocked_server_side_when_ci_not_green(self) -> None:
        handler = RecordingHandler(ci_handler=failing_ci_handler)
        service = make_service(handler)

        result = await service.merge(REPO, 9, HEAD_SHA)

        assert result.outcome == ActionOutcome.SKIPPED
        assert "not green" in result.message  # type: ignore[operator]
        assert not any(r.url.path.endswith("/merge") for r in handler.requests)

    async def test_merge_error_is_surfaced(self) -> None:
        handler = RecordingHandler(
            write_status=405, write_body={"message": "Pull Request is not mergeable"}
        )
        service = make_service(handler)

        result = await service.merge(REPO, 9, HEAD_SHA)

        assert result.outcome == ActionOutcome.FAILED
        assert result.message == "Pull Request is not mergeable"

    async def test_merge_blocked_when_repo_actions_disabled(self) -> None:
        handler = RecordingHandler()
        service = make_service(handler, fleet=make_fleet(actions=False))

        result = await service.merge(REPO, 9, HEAD_SHA)

        assert result.outcome == ActionOutcome.SKIPPED
        assert not any(r.url.path.endswith("/merge") for r in handler.requests)
        assert not any(r.url.path.endswith("/check-runs") for r in handler.requests)


class TestStructuredLogging:
    async def test_each_action_emits_one_structured_log_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = RecordingHandler(write_status=200)
        service = make_service(handler)

        with caplog.at_level(logging.INFO, logger="dependapilot.actions"):
            await service.approve(REPO, 1)

        records = [r for r in caplog.records if r.name == "dependapilot.actions"]
        assert len(records) == 1
        record = records[0]
        assert record.action == "approve"  # type: ignore[attr-defined]
        assert record.repo == REPO  # type: ignore[attr-defined]
        assert record.pr == 1  # type: ignore[attr-defined]
        assert record.outcome == "approved"  # type: ignore[attr-defined]

    async def test_failed_action_still_emits_exactly_one_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = RecordingHandler(write_status=422, write_body={"message": "nope"})
        service = make_service(handler)

        with caplog.at_level(logging.INFO, logger="dependapilot.actions"):
            await service.rebase(REPO, 3)

        records = [r for r in caplog.records if r.name == "dependapilot.actions"]
        assert len(records) == 1
        assert records[0].outcome == "failed"  # type: ignore[attr-defined]
