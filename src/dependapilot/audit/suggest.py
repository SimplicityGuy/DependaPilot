"""The suggested-config generator: the corrected `dependabot.yml` a fix PR carries.

`suggest_config` is pure — a `DetectionResult` plus the repo's existing parsed config
(or `None`) in, a corrected config document out — so it's testable without a transport
and safe to preview before anything is written. It never flattens a working config to
the template: an entry that already covers a detected ecosystem+directory is carried
over untouched except for two targeted fixes (a `pip` entry uv has taken over, and a
cooldown weaker than the fleet floor). Only entries for expectations nothing already
covers are freshly templated in.

Output ordering is deterministic — preserved entries keep their original order, newly
templated entries are appended sorted by `(ecosystem, directory)` — so the same inputs
always render the same YAML bytes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from fnmatch import fnmatchcase
from typing import Any, Final

import yaml

from dependapilot.audit.detect import DetectionResult, Ecosystem, Expectation

_GLOB_CHARS: Final = "*?["
_ROOT: Final = "/"

# Ecosystems whose manifests tend to accrue fewer simultaneous updates, so a smaller
# cap on open PRs is plenty; everything else defaults to the more generous limit.
_LOW_VOLUME_ECOSYSTEMS: Final = frozenset(
    {
        Ecosystem.DEVCONTAINERS,
        Ecosystem.DOCKER,
        Ecosystem.DOCKER_COMPOSE,
        Ecosystem.GITHUB_ACTIONS,
        Ecosystem.GITSUBMODULE,
        Ecosystem.PRE_COMMIT,
        Ecosystem.TERRAFORM,
    }
)
_LOW_VOLUME_LIMIT: Final = 5
_DEFAULT_LIMIT: Final = 10

_MINOR_PATCH_GROUP: Final = "minor-and-patch"
"""GitHub's own guidance: group minor+patch updates, leave majors arriving individually."""


def _open_pull_requests_limit(ecosystem: Ecosystem) -> int:
    return _LOW_VOLUME_LIMIT if ecosystem in _LOW_VOLUME_ECOSYSTEMS else _DEFAULT_LIMIT


def _normalise_directory(directory: str) -> str:
    stripped = directory.strip()
    if not stripped.startswith(_ROOT):
        stripped = f"/{stripped}"
    return stripped.rstrip("/") or _ROOT


def _covers(pattern: str, directory: str) -> bool:
    """Whether a configured `directory`/`directories` value covers a detected one."""
    if any(char in pattern for char in _GLOB_CHARS):
        return fnmatchcase(directory, pattern)
    return pattern == directory


def _entry_directory_patterns(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """The directories one `updates[]` element claims; `/` when it names none."""
    directories = entry.get("directories")
    if isinstance(directories, list):
        named = tuple(_normalise_directory(d) for d in directories if isinstance(d, str))
        if named:
            return named
    directory = entry.get("directory")
    if isinstance(directory, str):
        return (_normalise_directory(directory),)
    return (_ROOT,)


def _raise_weak_cooldown(entry: dict[str, Any], floor_days: int) -> None:
    """Raise any cooldown field below `floor_days` up to the floor, in place."""
    cooldown = entry.get("cooldown")
    if not isinstance(cooldown, dict):
        return
    for key, value in cooldown.items():
        if key != "default-days" and not (key.startswith("semver-") and key.endswith("-days")):
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < floor_days:
            cooldown[key] = floor_days


def _template_entry(expectation: Expectation) -> dict[str, Any]:
    """A fresh best-practice `updates[]` entry for a detected ecosystem+directory."""
    return {
        "package-ecosystem": expectation.ecosystem.value,
        "directory": expectation.directory,
        "schedule": {"interval": "weekly"},
        "open-pull-requests-limit": _open_pull_requests_limit(expectation.ecosystem),
        "groups": {
            _MINOR_PATCH_GROUP: {
                "patterns": ["*"],
                "update-types": ["minor", "patch"],
            }
        },
    }


def _fix_entries(
    existing_updates: Sequence[Any],
    detection: DetectionResult,
    cooldown_floor_days: int,
) -> tuple[list[Any], set[Expectation]]:
    """Carry over each existing `updates[]` element, fixing it in place where needed.

    Returns the fixed entries plus the set of detected expectations they now cover, so
    the caller can template in whatever's left over.
    """
    uv_directories = detection.directories_for(Ecosystem.UV)
    covered: set[Expectation] = set()
    fixed: list[Any] = []

    for raw_entry in existing_updates:
        if not isinstance(raw_entry, dict):
            # Malformed enough that the schema pass already reports it; carry it over
            # unguessed-at rather than inventing structure for it here.
            fixed.append(deepcopy(raw_entry))
            continue

        entry: dict[str, Any] = deepcopy(raw_entry)
        ecosystem_value = entry.get("package-ecosystem")
        patterns = _entry_directory_patterns(entry)

        if ecosystem_value == Ecosystem.PIP.value and any(
            _covers(pattern, directory) for pattern in patterns for directory in uv_directories
        ):
            entry["package-ecosystem"] = Ecosystem.UV.value
            ecosystem_value = Ecosystem.UV.value

        _raise_weak_cooldown(entry, cooldown_floor_days)

        if isinstance(ecosystem_value, str):
            try:
                ecosystem_enum = Ecosystem(ecosystem_value)
            except ValueError:
                ecosystem_enum = None
            if ecosystem_enum is not None:
                covered.update(
                    expectation
                    for expectation in detection.expectations
                    if expectation.ecosystem is ecosystem_enum
                    and any(_covers(pattern, expectation.directory) for pattern in patterns)
                )

        fixed.append(entry)

    return fixed, covered


def suggest_config(
    detection: DetectionResult,
    existing: Mapping[str, Any] | None,
    *,
    cooldown_floor_days: int,
) -> dict[str, Any]:
    """Build the corrected `dependabot.yml` document for one repo.

    Args:
        detection: What the repo's file tree says it needs.
        existing: The repo's current config, already YAML-parsed — or `None` if it has
            no config at all.
        cooldown_floor_days: The fleet's minimum acceptable cooldown.

    Every existing `updates[]` entry that already covers a detected expectation is
    preserved untouched apart from two targeted fixes: a `pip` entry over a
    uv-managed directory is rewritten to `uv`, and a cooldown field weaker than
    `cooldown_floor_days` is raised to it. Any other existing customization —
    schedule, labels, ignore, allow, groups, registries, commit-message — survives
    as written. Expectations nothing covers get a fresh best-practice entry appended,
    sorted by `(ecosystem, directory)` for a deterministic, byte-stable result.
    """
    existing_map: Mapping[str, Any] = existing if isinstance(existing, Mapping) else {}
    raw_updates = existing_map.get("updates")
    existing_updates = raw_updates if isinstance(raw_updates, list) else []

    fixed_entries, covered = _fix_entries(existing_updates, detection, cooldown_floor_days)
    missing = sorted(
        expectation for expectation in detection.expectations if expectation not in covered
    )
    fixed_entries.extend(_template_entry(expectation) for expectation in missing)

    document: dict[str, Any] = {"version": 2}
    for key, value in existing_map.items():
        if key in ("version", "updates"):
            continue
        document[key] = deepcopy(value)
    document["updates"] = fixed_entries
    return document


def render_config(document: Mapping[str, Any]) -> str:
    """Render a suggested config document as YAML text, byte-stable across runs."""
    return yaml.safe_dump(dict(document), sort_keys=False, default_flow_style=False)
