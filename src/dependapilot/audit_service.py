"""Audit view service: composes detection, the config audit, and the suggested fix
per repo, cached.

`AuditService.get_audit_view` is the audit page's single entry point -- and the
source the fleet badge reads too, so both surfaces agree without re-auditing on
every render. For each audit-enabled repo it detects the repo's ecosystems, runs
every mechanical config check plus the two settings checks, computes a unified
diff of the current config against the suggested fix, and checks whether a fix PR
is already open. A repo's failure is captured as `RepoAuditView.error` instead of
propagating, so one repo's outage doesn't blank the page -- the same contract
`FleetService` keeps for PR rows.

Results are cached in memory per repo for a TTL, mirroring `DiscoveryService`;
pass `force_refresh=True` (or `get_repo_view`'s own `force_refresh`) to bypass it.
"""

from __future__ import annotations

import asyncio
import difflib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final

import yaml

from dependapilot.audit.detect import detect_repo
from dependapilot.audit.engine import (
    CONFIG_PATH,
    check_repo_settings,
    evaluate_config,
    fetch_config,
)
from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.audit.suggest import render_config, suggest_config
from dependapilot.config import FleetConfig
from dependapilot.fixpr import find_open_fix_pr
from dependapilot.fixpr import open_fix_pr as _open_fix_pr_flow
from dependapilot.github.client import GitHubClient

DEFAULT_CACHE_TTL_SECONDS: Final = 300.0

Clock = Callable[[], float]

_SEVERITY_RANK: Final[dict[Severity, int]] = {
    Severity.HIGH: 0,
    Severity.MEDIUM: 1,
    Severity.LOW: 2,
    Severity.INFO: 3,
}

SETTINGS_CHECKS: Final = frozenset(
    {
        Check.ALERTS_DISABLED,
        Check.ALERTS_UNKNOWN,
        Check.SECURITY_UPDATES_DISABLED,
        Check.SECURITY_UPDATES_UNKNOWN,
    }
)
"""Findings about a repo-level toggle rather than the config file -- a fix PR can't
resolve these, so the dashboard shows a manual-remediation hint instead."""

SETTINGS_REMEDIATION_HINT: Final = (
    "Toggle this manually in the repo's Settings → Code security page; "
    "DependaPilot's token needs admin write on the repo to change it via the API."
)


class AuditBadgeState(StrEnum):
    """The fleet-view badge states, one repo at a time."""

    OK = "ok"
    FINDINGS = "findings"
    UNKNOWN = "unknown"
    OFF = "off"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AuditBadge:
    """The fleet-view badge for one repo: a state plus the finding count it summarizes."""

    state: AuditBadgeState
    count: int = 0


@dataclass(frozen=True, slots=True)
class RepoAuditView:
    """Everything the audit page (and the fleet badge) shows for one repo."""

    repo: str
    findings: tuple[Finding, ...] = ()
    current_config: str | None = None
    """The repo's current `.github/dependabot.yml` text, or None if it has none."""
    suggested_config: str = ""
    diff: tuple[str, ...] = ()
    """Unified diff lines (no trailing newlines) of current vs suggested config."""
    existing_fix_pr_url: str | None = None
    fix_pr_error: str | None = None
    """Set when the most recent "open fix PR" attempt failed against GitHub."""
    error: str | None = None
    """Set instead of the fields above when auditing this repo failed outright."""

    @property
    def compliant(self) -> bool:
        return not self.findings


def sort_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """Findings ordered high -> medium -> low -> info, then by check id."""
    return tuple(sorted(findings, key=lambda f: (_SEVERITY_RANK[f.severity], f.check.value)))


def render_diff(current: str | None, suggested: str) -> tuple[str, ...]:
    """A unified diff of `current` (or nothing, if absent) against `suggested`.

    A missing current config renders as a whole-file addition -- every line of
    `suggested` prefixed `+` -- rather than a special case callers have to detect
    separately.
    """
    return tuple(
        difflib.unified_diff(
            (current or "").splitlines(),
            suggested.splitlines(),
            fromfile=CONFIG_PATH if current is not None else "/dev/null",
            tofile=CONFIG_PATH,
            lineterm="",
        )
    )


def badge_for(view: RepoAuditView | None) -> AuditBadge:
    """The fleet-view badge for one repo's audit view; `None` means audit is off."""
    if view is None:
        return AuditBadge(AuditBadgeState.OFF)
    if view.error is not None:
        return AuditBadge(AuditBadgeState.ERROR)

    actionable = [f for f in view.findings if f.severity is not Severity.INFO]
    if actionable:
        return AuditBadge(AuditBadgeState.FINDINGS, len(actionable))
    if view.findings:
        # Nothing actionable, but a degraded-scope (INFO) finding exists -- the
        # audit couldn't fully vouch for this repo, distinct from a clean "ok".
        return AuditBadge(AuditBadgeState.UNKNOWN)
    return AuditBadge(AuditBadgeState.OK)


