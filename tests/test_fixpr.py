"""Tests for fix-PR automation: branch + file + PR creation, and idempotent reuse.

Every test drives an `httpx.MockTransport` (via `tests.github.conftest.make_client`) --
no real network or `gh` subprocess call.
"""

from __future__ import annotations

import base64
import inspect
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.fixpr import (
    BRANCH_NAME,
    PR_TITLE,
    _write_fix_branch_config,
    open_fix_pr,
)
from tests.github.conftest import make_client

Handler = Callable[[httpx.Request], httpx.Response]

OWNER = "octo"
NAME = "widget"
REPO = f"{OWNER}/{NAME}"
DEFAULT_BRANCH = "main"
DEFAULT_HEAD_SHA = "default-sha-123"
CONFIG_YAML = "version: 2\nupdates: []\n"

REPO_PATH = f"/repos/{OWNER}/{NAME}"
DEFAULT_REF_PATH = f"/repos/{OWNER}/{NAME}/git/ref/heads/{DEFAULT_BRANCH}"
FIX_REF_PATH = f"/repos/{OWNER}/{NAME}/git/ref/heads/{BRANCH_NAME}"
REFS_PATH = f"/repos/{OWNER}/{NAME}/git/refs"
CONTENTS_PATH = f"/repos/{OWNER}/{NAME}/contents/.github/dependabot.yml"
PULLS_PATH = f"/repos/{OWNER}/{NAME}/pulls"

FINDINGS = [
    Finding(
        repo=REPO,
        check=Check.MISSING_CONFIG,
        severity=Severity.HIGH,
        message=f"{REPO} has no .github/dependabot.yml; Dependabot is not configured at all.",
    ),
    Finding(
        repo=REPO,
        check=Check.WRONG_ECOSYSTEM,
        severity=Severity.HIGH,
        message="updates[0] configures 'pip' for /, which uv manages.",
    ),
]


