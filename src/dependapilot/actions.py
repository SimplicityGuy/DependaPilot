"""Single-PR actions: approve, merge, rebase -- acted on straight from the dashboard.

`ActionsService` is the one place these three GitHub writes happen, so every
caller (the FastAPI endpoints in `app.py`, and the bulk-action flow in
`bulk.py`) gets the same policy enforcement and the same audit trail:

- Merge is gated on fleet policy (`repos.yml`'s per-repo `actions: true`) and
  re-checks the PR's CI verdict *at execution time* rather than trusting
  whatever the dashboard rendered a moment (or a page load) ago -- CI can
  flip between render and click. A non-green verdict is a `SKIPPED` outcome,
  never attempted against GitHub.
- Merge always sends the resolved `merge_method` (`repos.yml` per-repo
  override, else the fleet default) and the caller-supplied head `sha` as a
  race guard: if the PR has moved on since the dashboard last rendered it,
  GitHub itself rejects the merge (409/422) rather than silently merging a
  commit nobody reviewed.
- Every attempt -- success, GitHub error, or a policy/CI skip -- emits
  exactly one structured log record, so dashboard actions are auditable the
  same way any other write path would be.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from dependapilot.ci import CIVerdict, CIVerdictService
from dependapilot.config import FleetConfig
from dependapilot.github import GitHubAPIError, GitHubClient

logger = logging.getLogger("dependapilot.actions")

REBASE_COMMENT_BODY: Final = "@dependabot rebase"
"""The exact comment body Dependabot recognizes as a rebase request."""


class ActionOutcome(StrEnum):
    """What happened when a single-PR action was attempted."""

    APPROVED = "approved"
    MERGED = "merged"
    REBASED = "rebased"
    SKIPPED = "skipped"
    """Not attempted against GitHub at all -- blocked by policy or a non-green CI verdict."""
    FAILED = "failed"
    """Attempted against GitHub and rejected -- `message` carries GitHub's own error."""


_OK_OUTCOMES: Final = frozenset(
    {ActionOutcome.APPROVED, ActionOutcome.MERGED, ActionOutcome.REBASED}
)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The outcome of one attempted action against one PR."""

    repo: str
    number: int
    action: str
    """One of "approve" / "merge" / "rebase"."""
    outcome: ActionOutcome
    message: str | None = None
    """GitHub's error message on `FAILED`, or the reason on `SKIPPED`; `None` on success."""

    @property
    def ok(self) -> bool:
        return self.outcome in _OK_OUTCOMES


def _extract_github_message(exc: GitHubAPIError) -> str:
    """GitHub's own `"message"` field from an error body, falling back to `str(exc)`.

    GitHub's JSON error bodies (405 branch-protection rejections, 422
    validation failures, etc.) carry a human-readable `message` -- surface
    that verbatim to the row rather than our own generic "request failed" text.
    """
    try:
        payload = json.loads(exc.body)
    except (json.JSONDecodeError, TypeError):
        return str(exc)
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message
    return str(exc)


def _log_action(result: ActionResult) -> None:
    logger.info(
        "dashboard action: %s %s#%d -> %s",
        result.action,
        result.repo,
        result.number,
        result.outcome.value,
        extra={
            "action": result.action,
            "repo": result.repo,
            "pr": result.number,
            "outcome": result.outcome.value,
            # Not "message": that key is reserved by `LogRecord` itself and
            # `makeRecord` raises `KeyError` if `extra` tries to overwrite it.
            "outcome_detail": result.message,
        },
    )


class ActionsService:
    """Executes approve/merge/rebase against GitHub, gated by fleet policy and CI."""

    def __init__(
        self, client: GitHubClient, fleet: FleetConfig, ci_service: CIVerdictService
    ) -> None:
        self._client = client
        self._fleet = fleet
        self._ci_service = ci_service

    async def approve(self, repo: str, number: int) -> ActionResult:
        """`POST /repos/{repo}/pulls/{number}/reviews` with `event=APPROVE`."""
        if not self._fleet.actions_enabled_for(repo):
            return self._finish(self._disabled_result(repo, number, "approve"))

        try:
            await self._client.request(
                "POST", f"/repos/{repo}/pulls/{number}/reviews", json={"event": "APPROVE"}
            )
        except GitHubAPIError as exc:
            result = ActionResult(
                repo, number, "approve", ActionOutcome.FAILED, _extract_github_message(exc)
            )
        else:
            result = ActionResult(repo, number, "approve", ActionOutcome.APPROVED)
        return self._finish(result)

    async def rebase(self, repo: str, number: int) -> ActionResult:
        """`POST /repos/{repo}/issues/{number}/comments` with the `@dependabot rebase` body."""
        if not self._fleet.actions_enabled_for(repo):
            return self._finish(self._disabled_result(repo, number, "rebase"))

        try:
            await self._client.request(
                "POST",
                f"/repos/{repo}/issues/{number}/comments",
                json={"body": REBASE_COMMENT_BODY},
            )
        except GitHubAPIError as exc:
            result = ActionResult(
                repo, number, "rebase", ActionOutcome.FAILED, _extract_github_message(exc)
            )
        else:
            result = ActionResult(repo, number, "rebase", ActionOutcome.REBASED)
        return self._finish(result)

    async def merge(self, repo: str, number: int, head_sha: str) -> ActionResult:
        """`PUT /repos/{repo}/pulls/{number}/merge` with the resolved `merge_method` and `sha`.

        Blocked (never sent to GitHub) unless the repo has opted into actions
        and a freshly-checked CI verdict for `head_sha` is green -- both
        checks re-run here regardless of what the dashboard last rendered.
        """
        if not self._fleet.actions_enabled_for(repo):
            return self._finish(self._disabled_result(repo, number, "merge"))

        ci_status = await self._ci_service.get_ci_status(repo, head_sha)
        if ci_status.verdict != CIVerdict.GREEN:
            return self._finish(
                ActionResult(
                    repo,
                    number,
                    "merge",
                    ActionOutcome.SKIPPED,
                    f"CI is not green (verdict={ci_status.verdict.value}); merge blocked",
                )
            )

        merge_method = self._fleet.merge_method_for(repo)
        try:
            await self._client.request(
                "PUT",
                f"/repos/{repo}/pulls/{number}/merge",
                json={"merge_method": merge_method.value, "sha": head_sha},
            )
        except GitHubAPIError as exc:
            result = ActionResult(
                repo, number, "merge", ActionOutcome.FAILED, _extract_github_message(exc)
            )
        else:
            result = ActionResult(repo, number, "merge", ActionOutcome.MERGED)
        return self._finish(result)

    @staticmethod
    def _disabled_result(repo: str, number: int, action: str) -> ActionResult:
        return ActionResult(
            repo, number, action, ActionOutcome.SKIPPED, f"actions are not enabled for {repo}"
        )

    @staticmethod
    def _finish(result: ActionResult) -> ActionResult:
        _log_action(result)
        return result
