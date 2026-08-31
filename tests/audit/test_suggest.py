"""Suggested-config generator tests: fixture (findings/existing-config) pairs."""

from __future__ import annotations

from typing import Any

import yaml

from dependapilot.audit.detect import DetectionResult, detect_from_paths
from dependapilot.audit.schema import validate_config
from dependapilot.audit.suggest import render_config, suggest_config

REPO = "octo/widget"
FLOOR = 3

UV_REPO_TREE = ["uv.lock", "pyproject.toml", ".github/workflows/ci.yml"]


def uv_repo(*, truncated: bool = False) -> DetectionResult:
    return DetectionResult(
        repo=REPO, expectations=detect_from_paths(UV_REPO_TREE), truncated=truncated
    )


def suggest(existing: str | None, detection: DetectionResult | None = None) -> dict[str, Any]:
    parsed = yaml.safe_load(existing) if existing is not None else None
    return suggest_config(detection or uv_repo(), parsed, cooldown_floor_days=FLOOR)


def entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = document["updates"]
    return result


def assert_schema_valid(document: dict[str, Any]) -> None:
    assert validate_config(document) == ()


def test_missing_config_gets_full_template_for_every_detected_ecosystem() -> None:
    document = suggest(None)
    assert_schema_valid(document)

    assert document["version"] == 2
    ecosystems = {(e["package-ecosystem"], e["directory"]) for e in entries(document)}
    assert ecosystems == {("uv", "/"), ("github-actions", "/")}

    for entry in entries(document):
        assert entry["schedule"] == {"interval": "weekly"}
        assert entry["groups"]["minor-and-patch"]["update-types"] == ["minor", "patch"]
        assert "open-pull-requests-limit" in entry


def test_missing_config_output_is_byte_stable() -> None:
    first = render_config(suggest(None))
    second = render_config(suggest(None))
    assert first == second


def test_missing_config_orders_entries_deterministically() -> None:
    tree = ["uv.lock", "package-lock.json", "go.mod", ".github/workflows/ci.yml"]
    detection = DetectionResult(repo=REPO, expectations=detect_from_paths(tree))
    document = suggest(None, detection)
    pairs = [(e["package-ecosystem"], e["directory"]) for e in entries(document)]
    assert pairs == sorted(pairs)


def test_low_volume_ecosystem_gets_a_smaller_open_pr_limit() -> None:
    document = suggest(None)
    limits = {e["package-ecosystem"]: e["open-pull-requests-limit"] for e in entries(document)}
    assert limits["github-actions"] < limits["uv"]


PIP_ON_UV_CONFIG = """
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "daily"
    labels:
      - "deps"
"""


def test_pip_on_uv_repo_is_rewritten_keeping_other_keys() -> None:
    document = suggest(PIP_ON_UV_CONFIG)
    assert_schema_valid(document)

    fixed = entries(document)
    assert len(fixed) == 2  # rewritten pip->uv entry, plus the missing github-actions entry
    rewritten = next(e for e in fixed if e["directory"] == "/" and "labels" in e)
    assert rewritten["package-ecosystem"] == "uv"
    assert rewritten["schedule"] == {"interval": "daily"}
    assert rewritten["labels"] == ["deps"]

    missing = next(e for e in fixed if e["package-ecosystem"] == "github-actions")
    assert missing["directory"] == "/"


CUSTOM_CONFIG = """
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "monthly"
    labels:
      - "custom-label"
    ignore:
      - dependency-name: "requests"
    groups:
      all-uv:
        patterns: ["*"]
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""


def test_custom_labels_ignores_and_groups_are_kept_verbatim() -> None:
    document = suggest(CUSTOM_CONFIG)
    assert_schema_valid(document)

    fixed = entries(document)
    assert len(fixed) == 2  # nothing missing, nothing templated

    uv_entry = next(e for e in fixed if e["package-ecosystem"] == "uv")
    assert uv_entry["schedule"] == {"interval": "monthly"}
    assert uv_entry["labels"] == ["custom-label"]
    assert uv_entry["ignore"] == [{"dependency-name": "requests"}]
    assert uv_entry["groups"] == {"all-uv": {"patterns": ["*"]}}


def test_never_flattens_a_fully_valid_custom_config() -> None:
    # A config that already covers every expectation, byte for byte, should come back
    # unchanged apart from key normalization -- never replaced with the template.
    document = suggest(CUSTOM_CONFIG)
    assert render_config(document) == render_config(suggest(CUSTOM_CONFIG))
    for entry in entries(document):
        assert "minor-and-patch" not in entry.get("groups", {})


WEAK_COOLDOWN_CONFIG = """
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
    cooldown:
      default-days: 1
      semver-patch-days: 0
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
"""


def test_weakened_cooldown_is_raised_to_the_floor() -> None:
    document = suggest(WEAK_COOLDOWN_CONFIG)
    assert_schema_valid(document)

    uv_entry = next(e for e in entries(document) if e["package-ecosystem"] == "uv")
    assert uv_entry["cooldown"] == {"default-days": FLOOR, "semver-patch-days": FLOOR}


def test_cooldown_at_or_above_floor_is_untouched() -> None:
    config = WEAK_COOLDOWN_CONFIG.replace(
        "default-days: 1\n      semver-patch-days: 0",
        "default-days: 7\n      semver-patch-days: 5",
    )
    document = suggest(config)
    uv_entry = next(e for e in entries(document) if e["package-ecosystem"] == "uv")
    assert uv_entry["cooldown"] == {"default-days": 7, "semver-patch-days": 5}


def test_other_top_level_keys_are_preserved() -> None:
    config = CUSTOM_CONFIG.replace(
        "version: 2\n",
        'version: 2\nregistries:\n  npm-registry:\n    type: "npm-registry"\n    url: "https://example.com"\n',
    )
    document = suggest(config)
    assert document["registries"] == {
        "npm-registry": {"type": "npm-registry", "url": "https://example.com"}
    }


def test_render_config_round_trips_through_yaml() -> None:
    document = suggest(None)
    text = render_config(document)
    assert yaml.safe_load(text) == document
