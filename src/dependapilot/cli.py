"""Command-line entry points for DependaPilot."""

import click
import uvicorn


@click.group()
def main() -> None:
    """DependaPilot: automated dependency update orchestration."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=8000, show_default=True, type=int, help="Bind port.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload for development.")
def serve(host: str, port: int, reload: bool) -> None:
    """Run the DependaPilot web server."""
    uvicorn.run("dependapilot.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
