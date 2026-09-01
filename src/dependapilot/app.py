"""FastAPI application factory for DependaPilot."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.types import Lifespan

from dependapilot.actions import ActionOutcome, ActionResult, ActionsService
from dependapilot.audit_service import (
    SETTINGS_CHECKS,
    SETTINGS_REMEDIATION_HINT,
    AuditBadge,
    AuditService,
    RepoAuditView,
    badge_for,
)
from dependapilot.bulk import execute_bulk, parse_selection, preview_bulk
from dependapilot.ci import CIVerdictService
from dependapilot.config import FleetConfig, load_fleet_config
from dependapilot.discovery import DiscoveryService
from dependapilot.fleet import FleetService, compute_fleet_totals
from dependapilot.github import GitHubClient
from dependapilot.github.errors import GitHubAPIError, github_error_message
from dependapilot.scoring import SafetyBucket

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["SETTINGS_CHECKS"] = frozenset(check.value for check in SETTINGS_CHECKS)
templates.env.globals["SETTINGS_REMEDIATION_HINT"] = SETTINGS_REMEDIATION_HINT


def create_app(
    fleet_service: FleetService | None = None,
    actions_service: ActionsService | None = None,
    audit_service: AuditService | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Build and configure the DependaPilot FastAPI application.

    `fleet_service` is `None` for the plain scaffold app (`/healthz`, a static
    `/` shell) -- the real `serve` command and every test that exercises the
    dashboard pass a `FleetService` explicitly. The `/fleet` route degrades to
    an inline "not configured" message rather than raising when it's absent.

    `actions_service` is likewise `None` until the dashboard is wired to a
    live `GitHubClient`; every action route degrades to an inline "not
    configured" outcome rather than raising when it's absent.

    `audit_service` follows the same contract for the `/audit` page and the
    fleet-view audit badge.

    `lifespan` lets a caller defer service wiring to ASGI startup (see
    `create_live_app`); every route reads its service off `app.state` per
    request, so state populated by a lifespan is picked up transparently.
    """
    app = FastAPI(title="DependaPilot", lifespan=lifespan)
    app.state.fleet_service = fleet_service
    app.state.actions_service = actions_service
    app.state.audit_service = audit_service
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
                request,
                "_fleet.html",
                {
                    "repos": (),
                    "unconfigured": True,
                    "audit_badges": {},
                    "actions_configured": False,
                },
            )
        audit: AuditService | None = request.app.state.audit_service
        actions: ActionsService | None = request.app.state.actions_service
        repos, audit_badges = await asyncio.gather(
            service.get_fleet_view(force_refresh=refresh),
            _audit_badges(audit, refresh=refresh),
        )
        return templates.TemplateResponse(
            request,
            "_fleet.html",
            {
                "repos": repos,
                "unconfigured": False,
                "audit_badges": audit_badges,
                "actions_configured": actions is not None,
                "totals": compute_fleet_totals(repos),
            },
        )

    def _render_audit_repo(request: Request, view: RepoAuditView) -> HTMLResponse:
        return templates.TemplateResponse(request, "_audit_repo.html", {"view": view})

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "audit.html", {})

    @app.get("/audit/list", response_class=HTMLResponse)
    async def audit_list(request: Request, refresh: bool = False) -> HTMLResponse:
        service: AuditService | None = request.app.state.audit_service
        if service is None:
            return templates.TemplateResponse(
                request, "_audit_list.html", {"views": (), "unconfigured": True}
            )
        views = await service.get_audit_view(force_refresh=refresh)
        return templates.TemplateResponse(
            request, "_audit_list.html", {"views": views, "unconfigured": False}
        )

    @app.get("/audit/{owner}/{repo}", response_class=HTMLResponse)
    async def reaudit_repo(request: Request, owner: str, repo: str) -> HTMLResponse:
        service: AuditService | None = request.app.state.audit_service
        repo_slug = f"{owner}/{repo}"
        if service is None:
            return _render_audit_repo(
                request, RepoAuditView(repo=repo_slug, error="Audit service is not configured.")
            )
        view = await service.get_repo_view(repo_slug, force_refresh=True)
        return _render_audit_repo(request, view)

    @app.post("/audit/{owner}/{repo}/fix-pr", response_class=HTMLResponse)
    async def audit_fix_pr(request: Request, owner: str, repo: str) -> HTMLResponse:
        service: AuditService | None = request.app.state.audit_service
        repo_slug = f"{owner}/{repo}"
        if service is None:
            return _render_audit_repo(
                request, RepoAuditView(repo=repo_slug, error="Audit service is not configured.")
            )
        try:
            await service.open_fix_pr(repo_slug)
        except GitHubAPIError as exc:
            view = service.record_fix_pr_error(repo_slug, github_error_message(exc))
        except RuntimeError as exc:
            view = service.record_fix_pr_error(repo_slug, str(exc))
        else:
            view = await service.get_repo_view(repo_slug)
        return _render_audit_repo(request, view)

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

    @app.post("/fleet/bulk/preview", response_class=HTMLResponse)
    async def bulk_preview(
        request: Request,
        action: Annotated[str, Form()],
        repo: Annotated[str | None, Form()] = None,
        min_bucket: Annotated[str, Form()] = SafetyBucket.SAFE.value,
        selected: Annotated[list[str] | None, Form()] = None,
    ) -> HTMLResponse:
        fleet_service: FleetService | None = request.app.state.fleet_service
        if fleet_service is None:
            return templates.TemplateResponse(request, "_bulk_panel.html", {"unconfigured": True})
        try:
            selection = parse_selection(selected) if selected else None
            preview = await preview_bulk(
                fleet_service,
                action=action,
                repo=repo,
                min_bucket=SafetyBucket(min_bucket),
                selected=selection,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "_bulk_panel.html",
            {
                "preview": preview,
                "repo": repo,
                "min_bucket": min_bucket,
                "unconfigured": False,
                "selected": selected or (),
            },
        )

    @app.post("/fleet/bulk/execute", response_class=HTMLResponse)
    async def bulk_execute(
        request: Request,
        action: Annotated[str, Form()],
        repo: Annotated[str | None, Form()] = None,
        min_bucket: Annotated[str, Form()] = SafetyBucket.SAFE.value,
        selected: Annotated[list[str] | None, Form()] = None,
    ) -> HTMLResponse:
        fleet_service: FleetService | None = request.app.state.fleet_service
        actions_service: ActionsService | None = request.app.state.actions_service
        if fleet_service is None or actions_service is None:
            return templates.TemplateResponse(request, "_bulk_results.html", {"unconfigured": True})
        try:
            selection = parse_selection(selected) if selected else None
            outcome = await execute_bulk(
                fleet_service,
                actions_service,
                action=action,
                repo=repo,
                min_bucket=SafetyBucket(min_bucket),
                selected=selection,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request, "_bulk_results.html", {"outcome": outcome, "unconfigured": False}
        )

    return app


