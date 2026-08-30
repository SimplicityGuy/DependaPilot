"""Update-metadata parser: extract structured facts from Dependabot commit trailers.

Dependabot stamps every commit it authors with a YAML trailer after a `---`
separator, e.g.::

    Bumps [foo](https://github.com/acme/foo) from 1.0.0 to 1.1.0.

    ---
    updated-dependencies:
    - dependency-name: foo
      dependency-type: direct:production
      update-type: version-update:semver-minor
    ...

    Signed-off-by: dependabot[bot] <support@github.com>

`updated-dependencies` is a *list* -- grouped-update PRs stamp one entry per
dependency in a single commit. This module fetches a PR's commits, verifies
every commit is authored by `dependabot[bot]` (flagging otherwise -- a PR with
a non-bot commit may have been tampered with), and parses the trailer into
typed `DependencyUpdate` records.

Security-advisory PRs and other edge cases may carry no trailer, or one that
doesn't parse as expected. Both are tolerated as an explicit unknown
`MetadataStatus` rather than raising, so callers never need to guard this with
a try/except to keep the rest of the pipeline running.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import yaml

from dependapilot.discovery import DEPENDABOT_LOGIN
from dependapilot.github import GitHubClient

_UNKNOWN_AUTHOR = "unknown"


class DependencyType(StrEnum):
    """How a dependency relates to the project, per Dependabot's trailer."""

    DIRECT_PRODUCTION = "direct:production"
    DIRECT_DEVELOPMENT = "direct:development"
    INDIRECT = "indirect"
    UNKNOWN = "unknown"
    """The trailer carried a value this parser doesn't recognize."""


class SemverUpdateType(StrEnum):
    """The semver bump size of an update, per Dependabot's trailer."""

    MAJOR = "version-update:semver-major"
    MINOR = "version-update:semver-minor"
    PATCH = "version-update:semver-patch"
    UNKNOWN = "unknown"
    """The trailer carried a value this parser doesn't recognize."""


class MetadataStatus(StrEnum):
    """How confidently `updated-dependencies` facts could be extracted."""

    PARSED = "parsed"
    """Every commit was authored by dependabot[bot] and a trailer parsed cleanly."""

    UNTRUSTED_AUTHOR = "untrusted_author"
    """At least one commit was not authored by dependabot[bot] -- possibly tampered."""

    TRAILER_MISSING = "trailer_missing"
    """No commit carried a `---` YAML trailer (e.g. some security-advisory PRs)."""

    TRAILER_MALFORMED = "trailer_malformed"
    """A trailer was found but didn't parse into the expected shape."""

    @property
    def is_unknown(self) -> bool:
        """True when no dependency facts could be trusted or extracted."""
        return self in (
            MetadataStatus.UNTRUSTED_AUTHOR,
            MetadataStatus.TRAILER_MISSING,
            MetadataStatus.TRAILER_MALFORMED,
        )


@dataclass(frozen=True, slots=True)
class DependencyUpdate:
    """One dependency's update facts, from one `updated-dependencies` entry."""

    dependency_name: str
    dependency_type: DependencyType
    update_type: SemverUpdateType

    @classmethod
    def from_entry(cls, entry: Any) -> DependencyUpdate | None:
        """Build an update from one parsed YAML list entry.

        Returns None if `entry` isn't a mapping or is missing a usable
        `dependency-name` -- callers should skip such entries rather than
        fail the whole trailer over one bad item.
        """
        if not isinstance(entry, dict):
            return None
        name = entry.get("dependency-name")
        if not isinstance(name, str) or not name:
            return None
        return cls(
            dependency_name=name,
            dependency_type=_parse_dependency_type(entry.get("dependency-type")),
            update_type=_parse_semver_update_type(entry.get("update-type")),
        )


@dataclass(frozen=True, slots=True)
class PRUpdateMetadata:
    """The update-metadata facts extracted from one Dependabot PR's commits."""

    status: MetadataStatus
    updates: tuple[DependencyUpdate, ...]
    untrusted_authors: tuple[str, ...] = ()
    """Logins of commit authors that weren't dependabot[bot], if any.

    "unknown" stands in for a commit with no linked GitHub account.
    """


