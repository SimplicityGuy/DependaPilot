"""Configuration auditing: what a repo actually needs, and what it actually declares."""

from dependapilot.audit.detect import (
    DetectionResult,
    Ecosystem,
    Expectation,
    detect_from_paths,
    detect_repo,
)
from dependapilot.audit.engine import (
    CONFIG_PATH,
    RepoAudit,
    audit_repo,
    check_repo_settings,
    evaluate_config,
    fetch_config,
)
from dependapilot.audit.findings import Check, Finding, Severity
from dependapilot.audit.schema import SCHEMA_URL, SchemaViolation, validate_config
from dependapilot.audit.suggest import render_config, suggest_config

__all__ = [
    "CONFIG_PATH",
    "SCHEMA_URL",
    "Check",
    "DetectionResult",
    "Ecosystem",
    "Expectation",
    "Finding",
    "RepoAudit",
    "SchemaViolation",
    "Severity",
    "audit_repo",
    "check_repo_settings",
    "detect_from_paths",
    "detect_repo",
    "evaluate_config",
    "fetch_config",
    "render_config",
    "suggest_config",
    "validate_config",
]
