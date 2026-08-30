"""Deterministic merge-safety rubric: score + bucket one Dependabot PR update.

`score_pr` is a pure function over facts already gathered by the other services
(update-metadata, CI verdict, PR facts, and an optional Dependabot-alert fact) --
no I/O, no clock reads unless `now` is omitted, so the dashboard and any future
bulk-merge decision can call it synchronously and reproducibly. Every signal
that moves the score is recorded, in the order it was applied, as a
`SignalBreakdown` entry so a human (or a bug report) can always see *why* a PR
landed where it did -- never a black box.

Two properties are stronger than "weighted sum, threshold the result":

- CI is a **hard cap**, not just a heavily-weighted signal: a PR can never land
  in `SAFE` unless CI is green, and a failing verdict forces `UNSAFE`
  outright, regardless of how favorable every other signal is. This holds
  even if someone retunes `ScoreWeights` later -- it's enforced as a cap on
  the bucket, not baked into a delta that could be out-tuned.
- Unknown/untrusted update-metadata (trailer missing, malformed, or a commit
  not authored by dependabot[bot]) degrades the same way: heavy score penalty
  *and* a bucket cap, since a PR whose dependency facts can't be trusted must
  never read as safe just because every other signal happens to look good.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from dependapilot.ci import CIVerdict
from dependapilot.metadata import DependencyType, PRUpdateMetadata, SemverUpdateType

STALE_AFTER_DAYS: Final = 30
"""PR age (days) beyond which Dependabot's auto-rebase has stopped and staleness flags."""


class SafetyBucket(StrEnum):
    """The three-way merge-safety verdict shown on the dashboard."""

    SAFE = "safe"
    CAUTION = "caution"
    UNSAFE = "unsafe"


_BUCKET_RANK: Final = {SafetyBucket.UNSAFE: 0, SafetyBucket.CAUTION: 1, SafetyBucket.SAFE: 2}
"""Ordering used to apply bucket *caps* (never raises a bucket, only lowers it)."""

# `UNKNOWN` ranks worse than the worst known value in each table -- an
# update-type or dependency-type this parser doesn't recognize is treated as
# riskier than a known major bump / indirect dependency: conservative
# degrade, never a crash or a free pass.
_SEMVER_RISK_ORDER: Final = {
    SemverUpdateType.PATCH: 0,
    SemverUpdateType.MINOR: 1,
    SemverUpdateType.MAJOR: 2,
    SemverUpdateType.UNKNOWN: 3,
}

_DEPENDENCY_TYPE_RISK_ORDER: Final = {
    DependencyType.DIRECT_DEVELOPMENT: 0,
    DependencyType.DIRECT_PRODUCTION: 1,
    DependencyType.INDIRECT: 2,
    DependencyType.UNKNOWN: 3,
}


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """The one tunable table every signal's point delta and bucket threshold lives in.

    `base_score` is the starting point before any signal is applied; every
    other field is a delta added to (or subtracted from) it. Thresholds are
    inclusive lower bounds: a raw score >= `safe_threshold` is (before caps)
    `SAFE`, >= `caution_threshold` is `CAUTION`, otherwise `UNSAFE`.
    """

    base_score: int = 60

    # CI verdict (always contributes -- see also the hard-cap logic in `score_pr`).
    ci_green: int = 10
    ci_pending: int = -10
    ci_no_ci: int = -15
    ci_failing: int = -60

    # Semver bump size of the riskiest update in the PR.
    semver_patch: int = 15
    semver_minor: int = 0
    semver_major: int = -25
    semver_unknown: int = -30

    # Dependency type of the riskiest update in the PR.
    dependency_type_direct_development: int = 10
    dependency_type_direct_production: int = 0
    dependency_type_indirect: int = -15
    dependency_type_unknown: int = -20

    # Mergeability.
    mergeable_clean: int = 10
    mergeable_conflicting: int = -20
    mergeable_not_clean: int = 0
    mergeable_unknown: int = 0

    # Optional signal: only ever applied when the caller has a real answer
    # (i.e. `security_events` capability is available) -- see `score_pr`.
    closes_open_alert: int = 10

    # Staleness (PR open > STALE_AFTER_DAYS): auto-rebase has stopped mattering.
    stale: int = -10

    # Update-metadata could not be trusted or extracted at all (see `score_pr`).
    metadata_unknown: int = -30

    safe_threshold: int = 80
    caution_threshold: int = 50


