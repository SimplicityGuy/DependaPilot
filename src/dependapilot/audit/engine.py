"""The config audit: every mechanical check on one repo's Dependabot setup.

Three layers, deliberately separable so the expensive one is the only async one:

* `evaluate_config` is pure — a config document plus a `DetectionResult` in, findings
  out. Every semantic rule lives here and is testable without a transport.
* `fetch_config` / `check_repo_settings` do the I/O.
* `audit_repo` wires them together for one repo.

Checks fail *open* where the evidence is incomplete: a 403 on a settings endpoint is
reported as unknown rather than as a violation, and orphan detection stands down on a
truncated file tree, because a manifest we couldn't see is not a manifest that's absent.
"""

from __future__ import annotations

import base64
import binascii
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Final

import yaml

from dependapilot.audit.detect import DetectionResult, Ecosystem, Expectation, detect_repo
from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.audit.schema import validate_config
from dependapilot.config import FleetConfig
from dependapilot.github.client import GitHubClient
from dependapilot.github.errors import GitHubAPIError

CONFIG_PATH: Final = ".github/dependabot.yml"

_DETECTABLE: Final = frozenset(ecosystem.value for ecosystem in Ecosystem)
_GLOB_CHARS: Final = "*?["
_ROOT: Final = "/"


@dataclass(frozen=True, slots=True)
class _Entry:
    """One `updates[]` element, normalised enough to compare against expectations."""

    index: int
    ecosystem: str
    directories: tuple[str, ...]
    """As written (so globs survive), each normalised to a leading-slash form."""

    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RepoAudit:
    """Everything the audit concluded about one repo."""

    repo: str
    detection: DetectionResult
    findings: tuple[Finding, ...] = ()

    @property
    def compliant(self) -> bool:
        return not self.findings


def _normalise_directory(directory: str) -> str:
    stripped = directory.strip()
    if not stripped.startswith(_ROOT):
        stripped = f"/{stripped}"
    return stripped.rstrip("/") or _ROOT


def _covers(pattern: str, directory: str) -> bool:
    """Whether a configured `directory`/`directories` value covers a detected one."""
    if any(char in pattern for char in _GLOB_CHARS):
        return fnmatchcase(directory, pattern)
    return pattern == directory


