set shell := ["bash", "-euo", "pipefail", "-c"]

# List available recipes.
default:
    @just --list

# ── setup ────────────────────────────────────────────────────────────

# Install/sync the dev environment.
[group('setup')]
install:
    uv sync --all-extras --dev

# Remove caches and build artifacts.
[group('setup')]
clean:
    rm -rf .pytest_cache .mypy_cache .ruff_cache dist build
    find . -type d -name __pycache__ -prune -exec rm -rf {} +

# ── quality ──────────────────────────────────────────────────────────

# Run the full validation suite: format check, lint, type check, tests.
[group('quality')]
check: fmt-check lint typecheck test

# Check formatting without modifying files.
[group('quality')]
fmt-check:
    uv run ruff format --check .

# Format the codebase.
[group('quality')]
fmt:
    uv run ruff format .

# Lint the codebase.
[group('quality')]
lint:
    uv run ruff check .

# Lint and auto-fix what ruff can.
[group('quality')]
lint-fix:
    uv run ruff check --fix .

# Run static type checking.
[group('quality')]
typecheck:
    uv run mypy src

# ── test ─────────────────────────────────────────────────────────────

# Run the test suite.
[group('test')]
test:
    uv run pytest

# Run the test suite with verbose per-test output.
[group('test')]
test-verbose:
    uv run pytest -v

# Run only tests matching a keyword expression, e.g. `just test-only scoring`.
[group('test')]
test-only pattern:
    uv run pytest -v -k "{{ pattern }}"

# ── ci ───────────────────────────────────────────────────────────────

# Exactly what CI runs: fresh sync, then the full validation suite.
[group('ci')]
ci: install check

# ── server ───────────────────────────────────────────────────────────

# Run the development server with auto-reload.
[group('server')]
serve:
    uv run dependapilot serve --reload

# Run the server without reload, on an explicit host/port.
[group('server')]
serve-prod host="127.0.0.1" port="8000":
    uv run dependapilot serve --host {{ host }} --port {{ port }}
