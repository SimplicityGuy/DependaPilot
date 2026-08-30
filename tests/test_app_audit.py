"""End-to-end tests for the audit FastAPI routes, against a fully faked
`AuditService` -- exercises only the FastAPI wiring and Jinja2 rendering
(`AuditService`'s own composition and diffing logic is covered by
`test_audit_service.py`).
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from dependapilot.app import create_app
from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.audit_service import RepoAuditView
from dependapilot.github.errors import GitHubAPIError

REPO = "acme/widgets"


def make_view(
    *,
    repo: str = REPO,
    findings: tuple[Finding, ...] = (),
    diff: tuple[str, ...] = (),
    existing_fix_pr_url: str | None = None,
    error: str | None = None,
) -> RepoAuditView:
    return RepoAuditView(
        repo=repo,
        findings=findings,
        current_config="version: 2\nupdates: []\n",
        suggested_config="version: 2\nupdates: [{}]\n",
        diff=diff,
        existing_fix_pr_url=existing_fix_pr_url,
        error=error,
    )


class FakeAuditService:
    def __init__(self, views: tuple[RepoAuditView, ...]) -> None:
        self.views: dict[str, RepoAuditView] = {view.repo: view for view in views}
        self.list_refresh_calls: list[bool] = []
        self.repo_refresh_calls: list[tuple[str, bool]] = []
        self.opened: list[str] = []
        self.fix_pr_url: str | None = None
        self.fix_pr_exception: Exception | None = None

    async def get_audit_view(self, *, force_refresh: bool = False) -> tuple[RepoAuditView, ...]:
        self.list_refresh_calls.append(force_refresh)
        return tuple(self.views.values())

    async def get_repo_view(self, repo: str, *, force_refresh: bool = False) -> RepoAuditView:
        self.repo_refresh_calls.append((repo, force_refresh))
        return self.views[repo]

    async def open_fix_pr(self, repo: str) -> str:
        self.opened.append(repo)
        if self.fix_pr_exception is not None:
            raise self.fix_pr_exception
        url = self.fix_pr_url or f"https://github.com/{repo}/pull/99"
        self.views[repo] = replace(self.views[repo], existing_fix_pr_url=url, fix_pr_error=None)
        return url

    def record_fix_pr_error(self, repo: str, message: str) -> RepoAuditView:
        updated = replace(self.views[repo], fix_pr_error=message)
        self.views[repo] = updated
        return updated


class TestAuditPageShell:
    def test_renders_shell_that_loads_the_list_fragment(self) -> None:
        client = TestClient(create_app(audit_service=FakeAuditService(())))  # type: ignore[arg-type]

        response = client.get("/audit")

        assert response.status_code == 200
        assert "DependaPilot" in response.text
        assert 'hx-get="/audit/list"' in response.text


class TestAuditListFragment:
    def test_unconfigured_service_renders_inline_message(self) -> None:
        client = TestClient(create_app())

        response = client.get("/audit/list")

        assert response.status_code == 200
        assert "not configured" in response.text.lower()

    def test_no_audit_enabled_repos_shows_empty_state(self) -> None:
        client = TestClient(create_app(audit_service=FakeAuditService(())))  # type: ignore[arg-type]

        response = client.get("/audit/list")

        assert response.status_code == 200
        assert "No repositories have auditing enabled" in response.text

    def test_zero_findings_repo_shows_compliant(self) -> None:
        views = (make_view(),)
        client = TestClient(create_app(audit_service=FakeAuditService(views)))  # type: ignore[arg-type]

        response = client.get("/audit/list")

        assert response.status_code == 200
        assert REPO in response.text
        assert "compliant" in response.text.lower()

    def test_findings_render_severity_sorted(self) -> None:
        # Pre-sorted, as `AuditService` itself hands them to the template --
        # this test is about rendering order, not `sort_findings` itself
        # (covered in `test_audit_service.py`).
        findings = (
            Finding(
                repo=REPO, check=Check.MISSING_CONFIG, severity=Severity.HIGH, message="high one"
            ),
            Finding(
                repo=REPO, check=Check.MISSING_ECOSYSTEM, severity=Severity.MEDIUM, message="m"
            ),
        )
        views = (make_view(findings=findings),)
        client = TestClient(create_app(audit_service=FakeAuditService(views)))  # type: ignore[arg-type]

        response = client.get("/audit/list")

        assert response.status_code == 200
        high_pos = response.text.index("MISSING_CONFIG")
        medium_pos = response.text.index("MISSING_ECOSYSTEM")
        assert high_pos < medium_pos
        assert "badge-severity-high" in response.text
        assert "badge-severity-medium" in response.text

    def test_unknown_scope_findings_render_distinctly_with_remediation_hint(self) -> None:
        findings = (
            Finding(
                repo=REPO,
                check=Check.ALERTS_UNKNOWN,
                severity=Severity.INFO,
                message="Could not read Dependabot alert settings",
            ),
        )
        views = (make_view(findings=findings),)
        client = TestClient(create_app(audit_service=FakeAuditService(views)))  # type: ignore[arg-type]

        response = client.get("/audit/list")

        assert response.status_code == 200
        assert "badge-severity-info" in response.text
        assert "remediation-hint" in response.text
        assert "Settings" in response.text

    def test_diff_lines_render_with_add_and_del_markers(self) -> None:
        diff = ("--- .github/dependabot.yml", "+++ .github/dependabot.yml", "-old", "+new")
        views = (make_view(diff=diff),)
        client = TestClient(create_app(audit_service=FakeAuditService(views)))  # type: ignore[arg-type]

        response = client.get("/audit/list")

        assert "diff-add" in response.text
        assert "diff-del" in response.text
        assert ">-old<" in response.text or "-old" in response.text
        assert "+new" in response.text

    def test_repo_error_renders_section_level_error_without_blanking_page(self) -> None:
        views = (
            RepoAuditView(repo="acme/broken", error="GitHub API request failed: 500"),
            make_view(repo="acme/fine"),
        )
        client = TestClient(create_app(audit_service=FakeAuditService(views)))  # type: ignore[arg-type]

        response = client.get("/audit/list")

        assert response.status_code == 200
        assert "Could not audit this repo" in response.text
        assert "acme/fine" in response.text

    def test_existing_fix_pr_is_shown(self) -> None:
        views = (make_view(existing_fix_pr_url="https://github.com/acme/widgets/pull/5"),)
        client = TestClient(create_app(audit_service=FakeAuditService(views)))  # type: ignore[arg-type]

        response = client.get("/audit/list")

        assert "https://github.com/acme/widgets/pull/5" in response.text
        assert "Update fix PR" in response.text

    def test_refresh_forwards_to_service(self) -> None:
        service = FakeAuditService((make_view(),))
        client = TestClient(create_app(audit_service=service))  # type: ignore[arg-type]

        client.get("/audit/list")
        client.get("/audit/list?refresh=true")

        assert service.list_refresh_calls == [False, True]


class TestReauditRoute:
    def test_reaudit_forces_a_fresh_view_and_swaps_the_section(self) -> None:
        service = FakeAuditService((make_view(),))
        client = TestClient(create_app(audit_service=service))  # type: ignore[arg-type]

        response = client.get("/audit/acme/widgets")

        assert response.status_code == 200
        assert service.repo_refresh_calls == [(REPO, True)]
        assert REPO in response.text

    def test_reaudit_without_configured_service_shows_inline_error(self) -> None:
        client = TestClient(create_app())

        response = client.get("/audit/acme/widgets")

        assert response.status_code == 200
        assert "not configured" in response.text.lower()


class TestFixPrRoute:
    def test_opens_a_fix_pr_and_renders_the_url(self) -> None:
        service = FakeAuditService((make_view(),))
        service.fix_pr_url = "https://github.com/acme/widgets/pull/42"
        client = TestClient(create_app(audit_service=service))  # type: ignore[arg-type]

        response = client.post("/audit/acme/widgets/fix-pr")

        assert response.status_code == 200
        assert service.opened == [REPO]
        assert "https://github.com/acme/widgets/pull/42" in response.text

    def test_github_failure_renders_the_error_inline(self) -> None:
        service = FakeAuditService((make_view(),))
        service.fix_pr_exception = GitHubAPIError(
            "boom", status_code=422, body='{"message": "Validation failed"}'
        )
        client = TestClient(create_app(audit_service=service))  # type: ignore[arg-type]

        response = client.post("/audit/acme/widgets/fix-pr")

        assert response.status_code == 200
        assert "Validation failed" in response.text

    def test_runtime_error_renders_the_error_inline(self) -> None:
        service = FakeAuditService((make_view(),))
        service.fix_pr_exception = RuntimeError("cannot open a fix PR: audit failed")
        client = TestClient(create_app(audit_service=service))  # type: ignore[arg-type]

        response = client.post("/audit/acme/widgets/fix-pr")

        assert response.status_code == 200
        assert "cannot open a fix PR" in response.text

    def test_without_configured_service_shows_inline_error(self) -> None:
        client = TestClient(create_app())

        response = client.post("/audit/acme/widgets/fix-pr")

        assert response.status_code == 200
        assert "not configured" in response.text.lower()
