"""rm-invoker (tasks.md 4.2): event mapping, STUB, the agent call and its errors.

Covers the signals spec "Alarm wakes the agent": an alarm entering ALARM sends one valid
`alarm` payload; OK or INSUFFICIENT_DATA sends nothing. No AWS calls: the SSM and AgentCore
clients are fakes, except in the end-to-end test, where a real botocore client is answered by
a before-send hook in place of the network.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
from botocore import UNSIGNED
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.response import StreamingBody

import contracts
from contracts import names
from signals.invoker import handler as invoker
from signals.invoker.agent_client import AGENT_CLIENT_SERVICE, agent_client_config

ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{names.REGION}:{names.ACCOUNT_ID}:runtime/rm_agent-Test123"
)
ENDPOINT = f"https://{AGENT_CLIENT_SERVICE}.{names.REGION}.amazonaws.com"

# The two timestamps of the single-metric example on
# docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-eventbridge.html
STATE_TIMESTAMP = "2019-10-02T17:04:40.989+0000"
EVENT_TIME = "2019-10-02T17:04:40Z"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def alarm_event(state=names.ALARM_STATE_ALARM, *, name=names.ALARM_NAME, timestamp=STATE_TIMESTAMP):
    """A CloudWatch Alarm State Change event shaped like the AWS docs example, for our alarm."""
    detail_state = {
        "reason": "Threshold Crossed: 2 out of the last 2 datapoints were greater than the "
        "threshold (10000.0) (minimum 2 datapoints for OK -> ALARM transition).",
        "reasonData": '{"version":"1.0","statistic":"p95","period":60,"threshold":10000.0}',
        "value": state,
    }
    if timestamp is not None:
        detail_state["timestamp"] = timestamp
    return {
        "version": "0",
        "id": "c4c1c1c9-6542-e61b-6ef0-8c4d36933a92",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": names.ACCOUNT_ID,
        "time": EVENT_TIME,
        "region": names.REGION,
        "resources": [f"arn:aws:cloudwatch:{names.REGION}:{names.ACCOUNT_ID}:alarm:{name}"],
        "detail": {
            "alarmName": name,
            "configuration": {
                "metrics": [
                    {
                        "id": "m1",
                        "metricStat": {
                            "metric": {
                                "dimensions": {names.CW_DIMENSION_VARIANT: names.VARIANT_CHEAPER},
                                "name": names.ALARM_METRIC,
                                "namespace": names.CW_NAMESPACE,
                            },
                            "period": names.ALARM_PERIOD_SECONDS,
                            "stat": names.ALARM_STATISTIC,
                        },
                        "returnData": True,
                    }
                ]
            },
            "previousState": {
                "reason": "Threshold Crossed: no datapoints were received.",
                "timestamp": "2019-10-02T17:00:40.112+0000",
                "value": names.ALARM_STATE_OK,
            },
            "state": detail_state,
        },
    }


TICK_EVENT = {"action": names.ACTION_TICK}  # the rm-tick schedule's input (task 4.1)

ALARM_PAYLOAD = {
    "action": names.ACTION_ALARM,
    "alarm_name": names.ALARM_NAME,
    "alarm_state": names.ALARM_STATE_ALARM,
    "at": STATE_TIMESTAMP,
}


# ---------------------------------------------------------------------------
# Agent responses and fakes
# ---------------------------------------------------------------------------
def agent_response(**changes):
    """A response.json object; the default is a successful rollback."""
    value = {
        "run_id": "r202611271530",
        "state": names.STATE_ROLLED_BACK,
        "message": "Rolled back: the cheaper arm's p95 latency crossed the threshold.",
        "awaiting": None,
        "error": None,
    }
    value.update(changes)
    return value


NOOP_RESPONSE = {
    "run_id": None,
    "state": None,
    "message": "No active run; nothing to do.",
    "awaiting": None,
    "error": None,
}
ERROR_RESPONSES = [
    agent_response(
        state=names.STATE_RUNNING,
        error={"code": names.ERROR_EFFECT_FAILED, "message": "SNS publish failed; pending kept."},
    ),
    agent_response(
        state=names.STATE_PLANNED,
        awaiting=names.AWAITS_LAUNCH,
        error={"code": names.ERROR_ILLEGAL_IN_STATE, "message": "check is not legal in PLANNED"},
    ),
    agent_response(
        run_id=None,
        state=None,
        error={"code": names.ERROR_BAD_REQUEST, "message": "payload does not match invoke.json"},
    ),
]


def body_of(value) -> bytes:
    return value if isinstance(value, bytes) else json.dumps(value).encode()


def agent_reply(value, *, status=200, content_type="application/json"):
    """What botocore returns from invoke_agent_runtime: the body is a StreamingBody."""
    raw = body_of(value)
    return {
        "response": StreamingBody(io.BytesIO(raw), len(raw)),
        "contentType": content_type,
        "statusCode": status,
    }


class FakeSSM:
    def __init__(self, value=RUNTIME_ARN, error=None):
        self.value, self.error, self.calls = value, error, []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Parameter": {"Name": kwargs["Name"], "Type": "String", "Value": self.value}}


class FakeAgent:
    def __init__(self, reply=None, *, error=None, status=200, content_type="application/json"):
        self.reply = agent_response() if reply is None else reply
        self.error, self.status, self.content_type, self.calls = error, status, content_type, []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return agent_reply(self.reply, status=self.status, content_type=self.content_type)


class Untouchable:
    """A client that must not be used."""

    def __init__(self, what):
        self.what = what

    def __getattr__(self, attribute):
        raise AssertionError(f"{self.what} was used: {attribute}")


def client_error(code, status, operation):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "test"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


@pytest.fixture(autouse=True)
def stub_off_and_no_default_clients(monkeypatch):
    """Each test starts with STUB unset, and the default clients fail if a test reaches them."""
    monkeypatch.delenv(names.INVOKER_STUB_ENV, raising=False)
    monkeypatch.setattr(invoker, "_default_ssm", lambda: Untouchable("the default SSM client"))
    monkeypatch.setattr(invoker, "_default_agent", lambda: Untouchable("the default agent client"))


@pytest.fixture
def stub_on(monkeypatch):
    monkeypatch.setenv(names.INVOKER_STUB_ENV, names.INVOKER_STUB_ON)


def invoker_logs(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == invoker.LOGGER.name]


def sent_payload(agent: FakeAgent) -> dict:
    [call] = agent.calls
    return json.loads(call["payload"])


# ---------------------------------------------------------------------------
# Mapping: both payloads validate against invoke.json
# ---------------------------------------------------------------------------
def test_alarm_entering_alarm_maps_to_the_alarm_payload():
    payload, reason = invoker.payload_for(alarm_event())
    assert payload == ALARM_PAYLOAD
    contracts.validate("invoke", payload)
    assert names.ALARM_NAME in reason


def test_at_is_the_moment_the_alarm_entered_alarm():
    """detail.state.timestamp, not the event's time, which is rounded to the second."""
    payload, _ = invoker.payload_for(alarm_event())
    assert payload["at"] == STATE_TIMESTAMP
    assert names.utc_iso(payload["at"]) == "2019-10-02T17:04:40.989000Z"


