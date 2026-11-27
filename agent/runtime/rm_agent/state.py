"""The run store and the state machine's writes (design.md section 6; tasks.md 2.1).

Table rm-runs holds one item per run (pk RUN#<run_id>) and the active-run pointer
(pk ACTIVE). A transition is applied as guard, claim, effects, commit. The handlers
(task 2.5) run the guard and the effects; this module makes every write:

- create_run: the (none) -> PLANNED row, the run item and the pointer in one transaction;
- claim: step 1, pending = <to> on condition the run is in <from> and nothing is pending;
- resume: the takeover of a pending that is PENDING_STALE_SECONDS old;
- release: after an effect throws, moves the caller's claim back PENDING_STALE_SECONDS, so
  the next tick, check or repeat takes it over at once;
- commit: step 3, state = <to>, pending removed and one history entry appended, on
  condition the claim is still the caller's. The launch commit also sets the pointer's
  feature_key, and a terminal commit deletes the pointer (clear_active).

The pointer changes only inside the TransactWriteItems that writes the run item it mirrors.
A write that loses to another invocation raises StaleState with a fresh read of the run, so
the caller can return the current status.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from contracts import dynamo, names


def _named(name: str, contract) -> str:
    """name, once the contract holds it. A name the contract lacks fails the import."""
    if name not in contract:
        raise KeyError(f"{name!r} is not in the contract: {sorted(contract)}")
    return name


# Run item attributes this module writes (names.RUN_ITEM_ATTRIBUTES).
_PK = names.PARTITION_KEY
_RUN_ID = _named("run_id", names.RUN_ITEM_ATTRIBUTES)
_STATE = _named("state", names.RUN_ITEM_ATTRIBUTES)
_PROMPT = _named("prompt", names.RUN_ITEM_ATTRIBUTES)
_PLAN = _named("plan", names.RUN_ITEM_ATTRIBUTES)
_FEATURE_KEY = _named("feature_key", names.RUN_ITEM_ATTRIBUTES)
_NOTIFIED = _named("notified", names.RUN_ITEM_ATTRIBUTES)
_HISTORY = _named("history", names.RUN_ITEM_ATTRIBUTES)
_PENDING = _named("pending", names.RUN_ITEM_ATTRIBUTES)
_PENDING_AT = _named("pending_at", names.RUN_ITEM_ATTRIBUTES)
_CREATED_AT = _named("created_at", names.RUN_ITEM_ATTRIBUTES)
_UPDATED_AT = _named("updated_at", names.RUN_ITEM_ATTRIBUTES)
_LAST_REFRESH_AT = _named("last_refresh_at", names.RUN_ITEM_ATTRIBUTES)
# The pointer's attributes (names.POINTER_ATTRIBUTES) and a history entry's fields.
_POINTER_RUN_ID = _named("run_id", names.POINTER_ATTRIBUTES)
_POINTER_FEATURE_KEY = _named("feature_key", names.POINTER_ATTRIBUTES)
_AT = _named("at", names.HISTORY_FIELDS)
_FROM = _named("from", names.HISTORY_FIELDS)
_TO = _named("to", names.HISTORY_FIELDS)
_ACTOR = _named("actor", names.HISTORY_FIELDS)
_REASON = _named("reason", names.HISTORY_FIELDS)

# A commit's updates may set any run item attribute except those create_run writes once,
# those this module keeps itself, and notified: only the notify tool (task 2.4) records a
# dedupe key, one at a time, so a whole map set from a stale copy can never drop one.
_WRITTEN_ONCE = {_PK, _RUN_ID, _PROMPT, _PLAN, _CREATED_AT}
_KEPT_HERE = {_STATE, _PENDING, _PENDING_AT, _HISTORY, _UPDATED_AT}
_WRITTEN_BY_NOTIFY = {_NOTIFIED}
_UPDATABLE = frozenset(names.RUN_ITEM_ATTRIBUTES) - _WRITTEN_ONCE - _KEPT_HERE - _WRITTEN_BY_NOTIFY

_START = next(t for t in names.TRANSITIONS if t.source is None)  # (none) -> PLANNED
_ROWS = {(t.source, t.target): t for t in names.TRANSITIONS if t.source is not None}
_LAUNCH = (names.STATE_PLANNED, names.STATE_RUNNING)
_STALE_AFTER = timedelta(seconds=names.PENDING_STALE_SECONDS)

# DynamoDB's error codes and transaction cancellation reason codes (API reference:
# TransactionCanceledException, CancellationReason; Developer Guide: "Transaction
# conflict handling in DynamoDB").
_CONDITION_FAILED = "ConditionalCheckFailedException"
_CONFLICT = "TransactionConflictException"  # a single-item write met an in-flight transaction
_CANCELLED = "TransactionCanceledException"
_REASON_NONE = "None"  # the item had no error; DynamoDB sends the literal string
_REASON_CONDITION_FAILED = "ConditionalCheckFailed"
_REASON_CONFLICT = "TransactionConflict"


class Pointer(NamedTuple):
    """The ACTIVE item: the active run and, once its launch has committed, its feature key."""

    run_id: str
    feature_key: str | None = None


@dataclass(frozen=True)
class Run:
    """A run item as Python holds it: contracts/schemas/run_item.json, with numbers as
    contracts.dynamo.from_dynamo returns them."""

    item: dict[str, Any]

    @property
    def run_id(self) -> str:
        return self.item[_RUN_ID]

    @property
    def state(self) -> str:
        return self.item[_STATE]

    @property
    def pending(self) -> str | None:
        """The target of a claimed, unfinished transition, or None."""
        return self.item.get(_PENDING)

    @property
    def pending_at(self) -> str | None:
        """When the pending transition was claimed or last taken over. A commit removes only
        pending, so this means something only while pending is set."""
        return self.item.get(_PENDING_AT)

    @property
    def history(self) -> list[dict[str, Any]]:
        return self.item[_HISTORY]


class StateError(Exception):
    """A refusal that carries one of design.md section 7's error codes."""

    code: str

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunActive(StateError):
    """The pointer names a run, so no other can start."""

    code = names.ERROR_RUN_ACTIVE

    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"run {active_run_id} is active")
        self.active_run_id = active_run_id


