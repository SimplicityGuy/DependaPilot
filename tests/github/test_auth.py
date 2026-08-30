"""Tests for `gh` CLI token retrieval. No live `gh` subprocess is ever run."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from dependapilot.github.auth import get_gh_cli_token
from dependapilot.github.errors import GitHubAuthError


class _FakeProcess:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[..., _FakeProcess]
) -> None:
    async def fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return factory()

    monkeypatch.setattr(
        "dependapilot.github.auth.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr("dependapilot.github.auth.shutil.which", lambda _cmd: "/usr/bin/gh")


async def test_returns_token_from_gh_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, lambda: _FakeProcess(0, b"gho_faketoken1234\n", b""))

    token = await get_gh_cli_token()

    assert token == "gho_faketoken1234"


async def test_raises_actionable_error_when_gh_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dependapilot.github.auth.shutil.which", lambda _cmd: None)

    with pytest.raises(GitHubAuthError, match="gh auth login"):
        await get_gh_cli_token()


async def test_raises_actionable_error_when_not_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_subprocess(
        monkeypatch,
        lambda: _FakeProcess(1, b"", b"You are not logged into any GitHub hosts.\n"),
    )

    with pytest.raises(GitHubAuthError, match="gh auth login"):
        await get_gh_cli_token()


async def test_raises_actionable_error_on_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, lambda: _FakeProcess(0, b"\n", b""))

    with pytest.raises(GitHubAuthError, match="gh auth login"):
        await get_gh_cli_token()