def test_at_falls_back_to_the_event_time():
    payload, _ = invoker.payload_for(alarm_event(timestamp=None))
    assert payload["at"] == EVENT_TIME
    contracts.validate("invoke", payload)


def test_the_tick_schedule_input_maps_to_the_tick_payload():
    payload, _ = invoker.payload_for(TICK_EVENT)
    assert payload == {"action": "tick"}
    contracts.validate("invoke", payload)
    assert payload is not TICK_EVENT


def test_mapping_does_not_change_the_event():
    event = alarm_event()
    before = copy.deepcopy(event)
    invoker.payload_for(event)
    assert event == before


# ---------------------------------------------------------------------------
# Events that send nothing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", [names.ALARM_STATE_OK, names.ALARM_STATE_INSUFFICIENT_DATA])
def test_alarm_leaving_alarm_sends_nothing(state, caplog):
    """Signals spec, scenario "Alarm leaves ALARM"."""
    caplog.set_level(logging.INFO)
    result = invoker.handler(
        alarm_event(state), None, ssm=Untouchable("SSM"), agent=Untouchable("AgentCore")
    )
    assert result["outcome"] == "ignored"
    assert state in result["reason"]
    [log] = invoker_logs(caplog)
    assert log["reason"] == result["reason"]


def test_another_alarm_entering_alarm_sends_nothing(caplog):
    caplog.set_level(logging.INFO)
    result = invoker.handler(
        alarm_event(name="rm-some-other-alarm"),
        None,
        ssm=Untouchable("SSM"),
        agent=Untouchable("AgentCore"),
    )
    assert result["outcome"] == "ignored"
    assert "rm-some-other-alarm" in result["reason"]
    assert invoker_logs(caplog)[0]["reason"] == result["reason"]