class BadRequest(StateError):
    code = names.ERROR_BAD_REQUEST


class NotFound(StateError):
    code = names.ERROR_NOT_FOUND


class IllegalTransition(StateError):
    """A (from, to) pair that is not a row of the transition table."""

    code = names.ERROR_ILLEGAL_IN_STATE


class CommitFailed(StateError):
    """A commit refused other than by a lost race, such as by a pointer that names another
    run. Nothing was written; the claim is still the caller's."""

    code = names.ERROR_EFFECT_FAILED


class StaleState(Exception):
    """A lost race: the run is not in the state the caller read, or another invocation owns
    the transition. run is a consistent read taken after the loss."""

    def __init__(self, what: str, run: Run) -> None:
        held = f", {run.pending} pending since {run.pending_at}" if run.pending else ""
        super().__init__(f"{what}: run {run.run_id} is {run.state}{held}")
        self.run = run


def _now() -> datetime:
    """The current time in UTC. Tests replace it."""
    return datetime.now(UTC)


@functools.cache
def _client():
    """The DynamoDB client, made on first use, in us-east-1 (design.md section 2). A boto3
    client is safe to share between threads; a resource is not."""
    return boto3.client("dynamodb", region_name=names.REGION)


_serialize = TypeSerializer().serialize
_deserialize = TypeDeserializer().deserialize


def _encode(item: dict[str, Any]) -> dict[str, Any]:
    """A Python item as DynamoDB attribute values (floats go through Decimal)."""
    return {key: _serialize(value) for key, value in dynamo.to_dynamo(item).items()}


def _decode(attributes: dict[str, Any]) -> dict[str, Any]:
    """DynamoDB attribute values as a Python item (whole numbers int, the rest float)."""
    return dynamo.from_dynamo({key: _deserialize(value) for key, value in attributes.items()})


def _key(pk: str) -> dict[str, Any]:
    return {_PK: {"S": pk}}


def _attribute_names(*attributes: str) -> dict[str, str]:
    """#<attribute> placeholders. DynamoDB reserves words such as state, plan and from."""
    return {f"#{attribute}": attribute for attribute in attributes}


