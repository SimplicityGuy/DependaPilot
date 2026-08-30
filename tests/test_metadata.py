"""Tests for the Dependabot commit-trailer update-metadata parser.

`parse_pr_commits` is pure and synchronous, so most cases are exercised
directly against fixture commit payloads; `fetch_pr_update_metadata` gets a
thin test over a mocked `GitHubClient` to confirm it wires the fetch and
parse steps together correctly.
"""

from __future__ import annotations

from typing import Any

import httpx

from dependapilot.metadata import (
    DependencyType,
    DependencyUpdate,
    MetadataStatus,
    PRUpdateMetadata,
    SemverUpdateType,
    fetch_pr_update_metadata,
    parse_pr_commits,
)
from tests.github.conftest import make_client

SINGLE_DEP_MESSAGE = """\
Bumps [foo](https://github.com/acme/foo) from 1.0.0 to 1.0.1.

---
updated-dependencies:
- dependency-name: foo
  dependency-type: direct:production
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

GROUPED_MULTI_DEP_MESSAGE = """\
Bumps the npm-deps group with 2 updates: [foo](...) and [bar](...).

---
updated-dependencies:
- dependency-name: foo
  dependency-type: direct:production
  update-type: version-update:semver-minor
- dependency-name: bar
  dependency-type: direct:development
  update-type: version-update:semver-major
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

MISSING_TRAILER_MESSAGE = """\
Bumps [foo](https://github.com/acme/foo) from 1.0.0 to 1.0.1 to patch a \
security advisory.
"""

MALFORMED_TRAILER_MESSAGE = """\
Bumps [foo](https://github.com/acme/foo) from 1.0.0 to 1.0.1.

---
this is not: [valid, {ya ml
...
"""

NOT_A_LIST_TRAILER_MESSAGE = """\
Bumps [foo](https://github.com/acme/foo) from 1.0.0 to 1.0.1.

---
updated-dependencies: not-a-list
...
"""

UNKNOWN_TYPE_VALUES_MESSAGE = """\
Bumps [foo](https://github.com/acme/foo) from 1.0.0 to 1.0.1.

---
updated-dependencies:
- dependency-name: foo
  dependency-type: some-new-type-dependabot-invents-later
  update-type: version-update:semver-prerelease
...
"""


def commit(
    *, message: str, login: str | None = "dependabot[bot]", sha: str = "abc123"
) -> dict[str, Any]:
    """Build one item of a `GET .../pulls/{n}/commits` payload."""
    return {
        "sha": sha,
        "commit": {"message": message},
        "author": {"login": login} if login is not None else None,
    }


class TestParsePRCommitsSingleDependency:
    def test_parses_a_single_dependency_update(self) -> None:
        result = parse_pr_commits([commit(message=SINGLE_DEP_MESSAGE)])

        assert result == PRUpdateMetadata(
            status=MetadataStatus.PARSED,
            updates=(
                DependencyUpdate(
                    dependency_name="foo",
                    dependency_type=DependencyType.DIRECT_PRODUCTION,
                    update_type=SemverUpdateType.PATCH,
                ),
            ),
        )


class TestParsePRCommitsGroupedMultiDependency:
    def test_yields_one_record_per_dependency(self) -> None:
        result = parse_pr_commits([commit(message=GROUPED_MULTI_DEP_MESSAGE)])

        assert result.status == MetadataStatus.PARSED
        assert result.updates == (
            DependencyUpdate(
                dependency_name="foo",
                dependency_type=DependencyType.DIRECT_PRODUCTION,
                update_type=SemverUpdateType.MINOR,
            ),
            DependencyUpdate(
                dependency_name="bar",
                dependency_type=DependencyType.DIRECT_DEVELOPMENT,
                update_type=SemverUpdateType.MAJOR,
            ),
        )


class TestParsePRCommitsMissingTrailer:
    def test_missing_trailer_is_an_explicit_unknown_status_not_a_crash(self) -> None:
        result = parse_pr_commits([commit(message=MISSING_TRAILER_MESSAGE)])

        assert result == PRUpdateMetadata(status=MetadataStatus.TRAILER_MISSING, updates=())
        assert result.status.is_unknown

    def test_no_commits_at_all_is_also_trailer_missing(self) -> None:
        result = parse_pr_commits([])

        assert result.status == MetadataStatus.TRAILER_MISSING
        assert result.updates == ()


class TestParsePRCommitsMalformedTrailer:
    def test_invalid_yaml_is_tolerated_as_malformed(self) -> None:
        result = parse_pr_commits([commit(message=MALFORMED_TRAILER_MESSAGE)])

        assert result.status == MetadataStatus.TRAILER_MALFORMED
        assert result.updates == ()
        assert result.status.is_unknown

    def test_non_list_updated_dependencies_is_tolerated_as_malformed(self) -> None:
        result = parse_pr_commits([commit(message=NOT_A_LIST_TRAILER_MESSAGE)])

        assert result.status == MetadataStatus.TRAILER_MALFORMED
        assert result.updates == ()

    def test_unrecognized_type_values_fall_back_to_unknown_members(self) -> None:
        result = parse_pr_commits([commit(message=UNKNOWN_TYPE_VALUES_MESSAGE)])

        assert result.status == MetadataStatus.PARSED
        assert result.updates == (
            DependencyUpdate(
                dependency_name="foo",
                dependency_type=DependencyType.UNKNOWN,
                update_type=SemverUpdateType.UNKNOWN,
            ),
        )

    def test_entry_missing_dependency_name_is_skipped_not_fatal(self) -> None:
        message = """\
---
updated-dependencies:
- dependency-type: direct:production
  update-type: version-update:semver-patch
- dependency-name: bar
  dependency-type: indirect
  update-type: version-update:semver-patch
...
"""
        result = parse_pr_commits([commit(message=message)])

        assert result.status == MetadataStatus.PARSED
        assert [update.dependency_name for update in result.updates] == ["bar"]


class TestParsePRCommitsNonBotCommit:
    def test_a_non_dependabot_commit_flags_untrusted_author(self) -> None:
        result = parse_pr_commits(
            [
                commit(message=SINGLE_DEP_MESSAGE, login="dependabot[bot]"),
                commit(message=SINGLE_DEP_MESSAGE, login="some-human", sha="def456"),
            ]
        )

        assert result.status == MetadataStatus.UNTRUSTED_AUTHOR
        assert result.updates == ()
        assert result.untrusted_authors == ("some-human",)
        assert result.status.is_unknown

    def test_a_commit_with_no_linked_account_is_reported_as_unknown(self) -> None:
        result = parse_pr_commits([commit(message=SINGLE_DEP_MESSAGE, login=None)])

        assert result.status == MetadataStatus.UNTRUSTED_AUTHOR
        assert result.untrusted_authors == ("unknown",)


class TestFetchPRUpdateMetadata:
    async def test_fetches_commits_and_parses_them(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/repos/acme/widgets/pulls/42/commits"
            return httpx.Response(200, json=[commit(message=SINGLE_DEP_MESSAGE)])

        client = make_client(handler)

        result = await fetch_pr_update_metadata(client, "acme/widgets", 42)

        assert result.status == MetadataStatus.PARSED
        assert result.updates[0].dependency_name == "foo"
