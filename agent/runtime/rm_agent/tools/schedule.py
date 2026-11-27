"""schedule: enable or disable the rm-tick schedule (tasks.md 2.4; design.md section 7).

An UpdateSchedule replaces every field of a schedule, so set_tick reads rm-tick with
GetSchedule and writes back every field it returned that UpdateSchedule takes, changing only
State. The fields come from botocore's own service model: all but Arn, CreationDate and
LastModificationDate. Imports only contracts, the standard library and boto3.
"""

from __future__ import annotations

import functools
import json

import boto3

from contracts import names

# EventBridge Scheduler's spellings of a schedule's state.
SCHEDULE_ENABLED = "ENABLED"
SCHEDULE_DISABLED = "DISABLED"
_TICK_PAYLOAD = {"action": names.ACTION_TICK}  # rm-tick's input (design.md section 7)


@functools.cache
def _client():
    """The Scheduler client, made on first use, in us-east-1 (design.md section 2)."""
    return boto3.client("scheduler", region_name=names.REGION)


def update_fields(client) -> tuple[str, ...]:
    """Every field GetSchedule returns that UpdateSchedule takes, by the service model."""
    model = client.meta.service_model
    taken = model.operation_model("UpdateSchedule").input_shape.members
    returned = model.operation_model("GetSchedule").output_shape.members
    return tuple(field for field in returned if field in taken)


def _check_is_the_tick(schedule: dict) -> None:
    """Refuse to write back a schedule that is not rm-tick's: its input must be the tick
    payload and its role rm-scheduler-role."""
    target = schedule.get("Target") or {}
    try:
        payload = json.loads(target.get("Input") or "null")
    except ValueError:
        payload = None
    role = str(target.get("RoleArn", ""))
    if payload != _TICK_PAYLOAD or not role.endswith(f":role/{names.SCHEDULER_ROLE}"):
        raise ValueError(
            f"schedule {names.TICK_SCHEDULE} does not target the tick payload with role "
            f"{names.SCHEDULER_ROLE}; nothing was written"
        )


def set_tick(enabled: bool) -> bool:
    """Enable or disable rm-tick. True when it wrote, False when the schedule was already in
    that state (a no-op, nothing written)."""
    if not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a bool, not {enabled!r}")
    client = _client()
    schedule = client.get_schedule(Name=names.TICK_SCHEDULE, GroupName=names.TICK_SCHEDULE_GROUP)
    _check_is_the_tick(schedule)
    state = SCHEDULE_ENABLED if enabled else SCHEDULE_DISABLED
    if schedule.get("State") == state:
        return False
    fields = {field: schedule[field] for field in update_fields(client) if field in schedule}
    client.update_schedule(**{**fields, "State": state})
    return True