def _values(**values: Any) -> dict[str, Any]:
    """:<name> placeholders."""
    return {f":{name}": _serialize(dynamo.to_dynamo(value)) for name, value in values.items()}


def _get(pk: str) -> dict[str, Any] | None:
    response = _client().get_item(TableName=names.RUNS_TABLE, Key=_key(pk), ConsistentRead=True)
    attributes = response.get("Item")
    return _decode(attributes) if attributes else None


def _code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


class _Reason(NamedTuple):
    code: str
    item: dict[str, Any] | None  # the item as it was, when ALL_OLD returned it


def _reasons(error: ClientError) -> list[_Reason] | None:
    """A cancelled transaction's reasons, one per item in request order; None for any other
    error."""
    if _code(error) != _CANCELLED:
        return None
    return [
        _Reason(reason.get("Code", ""), _decode(reason["Item"]) if reason.get("Item") else None)
        for reason in error.response.get("CancellationReasons", [])
    ]


def _row(source: str | None, target: str) -> names.Transition:
    row = _ROWS.get((source, target))
    if row is not None:
        return row
    if (source, target) == (_START.source, _START.target):
        raise IllegalTransition(f"(none) -> {target} is the start; only create_run writes it")
    raise IllegalTransition(f"{source or '(none)'} -> {target} is not in the transition table")


def _lost(run_id: str, what: str) -> StaleState | NotFound:
    """The exception for a lost race, with the run read afresh."""
    item = _get(names.run_pk(run_id))
    if item is None:
        return NotFound(f"no run {run_id}")
    return StaleState(what, Run(item))


def _entry(at: str, row: names.Transition, reason: str) -> dict[str, Any]:
    """One history entry. Its actor is the table's for the row, never the payload's."""
    return {_AT: at, _FROM: row.source, _TO: row.target, _ACTOR: row.actor, _REASON: reason}


def get_active() -> Pointer | None:
    """The active-run pointer, or None when no run is active."""
    item = _get(names.ACTIVE_POINTER_PK)
    if item is None:
        return None
    return Pointer(**{attribute: item.get(attribute) for attribute in names.POINTER_ATTRIBUTES})


def load_run(run_id: str) -> Run:
    """The run, read consistently. NotFound when there is no such run."""
    item = _get(names.run_pk(run_id))
    if item is None:
        raise NotFound(f"no run {run_id}")
    return Run(item)


def create_run(prompt: str, plan: dict[str, Any]) -> Run:
    """The (none) -> PLANNED row: the run item, with notified empty, and the pointer, in one
    transaction. The run ID is r + the UTC minute (names.run_id_for).

    RunActive, naming the active run, while the pointer exists. BadRequest when only the run
    item's condition fails: this minute's run ID is in use. Either way nothing is written.
    """
    moment = _now()
    run_id = names.run_id_for(moment)
    at = names.utc_iso(moment)
    item = {
        _PK: names.run_pk(run_id),
        _RUN_ID: run_id,
        _STATE: _START.target,
        _PROMPT: prompt,
        _PLAN: plan,
        _HISTORY: [_entry(at, _START, names.ACTION_START)],
        _NOTIFIED: {},
        _CREATED_AT: at,
        _UPDATED_AT: at,
    }
    pointer = {_PK: names.ACTIVE_POINTER_PK, _POINTER_RUN_ID: run_id}
    request = [
        {
            "Put": {
                "TableName": names.RUNS_TABLE,
                "Item": _encode(pointer),
                "ConditionExpression": names.POINTER_CREATE_CONDITION,
                "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
            }
        },
        {
            "Put": {
                "TableName": names.RUNS_TABLE,
                "Item": _encode(item),
                "ConditionExpression": names.POINTER_CREATE_CONDITION,
            }
        },
    ]
    try:
        _client().transact_write_items(TransactItems=request)
    except ClientError as error:
        reasons = _reasons(error)
        if not reasons or len(reasons) != len(request):
            raise
        on_pointer, on_run = reasons
        if on_pointer.code == _REASON_CONDITION_FAILED and on_pointer.item:
            raise RunActive(on_pointer.item[_POINTER_RUN_ID]) from error
        if on_run.code == _REASON_CONDITION_FAILED and on_pointer.code == _REASON_NONE:
            raise BadRequest(f"run ID {run_id} is in use: start again next minute") from error
        if _REASON_CONFLICT in (on_pointer.code, on_run.code):
            # A colliding write: a lost race. If another start won, its pointer says so.
            active = get_active()
            if active is not None:
                raise RunActive(active.run_id) from error
        raise
    return Run(dynamo.from_dynamo(dynamo.to_dynamo(item)))


