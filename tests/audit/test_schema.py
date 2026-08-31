"""Vendored-schema tests.

These pin the parts of the schema the semantic checks in `engine` lean on, so a
refresh of `schemas/dependabot-2.0.json` that drops them fails here instead of
silently weakening the audit.
"""

from __future__ import annotations

from typing import Any

import yaml

from dependapilot.audit.schema import validate_config


def parse(text: str) -> Any:
    return yaml.safe_load(text)


def entry(ecosystem: str, extra: str = "") -> str:
    """One well-formed `updates[]` element, optionally with extra keys appended."""
    return (
        f'  - package-ecosystem: "{ecosystem}"\n'
        f'    directory: "/"\n'
        f"    schedule:\n"
        f'      interval: "weekly"\n' + extra
    )


def test_a_valid_config_passes() -> None:
    document = parse(
        "version: 2\nupdates:\n"
        + entry("uv", "    cooldown:\n      default-days: 7\n      semver-major-days: 30\n")
    )
    assert validate_config(document) == ()


def test_the_schema_knows_every_ecosystem_detection_can_report() -> None:
    ecosystems = [
        "bundler",
        "cargo",
        "devcontainers",
        "docker",
        "docker-compose",
        "github-actions",
        "gitsubmodule",
        "gomod",
        "gradle",
        "maven",
        "npm",
        "pip",
        "pre-commit",
        "terraform",
        "uv",
    ]
    document = parse("version: 2\nupdates:\n" + "".join(entry(name) for name in ecosystems))
    assert validate_config(document) == ()


def test_an_unknown_ecosystem_is_rejected() -> None:
    document = parse("version: 2\nupdates:\n" + entry("nope"))
    assert [v.path for v in validate_config(document)] == ["updates[0].package-ecosystem"]


def test_a_missing_required_key_is_reported_at_the_root() -> None:
    violations = validate_config(parse("updates: []"))
    assert [(v.path, v.keyword) for v in violations] == [("$", "required")]


def test_an_entry_must_name_a_directory() -> None:
    document = parse(
        'version: 2\nupdates:\n  - package-ecosystem: "uv"\n    schedule:\n'
        '      interval: "weekly"\n'
    )
    assert [(v.path, v.keyword) for v in validate_config(document)] == [("updates[0]", "oneOf")]


def test_an_unknown_update_key_is_rejected() -> None:
    document = parse("version: 2\nupdates:\n" + entry("uv", "    typo: 1\n"))
    assert [(v.path, v.keyword) for v in validate_config(document)] == [
        ("updates[0]", "additionalProperties")
    ]


def test_violations_are_ordered_deterministically() -> None:
    document = parse("version: 3\nupdates:\n" + entry("nope"))
    assert [v.path for v in validate_config(document)] == [
        "updates[0].package-ecosystem",
        "version",
    ]


def test_a_non_mapping_document_is_rejected() -> None:
    assert [v.path for v in validate_config(parse("- one\n- two\n"))] == ["$"]