def _entry_directories(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """The directories one `updates[]` element claims; `/` when it names none."""
    directories = entry.get("directories")
    if isinstance(directories, list):
        named = tuple(_normalise_directory(d) for d in directories if isinstance(d, str))
        if named:
            return named
    directory = entry.get("directory")
    if isinstance(directory, str):
        return (_normalise_directory(directory),)
    return (_ROOT,)


def _entries(document: Any) -> tuple[_Entry, ...]:
    """Extract the `updates[]` elements well-formed enough for semantic checks.

    Malformed elements are skipped rather than guessed at: the schema pass has already
    reported them, and inventing an ecosystem for them would double-report the same
    mistake as a semantic finding too.
    """
    if not isinstance(document, dict):
        return ()
    updates = document.get("updates")
    if not isinstance(updates, list):
        return ()
    found: list[_Entry] = []
    for index, entry in enumerate(updates):
        if not isinstance(entry, dict):
            continue
        ecosystem = entry.get("package-ecosystem")
        if not isinstance(ecosystem, str):
            continue
        found.append(
            _Entry(
                index=index,
                ecosystem=ecosystem,
                directories=_entry_directories(entry),
                raw=entry,
            )
        )
    return tuple(found)


def _describe(expectations: Sequence[Expectation]) -> list[dict[str, str]]:
    return [{"ecosystem": e.ecosystem.value, "directory": e.directory} for e in expectations]


def _wrong_ecosystem_findings(
    repo: str, entries: Sequence[_Entry], detection: DetectionResult
) -> tuple[list[Finding], set[tuple[int, str]], set[str]]:
    """Flag `pip` entries pointing at directories uv manages.

    Returns the findings plus what they account for: the `(index, directory)` pairs
    already explained (so they aren't re-reported as orphans) and the uv directories
    the entry was evidently *trying* to cover (so they aren't re-reported as missing).
    A misnamed entry is one mistake and should read as one finding.
    """
    uv_directories = detection.directories_for(Ecosystem.UV)
    findings: list[Finding] = []
    explained: set[tuple[int, str]] = set()
    attempted: set[str] = set()

    for entry in entries:
        if entry.ecosystem != Ecosystem.PIP.value:
            continue
        for pattern in entry.directories:
            covered = sorted(d for d in uv_directories if _covers(pattern, d))
            if not covered:
                continue
            explained.add((entry.index, pattern))
            attempted.update(covered)
            findings.append(
                Finding(
                    repo=repo,
                    check=Check.WRONG_ECOSYSTEM,
                    severity=Severity.HIGH,
                    message=(
                        f"updates[{entry.index}] configures 'pip' for {pattern}, "
                        f"which uv manages; Dependabot will resolve the wrong "
                        f"dependency graph. Use 'uv' instead."
                    ),
                    context={
                        "index": entry.index,
                        "configured_ecosystem": Ecosystem.PIP.value,
                        "expected_ecosystem": Ecosystem.UV.value,
                        "directory": pattern,
                        "covers": covered,
                    },
                )
            )
    return findings, explained, attempted


def _completeness_findings(
    repo: str,
    entries: Sequence[_Entry],
    detection: DetectionResult,
    explained: set[tuple[int, str]],
    attempted: set[str],
) -> list[Finding]:
    """Compare the configured entries against the detected expectations both ways."""
    findings: list[Finding] = []

    for expectation in detection.expectations:
        if expectation.ecosystem is Ecosystem.UV and expectation.directory in attempted:
            continue
        if any(
            entry.ecosystem == expectation.ecosystem.value
            and any(_covers(pattern, expectation.directory) for pattern in entry.directories)
            for entry in entries
        ):
            continue
        findings.append(
            Finding(
                repo=repo,
                check=Check.MISSING_ECOSYSTEM,
                severity=Severity.MEDIUM,
                message=(
                    f"{expectation.ecosystem.value} manifests exist in "
                    f"{expectation.directory} but no updates[] entry covers them."
                ),
                context={
                    "ecosystem": expectation.ecosystem.value,
                    "directory": expectation.directory,
                },
            )
        )

    # A truncated tree can hide the very manifest an entry exists for, so "nothing
    # detected here" stops being evidence of an orphan.
    if detection.truncated:
        return findings

    for entry in entries:
        # Ecosystems we can't detect (nuget, helm, ...) are never called orphans —
        # we have no ground truth to contradict them with.
        if entry.ecosystem not in _DETECTABLE:
            continue
        for pattern in entry.directories:
            if (entry.index, pattern) in explained:
                continue
            if any(
                entry.ecosystem == expectation.ecosystem.value
                and _covers(pattern, expectation.directory)
                for expectation in detection.expectations
            ):
                continue
            findings.append(
                Finding(
                    repo=repo,
                    check=Check.ORPHAN_ENTRY,
                    severity=Severity.LOW,
                    message=(
                        f"updates[{entry.index}] configures {entry.ecosystem} for "
                        f"{pattern}, where no {entry.ecosystem} manifest was found."
                    ),
                    context={
                        "index": entry.index,
                        "ecosystem": entry.ecosystem,
                        "directory": pattern,
                    },
                )
            )
    return findings


def _duplicate_findings(repo: str, entries: Sequence[_Entry]) -> list[Finding]:
    """Report ecosystem+directory pairs configured more than once.

    Dependabot rejects the whole file over a duplicate pair, so this silently disables
    updates for the entire repo, not just the offending entry.
    """
    seen: dict[tuple[str, str], list[int]] = defaultdict(list)
    for entry in entries:
        for pattern in entry.directories:
            seen[(entry.ecosystem, pattern)].append(entry.index)

    return [
        Finding(
            repo=repo,
            check=Check.DUPLICATE_ENTRY,
            severity=Severity.MEDIUM,
            message=(
                f"{ecosystem} is configured for {directory} by updates entries "
                f"{', '.join(str(i) for i in indices)}; Dependabot rejects duplicates."
            ),
            context={"ecosystem": ecosystem, "directory": directory, "indices": indices},
        )
        for (ecosystem, directory), indices in sorted(seen.items())
        if len(indices) > 1
    ]


def _weak_cooldown_fields(cooldown: Mapping[str, Any], floor_days: int) -> dict[str, int]:
    weak: dict[str, int] = {}
    for key, value in cooldown.items():
        if key != "default-days" and not (key.startswith("semver-") and key.endswith("-days")):
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value < floor_days:
            weak[key] = value
    return dict(sorted(weak.items()))


def _cooldown_findings(repo: str, entries: Sequence[_Entry], floor_days: int) -> list[Finding]:
    """Report cooldowns configured *below* the fleet floor.

    Only an explicit, too-short cooldown is a finding. Omitting the block entirely is
    compliant: Dependabot then applies its own native cooldown, and rewriting silence
    into a violation would flag every correctly-configured repo in the fleet.
    """
    findings: list[Finding] = []
    for entry in entries:
        cooldown = entry.raw.get("cooldown")
        if not isinstance(cooldown, dict):
            continue
        weak = _weak_cooldown_fields(cooldown, floor_days)
        if not weak:
            continue
        rendered = ", ".join(f"{key}: {value}" for key, value in weak.items())
        findings.append(
            Finding(
                repo=repo,
                check=Check.WEAKENED_COOLDOWN,
                severity=Severity.MEDIUM,
                message=(
                    f"updates[{entry.index}] ({entry.ecosystem}) sets {rendered}, "
                    f"below the fleet cooldown floor of {floor_days} days."
                ),
                context={
                    "index": entry.index,
                    "ecosystem": entry.ecosystem,
                    "directories": list(entry.directories),
                    "floor_days": floor_days,
                    "weak_fields": weak,
                },
            )
        )
    return findings


def evaluate_config(
    repo: str,
    raw_config: str | None,
    detection: DetectionResult,
    *,
    cooldown_floor_days: int,
) -> list[Finding]:
    """Every check that can be made from the config text and the detected manifests.

    Args:
        repo: An `owner/name` slug, stamped onto each finding.
        raw_config: The `.github/dependabot.yml` source, or None if the repo has none.
        detection: What the repo's file tree says it needs.
        cooldown_floor_days: The fleet's minimum acceptable cooldown.
    """
    if raw_config is None:
        return [
            Finding(
                repo=repo,
                check=Check.MISSING_CONFIG,
                severity=Severity.HIGH,
                message=f"{repo} has no {CONFIG_PATH}; Dependabot is not configured at all.",
                context={
                    "path": CONFIG_PATH,
                    "expected": _describe(detection.expectations),
                },
            )
        ]

    try:
        document: Any = yaml.safe_load(raw_config)
    except yaml.YAMLError as exc:
        return [
            Finding(
                repo=repo,
                check=Check.INVALID_YAML,
                severity=Severity.HIGH,
                message=f"{CONFIG_PATH} is not valid YAML: {exc}",
                context={"path": CONFIG_PATH, "error": str(exc)},
            )
        ]

    findings = [
        Finding(
            repo=repo,
            check=Check.SCHEMA_ERROR,
            severity=Severity.MEDIUM,
            message=f"{CONFIG_PATH} violates the Dependabot schema at {v.path}: {v.message}",
            context={"path": v.path, "keyword": v.keyword, "error": v.message},
        )
        for v in validate_config(document)
    ]

    entries = _entries(document)
    wrong, explained, attempted = _wrong_ecosystem_findings(repo, entries, detection)
    findings.extend(wrong)
    findings.extend(_completeness_findings(repo, entries, detection, explained, attempted))
    findings.extend(_duplicate_findings(repo, entries))
    findings.extend(_cooldown_findings(repo, entries, cooldown_floor_days))
    return findings


async def fetch_config(client: GitHubClient, repo: str) -> str | None:
    """Read `.github/dependabot.yml` from `repo`, or None if it doesn't exist."""
    owner, _, name = repo.partition("/")
    try:
        response = await client.get(f"/repos/{owner}/{name}/contents/{CONFIG_PATH}")
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            return None
        raise

    payload: Any = response.json()
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    if payload.get("encoding") != "base64":
        return content
    try:
        return base64.b64decode(content).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise GitHubAPIError(
            f"{repo}: {CONFIG_PATH} contents were not decodable base64 UTF-8: {exc}",
            status_code=response.status_code,
        ) from exc


async def _repo_flag(client: GitHubClient, path: str) -> bool | None:
    """Read a boolean repo setting: True on, False off, None if we may not look.

    GitHub answers these endpoints with a bare status code (204 on, 404 off) or, for
    automated security fixes, a body — both shapes are accepted.
    """
    try:
        response = await client.get(path)
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            return False
        if exc.status_code == 403:
            return None
        raise

    if response.status_code == 204 or not response.content:
        return True
    payload: Any = response.json()
    if not isinstance(payload, dict):
        return True
    if payload.get("paused") is True:
        return False
    enabled = payload.get("enabled")
    return True if enabled is None else bool(enabled)


async def check_repo_settings(client: GitHubClient, repo: str) -> list[Finding]:
    """Audit the two repo-level switches Dependabot needs, degrading on 403.

    Both endpoints need admin on the repo. Without it GitHub answers 403, and the
    finding says the check couldn't run rather than asserting the feature is off.
    """
    owner, _, name = repo.partition("/")
    findings: list[Finding] = []

    alerts = await _repo_flag(client, f"/repos/{owner}/{name}/vulnerability-alerts")
    if alerts is None:
        findings.append(
            Finding(
                repo=repo,
                check=Check.ALERTS_UNKNOWN,
                severity=Severity.INFO,
                message=(
                    "Could not read Dependabot alert settings: the token lacks admin "
                    f"access to {repo}."
                ),
                context={"setting": "vulnerability-alerts", "reason": "forbidden"},
            )
        )
    elif not alerts:
        findings.append(
            Finding(
                repo=repo,
                check=Check.ALERTS_DISABLED,
                severity=Severity.HIGH,
                message=(
                    f"Dependabot alerts are disabled on {repo}; no vulnerability is "
                    f"being reported at all."
                ),
                context={"setting": "vulnerability-alerts", "enabled": False},
            )
        )

    fixes = await _repo_flag(client, f"/repos/{owner}/{name}/automated-security-fixes")
    if fixes is None:
        findings.append(
            Finding(
                repo=repo,
                check=Check.SECURITY_UPDATES_UNKNOWN,
                severity=Severity.INFO,
                message=(
                    "Could not read Dependabot security update settings: the token "
                    f"lacks admin access to {repo}."
                ),
                context={"setting": "automated-security-fixes", "reason": "forbidden"},
            )
        )
    elif not fixes:
        findings.append(
            Finding(
                repo=repo,
                check=Check.SECURITY_UPDATES_DISABLED,
                severity=Severity.MEDIUM,
                message=(
                    f"Dependabot security updates are not running on {repo}; "
                    f"vulnerable dependencies will not be patched automatically."
                ),
                context={"setting": "automated-security-fixes", "enabled": False},
            )
        )

    return findings


async def audit_repo(
    client: GitHubClient,
    repo: str,
    *,
    fleet: FleetConfig,
    ref: str = "HEAD",
    detection: DetectionResult | None = None,
) -> RepoAudit:
    """Run the full config audit for one repo.

    Args:
        client: Authenticated GitHub client.
        repo: An `owner/name` slug.
        fleet: Supplies the policy floor the cooldown check measures against.
        ref: Tree-ish to detect manifests from; the default is the default branch.
        detection: Pre-computed detection, to skip a redundant tree fetch.
    """
    resolved = detection if detection is not None else await detect_repo(client, repo, ref=ref)
    raw_config = await fetch_config(client, repo)
    findings = evaluate_config(
        repo,
        raw_config,
        resolved,
        cooldown_floor_days=fleet.defaults.cooldown_floor_days,
    )
    findings.extend(await check_repo_settings(client, repo))
    return RepoAudit(repo=repo, detection=resolved, findings=tuple(findings))
