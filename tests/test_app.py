"""Smoke tests for the DependaPilot FastAPI app."""

from fastapi.testclient import TestClient

from dependapilot.app import create_app


def test_healthz_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_renders_html() -> None:
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert "DependaPilot" in response.text
