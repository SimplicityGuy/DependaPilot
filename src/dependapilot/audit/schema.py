"""Structural validation against the vendored SchemaStore Dependabot schema.

The schema ships with the package rather than being fetched per audit, so the same
`dependabot.yml` yields the same findings whether or not schemastore.org is reachable.
See `schemas/README.md` for its provenance and the refresh procedure.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Final

from jsonschema import Draft7Validator

SCHEMA_URL: Final = "https://json.schemastore.org/dependabot-2.0.json"
_SCHEMA_FILE: Final = "dependabot-2.0.json"


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    """One structural error, located by a path into the parsed YAML document."""

    path: str
    """Dotted/indexed location, e.g. `updates[0].package-ecosystem`; `$` for the root."""

    message: str
    keyword: str
    """The JSON Schema keyword that rejected the value, e.g. `enum` or `required`."""


@lru_cache(maxsize=1)
def _validator() -> Draft7Validator:
    schema_text = (resources.files("dependapilot.audit") / "schemas" / _SCHEMA_FILE).read_text()
    return Draft7Validator(json.loads(schema_text))


def _format_path(parts: Iterable[Any]) -> str:
    rendered = ""
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = str(part)
    return rendered or "$"


def validate_config(document: Any) -> tuple[SchemaViolation, ...]:
    """Return every way `document` violates the Dependabot schema, innermost first.

    Ordered by location then message so a repo's findings are stable across runs —
    `iter_errors` itself makes no ordering promise.
    """
    errors = sorted(
        _validator().iter_errors(document),
        key=lambda error: ([str(part) for part in error.absolute_path], error.message),
    )
    return tuple(
        SchemaViolation(
            path=_format_path(error.absolute_path),
            message=error.message,
            keyword=str(error.validator),
        )
        for error in errors
    )
