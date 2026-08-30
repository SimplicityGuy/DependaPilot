"""Fleet dashboard service layer: discovery -> metadata -> CI -> scoring, per repo.

`FleetService.get_fleet_view` is the dashboard's single entry point: it reuses
`DiscoveryService`'s cached PR list, then concurrently builds one `RepoView`
per repo (and, within each repo, one `PRRow` per PR) by fetching that PR's
update metadata and CI verdict and running the safety rubric over the result.

Errors are captured at both levels rather than allowed to propagate, so one
repo's -- or one PR's -- failure degrades to an inline error instead of
blanking the whole page: a 404 mid-hydration, a repo the token can't see
Actions on, or any other per-repo/per-PR surprise leaves every unaffected row
rendering normally.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from dependapilot.ci import CIStatus, CIVerdictService
from dependapilot.discovery import DiscoveryService, PRRecord
from dependapilot.github import GitHubClient
from dependapilot.metadata import PRUpdateMetadata, fetch_pr_update_metadata
from dependapilot.scoring import DEFAULT_WEIGHTS, SafetyScore, ScoreWeights, score_pr

_BUMP_TITLE_RE: Final = re.compile(r"\bfrom\s+(\S+)\s+to\s+(\S+)\b")


@dataclass(frozen=True, slots=True)
class DependencySummary:
    """Presentation-ready summary of a PR's update-metadata for one dashboard row.

    Distinct from the scoring rubric's internal worst-case risk logic: this is
    purely "what should the row show", built even when metadata is unknown
    (falling back to the PR title / placeholders) and even when versions
    can't be determined at all -- degrading gracefully rather than omitting
    the row.
    """

    names: tuple[str, ...]
    semver_labels: tuple[str, ...]
    dependency_type_labels: tuple[str, ...]
    old_version: str | None
    new_version: str | None


@dataclass(frozen=True, slots=True)
class PRRow:
    """One dashboard row: a PR plus everything derived about it, or an error."""

    pr: PRRecord
    summary: DependencySummary | None = None
    metadata: PRUpdateMetadata | None = None
    ci_status: CIStatus | None = None
    safety: SafetyScore | None = None
    error: str | None = None
    """Set instead of the fields above when building this row failed."""


@dataclass(frozen=True, slots=True)
class RepoView:
    """One dashboard section: a repo plus its PR rows, or a section-level error."""

    repo: str
    audit_enabled: bool = False
    actions_enabled: bool = False
    """Mirrors `RepoConfig.actions`: whether this repo opted into dashboard actions."""
    rows: tuple[PRRow, ...] = ()
    error: str | None = None
    """Set instead of `rows` when this repo's PR list couldn't be hydrated."""


def parse_bump_title(title: str) -> tuple[str | None, str | None]:
    """Best-effort `(old_version, new_version)` from a Dependabot PR title.

    Dependabot titles typically read "Bump foo from 1.0.0 to 1.1.0[ in /dir]".
    Metadata's commit-trailer parser doesn't carry version numbers, so this is
    the only source for them; when the title doesn't match the expected shape
    (unusual title, edited by hand, etc.) both values come back `None` rather
    than raising -- callers must render "unknown" for either.
    """
    match = _BUMP_TITLE_RE.search(title)
    if match is None:
        return None, None
    return match.group(1), match.group(2)


def summarize_metadata(metadata: PRUpdateMetadata, pr_title: str) -> DependencySummary:
    """Build a `DependencySummary` for display, degrading gracefully when
    `metadata` carries no usable per-dependency facts."""
    old_version, new_version = parse_bump_title(pr_title)
    if not metadata.updates:
        return DependencySummary(
            names=(),
            semver_labels=(),
            dependency_type_labels=(),
            old_version=old_version,
            new_version=new_version,
        )

    names = tuple(update.dependency_name for update in metadata.updates)
    # `.name.lower()` for semver ("patch"/"minor"/"major"/"unknown") rather than
    # `.value` (the verbose trailer literal "version-update:semver-patch") --
    # this is purely a display label, not the value scoring keys off of.
    semver_labels = _unique_in_order(update.update_type.name.lower() for update in metadata.updates)
    dependency_type_labels = _unique_in_order(
        update.dependency_type.value for update in metadata.updates
    )
    return DependencySummary(
        names=names,
        semver_labels=semver_labels,
        dependency_type_labels=dependency_type_labels,
        old_version=old_version,
        new_version=new_version,
    )


def _unique_in_order(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class FleetService:
    """Composes discovery, metadata, CI, and scoring into dashboard-ready views."""

    def __init__(
        self,
        client: GitHubClient,
        discovery: DiscoveryService,
        ci_service: CIVerdictService,
        *,
        weights: ScoreWeights = DEFAULT_WEIGHTS,
        audit_enabled_repos: frozenset[str] = frozenset(),
        actions_enabled_repos: frozenset[str] = frozenset(),
    ) -> None:
        self._client = client
        self._discovery = discovery
        self._ci_service = ci_service
        self._weights = weights
        self._audit_enabled_repos = audit_enabled_repos
        self._actions_enabled_repos = actions_enabled_repos

    async def get_fleet_view(self, *, force_refresh: bool = False) -> tuple[RepoView, ...]:
        """Every managed repo's dashboard section, built concurrently.

        A repo whose PR list came back from discovery but whose per-PR
        hydration raises is represented as a `RepoView` with `error` set
        instead of raising -- the caller always gets one view per repo.
        """
        prs_by_repo = await self._discovery.discover(force_refresh=force_refresh)
        results = await asyncio.gather(
            *(self._build_repo_view(repo, prs) for repo, prs in prs_by_repo.items()),
            return_exceptions=True,
        )

        views: list[RepoView] = []
        for repo, result in zip(prs_by_repo.keys(), results, strict=True):
            if isinstance(result, BaseException):
                views.append(RepoView(repo=repo, error=str(result)))
            else:
                views.append(result)
        return tuple(views)

    async def _build_repo_view(self, repo: str, prs: list[PRRecord]) -> RepoView:
        rows = await asyncio.gather(*(self._build_row(repo, pr) for pr in prs))
        return RepoView(
            repo=repo,
            audit_enabled=repo in self._audit_enabled_repos,
            actions_enabled=repo in self._actions_enabled_repos,
            rows=tuple(rows),
        )

    async def _build_row(self, repo: str, pr: PRRecord) -> PRRow:
        try:
            metadata, ci_status = await asyncio.gather(
                fetch_pr_update_metadata(self._client, repo, pr.number),
                self._ci_service.get_ci_status(repo, pr.head_sha),
            )
        except Exception as exc:  # noqa: BLE001 -- isolate one PR's failure from the rest
            return PRRow(pr=pr, error=str(exc))

        safety = score_pr(
            metadata,
            ci_status.verdict,
            created_at=pr.created_at,
            mergeable=pr.mergeable,
            mergeable_state=pr.mergeable_state,
            # No source of "does this PR close an open Dependabot alert" exists
            # yet; always omit rather than guess. See `score_pr`'s docstring.
            closes_open_alert=None,
            weights=self._weights,
        )
        summary = summarize_metadata(metadata, pr.title)
        return PRRow(pr=pr, summary=summary, metadata=metadata, ci_status=ci_status, safety=safety)
