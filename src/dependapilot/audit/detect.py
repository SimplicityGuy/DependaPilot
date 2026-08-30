"""Ecosystem detection: which package ecosystems a repo actually uses.

This is the audit's ground truth. Everything downstream compares a repo's
`.github/dependabot.yml` against the `(ecosystem, directory)` pairs derived here from
the repo's own file tree, so a missing or superfluous `updates[]` entry is a
difference between two sets rather than a judgement call.

Directories are Dependabot `directory` values: POSIX-absolute and repo-relative, with
`/` for the repo root. Every manifest contributes the directory it lives in — none of
Dependabot's ecosystems search recursively, so a `Dockerfile` in three directories is
three separate expectations, not one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Final

from dependapilot.github.client import GitHubClient

ROOT: Final = "/"

_YAML_SUFFIXES: Final = frozenset({".yml", ".yaml"})


class Ecosystem(StrEnum):
    """Dependabot `package-ecosystem` values DependaPilot detects.

    A subset of the values Dependabot accepts — only the ones with a manifest we
    know how to recognise from a file tree.
    """

    BUNDLER = "bundler"
    CARGO = "cargo"
    DEVCONTAINERS = "devcontainers"
    DOCKER = "docker"
    DOCKER_COMPOSE = "docker-compose"
    GITHUB_ACTIONS = "github-actions"
    GITSUBMODULE = "gitsubmodule"
    GOMOD = "gomod"
    GRADLE = "gradle"
    MAVEN = "maven"
    NPM = "npm"
    PIP = "pip"
    PRE_COMMIT = "pre-commit"
    TERRAFORM = "terraform"
    UV = "uv"


@dataclass(frozen=True, slots=True, order=True)
class Expectation:
    """One `updates[]` entry the repo's file tree says ought to exist."""

    ecosystem: Ecosystem
    directory: str


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Everything detection knows about one repo.

    `truncated` mirrors the git trees API's own flag: the repo has more entries than
    a single recursive response can carry, so `expectations` may be incomplete and
    absence of an ecosystem is not evidence it isn't there.
    """

    repo: str
    expectations: tuple[Expectation, ...] = ()
    truncated: bool = False

    def directories_for(self, ecosystem: Ecosystem) -> frozenset[str]:
        """Every directory where `ecosystem` was detected."""
        return frozenset(e.directory for e in self.expectations if e.ecosystem is ecosystem)


# Manifests whose whole filename identifies the ecosystem. `Dockerfile`, requirements
# files, compose files and `*.tf` are matched by shape instead, below.
_FILENAME_ECOSYSTEMS: Final[dict[str, Ecosystem]] = {
    ".gitmodules": Ecosystem.GITSUBMODULE,
    ".pre-commit-config.yaml": Ecosystem.PRE_COMMIT,
    "Cargo.toml": Ecosystem.CARGO,
    "Gemfile": Ecosystem.BUNDLER,
    "Pipfile": Ecosystem.PIP,
    "build.gradle": Ecosystem.GRADLE,
    "build.gradle.kts": Ecosystem.GRADLE,
    "go.mod": Ecosystem.GOMOD,
    "package-lock.json": Ecosystem.NPM,
    "pnpm-lock.yaml": Ecosystem.NPM,
    "pom.xml": Ecosystem.MAVEN,
    "poetry.lock": Ecosystem.PIP,
    "setup.py": Ecosystem.PIP,
    "uv.lock": Ecosystem.UV,
    "yarn.lock": Ecosystem.NPM,
}


def _directory(path: PurePosixPath) -> str:
    """Render a directory path as a Dependabot `directory` value."""
    text = str(path)
    return ROOT if text == "." else f"/{text}"


def _classify(path: str) -> Expectation | None:
    """Map one repo-relative file path to the expectation it implies, if any."""
    pure = PurePosixPath(path)
    name = pure.name

    # Dependabot only reads workflows from the repo-root `.github/workflows`, and the
    # `github-actions` ecosystem is always configured at `/`.
    if pure.parts[:2] == (".github", "workflows") and len(pure.parts) == 3:
        if pure.suffix in _YAML_SUFFIXES:
            return Expectation(Ecosystem.GITHUB_ACTIONS, ROOT)
        return None

    # A devcontainer is configured at the directory *containing* `.devcontainer/`,
    # which is also where the single-file `.devcontainer.json` form lives.
    if name == "devcontainer.json" and pure.parent.name == ".devcontainer":
        return Expectation(Ecosystem.DEVCONTAINERS, _directory(pure.parent.parent))
    if name == ".devcontainer.json":
        return Expectation(Ecosystem.DEVCONTAINERS, _directory(pure.parent))

    directory = _directory(pure.parent)

    known = _FILENAME_ECOSYSTEMS.get(name)
    if known is not None:
        return Expectation(known, directory)
    if name.startswith("Dockerfile"):
        return Expectation(Ecosystem.DOCKER, directory)
    if name.startswith("docker-compose") and pure.suffix in _YAML_SUFFIXES:
        return Expectation(Ecosystem.DOCKER_COMPOSE, directory)
    if name.startswith("requirements") and pure.suffix == ".txt":
        return Expectation(Ecosystem.PIP, directory)
    if pure.suffix == ".tf":
        return Expectation(Ecosystem.TERRAFORM, directory)
    return None


def detect_from_paths(paths: Iterable[str]) -> tuple[Expectation, ...]:
    """Derive the expected `(ecosystem, directory)` pairs from a repo's file paths.

    A directory managed by uv yields `uv` and never `pip`, even when uv's own
    `requirements.txt` export or a legacy `setup.py` sits beside `uv.lock` — telling
    Dependabot `pip` there would have it resolve the wrong dependency graph.
    """
    found: set[Expectation] = set()
    for path in paths:
        expectation = _classify(path)
        if expectation is not None:
            found.add(expectation)

    uv_directories = {e.directory for e in found if e.ecosystem is Ecosystem.UV}

    def shadowed_by_uv(expectation: Expectation) -> bool:
        return expectation.ecosystem is Ecosystem.PIP and expectation.directory in uv_directories

    return tuple(sorted(e for e in found if not shadowed_by_uv(e)))


async def detect_repo(client: GitHubClient, repo: str, *, ref: str = "HEAD") -> DetectionResult:
    """Detect `repo`'s ecosystems from its git tree at `ref`.

    Args:
        client: Authenticated GitHub client.
        repo: An `owner/name` slug.
        ref: Any tree-ish; the default resolves the repo's default branch.
    """
    owner, _, name = repo.partition("/")
    path = f"/repos/{owner}/{name}/git/trees/{ref}"
    response = await client.get(path, params={"recursive": "1"})
    payload: Any = response.json()
    entries: list[Any] = payload.get("tree") or []
    paths = [str(entry["path"]) for entry in entries if entry.get("type") == "blob"]
    return DetectionResult(
        repo=repo,
        expectations=detect_from_paths(paths),
        truncated=bool(payload.get("truncated", False)),
    )