def configuration_change_event():
    event = alarm_event()
    event["detail-type"] = "CloudWatch Alarm Configuration Change"
    event["detail"]["operation"] = "update"
    return event


def contributor_event():
    event = alarm_event()
    event["detail-type"] = "CloudWatch Alarm Contributor State Change"
    return event


def eventbridge_rule_schedule_event():
    return {
        "version": "0",
        "id": "53dc4d37-cffa-4f76-80c9-8b7d4a4d2eaa",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": names.ACCOUNT_ID,
        "time": EVENT_TIME,
        "region": names.REGION,
        "resources": [f"arn:aws:events:{names.REGION}:{names.ACCOUNT_ID}:rule/rm-tick"],
        "detail": {},
    }


def alarm_event_without_any_time():
    event = alarm_event(timestamp=None)
    del event["time"]
    return event


@pytest.mark.parametrize(
    "event",
    [
        {},  # what Scheduler sends when the schedule has no input
        None,
        [],
        "tick",
        {"action": "tick", "extra": 1},
        {"action": "alarm"},
        {"action": "start", "prompt": "go", "actor": "engineer"},
        configuration_change_event(),
        contributor_event(),
        eventbridge_rule_schedule_event(),
        alarm_event_without_any_time(),
    ],
    ids=[
        "empty",
        "null",
        "list",
        "string",
        "tick-with-extra-key",
        "bare-alarm-action",
        "engineer-action",
        "alarm-configuration-change",
        "alarm-contributor-change",
        "eventbridge-rule-schedule",
        "alarm-without-time",
    ],
)
def test_unrecognised_events_send_nothing(event, caplog):
    caplog.set_level(logging.INFO)
    result = invoker.handler(event, None, ssm=Untouchable("SSM"), agent=Untouchable("AgentCore"))
    assert result["outcome"] == "ignored"
    [log] = invoker_logs(caplog)
    assert log["reason"] == result["reason"]


# ---------------------------------------------------------------------------
# STUB=1: log the payload, read nothing, send nothing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("event", [alarm_event(), TICK_EVENT], ids=["alarm", "tick"])
def test_stub_logs_the_payload_and_never_reads_the_parameter(event, stub_on, caplog):
    caplog.set_level(logging.INFO)
    # The autouse fixture makes the default clients fail if they are created or used.
    result = invoker.handler(event, None)
    assert result["outcome"] == "stub"
    [log] = invoker_logs(caplog)
    assert log["payload"] == result["payload"]
    contracts.validate("invoke", log["payload"])


def test_stub_ignores_injected_clients_too(stub_on):
    result = invoker.handler(alarm_event(), None, ssm=Untouchable("SSM"), agent=Untouchable("A"))
    assert result == {"outcome": "stub", "payload": ALARM_PAYLOAD}


def test_stub_still_sends_nothing_for_ok(stub_on, caplog):
    caplog.set_level(logging.INFO)
    result = invoker.handler(alarm_event(names.ALARM_STATE_OK), None)
    assert result["outcome"] == "ignored"
    assert "payload" not in invoker_logs(caplog)[0]


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_stub_1_is_stub(value, monkeypatch):
    monkeypatch.setenv(names.INVOKER_STUB_ENV, value)
    ssm, agent = FakeSSM(), FakeAgent()
    result = invoker.handler(TICK_EVENT, None, ssm=ssm, agent=agent)
    assert result["outcome"] == "invoked"
    assert len(ssm.calls) == len(agent.calls) == 1


