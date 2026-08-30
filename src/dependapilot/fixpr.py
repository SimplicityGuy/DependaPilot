"""Fix-PR automation: turn an audit's findings into a reviewable PR on the target repo.

`open_fix_pr` is the audit's action step -- it never writes to the repo's default
branch. It always lands the corrected config on a dedicated `BRANCH_NAME` branch
(created from the default branch's head sha, never the default branch's own ref) and
opens or reuses a PR from that branch. Idempotency is keyed on an *open* PR whose head
is `BRANCH_NAME`: a second run against the same repo updates that branch's file rather
than stacking a second PR.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, Final

from dependapilot.audit.engine import CONFIG_PATH
from dependapilot.audit.findings import Finding
from dependapilot.github.client import GitHubClient
from dependapilot.github.errors import GitHubAPIError

BRANCH_NAME: Final = "dependapilot/dependabot-config"
PR_TITLE: Final = "chore: fix dependabot configuration"
COMMIT_MESSAGE: Final = "chore: fix dependabot configuration"


async def _default_branch(client: GitHubClient, owner: str, name: str) -> str:
    response = await client.get(f"/repos/{owner}/{name}")
    payload: Any = response.json()
    return str(payload["default_branch"])


async def _branch_head_sha(client: GitHubClient, owner: str, name: str, branch: str) -> str | None:
    """The sha `branch` currently points at, or None if the branch doesn't exist."""
    try:
        response = await client.get(f"/repos/{owner}/{name}/git/ref/heads/{branch}")
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            return None
        raise
    payload: Any = response.json()
    return str(payload["object"]["sha"])


async def _find_open_fix_pr(client: GitHubClient, owner: str, name: str) -> dict[str, Any] | None:
    """The open PR from `BRANCH_NAME`, if DependaPilot already opened one for this repo."""
    response = await client.get(
        f"/repos/{owner}/{name}/pulls",
        params={"state": "open", "head": f"{owner}:{BRANCH_NAME}"},
    )
    payload: Any = response.json()
    return payload[0] if payload else None


async def _ensure_fix_branch(
    client: GitHubClient, owner: str, name: str, default_branch: str
) -> None:
    """Create `BRANCH_NAME` from `default_branch`'s head sha, unless it already exists.

    Never touches `default_branch`'s own ref -- only reads its head sha to seed a new
    branch of its own.
    """
    if await _branch_head_sha(client, owner, name, BRANCH_NAME) is not None:
        return
    head_sha = await _branch_head_sha(client, owner, name, default_branch)
    if head_sha is None:
        raise GitHubAPIError(
            f"{owner}/{name}: default branch {default_branch!r} has no head ref",
            status_code=404,
        )
    await client.request(
        "POST",
        f"/repos/{owner}/{name}/git/refs",
        json={"ref": f"refs/heads/{BRANCH_NAME}", "sha": head_sha},
    )


async def _existing_file_sha(
    client: GitHubClient, owner: str, name: str, branch: str
) -> str | None:
    try:
        response = await client.get(
            f"/repos/{owner}/{name}/contents/{CONFIG_PATH}", params={"ref": branch}
        )
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            return None
        raise
    payload: Any = response.json()
    sha = payload.get("sha") if isinstance(payload, dict) else None
    return sha if isinstance(sha, str) else None


async def _write_fix_branch_config(
    client: GitHubClient, owner: str, name: str, config_yaml: str
) -> None:
    """Create-or-update `.github/dependabot.yml` on `BRANCH_NAME` -- never elsewhere.

    `BRANCH_NAME` is the one and only target this function ever writes to: it takes
    no branch argument, so there is no code path through it that can reach the
    default branch.
    """
    existing_sha = await _existing_file_sha(client, owner, name, BRANCH_NAME)
    body: dict[str, Any] = {
        "message": COMMIT_MESSAGE,
        "content": base64.b64encode(config_yaml.encode()).decode(),
        "branch": BRANCH_NAME,
    }
    if existing_sha is not None:
        body["sha"] = existing_sha
    await client.request("PUT", f"/repos/{owner}/{name}/contents/{CONFIG_PATH}", json=body)


def _render_pr_body(findings: Sequence[Finding]) -> str:
    if not findings:
        return (
            "This PR was opened automatically by DependaPilot to fix your Dependabot configuration."
        )
    lines = [
        "This PR was opened automatically by DependaPilot to resolve the following "
        "Dependabot configuration findings:",
        "",
    ]
    lines.extend(f"- **{finding.check.value}**: {finding.message}" for finding in findings)
    return "\n".join(lines)


async def open_fix_pr(
    client: GitHubClient,
    repo: str,
    *,
    config_yaml: str,
    findings: Sequence[Finding],
) -> str:
    """Open (or update) the fix PR that carries `config_yaml` for `repo`.

    Args:
        client: Authenticated GitHub client.
        repo: An `owner/name` slug.
        config_yaml: The corrected `.github/dependabot.yml` text (see `audit.suggest`).
        findings: The findings this fix resolves, listed in the PR body.

    Returns:
        The URL of the open fix PR -- freshly created, or the existing one if
        DependaPilot already has one open for this repo.

    Idempotent: if an open PR from `BRANCH_NAME` already exists, this updates that
    branch's file and returns the existing PR instead of opening a second one. Never
    writes to the repo's default branch.
    """
    owner, _, name = repo.partition("/")

    existing_pr = await _find_open_fix_pr(client, owner, name)
    if existing_pr is not None:
        await _write_fix_branch_config(client, owner, name, config_yaml)
        return str(existing_pr["html_url"])

    default_branch = await _default_branch(client, owner, name)
    await _ensure_fix_branch(client, owner, name, default_branch)
    await _write_fix_branch_config(client, owner, name, config_yaml)

    response = await client.request(
        "POST",
        f"/repos/{owner}/{name}/pulls",
        json={
            "title": PR_TITLE,
            "head": BRANCH_NAME,
            "base": default_branch,
            "body": _render_pr_body(findings),
        },
    )
    payload: Any = response.json()
    return str(payload["html_url"])
