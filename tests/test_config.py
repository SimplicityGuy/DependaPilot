"""Tests for the repos.yml fleet configuration loader."""

from pathlib import Path

import pytest

from dependapilot.config import (
    ConfigError,
    Defaults,
    FleetConfig,
    MergeMethod,
    load_fleet_config,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_REPOS_YML = REPO_ROOT / "repos.yml"


def write_yaml(tmp_path: Path, text: str) -> Path:
    config_path = tmp_path / "repos.yml"
    config_path.write_text(text)
    return config_path


class TestDefaults:
    def test_defaults_are_applied_when_omitted(self, tmp_path: Path) -> None:
        config_path = write_yaml(tmp_path, "repos: []\n")

        config = load_fleet_config(config_path)

        assert config.defaults == Defaults(merge_method=MergeMethod.MERGE, cooldown_floor_days=3)

    def test_empty_file_yields_defaults_and_no_repos(self, tmp_path: Path) -> None:
        config_path = write_yaml(tmp_path, "")

        config = load_fleet_config(config_path)

        assert config == FleetConfig()

    def test_explicit_defaults_are_honored(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            defaults:
              merge_method: rebase
              cooldown_floor_days: 7
            repos: []
            """,
        )

        config = load_fleet_config(config_path)

        assert config.defaults.merge_method is MergeMethod.REBASE
        assert config.defaults.cooldown_floor_days == 7


class TestOverrides:
    def test_repo_without_overrides_inherits_defaults(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            defaults:
              merge_method: squash
            repos:
              - repo: SimplicityGuy/DependaPilot
            """,
        )

        config = load_fleet_config(config_path)

        assert config.merge_method_for("SimplicityGuy/DependaPilot") is MergeMethod.SQUASH
        entry = config.repos[0]
        assert entry.audit is False
        assert entry.actions is False

    def test_repo_merge_method_override_wins_over_default(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            defaults:
              merge_method: merge
            repos:
              - repo: SimplicityGuy/vinyldigger
                merge_method: squash
            """,
        )

        config = load_fleet_config(config_path)

        assert config.merge_method_for("SimplicityGuy/vinyldigger") is MergeMethod.SQUASH

    def test_audit_and_actions_flags_are_per_repo(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            repos:
              - repo: SimplicityGuy/cronduit
                audit: true
              - repo: SimplicityGuy/tracktion
                actions: true
            """,
        )

        config = load_fleet_config(config_path)

        by_repo = {entry.repo: entry for entry in config.repos}
        assert by_repo["SimplicityGuy/cronduit"].audit is True
        assert by_repo["SimplicityGuy/cronduit"].actions is False
        assert by_repo["SimplicityGuy/tracktion"].actions is True

    def test_merge_method_for_unknown_repo_raises_key_error(self, tmp_path: Path) -> None:
        config_path = write_yaml(tmp_path, "repos: []\n")
        config = load_fleet_config(config_path)

        with pytest.raises(KeyError):
            config.merge_method_for("SimplicityGuy/does-not-exist")


class TestRejectionCases:
    def test_bad_slug_missing_owner_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            repos:
              - repo: DependaPilot
            """,
        )

        with pytest.raises(ConfigError, match="owner/repo"):
            load_fleet_config(config_path)

    def test_bad_slug_extra_segment_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            repos:
              - repo: SimplicityGuy/Depend/aPilot
            """,
        )

        with pytest.raises(ConfigError, match="owner/repo"):
            load_fleet_config(config_path)

    def test_unknown_top_level_key_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            repos: []
            not_a_real_key: true
            """,
        )

        with pytest.raises(ConfigError):
            load_fleet_config(config_path)

    def test_unknown_repo_key_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            repos:
              - repo: SimplicityGuy/DependaPilot
                not_a_real_key: true
            """,
        )

        with pytest.raises(ConfigError):
            load_fleet_config(config_path)

    def test_bad_merge_method_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            defaults:
              merge_method: fast-forward
            """,
        )

        with pytest.raises(ConfigError):
            load_fleet_config(config_path)

    def test_bad_repo_merge_method_override_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            repos:
              - repo: SimplicityGuy/DependaPilot
                merge_method: force-push
            """,
        )

        with pytest.raises(ConfigError):
            load_fleet_config(config_path)

    def test_negative_cooldown_floor_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(
            tmp_path,
            """
            defaults:
              cooldown_floor_days: -1
            """,
        )

        with pytest.raises(ConfigError):
            load_fleet_config(config_path)

    def test_non_mapping_document_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(tmp_path, "- just\n- a\n- list\n")

        with pytest.raises(ConfigError, match="mapping"):
            load_fleet_config(config_path)

    def test_invalid_yaml_is_rejected(self, tmp_path: Path) -> None:
        config_path = write_yaml(tmp_path, "repos: [unclosed\n")

        with pytest.raises(ConfigError):
            load_fleet_config(config_path)

    def test_missing_file_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_fleet_config(tmp_path / "does-not-exist.yml")


class TestSampleFile:
    def test_committed_sample_repos_yml_loads(self) -> None:
        config = load_fleet_config(SAMPLE_REPOS_YML)

        assert config.defaults.merge_method is MergeMethod.MERGE
        assert config.defaults.cooldown_floor_days == 3

        by_repo = {entry.repo: entry for entry in config.repos}
        assert "SimplicityGuy/DependaPilot" in by_repo
        assert by_repo["SimplicityGuy/vinyldigger"].merge_method is MergeMethod.SQUASH
        assert by_repo["SimplicityGuy/cronduit"].audit is True
        assert by_repo["SimplicityGuy/tracktion"].actions is True
