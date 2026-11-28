"""notify: publish to SNS topic rm-incidents once per dedupe key (tasks.md 2.4; design.md
section 6).

A notification is published unless its dedupe key is already in the run item's notified;
then the key is recorded with the SNS message ID: at least once, never lost. notify writes
notified itself, one key at a time (a commit may not set it, task 2.1): a conditional
update of that key alone, so it never overwrites another key, and a repeat with a recorded
key sends nothing. A failed publish records nothing. Imports only contracts, the standard
library, boto3 and botocore.
"""

from __future__ import annotations

import functools
import unicodedata

import boto3
from botocore.exceptions import ClientError

from contracts import names

# SNS takes a Subject of "UTF-8 text with no line breaks or control characters, and less than
# 100 characters long" (docs.aws.amazon.com/sns/latest/api/API_Publish.html).
SNS_SUBJECT_MAX = 99
_TOPIC_ARN = f"arn:aws:sns:{names.REGION}:{names.ACCOUNT_ID}:{names.INCIDENTS_TOPIC}"
_NOTIFIED = "notified"
if _NOTIFIED not in names.RUN_ITEM_ATTRIBUTES:  # the contract's name, checked on import
    raise KeyError(f"{_NOTIFIED!r} is not in contracts.names.RUN_ITEM_ATTRIBUTES")
_CONDITION_FAILED = "ConditionalCheckFailedException"


@functools.cache
def _sns():
    return boto3.client("sns", region_name=names.REGION)


@functools.cache
def _dynamodb():
    return boto3.client("dynamodb", region_name=names.REGION)


def _check_key(dedupe_key: str) -> None:
    """A dedupe key exactly as names.dedupe_key() writes one, e.g. 2#ROLLED_BACK."""
    count, separator, state = (
        dedupe_key.partition("#") if isinstance(dedupe_key, str) else ("", "", "")
    )
    try:
        rebuilt = names.dedupe_key(int(count), state) if separator and count.isdigit() else None
    except ValueError:
        rebuilt = None
    if rebuilt != dedupe_key:
        raise ValueError(f"not a dedupe key (<history length>#<to state>): {dedupe_key!r}")


def _key(run_id: str) -> dict:
    return {names.PARTITION_KEY: {"S": names.run_pk(run_id)}}


def _recorded(run_id: str, dedupe_key: str) -> str | None:
    """The message ID recorded for dedupe_key, or None. LookupError when the run item
    or its notified map is missing. notify reads this before it publishes, since such
    a publish could never be recorded and every later call would send it again."""
    response = _dynamodb().get_item(
        TableName=names.RUNS_TABLE,
        Key=_key(run_id),
        ConsistentRead=True,
        ProjectionExpression="#notified",
        ExpressionAttributeNames={"#notified": _NOTIFIED},
    )
    notified = (response.get("Item") or {}).get(_NOTIFIED) or {}
    if "M" not in notified:
        raise LookupError(f"run {run_id} has no {_NOTIFIED} map to record in")
    return notified["M"].get(dedupe_key, {}).get("S")


def _subject(subject: str) -> str:
    """A Subject SNS accepts: every control character a space, every run of spaces and line
    breaks one space, then at most SNS_SUBJECT_MAX characters."""
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in str(subject))
    return " ".join(text.split())[:SNS_SUBJECT_MAX]


def notify(run_id: str, subject: str, body: str, dedupe_key: str) -> str:
    """Publish once per dedupe key and return the SNS message ID recorded for it.

    ValueError for a key that is not a dedupe key, before anything is sent. A failed publish
    raises and records nothing. If another invocation records the same key meanwhile, both
    were published and the first recorded message ID is returned.
    """
    _check_key(dedupe_key)
    found = _recorded(run_id, dedupe_key)
    if found:
        return found
    line = _subject(subject)  # a subject with nothing printable left is left out
    message_id = _sns().publish(
        TopicArn=_TOPIC_ARN, Message=body, **({"Subject": line} if line else {})
    )["MessageId"]
    try:
        _dynamodb().update_item(
            TableName=names.RUNS_TABLE,
            Key=_key(run_id),
            UpdateExpression="SET #notified.#key = :message_id",
            ConditionExpression=(
                "attribute_exists(#notified) AND attribute_not_exists(#notified.#key)"
            ),
            ExpressionAttributeNames={"#notified": _NOTIFIED, "#key": dedupe_key},
            ExpressionAttributeValues={":message_id": {"S": message_id}},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != _CONDITION_FAILED:
            raise
        found = _recorded(run_id, dedupe_key)
        if found is None:
            raise LookupError(f"run {run_id} has no {_NOTIFIED} map to record in") from error
        return found
    return message_id
