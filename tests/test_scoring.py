"""Tests for the deterministic merge-safety rubric.

Pure-function tests -- no I/O, no mocks. Table-driven cases pin score + bucket
for representative PRs per the acceptance criteria; targeted cases cover the
hard caps and conservative degrade paths individually.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from dependapilot.ci import CIVerdict
from dependapilot.metadata import (
    DependencyType,
    DependencyUpdate,
    MetadataStatus,
    PRUpdateMetadata,
    SemverUpdateType,
)
from dependapilot.scoring import STALE_AFTER_DAYS, SafetyBucket, ScoreWeights, score_pr

NOW = datetime(2026, 8, 29, tzinfo=UTC)
FRESH = NOW.isoformat().replace("+00:00", "Z")
STALE = (NOW - timedelta(days=STALE_AFTER_DAYS + 1)).isoformat().replace("+00:00", "Z")


def _metadata(
    *, update_type: SemverUpdateType, dependency_type: DependencyType, name: str = "foo"
) -> PRUpdateMetadata:
    return PRUpdateMetadata(
        status=MetadataStatus.PARSED,
        updates=(
            DependencyUpdate(
                dependency_name=name, dependency_type=dependency_type, update_type=update_type
            ),
        ),
    )


@dataclass(frozen=True)
class Case:
    id: str
    metadata: PRUpdateMetadata
    ci_verdict: CIVerdict
    mergeable: bool | None
    mergeable_state: str | None
    expected_bucket: SafetyBucket


TABLE: list[Case] = [
    Case(
        "green patch dev-dep is safe",
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.GREEN,
        True,
        "clean",
        SafetyBucket.SAFE,
    ),
    Case(
        "green major prod-dep is caution",
        _metadata(
            update_type=SemverUpdateType.MAJOR, dependency_type=DependencyType.DIRECT_PRODUCTION
        ),
        CIVerdict.GREEN,
        True,
        "clean",
        SafetyBucket.CAUTION,
    ),
    Case(
        "failing patch dev-dep is still unsafe",
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.FAILING,
        True,
        "clean",
        SafetyBucket.UNSAFE,
    ),
    Case(
        "failing major indirect is unsafe",
        _metadata(update_type=SemverUpdateType.MAJOR, dependency_type=DependencyType.INDIRECT),
        CIVerdict.FAILING,
        False,
        "dirty",
        SafetyBucket.UNSAFE,
    ),
    Case(
        "no_ci with otherwise-perfect signals is never safe",
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.NO_CI,
        True,
        "clean",
        SafetyBucket.CAUTION,
    ),
    Case(
        "pending with good signals is never safe",
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.PENDING,
        True,
        "clean",
        SafetyBucket.CAUTION,
    ),
]


@pytest.mark.parametrize("case", TABLE, ids=lambda case: case.id)
def test_table_driven_bucket(case: Case) -> None:
    result = score_pr(
        case.metadata,
        case.ci_verdict,
        created_at=FRESH,
        mergeable=case.mergeable,
        mergeable_state=case.mergeable_state,
        now=NOW,
    )

    assert result.bucket == case.expected_bucket


def test_green_patch_dev_dep_score_and_breakdown() -> None:
    result = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        now=NOW,
    )

    assert result.bucket == SafetyBucket.SAFE
    assert result.stale is False
    signals = {entry.signal for entry in result.breakdown}
    assert signals == {"ci_verdict", "semver", "dependency_type", "mergeable"}
    # Every recorded signal carries a positive delta in this all-favorable case.
    assert all(entry.delta > 0 for entry in result.breakdown)


def test_no_ci_never_safe_even_with_all_positive_signals() -> None:
    result = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.NO_CI,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        closes_open_alert=True,
        now=NOW,
    )

    assert result.bucket != SafetyBucket.SAFE
    assert any(entry.signal == "ci_safety_cap" for entry in result.breakdown)


def test_failing_forces_unsafe_regardless_of_score() -> None:
    """The FAILING cap holds even if a retuned weight table would otherwise
    let a failing PR score into SAFE range -- it's an override, not a delta."""
    lenient_weights = ScoreWeights(base_score=95, ci_failing=0)

    result = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.FAILING,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        closes_open_alert=True,
        now=NOW,
        weights=lenient_weights,
    )

    assert result.score >= lenient_weights.safe_threshold
    assert result.bucket == SafetyBucket.UNSAFE
    assert any(entry.signal == "ci_safety_cap" for entry in result.breakdown)


