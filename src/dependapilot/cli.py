"""Command-line entry points for DependaPilot."""

from __future__ import annotations

import asyncio
import os

import click
import uvicorn
from fastapi import FastAPI

from dependapilot.app import build_app
from dependapilot.config import ConfigError, load_fleet_config
from dependapilot.github import GitHubClient
from dependapilot.github.errors import GitHubAuthError

DEFAULT_CONFIG_PATH = "repos.yml"


@click.group()
def main() -> None:
    """DependaPilot: automated dependency update orchestration."""


@main.command()
@click.option(
    "--config",
    "config_path",
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to the fleet's repos.yml.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=8000, show_default=True, type=int, help="Bind port.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload for development.")
def serve(config_path: str, host: str, port: int, reload: bool) -> None:
    """Run the DependaPilot web server against a live fleet."""
    if reload:
        # uvicorn's auto-reload re-imports the app by string in a fresh
        # subprocess on every change, so it can't be handed an already-built
        # app carrying a live GitHubClient -- `create_live_app` re-wires the
        # config and client inside its ASGI lifespan on every reload, with
        # the config path handed across the subprocess boundary via the
        # environment. Watching the config file itself means an edit to
        # `repos.yml` shows up on save, no manual restart needed.
        os.environ["DEPENDAPILOT_CONFIG"] = config_path
        uvicorn.run(
            "dependapilot.app:create_live_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_includes=[config_path],
        )
        return
    asyncio.run(_serve(config_path, host, port))


async def _build_live_app(config_path: str) -> tuple[FastAPI, GitHubClient]:
    """Load config, authenticate, and wire a live app -- the bootstrap `serve` runs.

    Split out from `_serve` so it's exercisable directly (mocked `gh`
    subprocess + mocked HTTP transport, no real server) without also
    starting uvicorn.

    Raises `SystemExit` with an actionable, non-traceback message on a
    missing/invalid `repos.yml` or failed `gh` auth -- deliberately *before*
    `server.serve()` runs the ASGI lifespan, since a failure raised from
    inside that lifespan is reported by uvicorn as a full traceback instead.
    """
    try:
        fleet = load_fleet_config(config_path)
    except ConfigError as exc:
        raise SystemExit(f"error: {exc}") from exc

    try:
        client = await GitHubClient.create()
    except GitHubAuthError as exc:
        raise SystemExit(f"error: {exc}") from exc

    return build_app(fleet, client), client


async def _serve(config_path: str, host: str, port: int) -> None:
    app, client = await _build_live_app(config_path)
    try:
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
        await server.serve()
    finally:
        await client.aclose()


if __name__ == "__main__":
    main()
