"""Ecosystem detection tests. Trees are fixtures; the one API test mocks transport."""

from __future__ import annotations

import json

import httpx
import pytest

from dependapilot.audit.detect import (
    DetectionResult,
    Ecosystem,
    Expectation,
    detect_from_paths,
    detect_repo,
)
from tests.github.conftest import make_client

UV_ACTIONS_DOCKER_TREE = [
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "src/app/main.py",
    "Dockerfile",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yaml",
    ".github/dependabot.yml",
    ".pre-commit-config.yaml",
]

GO_TREE = [
    "go.mod",
    "go.sum",
    "cmd/server/main.go",
    ".github/workflows/test.yml",
]

NPM_TREE = [
    "package.json",
    "package-lock.json",
    "web/package.json",
    "web/yarn.lock",
]

MULTI_DOCKERFILE_TREE = [
    "Dockerfile",
    "services/api/Dockerfile",
    "services/worker/Dockerfile",
    "services/worker/Dockerfile.debug",
    "docker-compose.yml",
    "deploy/docker-compose.prod.yaml",
]


def test_uv_actions_docker_tree() -> None:
    assert detect_from_paths(UV_ACTIONS_DOCKER_TREE) == (
        Expectation(Ecosystem.DOCKER, "/"),
        Expectation(Ecosystem.GITHUB_ACTIONS, "/"),
        Expectation(Ecosystem.PRE_COMMIT, "/"),
        Expectation(Ecosystem.UV, "/"),
    )


def test_go_tree() -> None:
    assert detect_from_paths(GO_TREE) == (
        Expectation(Ecosystem.GITHUB_ACTIONS, "/"),
        Expectation(Ecosystem.GOMOD, "/"),
    )


def test_npm_tree_reports_each_lockfile_directory() -> None:
    assert detect_from_paths(NPM_TREE) == (
        Expectation(Ecosystem.NPM, "/"),
        Expectation(Ecosystem.NPM, "/web"),
    )


def test_multi_dockerfile_tree_gets_one_entry_per_directory() -> None:
    assert detect_from_paths(MULTI_DOCKERFILE_TREE) == (
        Expectation(Ecosystem.DOCKER, "/"),
        Expectation(Ecosystem.DOCKER, "/services/api"),
        Expectation(Ecosystem.DOCKER, "/services/worker"),
        Expectation(Ecosystem.DOCKER_COMPOSE, "/"),
        Expectation(Ecosystem.DOCKER_COMPOSE, "/deploy"),
    )


def test_uv_lockfile_suppresses_pip_in_the_same_directory() -> None:
    tree = ["uv.lock", "requirements.txt", "requirements-dev.txt", "setup.py", "poetry.lock"]
    assert detect_from_paths(tree) == (Expectation(Ecosystem.UV, "/"),)


def test_uv_suppression_is_scoped_to_its_own_directory() -> None:
    tree = ["uv.lock", "tools/requirements.txt"]
    assert detect_from_paths(tree) == (
        Expectation(Ecosystem.PIP, "/tools"),
        Expectation(Ecosystem.UV, "/"),
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Pipfile", Expectation(Ecosystem.PIP, "/")),
        ("poetry.lock", Expectation(Ecosystem.PIP, "/")),
        ("setup.py", Expectation(Ecosystem.PIP, "/")),
        ("requirements.txt", Expectation(Ecosystem.PIP, "/")),
        ("requirements-dev.txt", Expectation(Ecosystem.PIP, "/")),
        ("api/Cargo.toml", Expectation(Ecosystem.CARGO, "/api")),
        ("pnpm-lock.yaml", Expectation(Ecosystem.NPM, "/")),
        (".gitmodules", Expectation(Ecosystem.GITSUBMODULE, "/")),
        ("Gemfile", Expectation(Ecosystem.BUNDLER, "/")),
        ("pom.xml", Expectation(Ecosystem.MAVEN, "/")),
        ("build.gradle", Expectation(Ecosystem.GRADLE, "/")),
        ("app/build.gradle.kts", Expectation(Ecosystem.GRADLE, "/app")),
        ("infra/main.tf", Expectation(Ecosystem.TERRAFORM, "/infra")),
        (".devcontainer/devcontainer.json", Expectation(Ecosystem.DEVCONTAINERS, "/")),
        ("sub/.devcontainer/devcontainer.json", Expectation(Ecosystem.DEVCONTAINERS, "/sub")),
        (".devcontainer.json", Expectation(Ecosystem.DEVCONTAINERS, "/")),
    ],
)
def test_single_manifest_paths(path: str, expected: Expectation) -> None:
    assert detect_from_paths([path]) == (expected,)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "src/app/main.py",
        "pyproject.toml",
        "docs/requirements.md",
        "nested/.github/workflows/ci.yml",
        ".github/workflows/scripts/helper.sh",
        ".github/dependabot.yml",
    ],
)
def test_paths_that_imply_nothing(path: str) -> None:
    assert detect_from_paths([path]) == ()


def test_directories_for_groups_by_ecosystem() -> None:
    result = DetectionResult(repo="o/r", expectations=detect_from_paths(MULTI_DOCKERFILE_TREE))
    assert result.directories_for(Ecosystem.DOCKER) == frozenset(
        {"/", "/services/api", "/services/worker"}
    )
    assert result.directories_for(Ecosystem.NPM) == frozenset()


async def test_detect_repo_reads_the_recursive_git_tree() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        tree = [{"path": path, "type": "blob"} for path in UV_ACTIONS_DOCKER_TREE]
        tree.append({"path": "src", "type": "tree"})
        return httpx.Response(200, json={"sha": "abc", "tree": tree, "truncated": False})

    async with make_client(handler) as client:
        result = await detect_repo(client, "octo/widget")

    assert seen[0].url.path == "/repos/octo/widget/git/trees/HEAD"
    assert seen[0].url.params["recursive"] == "1"
    assert result.repo == "octo/widget"
    assert result.truncated is False
    assert Expectation(Ecosystem.UV, "/") in result.expectations


async def test_detect_repo_surfaces_a_truncated_tree() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sha": "abc", "tree": [{"path": "go.mod", "type": "blob"}], "truncated": True},
        )

    async with make_client(handler) as client:
        result = await detect_repo(client, "octo/widget", ref="main")

    assert result.truncated is True
    assert result.expectations == (Expectation(Ecosystem.GOMOD, "/"),)


async def test_detect_repo_honours_an_explicit_ref() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, content=json.dumps({"tree": []}))

    async with make_client(handler) as client:
        result = await detect_repo(client, "octo/widget", ref="v1.2.3")

    assert seen == ["/repos/octo/widget/git/trees/v1.2.3"]
    assert result.expectations == ()
