"""Shared contracts: names (contracts.names) and JSON schemas (contracts/schemas).

    from contracts import names, validate
    validate("invoke", {"action": "tick"})   # raises jsonschema.ValidationError

contracts.names and contracts.dynamo use only the standard library, so a
Lambda can import them without packaging jsonschema. Only validate() and
validator() need jsonschema, and they import it when first called.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

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
def _registry():
    from referencing import Registry, Resource

    resources = []
    for name in names.SCHEMA_NAMES:
        schema = load_schema(name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def validator(name: str):
    """A jsonschema Draft 2020-12 validator for one schema, resolving references
    to the others."""
    from jsonschema import Draft202012Validator

    return Draft202012Validator(load_schema(name), registry=_registry())


def validate(name: str, instance: object) -> None:
    """Raise jsonschema.ValidationError if instance does not match the named schema."""
    validator(name).validate(instance)
