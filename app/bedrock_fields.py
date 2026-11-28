"""Bedrock request fields from names.py's dotted names (tasks 3.1 and 3.3).

names.py spells the request fields task 1.2 proved as dotted paths inside Converse's
additionalModelRequestFields: THINKING_EFFORT_FIELD is "output_config.effort", and
THINKING_DISABLED_FIELD is "thinking.type". nested_field() turns one into the nested object
Bedrock takes. sampling_parameters() finds any sampling parameter in a request, which no
model in this project is ever sent (design.md section 3).

The answer functions (app/answer) and the judge (app/judge) both import it. It lives
outside either package so that neither function's package needs the other's (3.1's
review). Standard library only, so it adds nothing to either package.
"""

from __future__ import annotations

from contracts import names


def nested_field(dotted: str, value: object) -> dict:
    """The nested object a dotted field name stands for, holding value.

    nested_field("output_config.effort", "low") == {"output_config": {"effort": "low"}}
    """
    parts = dotted.split(".")
    if not all(parts):
        raise ValueError(f"not a dotted field name: {dotted!r}")
    field: object = value
    for part in reversed(parts):
        field = {part: field}
    return field


def sampling_parameters(request: object) -> list[str]:
    """Every key of names.FORBIDDEN_SAMPLING_PARAMETERS in request, at any depth, in order."""
    found: list[str] = []
    if isinstance(request, dict):
        for key, value in request.items():
            if key in names.FORBIDDEN_SAMPLING_PARAMETERS:
                found.append(key)
            found += sampling_parameters(value)
    elif isinstance(request, list | tuple):
        for item in request:
            found += sampling_parameters(item)
    return found