@pytest.mark.parametrize(
    "status",
    [
        MetadataStatus.TRAILER_MISSING,
        MetadataStatus.TRAILER_MALFORMED,
        MetadataStatus.UNTRUSTED_AUTHOR,
    ],
)
def test_unknown_metadata_degrades_conservatively_never_crashes(
    status: MetadataStatus,
) -> None:
    metadata = PRUpdateMetadata(status=status, updates=())

    result = score_pr(
        metadata,
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        now=NOW,
    )

    assert result.bucket != SafetyBucket.SAFE
    assert any(entry.signal == "metadata_unknown" for entry in result.breakdown)
    assert any(entry.delta < 0 for entry in result.breakdown)


def test_parsed_status_with_no_extracted_updates_also_degrades() -> None:
    """A trailer that parsed structurally but yielded zero usable entries."""
    metadata = PRUpdateMetadata(status=MetadataStatus.PARSED, updates=())

    result = score_pr(
        metadata,
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        now=NOW,
    )

    assert result.bucket != SafetyBucket.SAFE
    assert any(entry.signal == "metadata_unknown" for entry in result.breakdown)


def test_missing_alert_scope_omits_signal_without_penalty() -> None:
    with_none = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        closes_open_alert=None,
        now=NOW,
    )

    assert not any(entry.signal == "closes_open_alert" for entry in with_none.breakdown)

    with_false = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        closes_open_alert=False,
        now=NOW,
    )

    alert_entries = [e for e in with_false.breakdown if e.signal == "closes_open_alert"]
    assert len(alert_entries) == 1
    assert alert_entries[0].delta == 0
    assert with_false.score == with_none.score


def test_closes_open_alert_is_a_positive_signal_when_true() -> None:
    without = score_pr(
        _metadata(update_type=SemverUpdateType.MINOR, dependency_type=DependencyType.INDIRECT),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        closes_open_alert=False,
        now=NOW,
    )
    with_alert = score_pr(
        _metadata(update_type=SemverUpdateType.MINOR, dependency_type=DependencyType.INDIRECT),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        closes_open_alert=True,
        now=NOW,
    )

    assert with_alert.score > without.score


def test_stale_pr_flagged_and_penalized() -> None:
    fresh = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        now=NOW,
    )
    stale = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.GREEN,
        created_at=STALE,
        mergeable=True,
        mergeable_state="clean",
        now=NOW,
    )

    assert fresh.stale is False
    assert stale.stale is True
    assert stale.score < fresh.score
    assert any(entry.signal == "stale" for entry in stale.breakdown)


def test_semver_and_dependency_type_use_worst_case_across_grouped_updates() -> None:
    """A grouped-update PR is scored by its riskiest member, not its safest."""
    metadata = PRUpdateMetadata(
        status=MetadataStatus.PARSED,
        updates=(
            DependencyUpdate("safe-dep", DependencyType.DIRECT_DEVELOPMENT, SemverUpdateType.PATCH),
            DependencyUpdate("risky-dep", DependencyType.INDIRECT, SemverUpdateType.MAJOR),
        ),
    )

    result = score_pr(
        metadata,
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        now=NOW,
    )

    semver_entry = next(e for e in result.breakdown if e.signal == "semver")
    dep_entry = next(e for e in result.breakdown if e.signal == "dependency_type")
    assert "major" in semver_entry.reason
    assert "indirect" in dep_entry.reason


def test_score_is_clamped_to_0_100() -> None:
    metadata = PRUpdateMetadata(status=MetadataStatus.TRAILER_MISSING, updates=())

    result = score_pr(
        metadata,
        CIVerdict.FAILING,
        created_at=STALE,
        mergeable=False,
        mergeable_state="dirty",
        now=NOW,
    )

    assert 0 <= result.score <= 100
    assert result.bucket == SafetyBucket.UNSAFE


def test_not_mergeable_is_penalized() -> None:
    clean = score_pr(
        _metadata(
            update_type=SemverUpdateType.MINOR, dependency_type=DependencyType.DIRECT_PRODUCTION
        ),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=True,
        mergeable_state="clean",
        now=NOW,
    )
    conflicting = score_pr(
        _metadata(
            update_type=SemverUpdateType.MINOR, dependency_type=DependencyType.DIRECT_PRODUCTION
        ),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=False,
        mergeable_state="dirty",
        now=NOW,
    )

    assert conflicting.score < clean.score


def test_unknown_mergeability_is_neutral() -> None:
    result = score_pr(
        _metadata(
            update_type=SemverUpdateType.PATCH, dependency_type=DependencyType.DIRECT_DEVELOPMENT
        ),
        CIVerdict.GREEN,
        created_at=FRESH,
        mergeable=None,
        mergeable_state=None,
        now=NOW,
    )

    entry = next(e for e in result.breakdown if e.signal == "mergeable")
    assert entry.delta == 0
