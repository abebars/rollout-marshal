"""How a run item's numbers survive DynamoDB (standard library only).

boto3 refuses Python floats on write and returns every number as Decimal on
read. The plan holds fractions (1.1, 0.95, -0.05), so the rule is:

- write: to_dynamo(item) turns every float into Decimal(str(x));
- read: from_dynamo(item) turns every Decimal into int when it is whole and
  float otherwise.

from_dynamo(to_dynamo(x)) == x for any run item, and the result validates
against contracts/schemas/run_item.json. Converting every number to int
would turn 1.1 into 1 and fail the schema.
"""

from __future__ import annotations

from decimal import Decimal


def to_dynamo(value):
    """A copy of value that boto3 can write: floats become Decimal(str(x))."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value):
    """A copy of value as read from boto3: whole Decimals become int, others float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [from_dynamo(v) for v in value]
    if isinstance(value, set | frozenset):
        return {from_dynamo(v) for v in value}
    return value