class Requests:
    """Records every request a test's handler saw, for after-the-fact assertions."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.seen.append(request)

    def bodies(self, method: str, path: str) -> list[dict[str, Any]]:
        import json

        return [
            json.loads(r.content) for r in self.seen if r.method == method and r.url.path == path
        ]


@pytest.fixture
def requests() -> Requests:
    return Requests()


def make_handler(
    requests: Requests,
    *,
    fix_branch_exists: bool = False,
    fix_branch_file_sha: str | None = None,
    open_fix_prs: list[dict[str, Any]] | None = None,
    pr_number: int = 42,
) -> Handler:
    """A GitHub-API double covering exactly the endpoints `open_fix_pr` calls."""
    open_fix_prs = list(open_fix_prs or [])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.record(request)
        method, path = request.method, request.url.path

        if method == "GET" and path == REPO_PATH:
            return httpx.Response(200, json={"default_branch": DEFAULT_BRANCH})

        if method == "GET" and path == DEFAULT_REF_PATH:
            return httpx.Response(200, json={"object": {"sha": DEFAULT_HEAD_SHA}})

        if method == "GET" and path == FIX_REF_PATH:
            if fix_branch_exists:
                return httpx.Response(200, json={"object": {"sha": "fix-branch-sha"}})
            return httpx.Response(404, json={"message": "Not Found"})

        if method == "POST" and path == REFS_PATH:
            return httpx.Response(201, json={"ref": f"refs/heads/{BRANCH_NAME}"})

        if method == "GET" and path == CONTENTS_PATH:
            if fix_branch_file_sha is not None:
                return httpx.Response(200, json={"sha": fix_branch_file_sha})
            return httpx.Response(404, json={"message": "Not Found"})

        if method == "PUT" and path == CONTENTS_PATH:
            return httpx.Response(200, json={"content": {"sha": "new-file-sha"}})

        if method == "GET" and path == PULLS_PATH:
            head = request.url.params.get("head")
            if head == f"{OWNER}:{BRANCH_NAME}":
                return httpx.Response(200, json=open_fix_prs)
            return httpx.Response(200, json=[])

        if method == "POST" and path == PULLS_PATH:
            return httpx.Response(
                201,
                json={
                    "number": pr_number,
                    "html_url": f"https://github.com/{REPO}/pull/{pr_number}",
                },
            )

        raise AssertionError(f"unexpected request: {method} {request.url}")

    return handler


async def test_fresh_repo_creates_branch_file_and_pr(requests: Requests) -> None:
    handler = make_handler(requests)
    async with make_client(handler) as client:
        url = await open_fix_pr(client, REPO, config_yaml=CONFIG_YAML, findings=FINDINGS)

    assert url == f"https://github.com/{REPO}/pull/42"

    (refs_body,) = requests.bodies("POST", REFS_PATH)
    assert refs_body == {"ref": f"refs/heads/{BRANCH_NAME}", "sha": DEFAULT_HEAD_SHA}

    (put_body,) = requests.bodies("PUT", CONTENTS_PATH)
    assert put_body["branch"] == BRANCH_NAME
    assert "sha" not in put_body  # no file existed yet on the fix branch
    assert base64.b64decode(put_body["content"]).decode() == CONFIG_YAML

    (pr_body,) = requests.bodies("POST", PULLS_PATH)
    assert pr_body["title"] == PR_TITLE
    assert pr_body["head"] == BRANCH_NAME
    assert pr_body["base"] == DEFAULT_BRANCH
    assert "MISSING_CONFIG" in pr_body["body"]
    assert "WRONG_ECOSYSTEM" in pr_body["body"]
    assert FINDINGS[0].message in pr_body["body"]


async def test_rerun_with_open_fix_pr_updates_branch_without_a_new_pr(
    requests: Requests,
) -> None:
    existing_pr = {
        "number": 7,
        "html_url": f"https://github.com/{REPO}/pull/7",
    }
    handler = make_handler(
        requests,
        fix_branch_exists=True,
        fix_branch_file_sha="old-file-sha",
        open_fix_prs=[existing_pr],
    )
    async with make_client(handler) as client:
        url = await open_fix_pr(client, REPO, config_yaml=CONFIG_YAML, findings=FINDINGS)

    assert url == existing_pr["html_url"]
    assert requests.bodies("POST", PULLS_PATH) == []  # no duplicate PR
    assert requests.bodies("POST", REFS_PATH) == []  # branch already existed, untouched

    (put_body,) = requests.bodies("PUT", CONTENTS_PATH)
    assert put_body["branch"] == BRANCH_NAME
    assert put_body["sha"] == "old-file-sha"


async def test_update_path_uses_the_existing_files_blob_sha(requests: Requests) -> None:
    handler = make_handler(requests, fix_branch_exists=True, fix_branch_file_sha="blob-sha-9")
    async with make_client(handler) as client:
        await open_fix_pr(client, REPO, config_yaml=CONFIG_YAML, findings=[])

    (put_body,) = requests.bodies("PUT", CONTENTS_PATH)
    assert put_body["sha"] == "blob-sha-9"


async def test_branch_already_present_is_not_recreated(requests: Requests) -> None:
    handler = make_handler(requests, fix_branch_exists=True)
    async with make_client(handler) as client:
        await open_fix_pr(client, REPO, config_yaml=CONFIG_YAML, findings=[])

    assert requests.bodies("POST", REFS_PATH) == []


async def test_empty_findings_still_produce_a_readable_body(requests: Requests) -> None:
    handler = make_handler(requests)
    async with make_client(handler) as client:
        await open_fix_pr(client, REPO, config_yaml=CONFIG_YAML, findings=[])

    (pr_body,) = requests.bodies("POST", PULLS_PATH)
    assert pr_body["body"]


def test_the_fix_branch_writer_has_no_way_to_target_another_branch() -> None:
    # `_write_fix_branch_config` takes no branch argument at all, so there is no
    # code path through it that can reach the default branch (or any other branch).
    assert "branch" not in inspect.signature(_write_fix_branch_config).parameters
    source = inspect.getsource(_write_fix_branch_config)
    assert "default_branch" not in source


def test_open_fix_pr_never_mentions_pushing_the_default_branch_ref() -> None:
    import dependapilot.fixpr as fixpr_module

    source = inspect.getsource(fixpr_module)
    # The only POST to git/refs creates BRANCH_NAME; the default branch's own ref is
    # only ever read (to seed a new branch), never written.
    assert 'json={"ref": f"refs/heads/{BRANCH_NAME}"' in source
    assert "refs/heads/{default_branch}" not in source