DEFAULT_WEIGHTS: Final = ScoreWeights()


@dataclass(frozen=True, slots=True)
class SignalBreakdown:
    """One signal's contribution to the score, in the order it was applied."""

    signal: str
    delta: int
    reason: str


@dataclass(frozen=True, slots=True)
class SafetyScore:
    """The rubric's verdict for one PR: a 0-100 score, a bucket, and why."""

    score: int
    bucket: SafetyBucket
    breakdown: tuple[SignalBreakdown, ...]
    stale: bool


def _semver_delta(update_type: SemverUpdateType, weights: ScoreWeights) -> tuple[int, str]:
    return {
        SemverUpdateType.PATCH: (weights.semver_patch, "patch update"),
        SemverUpdateType.MINOR: (weights.semver_minor, "minor update"),
        SemverUpdateType.MAJOR: (weights.semver_major, "major update"),
        SemverUpdateType.UNKNOWN: (weights.semver_unknown, "unrecognized update-type"),
    }[update_type]


def _dependency_type_delta(dep_type: DependencyType, weights: ScoreWeights) -> tuple[int, str]:
    return {
        DependencyType.DIRECT_DEVELOPMENT: (
            weights.dependency_type_direct_development,
            "direct development dependency",
        ),
        DependencyType.DIRECT_PRODUCTION: (
            weights.dependency_type_direct_production,
            "direct production dependency",
        ),
        DependencyType.INDIRECT: (weights.dependency_type_indirect, "indirect dependency"),
        DependencyType.UNKNOWN: (
            weights.dependency_type_unknown,
            "unrecognized dependency-type",
        ),
    }[dep_type]


def _worst[RiskT: (SemverUpdateType, DependencyType)](
    risk_order: Mapping[RiskT, int], values: Iterable[RiskT]
) -> RiskT:
    """Pick the riskiest value in `values` (highest rank in `risk_order`)."""
    return max(values, key=lambda value: risk_order[value])


def _ci_delta(verdict: CIVerdict, weights: ScoreWeights) -> tuple[int, str]:
    return {
        CIVerdict.GREEN: (weights.ci_green, "CI is green"),
        CIVerdict.PENDING: (weights.ci_pending, "CI is still pending"),
        CIVerdict.NO_CI: (weights.ci_no_ci, "no CI signal was reported"),
        CIVerdict.FAILING: (weights.ci_failing, "CI is failing"),
    }[verdict]


def _mergeable_delta(
    mergeable: bool | None, mergeable_state: str | None, weights: ScoreWeights
) -> tuple[int, str]:
    if mergeable is False:
        return weights.mergeable_conflicting, "not mergeable (conflicts with base branch)"
    if mergeable is True and mergeable_state == "clean":
        return weights.mergeable_clean, "mergeable, clean"
    if mergeable is True:
        state = mergeable_state or "unknown"
        return weights.mergeable_not_clean, f"mergeable but not clean (state={state})"
    return weights.mergeable_unknown, "mergeability not yet known"


def _parse_created_at(created_at: str) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp; None (never raises) if it doesn't parse."""
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_stale(created_at: str, now: datetime) -> bool:
    parsed = _parse_created_at(created_at)
    if parsed is None:
        return False
    age_days = (now - parsed).total_seconds() / 86400
    return age_days > STALE_AFTER_DAYS


def _cap(bucket: SafetyBucket, ceiling: SafetyBucket) -> SafetyBucket:
    """Lower `bucket` to `ceiling` if it currently ranks above it; never raises it."""
    return bucket if _BUCKET_RANK[bucket] <= _BUCKET_RANK[ceiling] else ceiling


