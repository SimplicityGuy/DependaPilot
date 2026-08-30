"""The typed vocabulary every audit check speaks.

A `Finding` is one machine-actionable statement about one repo. `message` is for a
human reading a report; `context` is for the fix generator, and carries the identifiers
a fix needs (which ecosystem, which directory, which `updates[]` index) rather than
asking anyone to parse them back out of the prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """How much a finding should worry you.

    `INFO` also covers "we could not tell" — a check the token wasn't allowed to run
    reports what it couldn't see instead of guessing a verdict.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Check(StrEnum):
    """Stable identifiers for the audit's checks, safe to key dashboards off."""

    MISSING_CONFIG = "MISSING_CONFIG"
    INVALID_YAML = "INVALID_YAML"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    MISSING_ECOSYSTEM = "MISSING_ECOSYSTEM"
    ORPHAN_ENTRY = "ORPHAN_ENTRY"
    WRONG_ECOSYSTEM = "WRONG_ECOSYSTEM"
    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    WEAKENED_COOLDOWN = "WEAKENED_COOLDOWN"
    ALERTS_DISABLED = "ALERTS_DISABLED"
    ALERTS_UNKNOWN = "ALERTS_UNKNOWN"
    SECURITY_UPDATES_DISABLED = "SECURITY_UPDATES_DISABLED"
    SECURITY_UPDATES_UNKNOWN = "SECURITY_UPDATES_UNKNOWN"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the audit has to say about one repo."""

    repo: str
    check: Check
    severity: Severity
    message: str
    context: dict[str, Any] = field(default_factory=dict)
