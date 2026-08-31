"""The audit fixture matrix: one repo shape per check, plus the compliant baseline."""

from __future__ import annotations

import pytest

from dependapilot.audit.detect import DetectionResult, detect_from_paths
from dependapilot.audit.engine import (
    RepoAudit,
    audit_repo,
    check_repo_settings,
    evaluate_config,
    fetch_config,
)
from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.config import Defaults, FleetConfig
from tests.audit.conftest import contents_response, make_routed_client, tree_response

REPO = "octo/widget"
FLOOR = 3

UV_REPO_TREE = ["uv.lock", "pyproject.toml", ".github/workflows/ci.yml"]

COMPLIANT_CONFIG = """
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


def uv_repo(*, truncated: bool = False) -> DetectionResult:
    """The baseline repo the matrix varies: uv at `/` plus workflows."""
    return DetectionResult(
        repo=REPO, expectations=detect_from_paths(UV_REPO_TREE), truncated=truncated
    )


def evaluate(config: str | None, detection: DetectionResult | None = None) -> list[Finding]:
    return evaluate_config(REPO, config, detection or uv_repo(), cooldown_floor_days=FLOOR)


def only(findings: list[Finding]) -> Finding:
    assert len(findings) == 1, [(f.check, f.message) for f in findings]
    return findings[0]


def test_compliant_config_yields_nothing() -> None:
    assert evaluate(COMPLIANT_CONFIG) == []


def test_absent_cooldown_is_compliant() -> None:
    # GitHub applies its own native cooldown when the block is omitted, so silence
    # is not a weakening — only an explicit too-short value is.
    assert not any(f.check is Check.WEAKENED_COOLDOWN for f in evaluate(COMPLIANT_CONFIG))


def test_missing_config() -> None:
    finding = only(evaluate(None))
    assert finding.check is Check.MISSING_CONFIG
    assert finding.severity is Severity.HIGH
    assert finding.context["path"] == ".github/dependabot.yml"
    assert {"ecosystem": "uv", "directory": "/"} in finding.context["expected"]


def test_invalid_yaml() -> None:
    finding = only(evaluate("version: 2\nupdates:\n  - package-ecosystem: 'uv\n"))
    assert finding.check is Check.INVALID_YAML
    assert finding.severity is Severity.HIGH
    assert finding.context["error"]


def test_schema_violation() -> None:
    finding = only(evaluate(COMPLIANT_CONFIG.replace("version: 2", "version: 3")))
    assert finding.check is Check.SCHEMA_ERROR
    assert finding.severity is Severity.MEDIUM
    assert finding.context["path"] == "version"
    assert finding.context["keyword"] == "const"


def test_unknown_ecosystem_is_a_schema_violation() -> None:
    config = COMPLIANT_CONFIG.replace('"github-actions"', '"npmm"')
    findings = [f for f in evaluate(config) if f.check is Check.SCHEMA_ERROR]
    assert only(findings).context["path"] == "updates[1].package-ecosystem"


def test_missing_ecosystem() -> None:
    config = """
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""
    finding = only(evaluate(config))
    assert finding.check is Check.MISSING_ECOSYSTEM
    assert finding.severity is Severity.MEDIUM
    assert finding.context == {"ecosystem": "uv", "directory": "/"}


def test_orphan_entry() -> None:
    config = (
        COMPLIANT_CONFIG
        + """
  - package-ecosystem: "npm"
    directory: "/web"
    schedule:
      interval: "weekly"
"""
    )
    finding = only(evaluate(config))
    assert finding.check is Check.ORPHAN_ENTRY
    assert finding.severity is Severity.LOW
    assert finding.context == {"index": 2, "ecosystem": "npm", "directory": "/web"}


def test_undetectable_ecosystem_is_never_an_orphan() -> None:
    # DependaPilot has no nuget detector, so it has no ground truth to contradict
    # a nuget entry with.
    config = (
        COMPLIANT_CONFIG
        + """
  - package-ecosystem: "nuget"
    directory: "/svc"
    schedule:
      interval: "weekly"
"""
    )
    assert evaluate(config) == []


