"""rm-invoker: wakes rm_agent when the latency alarm enters ALARM, and on every rm-tick run.

design.md section 7. Two kinds of event reach this function. Both arrive asynchronously (an
EventBridge rule target, and an EventBridge Scheduler schedule), so an exception raised here
makes Lambda run the same event again, by default two more times.

- The rule's "CloudWatch Alarm State Change" event. Only rm-cheaper-latency-p95 entering ALARM
  becomes {"action": "alarm", "alarm_name": ..., "alarm_state": "ALARM", "at": ...}.
- The rm-tick schedule. Its input is the tick payload itself, {"action": "tick"}, which this
  function recognises and passes on. Task 4.1 configures the schedule with that input; a
  schedule without one would deliver an empty event, which sends nothing.

Any other alarm, any other state (OK, INSUFFICIENT_DATA) and any other event send nothing,
and the log says why. In the designed setup none of them reaches this function, so they are
logged at WARNING.

`at` is detail.state.timestamp, the moment the alarm entered ALARM (for example
2019-10-02T17:04:40.989+0000). When the state has no timestamp, `at` falls back to the event's
time (2019-10-02T17:04:40Z), which is rounded to the second. invoke.json accepts both forms.
Event shape: docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-eventbridge.html

With STUB=1 the function only logs the payload. It never reads /rm/agent-runtime-arn and never
calls AgentCore. Otherwise it reads the runtime ARN from that SSM parameter each time it sends,
and calls InvokeAgentRuntime once, with a new session ID and the shared client setting
(agent_client.py).

What raises, so that Lambda runs the event again:
- any transport or AWS error from SSM or AgentCore, as botocore raised it;
- AgentResponseError, when the reply is not a response.json object: nothing confirms that
  the agent handled the event. Every agent action is idempotent, so a repeat is safe.

What does not raise: a well-formed response whose `error` is not null, such as
ILLEGAL_IN_STATE or EFFECT_FAILED. That is the agent's answer. It is logged at WARNING and the
event is done; a later tick resumes an unfinished transition (design.md section 6).

Every log line is one JSON object. contracts.names uses only the standard library, so this
function needs no jsonschema.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any

import boto3

from contracts.names import (
    ACTION_ALARM,
    ACTION_TICK,
    AGENT_RUNTIME_ARN_PARAMETER,
    ALARM_NAME,
    ALARM_STATE_ALARM,
    ERROR_FIELDS,
    INVOKER_FUNCTION,
    INVOKER_STUB_ENV,
    INVOKER_STUB_ON,
    REGION,
    RESPONSE_FIELDS,
    new_session_id,
)
from signals.invoker.agent_client import new_agent_client

LOGGER = logging.getLogger(INVOKER_FUNCTION)
# With Lambda's default plain-text log format only WARN and above reach CloudWatch unless the
# level is set in code (docs.aws.amazon.com/lambda/latest/dg/python-logging.html).
LOGGER.setLevel(logging.INFO)

# A CloudWatch alarm state change, as EventBridge delivers it.
ALARM_EVENT_SOURCE = "aws.cloudwatch"
ALARM_EVENT_DETAIL_TYPE = "CloudWatch Alarm State Change"
# The tick payload, which is also the rm-tick schedule's input.
TICK_PAYLOAD = {"action": ACTION_TICK}
# The response.json fields that hold null or a string.
_NULL_OR_TEXT_FIELDS = ("run_id", "state", "awaiting", "pending")


class AgentResponseError(RuntimeError):
    """The agent's reply is not a response.json object."""


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def payload_for(event: Any) -> tuple[dict | None, str]:
    """The invoke.json payload an event maps to (None: send nothing), and why, for the log."""
    if event == TICK_PAYLOAD:
        return dict(TICK_PAYLOAD), "the rm-tick schedule's tick"
    if not (
        isinstance(event, dict)
        and event.get("source") == ALARM_EVENT_SOURCE
        and event.get("detail-type") == ALARM_EVENT_DETAIL_TYPE
    ):
        return None, "not a CloudWatch alarm state change, and not the tick payload"
    detail = _mapping(event.get("detail"))
    name = detail.get("alarmName")
    if name != ALARM_NAME:
        return None, f"the alarm is {name}, not {ALARM_NAME}"
    state = _mapping(detail.get("state"))
    value = state.get("value")
    if value != ALARM_STATE_ALARM:
        return None, f"{ALARM_NAME} changed to {value}, not to {ALARM_STATE_ALARM}"
    at = _text(state.get("timestamp")) or _text(event.get("time"))
    if at is None:
        return None, f"{ALARM_NAME} entered {ALARM_STATE_ALARM}, but the event has no time"
    payload = {
        "action": ACTION_ALARM,
        "alarm_name": ALARM_NAME,
        "alarm_state": ALARM_STATE_ALARM,
        "at": at,
    }
    return payload, f"{ALARM_NAME} entered {ALARM_STATE_ALARM}"