def _parse_existing_config(raw_config: str | None) -> Mapping[str, Any] | None:
    """Best-effort YAML parse for `suggest_config`'s `existing` argument.

    Invalid YAML has already been reported as `Check.INVALID_YAML`; `suggest_config`
    treats a non-mapping `existing` as "nothing to carry over" and templates a fresh
    entry for every detected expectation, which is the right fallback here too.
    """
    if raw_config is None:
        return None
    try:
        document = yaml.safe_load(raw_config)
    except yaml.YAMLError:
        return None
    return document if isinstance(document, Mapping) else None


class AuditService:
    """Composes detection, the config audit, and the suggested fix per repo, cached."""

    def __init__(
        self,
        client: GitHubClient,
        fleet: FleetConfig,
        *,
        audit_enabled_repos: frozenset[str] = frozenset(),
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client = client
        self._fleet = fleet
        self._audit_enabled_repos = audit_enabled_repos
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: dict[str, RepoAuditView] = {}
        self._cached_at: dict[str, float] = {}

    @property
    def audit_enabled_repos(self) -> frozenset[str]:
        return self._audit_enabled_repos

    async def get_audit_view(self, *, force_refresh: bool = False) -> tuple[RepoAuditView, ...]:
        """Every audit-enabled repo's view, built concurrently."""
        repos = sorted(self._audit_enabled_repos)
        views = await asyncio.gather(
            *(self._get_repo_view(repo, force_refresh=force_refresh) for repo in repos)
        )
        return tuple(views)

    async def get_repo_view(self, repo: str, *, force_refresh: bool = False) -> RepoAuditView:
        """One repo's audit view, from cache when fresh -- the re-audit button's target."""
        return await self._get_repo_view(repo, force_refresh=force_refresh)

    async def open_fix_pr(self, repo: str) -> str:
        """Open (or update) `repo`'s fix PR from its current audit view, then cache the URL.

        Raises whatever `dependapilot.fixpr.open_fix_pr` raises (a `GitHubAPIError`,
        typically) rather than swallowing it -- the caller needs to know *which*
        attempt failed in order to render it against the right repo section.
        """
        view = await self.get_repo_view(repo)
        if view.error is not None:
            raise RuntimeError(
                f"cannot open a fix PR: the last audit of {repo} failed: {view.error}"
            )

        config_findings = tuple(f for f in view.findings if f.check not in SETTINGS_CHECKS)
        url = await _open_fix_pr_flow(
            self._client, repo, config_yaml=view.suggested_config, findings=config_findings
        )
        self._cache[repo] = replace(view, existing_fix_pr_url=url, fix_pr_error=None)
        return url

    def record_fix_pr_error(self, repo: str, message: str) -> RepoAuditView:
        """Attach a fix-PR failure to `repo`'s cached view, for the caller to re-render."""
        cached = self._cache.get(repo)
        view = cached if cached is not None else RepoAuditView(repo=repo)
        updated = replace(view, fix_pr_error=message)
        self._cache[repo] = updated
        return updated

    async def _get_repo_view(self, repo: str, *, force_refresh: bool) -> RepoAuditView:
        if not force_refresh:
            cached = self._cache.get(repo)
            cached_at = self._cached_at.get(repo)
            if (
                cached is not None
                and cached_at is not None
                and self._clock() - cached_at < self._ttl_seconds
            ):
                return cached

        view = await self._build_repo_view(repo)
        self._cache[repo] = view
        self._cached_at[repo] = self._clock()
        return view

    async def _build_repo_view(self, repo: str) -> RepoAuditView:
        try:
            detection = await detect_repo(self._client, repo)
            raw_config = await fetch_config(self._client, repo)
            findings = evaluate_config(
                repo,
                raw_config,
                detection,
                cooldown_floor_days=self._fleet.defaults.cooldown_floor_days,
            )
            findings.extend(await check_repo_settings(self._client, repo))

            existing_document = _parse_existing_config(raw_config)
            suggested_document = suggest_config(
                detection,
                existing_document,
                cooldown_floor_days=self._fleet.defaults.cooldown_floor_days,
            )
            suggested_yaml = render_config(suggested_document)
            fix_pr_url = await find_open_fix_pr(self._client, repo)
        except Exception as exc:  # noqa: BLE001 -- isolate one repo's failure from the rest
            return RepoAuditView(repo=repo, error=str(exc))

        return RepoAuditView(
            repo=repo,
            findings=sort_findings(findings),
            current_config=raw_config,
            suggested_config=suggested_yaml,
            diff=render_diff(raw_config, suggested_yaml),
            existing_fix_pr_url=fix_pr_url,
        )