# ---------------------------------------------------------------------------
# The agent call
# ---------------------------------------------------------------------------
def test_alarm_sends_one_valid_alarm_payload(caplog):
    """Signals spec, scenario "Alarm enters ALARM"."""
    caplog.set_level(logging.INFO)
    ssm, agent = FakeSSM(), FakeAgent()
    result = invoker.handler(alarm_event(), None, ssm=ssm, agent=agent)

    assert ssm.calls == [{"Name": names.AGENT_RUNTIME_ARN_PARAMETER}]
    [call] = agent.calls
    assert set(call) == {"agentRuntimeArn", "runtimeSessionId", "payload"}
    assert call["agentRuntimeArn"] == RUNTIME_ARN
    assert isinstance(call["payload"], bytes)
    payload = sent_payload(agent)
    assert payload == ALARM_PAYLOAD
    contracts.validate("invoke", payload)

    assert result["outcome"] == "invoked"
    assert result["payload"] == payload
    assert result["session_id"] == call["runtimeSessionId"]
    assert result["response"] == agent_response()
    [log] = invoker_logs(caplog)
    assert log["response"] == agent_response()
    assert log["session_id"] == call["runtimeSessionId"]


def test_tick_sends_the_tick_payload():
    ssm, agent = FakeSSM(), FakeAgent(NOOP_RESPONSE)
    result = invoker.handler(TICK_EVENT, None, ssm=ssm, agent=agent)
    assert sent_payload(agent) == {"action": "tick"}
    contracts.validate("invoke", sent_payload(agent))
    assert result["response"] == NOOP_RESPONSE


def test_every_invocation_gets_a_new_session_id_and_reads_the_arn():
    ssm, agent = FakeSSM(), FakeAgent()
    for _ in range(3):
        invoker.handler(TICK_EVENT, None, ssm=ssm, agent=agent)
    sessions = [call["runtimeSessionId"] for call in agent.calls]
    assert len(set(sessions)) == 3
    for session in sessions:
        assert session.startswith(names.SESSION_ID_PREFIX)
        assert len(session) >= names.SESSION_ID_MIN_LENGTH
    assert len(ssm.calls) == 3


def test_the_lambda_signature_uses_the_default_clients(monkeypatch):
    ssm, agent = FakeSSM(), FakeAgent()
    monkeypatch.setattr(invoker, "_default_ssm", lambda: ssm)
    monkeypatch.setattr(invoker, "_default_agent", lambda: agent)
    result = invoker.handler(alarm_event(), object())
    assert result["outcome"] == "invoked"
    assert len(ssm.calls) == len(agent.calls) == 1


# ---------------------------------------------------------------------------
# Errors: transport and AWS errors raise; the agent's own errors are logged
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url=ENDPOINT),
        ConnectTimeoutError(endpoint_url=ENDPOINT),
        ReadTimeoutError(endpoint_url=ENDPOINT),
        client_error("ThrottlingException", 429, "InvokeAgentRuntime"),
        client_error("InternalServerException", 500, "InvokeAgentRuntime"),
        client_error("RuntimeClientError", 424, "InvokeAgentRuntime"),
        client_error("ResourceNotFoundException", 404, "InvokeAgentRuntime"),
        client_error("AccessDeniedException", 403, "InvokeAgentRuntime"),
    ],
    ids=lambda e: (
        type(e).__name__ if not isinstance(e, ClientError) else e.response["Error"]["Code"]
    ),
)
def test_a_transport_or_aws_failure_raises(error):
    """So Lambda runs the asynchronous event again."""
    agent = FakeAgent(error=error)
    with pytest.raises(type(error)) as raised:
        invoker.handler(alarm_event(), None, ssm=FakeSSM(), agent=agent)
    assert raised.value is error
    assert len(agent.calls) == 1


def test_a_failed_parameter_read_raises_before_any_invoke():
    ssm = FakeSSM(error=client_error("ParameterNotFound", 400, "GetParameter"))
    with pytest.raises(ClientError):
        invoker.handler(alarm_event(), None, ssm=ssm, agent=Untouchable("AgentCore"))


