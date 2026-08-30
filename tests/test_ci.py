"""Tests for the CI verdict service: check-run + legacy-status folding, mergeability retry.

Every test drives an `httpx.MockTransport` (via `tests.github.conftest.make_client`) --
no real network or `gh` subprocess call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from dependapilot.ci import (
    CheckDetail,
    CheckOutcome,
    CIStatus,
    CIVerdict,
    CIVerdictService,
    MergeabilityStatus,
)
from tests.github.conftest import make_client

Handler = Callable[[httpx.Request], httpx.Response]

REPO = "acme/widgets"
REF = "abc123"
CHECK_RUNS_PATH = f"/repos/{REPO}/commits/{REF}/check-runs"
STATUS_PATH = f"/repos/{REPO}/commits/{REF}/status"


def check_run(
    *,
    name: str = "build",
    status: str = "completed",
    conclusion: str | None = "success",
    html_url: str = "https://github.com/acme/widgets/runs/1",
) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": html_url}


def legacy_status(
    *,
    context: str = "ci/legacy",
    state: str = "success",
    target_url: str = "https://ci.example.com/1",
) -> dict[str, Any]:
    return {"context": context, "state": state, "target_url": target_url}


def make_handler(
    *,
    check_runs: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    check_runs_status_code: int = 200,
    status_status_code: int = 200,
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == CHECK_RUNS_PATH:
            return httpx.Response(check_runs_status_code, json={"check_runs": check_runs or []})
        if request.url.path == STATUS_PATH:
            return httpx.Response(status_status_code, json={"statuses": statuses or []})
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


# --- CIVerdictService.get_ci_status --------------------------------------------------


class TestGetCIStatusVerdict:
    async def test_all_green(self) -> None:
        client = make_client(
            make_handler(
                check_runs=[check_run(name="build"), check_run(name="test")],
                statuses=[legacy_status(context="ci/legacy")],
            )
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.GREEN
        assert {c.name for c in status.checks} == {"build", "test", "ci/legacy"}
        assert all(c.outcome is CheckOutcome.SUCCESS for c in status.checks)

    async def test_any_failing_check_run_wins_over_success(self) -> None:
        client = make_client(
            make_handler(
                check_runs=[
                    check_run(name="build"),
                    check_run(name="lint", status="completed", conclusion="failure"),
                ],
            )
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.FAILING
        lint = next(c for c in status.checks if c.name == "lint")
        assert lint.outcome is CheckOutcome.FAILING

    async def test_failing_beats_pending(self) -> None:
        client = make_client(
            make_handler(
                check_runs=[
                    check_run(name="lint", status="completed", conclusion="failure"),
                    check_run(name="build", status="in_progress", conclusion=None),
                ],
            )
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.FAILING

    async def test_pending_when_a_check_is_queued_or_in_progress(self) -> None:
        client = make_client(
            make_handler(
                check_runs=[
                    check_run(name="build", status="completed", conclusion="success"),
                    check_run(name="deploy", status="queued", conclusion=None),
                ],
            )
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.PENDING
        deploy = next(c for c in status.checks if c.name == "deploy")
        assert deploy.outcome is CheckOutcome.PENDING

    async def test_legacy_status_pending(self) -> None:
        client = make_client(
            make_handler(statuses=[legacy_status(context="ci/legacy", state="pending")])
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.PENDING

    async def test_legacy_status_error_state_is_failing(self) -> None:
        client = make_client(
            make_handler(statuses=[legacy_status(context="ci/legacy", state="error")])
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.FAILING

    async def test_skipped_and_neutral_conclusions_are_not_green(self) -> None:
        client = make_client(
            make_handler(
                check_runs=[
                    check_run(name="build", status="completed", conclusion="skipped"),
                    check_run(name="optional", status="completed", conclusion="neutral"),
                ],
            )
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        # Neutral/skipped carry no signal on their own -- must never read as GREEN.
        assert status.verdict is CIVerdict.NO_CI
        assert {c.outcome for c in status.checks} == {CheckOutcome.NEUTRAL}

    async def test_skipped_and_neutral_dont_block_a_real_success(self) -> None:
        client = make_client(
            make_handler(
                check_runs=[
                    check_run(name="build", status="completed", conclusion="success"),
                    check_run(name="optional", status="completed", conclusion="skipped"),
                ],
            )
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.GREEN

    async def test_legacy_status_only_repo_still_yields_a_verdict(self) -> None:
        client = make_client(
            make_handler(check_runs=[], statuses=[legacy_status(context="ci/legacy")])
        )
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.GREEN
        assert len(status.checks) == 1
        assert status.checks[0].source == "status"

    async def test_zero_checks_and_zero_statuses_is_no_ci(self) -> None:
        client = make_client(make_handler(check_runs=[], statuses=[]))
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert status.verdict is CIVerdict.NO_CI
        assert status.checks == []

    async def test_no_ci_is_distinguishable_from_green_in_the_public_api(self) -> None:
        assert CIVerdict.NO_CI != CIVerdict.GREEN
        assert CIVerdict.NO_CI.value == "no_ci"
        assert CIVerdict.GREEN.value == "green"

    async def test_check_runs_pagination_follows_link_header(self) -> None:
        page2_url = f"https://api.github.com{CHECK_RUNS_PATH}?per_page=100&page=2"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == CHECK_RUNS_PATH and request.url.params.get("page") != "2":
                return httpx.Response(
                    200,
                    json={"check_runs": [check_run(name="build")]},
                    headers={"Link": f'<{page2_url}>; rel="next"'},
                )
            if request.url.path == CHECK_RUNS_PATH:
                return httpx.Response(200, json={"check_runs": [check_run(name="test")]})
            assert request.url.path == STATUS_PATH
            return httpx.Response(200, json={"statuses": []})

        client = make_client(handler)
        service = CIVerdictService(client)

        status = await service.get_ci_status(REPO, REF)

        assert {c.name for c in status.checks} == {"build", "test"}


# --- CIVerdictService.get_mergeability ------------------------------------------------


def pull_payload(*, mergeable: bool | None, mergeable_state: str | None) -> dict[str, Any]:
    return {"mergeable": mergeable, "mergeable_state": mergeable_state}


class TestGetMergeability:
    async def test_returns_immediately_when_mergeable_is_already_known(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=pull_payload(mergeable=True, mergeable_state="clean"))

        client = make_client(handler)
        service = CIVerdictService(client)

        result = await service.get_mergeability(REPO, 7)

        assert result == MergeabilityStatus(mergeable=True, mergeable_state="clean")
        assert calls == 1

    async def test_retries_with_backoff_while_mergeable_is_null(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(200, json=pull_payload(mergeable=None, mergeable_state=None))
            return httpx.Response(200, json=pull_payload(mergeable=False, mergeable_state="dirty"))

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client = make_client(handler)
        service = CIVerdictService(client, mergeable_backoff_base=0.1, sleep=sleep)

        result = await service.get_mergeability(REPO, 7)

        assert result == MergeabilityStatus(mergeable=False, mergeable_state="dirty")
        assert calls == 3
        assert sleeps == [0.1, 0.2]

    async def test_gives_up_after_max_attempts_and_returns_none(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=pull_payload(mergeable=None, mergeable_state=None))

        async def sleep(_seconds: float) -> None:
            return None

        client = make_client(handler)
        service = CIVerdictService(client, mergeable_max_attempts=3, sleep=sleep)

        result = await service.get_mergeability(REPO, 7)

        assert result == MergeabilityStatus(mergeable=None, mergeable_state=None)
        assert calls == 3

    async def test_mergeable_state_is_carried_through_but_never_gates_alone(self) -> None:
        # "unstable" is a real mergeable_state GitHub returns for a mergeable PR with
        # failing/pending required checks -- advisory color only, not a merge gate.
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=pull_payload(mergeable=True, mergeable_state="unstable")
            )

        client = make_client(handler)
        service = CIVerdictService(client)

        result = await service.get_mergeability(REPO, 7)

        assert result.mergeable is True
        assert result.mergeable_state == "unstable"


# --- CheckDetail / CIStatus shape ------------------------------------------------------


class TestCheckDetailShape:
    def test_check_detail_carries_raw_state_for_debugging(self) -> None:
        detail = CheckDetail(
            name="build",
            source="check_run",
            outcome=CheckOutcome.PENDING,
            raw_state="in_progress/None",
            url=None,
        )

        assert detail.raw_state == "in_progress/None"

    def test_ci_status_defaults_to_empty_checks(self) -> None:
        assert CIStatus(verdict=CIVerdict.NO_CI).checks == []
