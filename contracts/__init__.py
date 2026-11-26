"""Shared contracts: names (contracts.names) and JSON schemas (contracts/schemas).

from contracts import names, validate
validate("invoke", {"action": "tick"})   # raises jsonschema.ValidationError
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from contracts import names

SCHEMA_DIR = Path(__file__).parent / "schemas"

__all__ = ["SCHEMA_DIR", "load_schema", "names", "validate", "validator"]


@cache
def load_schema(name: str) -> dict:
    """One of names.SCHEMA_NAMES, parsed."""
    if name not in names.SCHEMA_NAMES:
        raise KeyError(f"unknown schema {name!r}; expected one of {names.SCHEMA_NAMES}")
    return json.loads((SCHEMA_DIR / f"{name}.json").read_text())


@cache
def _registry() -> Registry:
    resources = []
    for name in names.SCHEMA_NAMES:
        schema = load_schema(name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def validator(name: str) -> Draft202012Validator:
    """A validator for one schema, resolving references to the others."""
    return Draft202012Validator(
        load_schema(name), registry=_registry(), format_checker=FormatChecker()
    )


def validate(name: str, instance: object) -> None:
    """Raise jsonschema.ValidationError if instance does not match the named schema."""
    validator(name).validate(instance)
