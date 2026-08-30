"""Configuration auditing: what a repo actually needs, and what it actually declares."""

from dependapilot.audit.detect import (
    DetectionResult,
    Ecosystem,
    Expectation,
    detect_from_paths,
    detect_repo,
)

__all__ = [
    "DetectionResult",
    "Ecosystem",
    "Expectation",
    "detect_from_paths",
    "detect_repo",
]
