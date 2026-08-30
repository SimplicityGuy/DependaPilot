# DependaPilot

Automated dependency update orchestration.

## Getting started

Install [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/), then:

```sh
just install
```

### Run the server

```sh
uv run dependapilot serve
```

The app starts on `http://127.0.0.1:8000` by default; `GET /healthz` returns `{"status": "ok"}`.

### Fleet configuration

[`repos.yml`](repos.yml) is the committed source of truth for which repos DependaPilot
manages and their policy overrides; see `src/dependapilot/config.py` for the schema.

### Run checks

```sh
just check
```

This runs `ruff format --check`, `ruff check`, `mypy`, and `pytest`.