def stub_on() -> bool:
    """STUB=1, and nothing else, means log the payload and send nothing."""
    return os.environ.get(INVOKER_STUB_ENV) == INVOKER_STUB_ON


def read_runtime_arn(ssm: Any) -> str:
    return ssm.get_parameter(Name=AGENT_RUNTIME_ARN_PARAMETER)["Parameter"]["Value"]


def is_response(value: Any) -> bool:
    """True when value has response.json's shape as far as this function relies on it.

    Exactly the fields in RESPONSE_FIELDS; run_id, state, awaiting and pending null or a
    string; message a string; error null or exactly a code and a message, both strings.
    The agent validates the full schema (task 2.5); a Lambda without jsonschema checks
    only this much.
    """
    if not isinstance(value, dict) or set(value) != set(RESPONSE_FIELDS):
        return False
    if not isinstance(value["message"], str):
        return False
    if any(not isinstance(value[field], str | None) for field in _NULL_OR_TEXT_FIELDS):
        return False
    error = value["error"]
    return error is None or (
        isinstance(error, dict)
        and set(error) == set(ERROR_FIELDS)
        and all(isinstance(error[field], str) for field in ERROR_FIELDS)
    )


def invoke_agent(agent: Any, runtime_arn: str, payload: dict, session_id: str) -> dict:
    """Call InvokeAgentRuntime once and return the agent's response.json object.

    The call has the form docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
    runtime-invoke-agent.html shows: the ARN, a session ID and the JSON payload as bytes.
    botocore's exceptions pass through; AgentResponseError when the reply is not a response.
    """
    reply = agent.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    body = reply["response"].read()
    status = reply.get("statusCode", 200)
    try:
        response = json.loads(body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        response = None
    if status != 200 or not is_response(response):
        raise AgentResponseError(
            f"rm_agent's reply is not a response.json object (status {status}, "
            f"content type {reply.get('contentType')}): {body[:500]!r}"
        )
    return response


@functools.cache
def _default_ssm():
    return boto3.client("ssm", region_name=REGION)


@functools.cache
def _default_agent():
    return new_agent_client()


def _log(level: int, message: str, **fields: Any) -> None:
    LOGGER.log(level, json.dumps({"message": message, **fields}, default=str))


def handler(event: Any, context: Any, *, ssm: Any = None, agent: Any = None) -> dict:
    """Lambda entry point. Tests pass ssm and agent in place of the default clients.

    Returns what happened, for a synchronous test invoke (an asynchronous one discards it):
    {"outcome": "ignored", "reason"}, {"outcome": "stub", "payload"}, or
    {"outcome": "invoked", "payload", "session_id", "response"}.
    """
    payload, reason = payload_for(event)
    if payload is None:
        _log(logging.WARNING, "nothing sent", reason=reason, event=event)
        return {"outcome": "ignored", "reason": reason}
    if stub_on():
        _log(logging.INFO, "STUB=1: payload logged, not sent", reason=reason, payload=payload)
        return {"outcome": "stub", "payload": payload}

    session_id = new_session_id()
    try:
        runtime_arn = read_runtime_arn(ssm if ssm is not None else _default_ssm())
        response = invoke_agent(
            agent if agent is not None else _default_agent(), runtime_arn, payload, session_id
        )
    except Exception:
        _log(
            logging.ERROR,
            "the rm_agent call failed; raising so Lambda runs the event again",
            reason=reason,
            payload=payload,
            session_id=session_id,
        )
        raise

    failed = response["error"] is not None
    _log(
        logging.WARNING if failed else logging.INFO,
        "rm_agent answered with an error" if failed else "rm_agent answered",
        reason=reason,
        payload=payload,
        session_id=session_id,
        response=response,
    )
    return {
        "outcome": "invoked",
        "payload": payload,
        "session_id": session_id,
        "response": response,
    }