def test_orphan_detection_stands_down_on_a_truncated_tree() -> None:
    config = (
        COMPLIANT_CONFIG
        + """
  - package-ecosystem: "npm"
    directory: "/web"
    schedule:
      interval: "weekly"
"""
    )
    assert evaluate(config, uv_repo(truncated=True)) == []


def test_pip_on_a_uv_repo() -> None:
    config = COMPLIANT_CONFIG.replace('"uv"', '"pip"')
    finding = only(evaluate(config))
    assert finding.check is Check.WRONG_ECOSYSTEM
    assert finding.severity is Severity.HIGH
    assert finding.context["configured_ecosystem"] == "pip"
    assert finding.context["expected_ecosystem"] == "uv"
    assert finding.context["directory"] == "/"


def test_pip_is_fine_where_uv_is_absent() -> None:
    detection = DetectionResult(repo=REPO, expectations=detect_from_paths(["requirements.txt"]))
    config = """
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
"""
    assert evaluate(config, detection) == []


def test_duplicate_entry() -> None:
    config = (
        COMPLIANT_CONFIG
        + """
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "daily"
"""
    )
    finding = only(evaluate(config))
    assert finding.check is Check.DUPLICATE_ENTRY
    assert finding.severity is Severity.MEDIUM
    assert finding.context == {"ecosystem": "uv", "directory": "/", "indices": [0, 2]}


def test_weakened_cooldown() -> None:
    config = COMPLIANT_CONFIG.replace(
        '  - package-ecosystem: "uv"\n    directory: "/"\n',
        '  - package-ecosystem: "uv"\n    directory: "/"\n    cooldown:\n      default-days: 1\n',
    )
    finding = only(evaluate(config))
    assert finding.check is Check.WEAKENED_COOLDOWN
    assert finding.severity is Severity.MEDIUM
    assert finding.context["weak_fields"] == {"default-days": 1}
    assert finding.context["floor_days"] == FLOOR


def test_weakened_cooldown_reports_every_offending_semver_field() -> None:
    config = COMPLIANT_CONFIG.replace(
        '  - package-ecosystem: "uv"\n    directory: "/"\n',
        '  - package-ecosystem: "uv"\n    directory: "/"\n    cooldown:\n'
        "      default-days: 7\n      semver-patch-days: 1\n      semver-minor-days: 2\n",
    )
    finding = only(evaluate(config))
    assert finding.context["weak_fields"] == {"semver-minor-days": 2, "semver-patch-days": 1}


def test_cooldown_at_or_above_the_floor_is_compliant() -> None:
    config = COMPLIANT_CONFIG.replace(
        '  - package-ecosystem: "uv"\n    directory: "/"\n',
        '  - package-ecosystem: "uv"\n    directory: "/"\n    cooldown:\n      default-days: 3\n',
    )
    assert evaluate(config) == []


def test_directories_globs_cover_detected_directories() -> None:
    detection = DetectionResult(
        repo=REPO,
        expectations=detect_from_paths(["packages/a/package-lock.json", "packages/b/yarn.lock"]),
    )
    config = """
version: 2
updates:
  - package-ecosystem: "npm"
    directories:
      - "/packages/*"
    schedule:
      interval: "weekly"
"""
    assert evaluate(config, detection) == []


def test_directories_list_is_checked_element_by_element() -> None:
    detection = DetectionResult(
        repo=REPO, expectations=detect_from_paths(["packages/a/package-lock.json"])
    )
    config = """
version: 2
updates:
  - package-ecosystem: "npm"
    directories:
      - "/packages/a"
      - "/packages/gone"
    schedule:
      interval: "weekly"
"""
    finding = only(evaluate(config, detection))
    assert finding.check is Check.ORPHAN_ENTRY
    assert finding.context["directory"] == "/packages/gone"


