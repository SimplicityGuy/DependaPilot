"""FastAPI application factory for DependaPilot."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from dependapilot.fleet import FleetService

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(fleet_service: FleetService | None = None) -> FastAPI:
    """Build and configure the DependaPilot FastAPI application.

    `fleet_service` is `None` for the plain scaffold app (`/healthz`, a static
    `/` shell) -- the real `serve` command and every test that exercises the
    dashboard pass a `FleetService` explicitly. The `/fleet` route degrades to
    an inline "not configured" message rather than raising when it's absent.
    """
    app = FastAPI(title="DependaPilot")
    app.state.fleet_service = fleet_service
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", {})

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet(request: Request, refresh: bool = False) -> HTMLResponse:
        service: FleetService | None = request.app.state.fleet_service
        if service is None:
            return templates.TemplateResponse(
                request, "_fleet.html", {"repos": (), "unconfigured": True}
            )
        repos = await service.get_fleet_view(force_refresh=refresh)
        return templates.TemplateResponse(
            request, "_fleet.html", {"repos": repos, "unconfigured": False}
        )

    return app


app = create_app()
