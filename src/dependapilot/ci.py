"""CI verdict service: fold check-runs + legacy commit status into one verdict.

`CIVerdictService` is the single trustworthy answer to "is this PR's CI green?"
that the dashboard and bulk-merge both rely on. GitHub exposes two independent,
overlapping signals for a commit's CI state -- the Checks API
(`GET .../check-runs`) that GitHub Actions and most modern CI report through,
and the older combined-status API (`GET .../status`) that some CI still uses
exclusively -- so a trustworthy verdict has to fold both together rather than
trust either alone. It also owns mergeability: a PR's `mergeable` field is
`None` while GitHub is still computing it, so `get_mergeability` retries with
bounded backoff instead of handing callers a `None` they might mistake for
"not mergeable".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal

from dependapilot.github import GitHubClient

DEFAULT_MERGEABLE_MAX_ATTEMPTS: Final = 5
DEFAULT_MERGEABLE_BACKOFF_BASE: Final = 0.5

# check-run `conclusion` values that mean "this CI signal failed".
_FAILING_CONCLUSIONS: Final = frozenset({"failure", "timed_out", "action_required", "cancelled"})
# legacy commit-status `state` values that mean the same.
_FAILING_LEGACY_STATES: Final = frozenset({"failure", "error"})


class CIVerdict(StrEnum):
    """The one trustworthy per-PR CI answer bulk-merge and the dashboard read.

    - `GREEN`: at least one check/status succeeded, and nothing failed or is
      still running.
    - `PENDING`: nothing has failed yet, but at least one check/status is
      still queued, in progress, or pending.
    - `FAILING`: at least one check/status failed, timed out, was cancelled,
      or needs action.
    - `NO_CI`: no check run and no legacy status reported *any* signal --
      including the case where every reported check was neutral or skipped,
      since neutral/skipped conclusions don't count as a success signal on
      their own. Deliberately distinct from `GREEN`: bulk-merge must never
      treat "no CI ran" as "CI passed".
    """

    GREEN = "green"
    PENDING = "pending"
    FAILING = "failing"
    NO_CI = "no_ci"


class CheckOutcome(StrEnum):
    """How one check-run or legacy status contributed to the fold into `CIVerdict`."""

    SUCCESS = "success"
    PENDING = "pending"
    FAILING = "failing"
    NEUTRAL = "neutral"
    """Reported, but not a success/failure/pending signal (neutral or skipped)."""


@dataclass(frozen=True, slots=True)
class CheckDetail:
    """One check-run or legacy status, normalized to a common shape."""

    name: str
    source: Literal["check_run", "status"]
    outcome: CheckOutcome
    raw_state: str
    """The original GitHub value(s) this was derived from, e.g. "completed/success"."""
    url: str | None = None


@dataclass(frozen=True, slots=True)
class CIStatus:
    """The folded verdict for one commit, plus the per-check detail behind it."""

    verdict: CIVerdict
    checks: list[CheckDetail] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MergeabilityStatus:
    """GitHub's mergeability read for one PR.

    `mergeable_state` is advisory color (e.g. "clean", "dirty", "unstable",
    "blocked") for display only -- never the merge gate. Callers must gate on
    `mergeable` (and, separately, on the `CIVerdict`) instead.
    """

    mergeable: bool | None
    mergeable_state: str | None


def _check_run_outcome(payload: dict[str, Any]) -> CheckOutcome:
    if payload.get("status") != "completed":
        return CheckOutcome.PENDING
    conclusion = payload.get("conclusion")
    if conclusion in _FAILING_CONCLUSIONS:
        return CheckOutcome.FAILING
    if conclusion == "success":
        return CheckOutcome.SUCCESS
    # "neutral", "skipped", or an unexpected/missing conclusion: no signal either way.
    return CheckOutcome.NEUTRAL


def _legacy_status_outcome(state: str) -> CheckOutcome:
    if state in _FAILING_LEGACY_STATES:
        return CheckOutcome.FAILING
    if state == "pending":
        return CheckOutcome.PENDING
    if state == "success":
        return CheckOutcome.SUCCESS
    return CheckOutcome.NEUTRAL


def _fold(checks: list[CheckDetail]) -> CIVerdict:
    """Fold per-check outcomes into one verdict: failing > pending > green > no_ci."""
    outcomes = {check.outcome for check in checks}
    if CheckOutcome.FAILING in outcomes:
        return CIVerdict.FAILING
    if CheckOutcome.PENDING in outcomes:
        return CIVerdict.PENDING
    if CheckOutcome.SUCCESS in outcomes:
        return CIVerdict.GREEN
    return CIVerdict.NO_CI


async def _fetch_check_runs(client: GitHubClient, repo: str, ref: str) -> list[dict[str, Any]]:
    """Paginate `GET /repos/{repo}/commits/{ref}/check-runs`.

    The endpoint wraps its list in `{"check_runs": [...]}` rather than
    returning a bare array, so (like discovery's search pagination) this
    follows `response.links["next"]` by hand instead of `GitHubClient.paginate`.
    """
    runs: list[dict[str, Any]] = []
    path: str | None = f"/repos/{repo}/commits/{ref}/check-runs"
    params: dict[str, Any] | None = {"per_page": 100}
    while path is not None:
        response = await client.get(path, params=params)
        runs.extend(response.json().get("check_runs", []))
        path = response.links.get("next", {}).get("url")
        params = None  # the next URL already carries per_page/page
    return runs


async def _fetch_legacy_statuses(client: GitHubClient, repo: str, ref: str) -> list[dict[str, Any]]:
    """`GET /repos/{repo}/commits/{ref}/status`: the combined legacy status."""
    response = await client.get(f"/repos/{repo}/commits/{ref}/status", params={"per_page": 100})
    return list(response.json().get("statuses", []))


class CIVerdictService:
    """Folds a commit's checks + legacy statuses into one `CIStatus`, and
    resolves a PR's mergeability with bounded backoff while GitHub computes it.
    """

    def __init__(
        self,
        client: GitHubClient,
        *,
        mergeable_max_attempts: int = DEFAULT_MERGEABLE_MAX_ATTEMPTS,
        mergeable_backoff_base: float = DEFAULT_MERGEABLE_BACKOFF_BASE,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._mergeable_max_attempts = mergeable_max_attempts
        self._mergeable_backoff_base = mergeable_backoff_base
        self._sleep = sleep or asyncio.sleep

    async def get_ci_status(self, repo: str, ref: str) -> CIStatus:
        """The folded CI verdict for `repo`'s commit `ref` (typically a PR head sha)."""
        check_runs = await _fetch_check_runs(self._client, repo, ref)
        statuses = await _fetch_legacy_statuses(self._client, repo, ref)

        checks = [
            CheckDetail(
                name=run["name"],
                source="check_run",
                outcome=_check_run_outcome(run),
                raw_state=f"{run.get('status')}/{run.get('conclusion')}",
                url=run.get("html_url"),
            )
            for run in check_runs
        ] + [
            CheckDetail(
                name=status["context"],
                source="status",
                outcome=_legacy_status_outcome(status["state"]),
                raw_state=status["state"],
                url=status.get("target_url"),
            )
            for status in statuses
        ]

        return CIStatus(verdict=_fold(checks), checks=checks)

    async def get_mergeability(self, repo: str, number: int) -> MergeabilityStatus:
        """The PR's mergeability, re-fetching with bounded backoff while GitHub
        is still computing it (`mergeable` is `None` in that window).

        Gives up after `mergeable_max_attempts` and returns the last-seen
        (possibly still-`None`) result rather than raising -- callers should
        treat a `None` result here as "unknown", never as license to proceed.
        """
        attempt = 0
        while True:
            payload = (await self._client.get(f"/repos/{repo}/pulls/{number}")).json()
            mergeable = payload.get("mergeable")
            mergeable_state = payload.get("mergeable_state")
            attempt += 1
            if mergeable is not None or attempt >= self._mergeable_max_attempts:
                return MergeabilityStatus(mergeable=mergeable, mergeable_state=mergeable_state)
            await self._sleep(self._mergeable_backoff_base * (2 ** (attempt - 1)))
