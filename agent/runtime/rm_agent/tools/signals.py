"""signals: the latency alarm's state and threshold (tasks.md 2.4; design.md sections 5, 6).

Both come from one DescribeAlarms call, so the probe's threshold is the one the alarm was
judged by. Imports only contracts, the standard library and boto3.
"""

from __future__ import annotations

import functools
from typing import NamedTuple

import boto3

from contracts import names


class Alarm(NamedTuple):
    state: str  # one of names.ALARM_STATES
    threshold_ms: float


@functools.cache
def _client():
    """The CloudWatch client, made on first use, in us-east-1 (design.md section 2)."""
    return boto3.client("cloudwatch", region_name=names.REGION)


def alarm_state(name: str) -> Alarm:
    """The alarm's state, OK, ALARM or INSUFFICIENT_DATA, and its threshold, from one
    DescribeAlarms call. LookupError when there is no such metric alarm; ValueError for a
    state outside names.ALARM_STATES. A failed call propagates as it came."""
    response = _client().describe_alarms(AlarmNames=[name], AlarmTypes=["MetricAlarm"])
    found = [alarm for alarm in response.get("MetricAlarms", []) if alarm.get("AlarmName") == name]
    if not found:
        raise LookupError(f"no metric alarm named {name}")
    state = found[0].get("StateValue")
    if state not in names.ALARM_STATES:
        raise ValueError(f"alarm {name} is in state {state!r}, not one of {names.ALARM_STATES}")
    return Alarm(state, float(found[0]["Threshold"]))
