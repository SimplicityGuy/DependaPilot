"""Async GitHub REST API client: gh-CLI auth, pagination, rate limits, retries."""

from dependapilot.github.client import (
    API_VERSION,
    GITHUB_API_URL,
    Capabilities,
    GitHubClient,
    Identity,
)
from dependapilot.github.errors import (
    GitHubAPIError,
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
)

__all__ = [
    "API_VERSION",
    "GITHUB_API_URL",
    "Capabilities",
    "GitHubAPIError",
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubRateLimitError",
    "Identity",
]