def test_a_missing_directory_key_is_read_as_the_repo_root() -> None:
    # The schema requires `directory`, so this is reported — but the semantic checks
    # still place the entry at `/` rather than compounding one mistake into three.
    config = """
version: 2
updates:
  - package-ecosystem: "uv"
    schedule:
      interval: "weekly"
  - package-ecosystem: "github-actions"
    schedule:
      interval: "weekly"
"""
    findings = evaluate(config)
    assert {f.check for f in findings} == {Check.SCHEMA_ERROR}


async def test_fetch_config_decodes_base64_contents() -> None:
    routes = {"/repos/octo/widget/contents/.github/dependabot.yml": contents_response("version: 2")}
    async with make_routed_client(routes) as client:
        assert await fetch_config(client, REPO) == "version: 2"


async def test_fetch_config_returns_none_on_404() -> None:
    async with make_routed_client({}) as client:
        assert await fetch_config(client, REPO) is None


SETTINGS_ON = {
    "/repos/octo/widget/vulnerability-alerts": (204, None),
    "/repos/octo/widget/automated-security-fixes": (200, {"enabled": True, "paused": False}),
}


async def test_settings_enabled_yields_nothing() -> None:
    async with make_routed_client(SETTINGS_ON) as client:
        assert await check_repo_settings(client, REPO) == []


async def test_settings_disabled() -> None:
    routes = {
        "/repos/octo/widget/vulnerability-alerts": (404, None),
        "/repos/octo/widget/automated-security-fixes": (200, {"enabled": False, "paused": False}),
    }
    async with make_routed_client(routes) as client:
        findings = await check_repo_settings(client, REPO)

    assert [(f.check, f.severity) for f in findings] == [
        (Check.ALERTS_DISABLED, Severity.HIGH),
        (Check.SECURITY_UPDATES_DISABLED, Severity.MEDIUM),
    ]


async def test_paused_security_updates_read_as_disabled() -> None:
    routes = dict(SETTINGS_ON)
    routes["/repos/octo/widget/automated-security-fixes"] = (
        200,
        {"enabled": True, "paused": True},
    )
    async with make_routed_client(routes) as client:
        findings = await check_repo_settings(client, REPO)

    assert only(findings).check is Check.SECURITY_UPDATES_DISABLED


async def test_settings_unknown_without_admin() -> None:
    routes = {
        "/repos/octo/widget/vulnerability-alerts": (403, {"message": "Must have admin rights"}),
        "/repos/octo/widget/automated-security-fixes": (
            403,
            {"message": "Must have admin rights"},
        ),
    }
    async with make_routed_client(routes) as client:
        findings = await check_repo_settings(client, REPO)

    assert [(f.check, f.severity) for f in findings] == [
        (Check.ALERTS_UNKNOWN, Severity.INFO),
        (Check.SECURITY_UPDATES_UNKNOWN, Severity.INFO),
    ]
    assert all(f.context["reason"] == "forbidden" for f in findings)


@pytest.fixture
def fleet() -> FleetConfig:
    return FleetConfig(defaults=Defaults(cooldown_floor_days=FLOOR))


async def audit(routes: dict[str, tuple[int, object]], fleet: FleetConfig) -> RepoAudit:
    async with make_routed_client(routes) as client:
        return await audit_repo(client, REPO, fleet=fleet)


async def test_a_fully_correct_repo_yields_zero_findings(fleet: FleetConfig) -> None:
    routes = {
        "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
        "/repos/octo/widget/contents/.github/dependabot.yml": contents_response(COMPLIANT_CONFIG),
        **SETTINGS_ON,
    }
    result = await audit(routes, fleet)

    assert result.findings == ()
    assert result.compliant
    assert result.detection.expectations


async def test_audit_repo_combines_config_and_settings_findings(fleet: FleetConfig) -> None:
    routes = {
        "/repos/octo/widget/git/trees/HEAD": tree_response(UV_REPO_TREE),
        "/repos/octo/widget/vulnerability-alerts": (404, None),
        "/repos/octo/widget/automated-security-fixes": (200, {"enabled": True, "paused": False}),
    }
    result = await audit(routes, fleet)

    assert [f.check for f in result.findings] == [Check.MISSING_CONFIG, Check.ALERTS_DISABLED]
    assert not result.compliant
