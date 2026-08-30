"""Tests for the audit view service: per-repo composition, diffing, badges, caching."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from dependapilot.audit.detect import DetectionResult, detect_from_paths
from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.audit.suggest import render_config, suggest_config
from dependapilot.audit_service import (
    AuditBadgeState,
    AuditService,
    RepoAuditView,
    badge_for,
    render_diff,
    sort_findings,
)
from dependapilot.config import Defaults, FleetConfig
from tests.audit.conftest import Route, contents_response, make_routed_client, tree_response
from tests.github.conftest import make_client

REPO = "octo/widget"
FLOOR = 3

UV_REPO_TREE = ["uv.lock", "pyproject.toml", ".github/workflows/ci.yml"]

COMPLIANT_CONFIG = """\
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""

SETTINGS_ON: dict[str, Route] = {
    "/repos/octo/widget/vulnerability-alerts": (204, None),
    "/repos/octo/widget/automated-security-fixes": (200, {"enabled": True, "paused": False}),
}

NO_OPEN_FIX_PR: Route = (200, [])


def fleet(*, floor: int = FLOOR) -> FleetConfig:
    return FleetConfig(defaults=Defaults(cooldown_floor_days=floor))


def _make_service(
    routes: Mapping[str, Route], *, audit_enabled_repos: frozenset[str]
) -> AuditService:
    client = make_routed_client(routes)
    return AuditService(client, fleet(), audit_enabled_repos=audit_enabled_repos)


class TestSortFindings:
    def test_orders_high_before_medium_before_low_before_info(self) -> None:
        findings = [
            Finding(
                repo=REPO, check=Check.SECURITY_UPDATES_UNKNOWN, severity=Severity.INFO, message="i"
            ),
            Finding(
                repo=REPO, check=Check.MISSING_ECOSYSTEM, severity=Severity.MEDIUM, message="m"
            ),
            Finding(repo=REPO, check=Check.ORPHAN_ENTRY, severity=Severity.LOW, message="l"),
            Finding(repo=REPO, check=Check.MISSING_CONFIG, severity=Severity.HIGH, message="h"),
        ]

        ordered = sort_findings(findings)

        assert [f.severity for f in ordered] == [
            Severity.HIGH,
            Severity.MEDIUM,
            Severity.LOW,
            Severity.INFO,
        ]

    def test_ties_broken_by_check_id(self) -> None:
        findings = [
            Finding(repo=REPO, check=Check.WRONG_ECOSYSTEM, severity=Severity.HIGH, message="a"),
            Finding(repo=REPO, check=Check.MISSING_CONFIG, severity=Severity.HIGH, message="b"),
        ]

        ordered = sort_findings(findings)

        assert [f.check for f in ordered] == [Check.MISSING_CONFIG, Check.WRONG_ECOSYSTEM]


class TestRenderDiff:
    def test_missing_current_config_is_a_whole_file_addition(self) -> None:
        diff = render_diff(None, "version: 2\nupdates: []\n")

        content_lines = [line for line in diff if not line.startswith(("---", "+++", "@@"))]
        assert content_lines
        assert all(line.startswith("+") for line in content_lines)
        assert any(line.startswith("--- /dev/null") for line in diff)

    def test_identical_configs_yield_no_diff(self) -> None:
        assert render_diff(COMPLIANT_CONFIG, COMPLIANT_CONFIG) == ()

    def test_changed_line_appears_as_del_then_add(self) -> None:
        current = "version: 2\nupdates: []\n"
        suggested = "version: 2\nupdates: [1]\n"

        diff = render_diff(current, suggested)

        assert any(line == "-updates: []" for line in diff)
        assert any(line == "+updates: [1]" for line in diff)


class TestBadgeFor:
    def test_none_view_is_off(self) -> None:
        assert badge_for(None).state is AuditBadgeState.OFF

    def test_error_view_is_error(self) -> None:
        view = RepoAuditView(repo=REPO, error="boom")
        assert badge_for(view).state is AuditBadgeState.ERROR

    def test_no_findings_is_ok(self) -> None:
        view = RepoAuditView(repo=REPO, findings=())
        assert badge_for(view).state is AuditBadgeState.OK

    def test_actionable_findings_count_and_state(self) -> None:
        view = RepoAuditView(
            repo=REPO,
            findings=(
                Finding(repo=REPO, check=Check.MISSING_CONFIG, severity=Severity.HIGH, message="x"),
                Finding(repo=REPO, check=Check.ORPHAN_ENTRY, severity=Severity.LOW, message="y"),
            ),
        )
        badge = badge_for(view)
        assert badge.state is AuditBadgeState.FINDINGS
        assert badge.count == 2

    def test_only_info_findings_is_unknown_not_counted_as_findings(self) -> None:
        view = RepoAuditView(
            repo=REPO,
            findings=(
                Finding(repo=REPO, check=Check.ALERTS_UNKNOWN, severity=Severity.INFO, message="x"),
            ),
        )
        badge = badge_for(view)
        assert badge.state is AuditBadgeState.UNKNOWN
        assert badge.count == 0

    def test_info_finding_alongside_actionable_still_counts_only_actionable(self) -> None:
        view = RepoAuditView(
            repo=REPO,
            findings=(
                Finding(repo=REPO, check=Check.ALERTS_UNKNOWN, severity=Severity.INFO, message="x"),
                Finding(repo=REPO, check=Check.MISSING_CONFIG, severity=Severity.HIGH, message="y"),
            ),
        )
        badge = badge_for(view)
        assert badge.state is AuditBadgeState.FINDINGS
        assert badge.count == 1


