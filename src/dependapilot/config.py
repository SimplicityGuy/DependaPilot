"""Typed access to the `repos.yml` fleet configuration.

`repos.yml` is the committed source of truth for which GitHub repositories
DependaPilot manages and the policy knobs the rest of the app reads: a
fleet-wide `defaults` block plus a `repos` list of `owner/repo` entries that
may override those defaults individually.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_REPO_SLUG_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)


class MergeMethod(StrEnum):
    """GitHub merge strategies DependaPilot may use when merging a PR."""

    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


class ConfigError(ValueError):
    """Raised when `repos.yml` cannot be read, parsed, or validated."""


class Defaults(BaseModel):
    """Fleet-wide policy applied to every repo unless it overrides a field."""

    model_config = ConfigDict(extra="forbid")

    merge_method: MergeMethod = MergeMethod.MERGE
    cooldown_floor_days: int = Field(default=3, ge=0)


class RepoConfig(BaseModel):
    """One managed repository, with optional overrides of the fleet defaults."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    merge_method: MergeMethod | None = None
    audit: bool = False
    actions: bool = False

    @field_validator("repo")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        if not _REPO_SLUG_RE.match(value):
            raise ValueError(f"repo must be an 'owner/repo' slug, got {value!r}")
        return value


class FleetConfig(BaseModel):
    """Top-level schema for `repos.yml`."""

    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    repos: list[RepoConfig] = Field(default_factory=list)

    def merge_method_for(self, repo: str) -> MergeMethod:
        """Effective merge method for `repo`: its override, else the fleet default.

        Raises KeyError if `repo` is not present in `repos`.
        """
        return self._entry(repo).merge_method or self.defaults.merge_method

    def _entry(self, repo: str) -> RepoConfig:
        for entry in self.repos:
            if entry.repo == repo:
                return entry
        raise KeyError(f"repo {repo!r} is not in the fleet config")


def load_fleet_config(path: str | Path) -> FleetConfig:
    """Load and validate a `repos.yml` fleet config file.

    Raises ConfigError, with a message naming the offending field, if the
    file is missing or unreadable, is not valid YAML, is not a YAML mapping,
    or fails schema validation (unknown keys, a malformed `owner/repo` slug,
    an unrecognized `merge_method`, and so on).
    """
    config_path = Path(path)
    try:
        raw_text = config_path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read fleet config {config_path}: {exc}") from exc

    try:
        raw_data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    if raw_data is None:
        raw_data = {}
    if not isinstance(raw_data, dict):
        raise ConfigError(
            f"fleet config {config_path} must be a YAML mapping, got {type(raw_data).__name__}"
        )

    try:
        return FleetConfig.model_validate(raw_data)
    except ValidationError as exc:
        raise ConfigError(f"invalid fleet config {config_path}:\n{exc}") from exc