def _update(run_id: str, what: str, **request: Any) -> Run:
    """One conditional UpdateItem on the run item. A failed condition, or a collision with an
    in-flight transaction, is a lost race."""
    try:
        response = _client().update_item(
            TableName=names.RUNS_TABLE,
            Key=_key(names.run_pk(run_id)),
            ReturnValues="ALL_NEW",
            **request,
        )
    except ClientError as error:
        if _code(error) not in (_CONDITION_FAILED, _CONFLICT):
            raise
        raise _lost(run_id, what) from error
    return Run(_decode(response["Attributes"]))


def claim(run_id: str, expected_from: str, to: str) -> Run:
    """Step 1: pending = to and pending_at = now, on condition the run is in expected_from
    and nothing is pending. StaleState when another invocation got there first."""
    _row(expected_from, to)
    now = names.utc_iso(_now())
    return _update(
        run_id,
        f"claim {expected_from} -> {to} lost",
        UpdateExpression="SET #pending = :target, #pending_at = :now, #updated_at = :now",
        ConditionExpression="#state = :source AND attribute_not_exists(#pending)",
        ExpressionAttributeNames=_attribute_names(_STATE, _PENDING, _PENDING_AT, _UPDATED_AT),
        ExpressionAttributeValues=_values(source=expected_from, target=to, now=now),
    )


def resume(run_id: str, expected_from: str, to: str, seen_pending_at: str) -> Run:
    """The takeover: pending_at = now, on condition the run is still in expected_from with to
    pending and pending_at still seen_pending_at. Only the winner runs the effects.

    StaleState, with nothing written, unless the pending is older than PENDING_STALE_SECONDS
    (strictly). release moves a claim back by exactly that much, so a released claim is
    older as soon as any time has passed since it was made. Either way the taker's
    pending_at is later than the replaced claim's, so the replaced owner can no longer
    release or commit it.
    """
    _row(expected_from, to)
    moment = _now()
    if moment - datetime.fromisoformat(seen_pending_at) <= _STALE_AFTER:
        raise _lost(
            run_id,
            f"takeover of {to} refused: the claim is not older than "
            f"{names.PENDING_STALE_SECONDS} s",
        )
    now = names.utc_iso(moment)
    return _update(
        run_id,
        f"takeover {expected_from} -> {to} lost",
        UpdateExpression="SET #pending_at = :now, #updated_at = :now",
        ConditionExpression="#state = :source AND #pending = :target AND #pending_at = :seen",
        ExpressionAttributeNames=_attribute_names(_STATE, _PENDING, _PENDING_AT, _UPDATED_AT),
        ExpressionAttributeValues=_values(
            source=expected_from, target=to, seen=seen_pending_at, now=now
        ),
    )


def _holds(run_id: str, claimed_at: str) -> bool:
    """Whether the caller's claim is still in place: pending set, pending_at claimed_at."""
    item = _get(names.run_pk(run_id)) or {}
    return item.get(_PENDING) is not None and item.get(_PENDING_AT) == claimed_at


def release(run_id: str, claimed_at: str) -> bool:
    """After an effect throws: move the caller's claim back PENDING_STALE_SECONDS, on
    condition it is still the caller's (pending set, pending_at still claimed_at). False,
    with nothing written, when it is not.

    A collision with an in-flight transaction is re-read like a commit's: retried once if
    the claim is still the caller's, so the next tick need not wait out the claim.
    """
    back = names.utc_iso(datetime.fromisoformat(claimed_at) - _STALE_AFTER)
    now = names.utc_iso(_now())
    retried = False
    while True:
        try:
            _client().update_item(
                TableName=names.RUNS_TABLE,
                Key=_key(names.run_pk(run_id)),
                UpdateExpression="SET #pending_at = :back, #updated_at = :now",
                ConditionExpression="attribute_exists(#pending) AND #pending_at = :claimed_at",
                ExpressionAttributeNames=_attribute_names(_PENDING, _PENDING_AT, _UPDATED_AT),
                ExpressionAttributeValues=_values(back=back, claimed_at=claimed_at, now=now),
            )
        except ClientError as error:
            if _code(error) == _CONDITION_FAILED:
                return False
            if _code(error) != _CONFLICT:
                raise
            if retried or not _holds(run_id, claimed_at):
                return False
            retried = True
            continue
        return True


