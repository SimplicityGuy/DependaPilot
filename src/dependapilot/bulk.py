"""Bulk actions: "approve all" / "merge all eligible", at repo and fleet scope.

The reason the tool exists: clearing a week's worth of green Dependabot PRs
in one decision instead of clicking through each one.

Two-phase preview-then-confirm, both driven by the same `is_eligible` rule so
they can never disagree:

- `preview_bulk` answers "what would this do right now" -- every PR in scope
  split into the ones that qualify and the ones that don't (with a reason),
  for the dashboard to show *before* anything happens.
- `execute_bulk` re-derives eligibility from scratch (not by trusting whatever
  the preview call returned) and then executes sequentially against
  `ActionsService`, never stopping early: a 405 on one PR is recorded and the
  batch continues, so one bad repo can't swallow the rest of the fleet's
  results. Eligibility can only ever *shrink* between preview and execute --
  CI regressing between the two is the expected case, not an error -- so a
  demoted PR comes back skipped-with-reason rather than merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from dependapilot.actions import ActionResult, ActionsService
from dependapilot.ci import CIVerdict
from dependapilot.fleet import FleetService, PRRow
from dependapilot.scoring import SafetyBucket

_BUCKET_RANK: Final = {SafetyBucket.UNSAFE: 0, SafetyBucket.CAUTION: 1, SafetyBucket.SAFE: 2}

BULK_ACTIONS: Final = frozenset({"approve", "merge"})


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Whether one PR row qualifies for a bulk action, and why not if it doesn't."""

    repo: str
    number: int
    eligible: bool
    reason: str | None = None
    """Set (a skip reason) whenever `eligible` is False; `None` when eligible."""


def is_eligible(row: PRRow, *, min_bucket: SafetyBucket = SafetyBucket.SAFE) -> EligibilityDecision:
    """CI green AND safety bucket ranks at or above `min_bucket`.

    A row that failed to build at all (`row.error` set) is never eligible --
    there's no safety score or CI verdict to trust.
    """
    repo, number = row.pr.repo, row.pr.number
    if row.error is not None:
        return EligibilityDecision(repo, number, False, f"row failed to load: {row.error}")

    if row.ci_status is None or row.ci_status.verdict != CIVerdict.GREEN:
        verdict = row.ci_status.verdict.value if row.ci_status is not None else "unknown"
        return EligibilityDecision(repo, number, False, f"CI is not green (verdict={verdict})")

    if row.safety is None or _BUCKET_RANK[row.safety.bucket] < _BUCKET_RANK[min_bucket]:
        bucket = row.safety.bucket.value if row.safety is not None else "unknown"
        return EligibilityDecision(
            repo,
            number,
            False,
            f"safety bucket is {bucket}, below the {min_bucket.value} threshold",
        )

    return EligibilityDecision(repo, number, True)


@dataclass(frozen=True, slots=True)
class BulkPreview:
    """What a bulk action would do right now: who qualifies, who doesn't and why."""

    action: str
    """"approve" or "merge"."""
    min_bucket: SafetyBucket
    eligible: tuple[PRRow, ...]
    skipped: tuple[EligibilityDecision, ...]


async def _scoped_rows(fleet_service: FleetService, *, repo: str | None) -> list[PRRow]:
    """Every PR row in scope: one repo's, or the whole fleet's."""
    views = await fleet_service.get_fleet_view()
    if repo is not None:
        views = tuple(view for view in views if view.repo == repo)
    return [row for view in views for row in view.rows]


async def preview_bulk(
    fleet_service: FleetService,
    *,
    action: str,
    repo: str | None = None,
    min_bucket: SafetyBucket = SafetyBucket.SAFE,
) -> BulkPreview:
    """Split every in-scope PR into eligible / skipped-with-reason, for display
    before anything is confirmed. `repo=None` means fleet-wide."""
    if action not in BULK_ACTIONS:
        raise ValueError(f"unknown bulk action {action!r}; expected one of {sorted(BULK_ACTIONS)}")
    rows = await _scoped_rows(fleet_service, repo=repo)

    eligible: list[PRRow] = []
    skipped: list[EligibilityDecision] = []
    for row in rows:
        decision = is_eligible(row, min_bucket=min_bucket)
        if decision.eligible:
            eligible.append(row)
        else:
            skipped.append(decision)

    return BulkPreview(
        action=action, min_bucket=min_bucket, eligible=tuple(eligible), skipped=tuple(skipped)
    )


@dataclass(frozen=True, slots=True)
class BulkOutcome:
    """The result of executing a bulk action: one entry per in-scope PR.

    Every entry is either an `ActionResult` (attempted, however it turned
    out) or an `EligibilityDecision` with `eligible=False` (never attempted,
    filtered out by the fresh eligibility re-check at execution time).
    """

    action: str
    results: tuple[ActionResult | EligibilityDecision, ...]

    @property
    def acted_on(self) -> tuple[ActionResult, ...]:
        return tuple(r for r in self.results if isinstance(r, ActionResult))

    @property
    def skipped(self) -> tuple[EligibilityDecision, ...]:
        return tuple(r for r in self.results if isinstance(r, EligibilityDecision))


async def execute_bulk(
    fleet_service: FleetService,
    actions_service: ActionsService,
    *,
    action: str,
    repo: str | None = None,
    min_bucket: SafetyBucket = SafetyBucket.SAFE,
) -> BulkOutcome:
    """Re-check eligibility fresh, then act on every eligible PR sequentially.

    Never fail-fast: each PR's outcome (merged/approved/skipped/failed) is
    collected independently, so one GitHub rejection doesn't abort the rest
    of the batch. Eligibility is re-derived here rather than trusted from a
    prior `preview_bulk` call -- CI may have changed since the dashboard
    rendered the preview, and a PR that regressed in the meantime is skipped
    with a reason instead of being acted on.
    """
    preview = await preview_bulk(fleet_service, action=action, repo=repo, min_bucket=min_bucket)

    results: list[ActionResult | EligibilityDecision] = list(preview.skipped)
    for row in preview.eligible:
        if action == "approve":
            result = await actions_service.approve(row.pr.repo, row.pr.number)
        else:
            result = await actions_service.merge(row.pr.repo, row.pr.number, row.pr.head_sha)
        results.append(result)

    return BulkOutcome(action=action, results=tuple(results))
