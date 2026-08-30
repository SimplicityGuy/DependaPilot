"""FastAPI application factory for DependaPilot."""

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from dependapilot.actions import ActionOutcome, ActionResult, ActionsService
from dependapilot.fleet import FleetService

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(
    fleet_service: FleetService | None = None,
    actions_service: ActionsService | None = None,
) -> FastAPI:
    """Build and configure the DependaPilot FastAPI application.

    `fleet_service` is `None` for the plain scaffold app (`/healthz`, a static
    `/` shell) -- the real `serve` command and every test that exercises the
    dashboard pass a `FleetService` explicitly. The `/fleet` route degrades to
    an inline "not configured" message rather than raising when it's absent.

    `actions_service` is likewise `None` until the dashboard is wired to a
    live `GitHubClient`; every action route degrades to an inline "not
    configured" outcome rather than raising when it's absent.
    """
    app = FastAPI(title="DependaPilot")
    app.state.fleet_service = fleet_service
    app.state.actions_service = actions_service
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

    def _render_action_result(
        request: Request, owner: str, repo: str, number: int, sha: str, result: ActionResult
    ) -> HTMLResponse:
        # A merge that just succeeded leaves nothing further to do on this PR;
        # any other outcome (approved/rebased/skipped/failed) is retryable, so
        # the buttons come back live rather than being permanently disabled by
        # a single failed attempt.
        ci_green = result.outcome != ActionOutcome.MERGED
        return templates.TemplateResponse(
            request,
            "_actions_cell.html",
            {
                "owner": owner,
                "repo_name": repo,
                "number": number,
                "head_sha": sha,
                "ci_green": ci_green,
                "outcome": result,
            },
        )

    @app.post("/repos/{owner}/{repo}/pulls/{number}/approve", response_class=HTMLResponse)
    async def approve_pr(
        request: Request, owner: str, repo: str, number: int, sha: Annotated[str, Form()]
    ) -> HTMLResponse:
        service: ActionsService | None = request.app.state.actions_service
        if service is None:
            result = _unconfigured_result(owner, repo, number, "approve")
        else:
            result = await service.approve(f"{owner}/{repo}", number)
        return _render_action_result(request, owner, repo, number, sha, result)

    @app.post("/repos/{owner}/{repo}/pulls/{number}/merge", response_class=HTMLResponse)
    async def merge_pr(
        request: Request, owner: str, repo: str, number: int, sha: Annotated[str, Form()]
    ) -> HTMLResponse:
        service: ActionsService | None = request.app.state.actions_service
        if service is None:
            result = _unconfigured_result(owner, repo, number, "merge")
        else:
            result = await service.merge(f"{owner}/{repo}", number, sha)
        return _render_action_result(request, owner, repo, number, sha, result)

    @app.post("/repos/{owner}/{repo}/pulls/{number}/rebase", response_class=HTMLResponse)
    async def rebase_pr(
        request: Request, owner: str, repo: str, number: int, sha: Annotated[str, Form()]
    ) -> HTMLResponse:
        service: ActionsService | None = request.app.state.actions_service
        if service is None:
            result = _unconfigured_result(owner, repo, number, "rebase")
        else:
            result = await service.rebase(f"{owner}/{repo}", number)
        return _render_action_result(request, owner, repo, number, sha, result)

    return app


def _unconfigured_result(owner: str, repo: str, number: int, action: str) -> ActionResult:
    return ActionResult(
        repo=f"{owner}/{repo}",
        number=number,
        action=action,
        outcome=ActionOutcome.FAILED,
        message="Actions are not configured for this dashboard.",
    )


app = create_app()
