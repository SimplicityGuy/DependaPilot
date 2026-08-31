"""Integration tests for `cli.py serve`'s bootstrap: config loading, `gh` auth,
and live service wiring -- exercised against a mocked `gh` subprocess and a
mocked GitHub HTTP transport, never a real subprocess or live network.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from dependapilot.actions import ActionsService
from dependapilot.audit_service import AuditService
from dependapilot.cli import _build_live_app
from dependapilot.fleet import FleetService
from dependapilot.github import client as gh_client_module
from dependapilot.github.errors import GitHubAuthError


def write_repos_yml(tmp_path: Path, text: str) -> Path:
    config_path = tmp_path / "repos.yml"
    config_path.write_text(text)
    return config_path


def patch_gh_cli_token(monkeypatch: pytest.MonkeyPatch, *, token: str = "test-token") -> None:
    """Mock the `gh auth token` subprocess boundary with a fixed token."""

    async def fake_get_gh_cli_token() -> str:
        return token

    monkeypatch.setattr(gh_client_module, "get_gh_cli_token", fake_get_gh_cli_token)


def patch_github_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Mock every `httpx.AsyncClient` the GitHub client builds with a `MockTransport`."""
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return real_async_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)


async def test_missing_config_exits_with_actionable_message_not_a_traceback(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "does-not-exist.yml"

    with pytest.raises(SystemExit) as exc_info:
        await _build_live_app(str(missing_path))

    assert "does-not-exist.yml" in str(exc_info.value)


async def test_invalid_config_exits_with_actionable_message(tmp_path: Path) -> None:
    config_path = write_repos_yml(tmp_path, "not_a_real_key: true\n")

    with pytest.raises(SystemExit) as exc_info:
        await _build_live_app(str(config_path))

    assert "error:" in str(exc_info.value)


async def test_absent_gh_auth_exits_with_actionable_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_repos_yml(tmp_path, "repos: []\n")

    async def fake_get_gh_cli_token() -> str:
        raise GitHubAuthError("The `gh` CLI is not installed or not on PATH.")

    monkeypatch.setattr(gh_client_module, "get_gh_cli_token", fake_get_gh_cli_token)

    with pytest.raises(SystemExit) as exc_info:
        await _build_live_app(str(config_path))

    assert "gh" in str(exc_info.value)


async def test_rejected_token_exits_with_actionable_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_repos_yml(tmp_path, "repos: []\n")
    patch_gh_cli_token(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    patch_github_transport(monkeypatch, handler)

    with pytest.raises(SystemExit) as exc_info:
        await _build_live_app(str(config_path))

    assert "gh auth login" in str(exc_info.value)


async def test_serve_wires_real_services_honoring_config_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_repos_yml(
        tmp_path,
        """
        repos:
          - repo: acme/one
          - repo: acme/two
            actions: true
          - repo: acme/three
            audit: false
        """,
    )
    patch_gh_cli_token(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "test-user"})
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"message": "not found"})

    patch_github_transport(monkeypatch, handler)

    app, client = await _build_live_app(str(config_path))
    try:
        assert isinstance(app.state.fleet_service, FleetService)
        assert isinstance(app.state.actions_service, ActionsService)
        assert isinstance(app.state.audit_service, AuditService)
        # acme/one and acme/two default to audit: true; acme/three opts out.
        assert app.state.audit_service.audit_enabled_repos == {"acme/one", "acme/two"}

        views = await app.state.fleet_service.get_fleet_view()
        by_repo = {view.repo: view for view in views}
        assert by_repo["acme/one"].audit_enabled is True
        assert by_repo["acme/one"].actions_enabled is False
        assert by_repo["acme/two"].actions_enabled is True
        assert by_repo["acme/three"].audit_enabled is False

        with TestClient(app) as test_client:
            response = test_client.get("/fleet")
            assert response.status_code == 200
            assert "not configured" not in response.text
            # acme/three opted out of audit -- its badge reads "off"; the
            # other two are audit-enabled and rendered (live, not a placeholder).
            assert "audit off" in response.text
    finally:
        await client.aclose()