def _parse_dependency_type(value: Any) -> DependencyType:
    """Map `value` onto `DependencyType`, falling back to `UNKNOWN`.

    Guards against Dependabot introducing a new dependency-type value this
    parser doesn't know about yet -- never raises.
    """
    if isinstance(value, str):
        try:
            return DependencyType(value)
        except ValueError:
            pass
    return DependencyType.UNKNOWN


def _parse_semver_update_type(value: Any) -> SemverUpdateType:
    """Map `value` onto `SemverUpdateType`, falling back to `UNKNOWN`.

    Guards against Dependabot introducing a new update-type value this parser
    doesn't know about yet -- never raises.
    """
    if isinstance(value, str):
        try:
            return SemverUpdateType(value)
        except ValueError:
            pass
    return SemverUpdateType.UNKNOWN


def _commit_author_login(commit: dict[str, Any]) -> str:
    """The GitHub login associated with a commit, or "unknown" if none."""
    author = commit.get("author")
    if isinstance(author, dict) and isinstance(author.get("login"), str) and author["login"]:
        return str(author["login"])
    return _UNKNOWN_AUTHOR


def _extract_yaml_block(commit_message: str) -> str | None:
    """Return the text between a `---` trailer marker and the next `...`/EOF.

    Returns None if the message has no `---` line at all.
    """
    lines = commit_message.splitlines()
    try:
        start = lines.index("---")
    except ValueError:
        return None

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == "...":
            end = index
            break

    block = "\n".join(lines[start + 1 : end]).strip()
    return block or None


def _parse_updated_dependencies(trailer_yaml: str) -> list[DependencyUpdate] | None:
    """Parse a trailer's YAML body into `DependencyUpdate`s.

    Returns None if the YAML doesn't parse, or doesn't have the expected
    `{"updated-dependencies": [...]}` shape -- a signal to the caller that
    the trailer is malformed, not merely empty.
    """
    try:
        parsed = yaml.safe_load(trailer_yaml)
    except yaml.YAMLError:
        return None

    if not isinstance(parsed, dict):
        return None
    entries = parsed.get("updated-dependencies")
    if not isinstance(entries, list):
        return None

    updates: list[DependencyUpdate] = []
    for entry in entries:
        update = DependencyUpdate.from_entry(entry)
        if update is not None:
            updates.append(update)
    return updates


def parse_pr_commits(commits: Sequence[dict[str, Any]]) -> PRUpdateMetadata:
    """Derive `PRUpdateMetadata` from a PR's `GET .../pulls/{n}/commits` payload.

    Pure and synchronous -- fetching is `fetch_pr_update_metadata`'s job, this
    is the parsing logic tests exercise directly against fixture payloads.
    """
    if not commits:
        return PRUpdateMetadata(status=MetadataStatus.TRAILER_MISSING, updates=())

    untrusted_authors = tuple(
        login for commit in commits if (login := _commit_author_login(commit)) != DEPENDABOT_LOGIN
    )
    if untrusted_authors:
        return PRUpdateMetadata(
            status=MetadataStatus.UNTRUSTED_AUTHOR,
            updates=(),
            untrusted_authors=untrusted_authors,
        )

    found_trailer = False
    found_malformed_trailer = False
    updates: list[DependencyUpdate] = []
    for commit in commits:
        message = commit.get("commit", {}).get("message", "")
        block = _extract_yaml_block(message)
        if block is None:
            continue
        found_trailer = True
        entries = _parse_updated_dependencies(block)
        if entries is None:
            found_malformed_trailer = True
            continue
        updates.extend(entries)

    if not found_trailer:
        return PRUpdateMetadata(status=MetadataStatus.TRAILER_MISSING, updates=())
    if not updates and found_malformed_trailer:
        return PRUpdateMetadata(status=MetadataStatus.TRAILER_MALFORMED, updates=())
    return PRUpdateMetadata(status=MetadataStatus.PARSED, updates=tuple(updates))


async def fetch_pr_update_metadata(
    client: GitHubClient, repo: str, pr_number: int
) -> PRUpdateMetadata:
    """Fetch `repo`'s PR #`pr_number` commits and parse their update metadata."""
    owner, name = repo.split("/", 1)
    commits = await client.paginate_all(f"/repos/{owner}/{name}/pulls/{pr_number}/commits")
    return parse_pr_commits(commits)
