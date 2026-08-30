"""Token retrieval from the `gh` CLI.

DependaPilot never stores a GitHub token itself: it shells out to the `gh` CLI, which
owns login state (keyring or `~/.config/gh/hosts.yml`). No token ever lives in our own
env vars or config files.
"""

from __future__ import annotations

import asyncio
import shutil

from dependapilot.github.errors import GitHubAuthError

_REAUTH_HINT = "Run `gh auth login` (or `gh auth refresh` if already logged in) and retry."


async def get_gh_cli_token() -> str:
    """Return the active `gh` CLI auth token for github.com.

    Raises:
        GitHubAuthError: `gh` is not installed, or has no active login.
    """
    if shutil.which("gh") is None:
        raise GitHubAuthError(f"The `gh` CLI is not installed or not on PATH. {_REAUTH_HINT}")

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except OSError as exc:
        raise GitHubAuthError(f"Failed to run `gh auth token`: {exc}. {_REAUTH_HINT}") from exc

    if proc.returncode != 0:
        detail = stderr.decode().strip() or "no output"
        raise GitHubAuthError(f"`gh auth token` failed ({detail}). {_REAUTH_HINT}")

    token = stdout.decode().strip()
    if not token:
        raise GitHubAuthError(f"`gh auth token` returned an empty token. {_REAUTH_HINT}")

    return token