async def _audit_badges(service: AuditService | None, *, refresh: bool) -> dict[str, AuditBadge]:
    """Every audit-enabled repo's fleet-view badge, sourced from `AuditService`'s
    own cache so the fleet view never triggers a redundant audit run."""
    if service is None:
        return {}
    views = await service.get_audit_view(force_refresh=refresh)
    return {view.repo: badge_for(view) for view in views}


def _unconfigured_result(owner: str, repo: str, number: int, action: str) -> ActionResult:
    return ActionResult(
        repo=f"{owner}/{repo}",
        number=number,
        action=action,
        outcome=ActionOutcome.FAILED,
        message="Actions are not configured for this dashboard.",
    )


def _build_services(
    fleet: FleetConfig, client: GitHubClient
) -> tuple[FleetService, ActionsService, AuditService]:
    """Build every service off the one client and `FleetConfig`, keyed by each
    repo's `audit`/`actions` flags."""
    discovery = DiscoveryService(client, fleet)
    ci_service = CIVerdictService(client)
    audit_enabled_repos = frozenset(entry.repo for entry in fleet.repos if entry.audit)
    actions_enabled_repos = frozenset(entry.repo for entry in fleet.repos if entry.actions)
    fleet_service = FleetService(
        client,
        discovery,
        ci_service,
        audit_enabled_repos=audit_enabled_repos,
        actions_enabled_repos=actions_enabled_repos,
    )
    actions_service = ActionsService(client, fleet, ci_service)
    audit_service = AuditService(client, fleet, audit_enabled_repos=audit_enabled_repos)
    return fleet_service, actions_service, audit_service


def build_app(fleet: FleetConfig, client: GitHubClient) -> FastAPI:
    """Wire a live dashboard from an already-authenticated `GitHubClient`.

    `cli.py serve`'s non-reload entry point -- the counterpart to the
    reload-mode factory `create_live_app` below.
    """
    return create_app(*_build_services(fleet, client))


def create_live_app() -> FastAPI:
    """uvicorn factory for `serve --reload`: a live app wired at ASGI startup.

    Reload mode re-imports the app by string in a fresh subprocess on every
    change, so config loading and `gh` auth can't happen in `cli.py` -- they
    run in the lifespan instead, re-reading `repos.yml` (path via the
    `DEPENDAPILOT_CONFIG` env var) on every reload. A bad config or failed
    auth fails startup with uvicorn's traceback; the reloader stays up and
    retries on the next file save.
    """
    config_path = os.environ.get("DEPENDAPILOT_CONFIG", "repos.yml")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        fleet = load_fleet_config(config_path)
        client = await GitHubClient.create()
        app.state.fleet_service, app.state.actions_service, app.state.audit_service = (
            _build_services(fleet, client)
        )
        try:
            yield
        finally:
            await client.aclose()

    return create_app(lifespan=lifespan)


app = create_app()
