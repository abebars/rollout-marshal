"""Direct Lambda invokes run with OpenTelemetry's botocore instrumentation suppressed (task 5.1).

Run r202611271927 stuck in ROLLED_BACK, because every probe invoke reached rm-answer-cheaper
with a "headers" key that answer_request refuses. The runtime starts under
opentelemetry-instrument, and opentelemetry-instrumentation-botocore injects the trace context
into any JSON payload of a Lambda Invoke (extensions/lmbd.py, _inject_current_span). It does
so unless instrumentation is suppressed in the current context: upstream's _patched_api_call,
and the AWS distro's copy of it, check is_instrumentation_enabled() first.

The first tests read that same check, from the real opentelemetry package, at the moment the
client is called. The last two instrument botocore for real and require that the payload
reaching Lambda is exactly what the code wrote. They run where
opentelemetry-instrumentation-botocore is installed, which is the runtime's own dependency set,
and are skipped in the root environment, which does not ship it.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from rm_agent.tools import flagsmith, probe, untraced

from contracts import names

GOOD_REPLY = {
    "answer": "Open Settings, choose Security, then Reset password.",
    "input_tokens": 49,
    "output_tokens": 60,
    "model_id": names.MODEL_CHEAPER,
}
ADMIN_ARGS = {"environment_api_key": "AbCdEf0123456789", "experiment_id": 819}
ADMIN_REPLY = {"ok": True, "result": {"status": "running"}}


class RecordingLambda:
    """Records each invoke's arguments, and whether the instrumentation would run then."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls: list[tuple[dict, bool]] = []

    def invoke(self, **kwargs):
        self.calls.append((kwargs, is_instrumentation_enabled()))
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.reply).encode())}


def test_the_probe_invokes_with_the_instrumentation_suppressed():
    client = RecordingLambda(GOOD_REPLY)
    assert is_instrumentation_enabled()
    _, problem = probe._invoke(client)
    assert problem is None
    ((kwargs, enabled),) = client.calls
    assert enabled is False
    assert kwargs["Payload"] == json.dumps(probe._REQUEST)
    assert is_instrumentation_enabled()  # only the invoke itself is suppressed


def test_each_of_the_probes_threads_suppresses_its_own_invoke(monkeypatch):
    client = RecordingLambda(GOOD_REPLY)
    monkeypatch.setattr(probe, "_client", lambda: client)
    result = probe.probe_cheaper(threshold_ms=10_000)
    assert result["passed"] is True, result
    assert len(client.calls) == names.PROBE_INVOKES
    assert [enabled for _, enabled in client.calls] == [False] * names.PROBE_INVOKES


def test_lambda_admin_invokes_with_the_instrumentation_suppressed():
    client = RecordingLambda(ADMIN_REPLY)
    assert flagsmith.LambdaAdmin(client).call("get_experiment", ADMIN_ARGS) == {"status": "running"}
    ((kwargs, enabled),) = client.calls
    assert enabled is False
    assert json.loads(kwargs["Payload"]) == {"tool": "get_experiment", "arguments": ADMIN_ARGS}


def test_without_opentelemetry_there_is_nothing_to_suppress(monkeypatch):
    """The guarded import: an environment without opentelemetry still runs the invokes."""
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.utils", None)
    try:
        reloaded = importlib.reload(untraced)
        assert reloaded.suppress_instrumentation is contextlib.nullcontext
        with reloaded.suppress_instrumentation():
            pass
    finally:
        monkeypatch.undo()
        importlib.reload(untraced)
    assert untraced.suppress_instrumentation is not contextlib.nullcontext


# ---------------------------------------------------------------------------
# The real instrumentation, where it is installed (the runtime's dependency set)
# ---------------------------------------------------------------------------
@pytest.fixture
def instrumented_botocore():
    instrumentation = pytest.importorskip(
        "opentelemetry.instrumentation.botocore",
        reason="opentelemetry-instrumentation-botocore is only in the runtime's dependency set",
    )
    instrumentor = instrumentation.BotocoreInstrumentor()
    instrumentor.instrument()
    try:
        yield
    finally:
        instrumentor.uninstrument()


def _lambda_client():
    session = boto3.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name=names.REGION
    )
    return session.client("lambda")


def _body(value: dict) -> StreamingBody:
    data = json.dumps(value).encode()
    return StreamingBody(io.BytesIO(data), len(data))


def test_the_probes_payload_reaches_lambda_unchanged_under_the_instrumentation(
    instrumented_botocore,
):
    client = _lambda_client()
    with Stubber(client) as stub:
        stub.add_response(
            "invoke",
            {"StatusCode": 200, "Payload": _body(GOOD_REPLY)},
            {
                "FunctionName": names.ANSWER_CHEAPER_FUNCTION,
                "InvocationType": "RequestResponse",
                "Payload": json.dumps(probe._REQUEST),  # exactly: no "headers" added
            },
        )
        _, problem = probe._invoke(client)
        stub.assert_no_pending_responses()
    assert problem is None


def test_lambda_admins_payload_reaches_lambda_unchanged_under_the_instrumentation(
    instrumented_botocore,
):
    client = _lambda_client()
    with Stubber(client) as stub:
        stub.add_response(
            "invoke",
            {"StatusCode": 200, "Payload": _body(ADMIN_REPLY)},
            {
                "FunctionName": names.FLAGSMITH_ADMIN_FUNCTION,
                "Payload": json.dumps({"tool": "get_experiment", "arguments": ADMIN_ARGS}),
            },
        )
        result = flagsmith.LambdaAdmin(client).call("get_experiment", ADMIN_ARGS)
        stub.assert_no_pending_responses()
    assert result == {"status": "running"}