def score_pr(
    metadata: PRUpdateMetadata,
    ci_verdict: CIVerdict,
    *,
    created_at: str,
    mergeable: bool | None,
    mergeable_state: str | None = None,
    closes_open_alert: bool | None = None,
    now: datetime | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> SafetyScore:
    """Score and bucket one PR's merge safety, with a full per-signal breakdown.

    `closes_open_alert` should be `None` whenever the caller can't answer the
    question (typically because `Capabilities.security_events` is falsy) --
    passing `None` omits the signal from the breakdown entirely, with no
    penalty, rather than guessing. Pass `True`/`False` only when the fact is
    actually known.
    """
    now = now or datetime.now(UTC)
    breakdown: list[SignalBreakdown] = []
    score = weights.base_score

    ci_delta, ci_reason = _ci_delta(ci_verdict, weights)
    score += ci_delta
    breakdown.append(SignalBreakdown("ci_verdict", ci_delta, ci_reason))

    metadata_unknown = metadata.status.is_unknown or not metadata.updates
    if metadata_unknown:
        score += weights.metadata_unknown
        breakdown.append(
            SignalBreakdown(
                "metadata_unknown",
                weights.metadata_unknown,
                f"update metadata could not be trusted (status={metadata.status.value})",
            )
        )
    else:
        worst_semver = _worst(_SEMVER_RISK_ORDER, {u.update_type for u in metadata.updates})
        semver_delta, semver_reason = _semver_delta(worst_semver, weights)
        score += semver_delta
        breakdown.append(SignalBreakdown("semver", semver_delta, semver_reason))

        worst_dep_type = _worst(
            _DEPENDENCY_TYPE_RISK_ORDER, {u.dependency_type for u in metadata.updates}
        )
        dep_delta, dep_reason = _dependency_type_delta(worst_dep_type, weights)
        score += dep_delta
        breakdown.append(SignalBreakdown("dependency_type", dep_delta, dep_reason))

    mergeable_delta, mergeable_reason = _mergeable_delta(mergeable, mergeable_state, weights)
    score += mergeable_delta
    breakdown.append(SignalBreakdown("mergeable", mergeable_delta, mergeable_reason))

    if closes_open_alert is not None:
        alert_delta = weights.closes_open_alert if closes_open_alert else 0
        alert_reason = (
            "closes an open Dependabot alert"
            if closes_open_alert
            else "does not close an open Dependabot alert"
        )
        breakdown.append(SignalBreakdown("closes_open_alert", alert_delta, alert_reason))
        score += alert_delta

    stale = _is_stale(created_at, now)
    if stale:
        score += weights.stale
        breakdown.append(
            SignalBreakdown(
                "stale", weights.stale, f"PR has been open more than {STALE_AFTER_DAYS} days"
            )
        )

    score = max(0, min(100, score))

    if score >= weights.safe_threshold:
        bucket = SafetyBucket.SAFE
    elif score >= weights.caution_threshold:
        bucket = SafetyBucket.CAUTION
    else:
        bucket = SafetyBucket.UNSAFE

    if ci_verdict == CIVerdict.FAILING:
        if bucket != SafetyBucket.UNSAFE:
            breakdown.append(
                SignalBreakdown(
                    "ci_safety_cap", 0, "CI is failing: PR is forced unsafe regardless of score"
                )
            )
        bucket = SafetyBucket.UNSAFE
    elif ci_verdict != CIVerdict.GREEN:
        capped = _cap(bucket, SafetyBucket.CAUTION)
        if capped != bucket:
            breakdown.append(
                SignalBreakdown("ci_safety_cap", 0, "CI is not green: PR cannot be marked safe")
            )
        bucket = capped

    if metadata_unknown:
        capped = _cap(bucket, SafetyBucket.CAUTION)
        if capped != bucket:
            breakdown.append(
                SignalBreakdown(
                    "metadata_safety_cap",
                    0,
                    "update metadata is untrusted: PR cannot be marked safe",
                )
            )
        bucket = capped

    return SafetyScore(score=score, bucket=bucket, breakdown=tuple(breakdown), stale=stale)