class TestAuditServiceGetRepoView:
    async def test_compliant_repo_has_no_findings(self) -> None:
        routes: dict[str, Route] = {
            "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/widget/contents/.github/dependabot.yml": contents_response(
                COMPLIANT_CONFIG
            ),
            "/repos/octo/widget/pulls": NO_OPEN_FIX_PR,
            **SETTINGS_ON,
        }
        service = _make_service(routes, audit_enabled_repos=frozenset({REPO}))

        view = await service.get_repo_view(REPO)

        assert view.error is None
        assert view.findings == ()
        assert view.existing_fix_pr_url is None

    async def test_config_already_matching_the_suggested_render_has_no_diff(self) -> None:
        # A config text that's a byte-for-byte round trip of `suggest_config` +
        # `render_config` -- unlike a hand-authored file, this is guaranteed to
        # diff empty, since it *is* what the suggestion generator would emit.
        detection = DetectionResult(repo=REPO, expectations=detect_from_paths(UV_REPO_TREE))
        rendered = render_config(suggest_config(detection, None, cooldown_floor_days=FLOOR))
        routes: dict[str, Route] = {
            "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/widget/contents/.github/dependabot.yml": contents_response(rendered),
            "/repos/octo/widget/pulls": NO_OPEN_FIX_PR,
            **SETTINGS_ON,
        }
        service = _make_service(routes, audit_enabled_repos=frozenset({REPO}))

        view = await service.get_repo_view(REPO)

        assert view.findings == ()
        assert view.diff == ()

    async def test_missing_config_yields_a_whole_file_diff_and_high_finding(self) -> None:
        routes: dict[str, Route] = {
            "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/widget/pulls": NO_OPEN_FIX_PR,
            **SETTINGS_ON,
        }
        service = _make_service(routes, audit_enabled_repos=frozenset({REPO}))

        view = await service.get_repo_view(REPO)

        assert view.current_config is None
        assert [f.check for f in view.findings] == [Check.MISSING_CONFIG]
        assert view.findings[0].severity is Severity.HIGH
        assert view.diff
        assert "uv" in view.suggested_config

    async def test_findings_are_severity_sorted(self) -> None:
        routes: dict[str, Route] = {
            "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/widget/contents/.github/dependabot.yml": contents_response(
                COMPLIANT_CONFIG
            ),
            "/repos/octo/widget/pulls": NO_OPEN_FIX_PR,
            "/repos/octo/widget/vulnerability-alerts": (403, {"message": "Must have admin rights"}),
            "/repos/octo/widget/automated-security-fixes": (404, None),
        }
        service = _make_service(routes, audit_enabled_repos=frozenset({REPO}))

        view = await service.get_repo_view(REPO)

        # MEDIUM (security updates disabled) must sort before INFO (alerts unknown).
        assert [f.check for f in view.findings] == [
            Check.SECURITY_UPDATES_DISABLED,
            Check.ALERTS_UNKNOWN,
        ]

    async def test_unknown_settings_findings_are_info_severity(self) -> None:
        routes: dict[str, Route] = {
            "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/widget/contents/.github/dependabot.yml": contents_response(
                COMPLIANT_CONFIG
            ),
            "/repos/octo/widget/pulls": NO_OPEN_FIX_PR,
            "/repos/octo/widget/vulnerability-alerts": (403, {"message": "Must have admin rights"}),
            "/repos/octo/widget/automated-security-fixes": (
                403,
                {"message": "Must have admin rights"},
            ),
        }
        service = _make_service(routes, audit_enabled_repos=frozenset({REPO}))

        view = await service.get_repo_view(REPO)

        assert all(f.severity is Severity.INFO for f in view.findings)
        assert {f.check for f in view.findings} == {
            Check.ALERTS_UNKNOWN,
            Check.SECURITY_UPDATES_UNKNOWN,
        }

    async def test_existing_open_fix_pr_is_surfaced(self) -> None:
        routes: dict[str, Route] = {
            "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/widget/contents/.github/dependabot.yml": contents_response(
                COMPLIANT_CONFIG
            ),
            "/repos/octo/widget/pulls": (
                200,
                [{"number": 7, "html_url": f"https://github.com/{REPO}/pull/7"}],
            ),
            **SETTINGS_ON,
        }
        service = _make_service(routes, audit_enabled_repos=frozenset({REPO}))

        view = await service.get_repo_view(REPO)

        assert view.existing_fix_pr_url == f"https://github.com/{REPO}/pull/7"

    async def test_repo_error_is_captured_not_raised(self) -> None:
        service = _make_service({}, audit_enabled_repos=frozenset({REPO}))

        view = await service.get_repo_view(REPO)

        assert view.error is not None
        assert view.findings == ()

    async def test_one_repo_failure_does_not_affect_another_in_get_audit_view(self) -> None:
        routes: dict[str, Route] = {
            "/repos/octo/good/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/good/contents/.github/dependabot.yml": contents_response(COMPLIANT_CONFIG),
            "/repos/octo/good/pulls": NO_OPEN_FIX_PR,
            "/repos/octo/good/vulnerability-alerts": (204, None),
            "/repos/octo/good/automated-security-fixes": (200, {"enabled": True, "paused": False}),
        }
        client = make_routed_client(routes)
        service = AuditService(
            client, fleet(), audit_enabled_repos=frozenset({"octo/good", "octo/broken"})
        )

        views = await service.get_audit_view()

        by_repo = {v.repo: v for v in views}
        assert by_repo["octo/good"].error is None
        assert by_repo["octo/broken"].error is not None