def record_refresh(run_id: str, refreshed_at: str) -> bool:
    """A tick that refreshes results without moving the run records when (tasks.md 2.5): no
    commit carries last_refresh_at then. last_refresh_at = refreshed_at, on condition the run
    is RUNNING and the value only moves forward.

    False, with nothing written, when the condition fails or the write meets an in-flight
    transaction: the run has moved on, or a later refresh is already recorded. Either way
    the refresh window is the newer one's.
    """
    refreshed_at = names.utc_iso(refreshed_at)  # one form, so the comparison is by time
    try:
        _client().update_item(
            TableName=names.RUNS_TABLE,
            Key=_key(names.run_pk(run_id)),
            UpdateExpression="SET #last_refresh_at = :refreshed_at, #updated_at = :now",
            ConditionExpression=(
                "#state = :running AND (attribute_not_exists(#last_refresh_at) "
                "OR #last_refresh_at < :refreshed_at)"
            ),
            ExpressionAttributeNames=_attribute_names(_STATE, _LAST_REFRESH_AT, _UPDATED_AT),
            ExpressionAttributeValues=_values(
                running=names.STATE_RUNNING,
                refreshed_at=refreshed_at,
                now=names.utc_iso(_now()),
            ),
        )
    except ClientError as error:
        if _code(error) in (_CONDITION_FAILED, _CONFLICT):
            return False
        raise
    return True


def _checked(updates: dict[str, Any]) -> dict[str, Any]:
    """A copy of a commit's updates, refused unless each key is an attribute the commit may
    set and each value is not None."""
    refused = sorted(set(updates) - _UPDATABLE)
    if refused:
        raise ValueError(f"a commit cannot set {refused}; it may set {sorted(_UPDATABLE)}")
    empty = sorted(key for key, value in updates.items() if value is None)
    if empty:
        raise ValueError(f"a commit cannot set {empty} to None")
    return dict(updates)


def _commit_run_item(
    run_id: str, row: names.Transition, reason: str, sets: dict[str, Any], claimed_at: str, at: str
) -> dict[str, Any]:
    assignments = [
        "#state = :target",
        "#history = list_append(#history, :entry)",
        "#updated_at = :at",
        *(f"#{attribute} = :{attribute}" for attribute in sets),
    ]
    return {
        "Update": {
            "TableName": names.RUNS_TABLE,
            "Key": _key(names.run_pk(run_id)),
            "UpdateExpression": f"SET {', '.join(assignments)} REMOVE #pending",
            "ConditionExpression": (
                "#state = :source AND #pending = :target AND #pending_at = :claimed_at"
            ),
            "ExpressionAttributeNames": _attribute_names(
                _STATE, _HISTORY, _UPDATED_AT, _PENDING, _PENDING_AT, *sets
            ),
            "ExpressionAttributeValues": _values(
                source=row.source,
                target=row.target,
                entry=[_entry(at, row, reason)],
                at=at,
                claimed_at=claimed_at,
                **sets,
            ),
        }
    }


