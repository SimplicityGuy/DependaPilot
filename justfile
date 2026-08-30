set shell := ["bash", "-euo", "pipefail", "-c"]

# List available recipes.
default:
    @just --list

# Install/sync the dev environment.
install:
    uv sync --all-extras --dev

# Run the full validation suite: format check, lint, type check, tests.
check: fmt-check lint typecheck test

# Check formatting without modifying files.
fmt-check:
    uv run ruff format --check .

# Format the codebase.
fmt:
    uv run ruff format .

# Lint the codebase.
lint:
    uv run ruff check .

# Run static type checking.
typecheck:
    uv run mypy src

# Run the test suite.
test:
    uv run pytest

# Run the development server.
serve:
    uv run dependapilot serve --reload