class TestAuditServiceCaching:
    async def test_repeat_call_serves_from_cache_within_ttl(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            path = request.url.path
            if path == "/repos/octo/widget/git/trees/HEAD":
                return httpx.Response(200, json={"tree": [], "truncated": False})
            if path == "/repos/octo/widget/contents/.github/dependabot.yml":
                return httpx.Response(404, json={"message": "Not Found"})
            if path == "/repos/octo/widget/pulls":
                return httpx.Response(200, json=[])
            if path in (
                "/repos/octo/widget/vulnerability-alerts",
                "/repos/octo/widget/automated-security-fixes",
            ):
                return httpx.Response(204)
            raise AssertionError(path)

        client = make_client(handler)
        service = AuditService(
            client,
            fleet(),
            audit_enabled_repos=frozenset({REPO}),
            ttl_seconds=100.0,
            clock=lambda: 0.0,
        )

        await service.get_repo_view(REPO)
        first_call_count = len(calls)
        await service.get_repo_view(REPO)

        assert len(calls) == first_call_count

    async def test_force_refresh_bypasses_the_cache(self) -> None:
        routes: dict[str, Route] = {
            "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
            "/repos/octo/widget/contents/.github/dependabot.yml": contents_response(
                COMPLIANT_CONFIG
            ),
            "/repos/octo/widget/pulls": NO_OPEN_FIX_PR,
            **SETTINGS_ON,
        }
        service = _make_service(routes, audit_enabled_repos=frozenset({REPO}))

        first = await service.get_repo_view(REPO)
        second = await service.get_repo_view(REPO, force_refresh=True)

        assert first == second


def _fix_pr_handler(*, existing_config: str | None, seen_pr_bodies: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/repos/octo/widget/git/trees/HEAD":
            _, payload = tree_response(UV_REPO_TREE)
            return httpx.Response(200, json=payload)
        if path == "/repos/octo/widget/contents/.github/dependabot.yml":
            if method == "GET":
                if existing_config is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                _, payload = contents_response(existing_config)
                return httpx.Response(200, json=payload)
            return httpx.Response(200, json={"content": {"sha": "new"}})
        if path == "/repos/octo/widget/vulnerability-alerts":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/repos/octo/widget/automated-security-fixes":
            return httpx.Response(200, json={"enabled": True, "paused": False})
        if path == "/repos/octo/widget/pulls":
            if method == "GET":
                return httpx.Response(200, json=[])
            seen_pr_bodies.append(json.loads(request.content)["body"])
            return httpx.Response(
                201, json={"number": 1, "html_url": f"https://github.com/{REPO}/pull/1"}
            )
        if path == "/repos/octo/widget":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/octo/widget/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "deadbeef"}})
        if path == "/repos/octo/widget/git/ref/heads/dependapilot/dependabot-config":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/repos/octo/widget/git/refs":
            return httpx.Response(201, json={"ref": "refs/heads/dependapilot/dependabot-config"})
        raise AssertionError(f"unexpected request: {method} {path}")

    return handler


class TestAuditServiceOpenFixPr:
    async def test_opens_a_fix_pr_and_caches_the_url(self) -> None:
        seen_pr_bodies: list[str] = []
        client = make_client(
            _fix_pr_handler(existing_config=COMPLIANT_CONFIG, seen_pr_bodies=seen_pr_bodies)
        )
        service = AuditService(client, fleet(), audit_enabled_repos=frozenset({REPO}))

        url = await service.open_fix_pr(REPO)

        assert url == f"https://github.com/{REPO}/pull/1"
        cached = await service.get_repo_view(REPO)
        assert cached.existing_fix_pr_url == url

    async def test_settings_findings_are_excluded_from_the_fix_pr_body(self) -> None:
        seen_pr_bodies: list[str] = []
        client = make_client(_fix_pr_handler(existing_config=None, seen_pr_bodies=seen_pr_bodies))
        service = AuditService(client, fleet(), audit_enabled_repos=frozenset({REPO}))

        await service.open_fix_pr(REPO)

        (body,) = seen_pr_bodies
        assert "MISSING_CONFIG" in body
        assert "ALERTS_DISABLED" not in body