def _launch_pointer(run_id: str, feature_key: str) -> dict[str, Any]:
    """The launch commit's pointer update: feature_key, on condition the pointer names the
    run (so a run whose pointer was reset can never launch)."""
    return {
        "Update": {
            "TableName": names.RUNS_TABLE,
            "Key": _key(names.ACTIVE_POINTER_PK),
            "UpdateExpression": "SET #feature_key = :feature_key",
            "ConditionExpression": "#run_id = :run_id",
            "ExpressionAttributeNames": _attribute_names(_POINTER_FEATURE_KEY, _POINTER_RUN_ID),
            "ExpressionAttributeValues": _values(feature_key=feature_key, run_id=run_id),
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
    }


def _clear_active(run_id: str) -> dict[str, Any]:
    """clear_active: delete the pointer, on condition it is absent or names the run."""
    return {
        "Delete": {
            "TableName": names.RUNS_TABLE,
            "Key": _key(names.ACTIVE_POINTER_PK),
            "ConditionExpression": "attribute_not_exists(#pk) OR #run_id = :run_id",
            "ExpressionAttributeNames": _attribute_names(_PK, _POINTER_RUN_ID),
            "ExpressionAttributeValues": _values(run_id=run_id),
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
    }


def _still_claimed(run_id: str, source: str, target: str, claimed_at: str) -> bool:
    item = _get(names.run_pk(run_id)) or {}
    return (item.get(_STATE), item.get(_PENDING), item.get(_PENDING_AT)) == (
        source,
        target,
        claimed_at,
    )


def commit(
    run_id: str,
    expected_from: str,
    to: str,
    actor: str,
    reason: str,
    updates: dict[str, Any],
    claimed_at: str,
) -> Run:
    """Step 3: state = to, pending removed, one history entry appended and updates set, on
    condition the run is in expected_from with to pending and pending_at still claimed_at,
    the value the caller's claim or takeover wrote.

    The launch commit (PLANNED -> RUNNING) also sets feature_key, on the run item and on the
    pointer, on condition the pointer names this run. A terminal commit also deletes the
    pointer (clear_active), on condition it is absent or names this run. Each goes in the
    same TransactWriteItems as the run item.

    actor must be the table's actor for the row, reason a non-empty string, and a
    feature_key in updates the run's own, on any row; else ValueError before any write.
    StaleState when the claim is no longer the caller's. A transaction cancelled by a
    colliding write is a lost race too: re-read, and retry once if the claim is still the
    caller's. CommitFailed when the pointer's condition, or anything else that is not a
    race, cancels the commit. Other errors, such as throttling or a network failure,
    propagate as they came.
    """
    row = _row(expected_from, to)
    if actor != row.actor:
        raise ValueError(f"{expected_from} -> {to} is recorded with actor {row.actor!r}")
    if not isinstance(reason, str) or not reason:
        raise ValueError(f"a commit's reason must be a non-empty string, not {reason!r}")
    sets = _checked(updates)
    # Every commit, not only the launch: no commit may store another run's feature key.
    feature_key = names.experiment_feature_key(run_id)
    if sets.get(_FEATURE_KEY, feature_key) != feature_key:
        raise ValueError(f"the feature_key of {run_id} is {feature_key}")
    launch = (expected_from, to) == _LAUNCH
    pointer_write = None
    if launch:
        sets.setdefault(_FEATURE_KEY, feature_key)
        pointer_write = _launch_pointer(run_id, feature_key)
    elif to in names.TERMINAL_STATES:
        pointer_write = _clear_active(run_id)
    at = names.utc_iso(_now())
    request = [_commit_run_item(run_id, row, reason, sets, claimed_at, at)]
    if pointer_write is not None:
        request.append(pointer_write)

    what = f"commit {expected_from} -> {to}"
    retried = False
    while True:
        try:
            _client().transact_write_items(TransactItems=request)
        except ClientError as error:
            reasons = _reasons(error)
            if reasons is None:
                raise
            codes = [r.code for r in reasons]
            if codes[:1] == [_REASON_CONDITION_FAILED]:
                raise _lost(run_id, f"{what} lost: the claim is not the caller's") from error
            if codes[1:2] == [_REASON_CONDITION_FAILED]:
                old = reasons[1].item
                holder = f"the pointer names run {old[_POINTER_RUN_ID]}" if old else "no pointer"
                step = "launch commit" if launch else names.CLEAR_ACTIVE
                raise CommitFailed(f"{what} cancelled by its {step}: {holder}") from error
            if _REASON_CONFLICT not in codes:
                raise CommitFailed(f"{what} cancelled: {', '.join(codes)}") from error
            if not retried and _still_claimed(run_id, expected_from, to, claimed_at):
                retried = True
                continue
            raise _lost(run_id, f"{what} lost to a colliding write") from error
        return load_run(run_id)