@pytest.mark.parametrize("response", ERROR_RESPONSES, ids=lambda r: r["error"]["code"])
def test_a_well_formed_response_with_an_error_does_not_raise(response, caplog):
    contracts.validate("response", response)
    caplog.set_level(logging.INFO)
    result = invoker.handler(alarm_event(), None, ssm=FakeSSM(), agent=FakeAgent(response))
    assert result["outcome"] == "invoked"
    assert result["response"] == response
    [record] = [r for r in caplog.records if r.name == invoker.LOGGER.name]
    assert record.levelno == logging.WARNING
    assert json.loads(record.getMessage())["response"]["error"] == response["error"]


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b'"Rolled back."',
        json.dumps({"error": "Invocation failed"}).encode(),  # the AgentCore SDK's 500 body
        json.dumps({k: v for k, v in agent_response().items() if k != "awaiting"}).encode(),
        json.dumps({**agent_response(), "extra": 1}).encode(),
        json.dumps(agent_response(error="EFFECT_FAILED")).encode(),
        json.dumps(agent_response(error={"code": "EFFECT_FAILED"})).encode(),
        json.dumps(agent_response(message=None)).encode(),
        json.dumps(agent_response(run_id=202611271530)).encode(),
        json.dumps(agent_response(state=[names.STATE_RUNNING])).encode(),
    ],
    ids=[
        "empty",
        "not-json",
        "not-utf8",
        "array",
        "string",
        "sdk-error-body",
        "missing-field",
        "extra-field",
        "error-as-string",
        "error-without-message",
        "message-null",
        "run-id-number",
        "state-list",
    ],
)
def test_a_reply_that_is_not_a_response_raises(reply):
    """Nothing confirms the agent handled the event, so Lambda should run it again."""
    agent = FakeAgent(reply)
    with pytest.raises(invoker.AgentResponseError):
        invoker.handler(alarm_event(), None, ssm=FakeSSM(), agent=agent)
    assert len(agent.calls) == 1


def test_a_reply_with_a_status_other_than_200_raises():
    with pytest.raises(invoker.AgentResponseError):
        invoker.handler(alarm_event(), None, ssm=FakeSSM(), agent=FakeAgent(status=202))


@pytest.mark.parametrize("response", [agent_response(), NOOP_RESPONSE, *ERROR_RESPONSES])
def test_every_response_json_object_is_accepted(response):
    contracts.validate("response", response)
    assert invoker.is_response(response)


# ---------------------------------------------------------------------------
# End to end through a real botocore client (no network)
# ---------------------------------------------------------------------------
class RawBody:
    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)

    def stream(self, **_):
        yield self._data.read()

    def read(self, *args, **_):
        return self._data.read(*args)


def test_end_to_end_through_botocore():
    """The request validates against the bedrock-agentcore service model, carries the session
    ID header and the payload bytes, and the streaming reply parses back to the response."""
    unsigned = agent_client_config().merge(Config(signature_version=UNSIGNED))
    agent = boto3.session.Session().client(
        AGENT_CLIENT_SERVICE, region_name=names.REGION, config=unsigned
    )
    requests = []

    def before_send(request, **_):
        requests.append(request)
        return AWSResponse(
            request.url,
            200,
            {"Content-Type": "application/json"},
            RawBody(json.dumps(agent_response()).encode()),
        )

    agent.meta.events.register("before-send", before_send)
    result = invoker.handler(alarm_event(), None, ssm=FakeSSM(), agent=agent)

    [request] = requests
    assert request.method == "POST"
    assert request.url.startswith(f"{ENDPOINT}/runtimes/")
    assert request.url.endswith("/invocations")
    session = request.headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]  # bytes here
    assert session == result["session_id"].encode()
    assert json.loads(request.body) == ALARM_PAYLOAD
    assert result["response"] == agent_response()


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------
def test_the_handler_imports_without_jsonschema():
    """rm-invoker ships without jsonschema; only the tests validate against the schemas."""
    code = (
        "import sys; sys.modules['jsonschema'] = None; sys.modules['referencing'] = None\n"
        "import signals.invoker.handler as h\n"
        "assert h.payload_for({'action': 'tick'})[0] == {'action': 'tick'}\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
