"""Tests for app/router, rm-router (task 3.2).

The router's evaluation order is design.md section 4's; its request path and metric are
section 5's. rm-runs, the answer functions, rm-judge and the Flagsmith client are fakes
(app/tests/router_fakes.py), and the Flagsmith client's flags are the real SDK's Flag and
Flags objects. So nothing reaches AWS or Flagsmith.

Two tests run the real Flagsmith SDK against a local Flagsmith API on 127.0.0.1. One
checks the exposure on the wire. The other holds every events POST open and records how
long the flush then takes.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from flagsmith import EventProcessorConfig, Flagsmith
from flagsmith.analytics import (
    EVENTS_MAX_ATTEMPTS,
    EVENTS_REQUEST_TIMEOUT_SECONDS,
    EVENTS_RETRY_BASE_SECONDS,
)
from flagsmith.exceptions import FlagsmithAPIError
from jsonschema import ValidationError

from app.answer import handler as answer_handler
from app.router import handler
from app.tests.router_fakes import (
    DUMMY_SDK_KEY,
    EXPERIMENT_ID,
    LOOPBACK,
    FakeDynamo,
    FakeFlagsmith,
    FakeFlagsmithApi,
    FakeLambda,
    FakeTime,
    api_flag,
    default_model,
    experiment_flag,
    flags_of,
    function_error,
    kill_switch,
    pointer_item,
)
from contracts import names, validate

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "rroutertest1200"
OTHER_RUN_ID = "rroutertest1201"
FEATURE_KEY = names.experiment_feature_key(RUN_ID)
IDENTITY = names.run_identity(RUN_ID, 7)
QUESTION = "How do I export all of my projects to CSV?"
QUESTION_FIELD, IDENTITY_FIELD = names.ANSWER_REQUEST_FIELDS
ROUTER_LOGGER = "app.router.handler"


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    """Dummy credentials and no config, so nothing can find real credentials; no proxy for
    127.0.0.1."""
    for name in [name for name in os.environ if name.startswith("AWS_") or name == "BOTO_CONFIG"]:
        monkeypatch.delenv(name)
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing-secret",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "BOTO_CONFIG": os.devnull,
        "NO_PROXY": LOOPBACK,
        "no_proxy": LOOPBACK,
    }.items():
        monkeypatch.setenv(name, value)


class World(NamedTuple):
    router: handler.Router
    time: FakeTime
    dynamo: FakeDynamo
    lam: FakeLambda
    flagsmith: FakeFlagsmith
    lines: list[str]


def _world(*, pointer: dict | None = None, flagsmith=None, **router_kwargs) -> World:
    clock = FakeTime()
    dynamo = FakeDynamo(pointer)
    lam = FakeLambda(clock)
    fake = FakeFlagsmith()
    lines: list[str] = []
    router = handler.Router(
        dynamodb=dynamo,
        lambda_client=lam,
        flagsmith_factory=flagsmith or (lambda: fake),
        clock=clock.monotonic,
        timer=clock.perf_counter,
        wall=clock.time,
        emit=lines.append,
        **router_kwargs,
    )
    return World(router, clock, dynamo, lam, fake, lines)


@pytest.fixture
def active() -> World:
    """A launched run, RUN_ID; IDENTITY is assigned cheaper; the kill switch is on."""
    world = _world(pointer=pointer_item(RUN_ID))
    world.flagsmith.identity_flags = lambda identifier: flags_of(
        experiment_flag(FEATURE_KEY, names.VARIANT_CHEAPER), kill_switch(True)
    )
    return world


@pytest.fixture
def idle() -> World:
    """No pointer: no active run."""
    return _world(pointer=None)


def _event(body: object = None, *, raw: str | None = None, encoded: bool = False) -> dict:
    """An API Gateway HTTP API event (payload format 2.0) for POST /ask."""
    if body is None and raw is None:
        body = dict(zip(names.ANSWER_REQUEST_FIELDS, (QUESTION, IDENTITY), strict=True))
    text = raw if raw is not None else json.dumps(body)
    if encoded:
        text = base64.b64encode(text.encode()).decode()
    return {
        "version": "2.0",
        "routeKey": names.API_ROUTE_KEY,
        "rawPath": "/ask",
        "headers": {"content-type": "application/json"},
        "body": text,
        "isBase64Encoded": encoded,
    }


def _ask(world: World, identity: str = IDENTITY, question: str = QUESTION) -> tuple[int, dict]:
    body = dict(zip(names.ANSWER_REQUEST_FIELDS, (question, identity), strict=True))
    return _reply(world.router.handle(_event(body)))


def _reply(response: dict) -> tuple[int, dict]:
    assert response["headers"]["Content-Type"] == "application/json"
    return response["statusCode"], json.loads(response["body"])


def _metrics(world: World) -> list[dict]:
    return [json.loads(line) for line in world.lines]


def _latencies(world: World) -> list[tuple[str, float]]:
    metric = names.CW_METRIC_ANSWER_LATENCY_MS
    return [(doc[names.CW_DIMENSION_VARIANT], doc[metric]) for doc in _metrics(world)]


def _judge_payloads(world: World) -> list[dict]:
    return [invoke.payload for invoke in world.lam.judged()]


def _recorded_nothing(world: World) -> None:
    """No exposure, no event and no judge: what "records nothing" means for the router."""
    assert world.flagsmith.exposures == []
    assert world.flagsmith.events == []
    assert world.lam.judged() == []


# ---------------------------------------------------------------------------
# Routing: design.md section 4, steps 1 to 6
# ---------------------------------------------------------------------------
def test_an_identity_assigned_cheaper_with_the_kill_switch_on_is_served_cheaper(active):
    status, body = _ask(active)

    assert status == 200
    assert body == {names.ANSWER_RESPONSE_FIELDS[0]: "An answer from cheaper."}
    [answer] = active.lam.answers()
    assert answer.function == names.ANSWER_CHEAPER_FUNCTION
    assert answer.payload == {QUESTION_FIELD: QUESTION, IDENTITY_FIELD: IDENTITY}
    [judged] = _judge_payloads(active)
    assert (judged["assigned"], judged["served"]) == (names.VARIANT_CHEAPER, names.VARIANT_CHEAPER)
    assert _latencies(active) == [(names.VARIANT_CHEAPER, 1500.0)]


def test_kill_switch_off_serves_current_to_an_identity_assigned_cheaper(active):
    active.flagsmith.identity_flags = lambda identifier: flags_of(
        experiment_flag(FEATURE_KEY, names.VARIANT_CHEAPER), kill_switch(False)
    )
    status, _ = _ask(active)

    assert status == 200
    assert [a.function for a in active.lam.answers()] == [names.ANSWER_CURRENT_FUNCTION]
    [judged] = _judge_payloads(active)
    assert judged["assigned"] == names.VARIANT_CHEAPER  # what the experiment assigned
    assert judged["served"] == names.VARIANT_CURRENT  # what the kill switch let through
    assert judged["model_id"] == names.MODEL_CURRENT
    assert _latencies(active) == [(names.VARIANT_CURRENT, 1500.0)]
    # The exposure still says what the experiment assigned: flag.variant.
    assert [e["value"] for e in active.flagsmith.exposures] == [names.VARIANT_CHEAPER]


def test_a_missing_kill_switch_serves_current_like_a_disabled_one(active, caplog):
    active.flagsmith.identity_flags = lambda identifier: flags_of(
        experiment_flag(FEATURE_KEY, names.VARIANT_CHEAPER)
    )
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)
    status, _ = _ask(active)

    assert status == 200
    assert [a.function for a in active.lam.answers()] == [names.ANSWER_CURRENT_FUNCTION]
    assert names.KILL_SWITCH_FEATURE in caplog.text
    assert IDENTITY not in caplog.text


def test_the_control_arm_is_served_current_and_its_exposure_says_control(active):
    active.flagsmith.identity_flags = lambda identifier: flags_of(
        experiment_flag(FEATURE_KEY, names.VARIANT_CURRENT), kill_switch(True)
    )
    status, _ = _ask(active)

    assert status == 200
    assert [a.function for a in active.lam.answers()] == [names.ANSWER_CURRENT_FUNCTION]
    [judged] = _judge_payloads(active)
    assert (judged["assigned"], judged["served"]) == (names.VARIANT_CURRENT, names.VARIANT_CURRENT)
    # assigned is the flag's value; the exposure sends flag.variant, which reads control.
    assert [e["value"] for e in active.flagsmith.exposures] == [names.CONTROL_LABEL]


@pytest.mark.parametrize(
    "pointer",
    [None, pointer_item(RUN_ID, feature_key=None)],
    ids=["no pointer", "a pointer without feature_key"],
)
@pytest.mark.parametrize("default", [names.VARIANT_CURRENT, names.VARIANT_CHEAPER])
def test_with_no_active_run_the_default_model_is_served_and_nothing_is_recorded(pointer, default):
    world = _world(pointer=pointer)
    world.flagsmith.environment = flags_of(default_model(default))
    status, _ = _ask(world)

    assert status == 200
    assert [a.function for a in world.lam.answers()] == [names.ANSWER_FUNCTION_BY_VARIANT[default]]
    assert world.flagsmith.identity_requests == []
    _recorded_nothing(world)  # so no AnswerCostMicroUsd or AnswerHelpful either
    # The latency of every answer invoke is still measured.
    assert _latencies(world) == [(default, 1500.0)]


def test_with_no_active_run_rm_judge_is_not_invoked(idle):
    for _ in range(3):
        assert _ask(idle)[0] == 200
    assert idle.lam.judged() == []
    assert len(idle.lam.answers()) == 3


def test_the_default_model_is_read_from_the_environment_flags_and_cached_30_seconds(idle):
    idle.lam.answer_seconds = 0  # only the test moves the clock
    _ask(idle)
    idle.time.advance(names.DEFAULT_MODEL_CACHE_SECONDS - 0.1)
    _ask(idle)
    assert idle.flagsmith.environment_requests == 1
    idle.time.advance(0.1)
    _ask(idle)
    assert idle.flagsmith.environment_requests == 2


@pytest.mark.parametrize(
    "environment",
    [flags_of(), flags_of(default_model("turbo")), FlagsmithAPIError("down")],
    ids=["no such feature", "an odd value", "Flagsmith down"],
)
def test_without_a_usable_default_model_current_is_served(idle, caplog, environment):
    if isinstance(environment, Exception):

        def fail():
            raise environment

        idle.flagsmith.get_environment_flags = fail
    else:
        idle.flagsmith.environment = environment
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)
    status, _ = _ask(idle)

    assert status == 200
    assert [a.function for a in idle.lam.answers()] == [names.ANSWER_CURRENT_FUNCTION]
    assert names.DEFAULT_MODEL_FEATURE in caplog.text


def test_exactly_one_identity_request_per_call_and_never_get_experiment_flag(active):
    for _ in range(3):
        assert _ask(active)[0] == 200
    # FakeFlagsmith.get_experiment_flag fails the test if the router ever calls it.
    assert active.flagsmith.identity_requests == [IDENTITY] * 3
    assert active.flagsmith.environment_requests == 0


@pytest.mark.parametrize(
    ("flag_settings", "exposed"),
    [
        ({}, True),
        ({"in_experiment": False}, False),
        ({"in_experiment": None}, False),  # no experiment metadata at all
        ({"enabled": False}, False),
        ({"variant": None}, False),
        ({"variant": ""}, False),
    ],
    ids=[
        "in, enabled, variant",
        "not in",
        "no metadata",
        "disabled",
        "no variant",
        "empty variant",
    ],
)
def test_the_exposure_is_tracked_only_in_the_experiment_enabled_with_a_variant(
    active, flag_settings, exposed
):
    active.flagsmith.identity_flags = lambda identifier: flags_of(
        experiment_flag(FEATURE_KEY, names.VARIANT_CHEAPER, **flag_settings), kill_switch(True)
    )
    assert _ask(active)[0] == 200

    expected = {
        "feature_name": FEATURE_KEY,
        "identifier": IDENTITY,
        "value": names.VARIANT_CHEAPER,
        "metadata": {names.EXPOSURE_METADATA_KEY: EXPERIMENT_ID},
    }
    assert active.flagsmith.exposures == ([expected] if exposed else [])


def test_the_pointer_is_cached_30_seconds(active):
    active.lam.answer_seconds = 0  # only the test moves the clock
    _ask(active)
    active.time.advance(names.POINTER_CACHE_SECONDS - 0.1)
    _ask(active)
    assert len(active.dynamo.reads) == 1
    active.time.advance(0.1)
    _ask(active)
    assert len(active.dynamo.reads) == 2
    assert active.dynamo.reads[0] == {
        "TableName": names.RUNS_TABLE,
        "Key": {names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}},
    }


def test_a_cleared_pointer_still_counts_as_active_for_up_to_30_seconds(active):
    _ask(active)
    active.dynamo.item = None  # the run reached a terminal state
    _ask(active)
    assert len(active.flagsmith.identity_requests) == 2  # still the run's path, as accepted
    active.time.advance(names.POINTER_CACHE_SECONDS)
    _ask(active)
    assert len(active.flagsmith.identity_requests) == 2  # now no active run
    assert active.flagsmith.environment_requests == 1


@pytest.mark.parametrize(
    "identity",
    [
        names.run_identity(OTHER_RUN_ID, 7),
        f"{RUN_ID}1-user-001",  # another run whose ID starts with this one's
        IDENTITY + "\n",  # this run's, with a trailing newline that a $ would let through
    ],
    ids=["another run's", "a run ID this one's is a prefix of", "a trailing newline"],
)
def test_an_identity_that_is_not_the_active_runs_gets_400_and_no_flagsmith_call(active, identity):
    status, body = _ask(active, identity=identity)

    assert (status, body) == (400, {"error": handler.BAD_REQUEST})
    assert active.flagsmith.calls == []  # no identity request, nothing tracked or flushed
    assert active.lam.invokes == []
    assert active.lines == []


def test_flags_without_the_experiment_feature_serve_current_and_record_nothing(active, caplog):
    active.flagsmith.identity_flags = lambda identifier: flags_of(kill_switch(True))
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)
    status, body = _ask(active)

    assert status == 200
    assert body == {names.ANSWER_RESPONSE_FIELDS[0]: "An answer from current."}
    assert [a.function for a in active.lam.answers()] == [names.ANSWER_CURRENT_FUNCTION]
    _recorded_nothing(active)
    assert _latencies(active) == [(names.VARIANT_CURRENT, 1500.0)]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert FEATURE_KEY in warning.getMessage()
    assert IDENTITY not in warning.getMessage()
    assert "user-007" not in warning.getMessage()


def test_an_experiment_value_that_is_neither_variant_serves_current_and_records_nothing(
    active, caplog
):
    active.flagsmith.identity_flags = lambda identifier: flags_of(
        experiment_flag(FEATURE_KEY, "turbo", variant="turbo"), kill_switch(True)
    )
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)
    status, _ = _ask(active)

    assert status == 200
    assert [a.function for a in active.lam.answers()] == [names.ANSWER_CURRENT_FUNCTION]
    _recorded_nothing(active)
    assert IDENTITY not in caplog.text


# ---------------------------------------------------------------------------
# The answer invoke and its errors
# ---------------------------------------------------------------------------
def test_the_answer_is_invoked_synchronously_and_the_judge_asynchronously(active):
    _ask(active)

    [answer, judge] = active.lam.invokes
    assert (answer.function, answer.invocation_type) == (
        names.ANSWER_CHEAPER_FUNCTION,
        "RequestResponse",
    )
    assert (judge.function, judge.invocation_type) == (names.JUDGE_FUNCTION, "Event")
    validate("answer_request", answer.payload)


def test_the_judge_payload_is_a_judge_payload_with_assigned_from_the_flags_value(active):
    _ask(active)

    [payload] = _judge_payloads(active)
    validate("judge_payload", payload)
    assert set(payload) == set(names.JUDGE_PAYLOAD_FIELDS)
    assert payload == {
        "run_id": RUN_ID,
        "identity": IDENTITY,
        "assigned": names.VARIANT_CHEAPER,
        "served": names.VARIANT_CHEAPER,
        "model_id": names.MODEL_CHEAPER,
        "question": QUESTION,
        "answer": "An answer from cheaper.",
        "input_tokens": 51,
        "output_tokens": 197,
        "latency_ms": 1500.0,
    }
    # The latency the judge gets is the one the metric reports.
    assert _latencies(active) == [(names.VARIANT_CHEAPER, payload["latency_ms"])]


@pytest.mark.parametrize(
    ("error_type", "status"),
    [
        ("AnswerThrottled", 503),
        ("AnswerFailed", 502),
        ("Sandbox.Timedout", 502),  # any other function error
    ],
)
def test_an_answer_function_error_is_a_5xx_that_still_emits_latency(active, error_type, status):
    active.lam.reply = lambda function, request: function_error(error_type)
    got, body = _ask(active)

    assert (got, body) == (status, {"error": error_type})
    assert _latencies(active) == [(names.VARIANT_CHEAPER, 1500.0)]  # a slow failure counts
    assert active.lam.judged() == []  # nothing to judge, so no outcome is recorded
    assert active.flagsmith.events == []
    assert len(active.flagsmith.exposures) == 1  # already sent; the report's Known bias
    assert active.flagsmith.flushes == 1


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            {
                "errorType": "Sandbox.Timedout",
                "errorMessage": "RequestId: r Error: Task timed out after 25.00 seconds",
            },
            "Sandbox.Timedout",
        ),
        (
            {"errorMessage": "2026-11-27T02:00:00.000Z r Task timed out after 25.00 seconds"},
            handler.INVOKE_FAILED,
        ),
    ],
    ids=["errorType Sandbox.Timedout", "the older shape, no errorType"],
)
def test_an_answer_that_outlives_its_timeout_is_502_and_still_emits_latency(active, payload, error):
    """Lambda stops an answer function at its 25 s timeout and answers the invoke itself, so
    the errorType is Lambda's own, not AnswerFailed. As the lead decided, any errorType but
    AnswerThrottled is a 502, and the latency of that slow failure still reaches the alarm."""
    active.lam.answer_seconds = names.ANSWER_TIMEOUT_SECONDS
    timed_out = {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(json.dumps(payload).encode()),
    }
    active.lam.reply = lambda function, request: timed_out

    assert _ask(active) == (502, {"error": error})
    assert _latencies(active) == [(names.VARIANT_CHEAPER, names.ANSWER_TIMEOUT_SECONDS * 1000)]
    assert active.lam.judged() == []


def test_a_function_error_with_no_error_type_is_502_invoke_failed(active):
    broken = {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": io.BytesIO(b"not json")}
    active.lam.reply = lambda function, request: broken
    assert _ask(active) == (502, {"error": handler.INVOKE_FAILED})
    assert _latencies(active) == [(names.VARIANT_CHEAPER, 1500.0)]


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "TooManyRequestsException"}}, "Invoke"),
        ReadTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com/x"),
    ],
    ids=["throttled invoke", "read timeout"],
)
def test_a_failed_invoke_is_502_invoke_failed_and_still_emits_latency(active, error):
    active.lam.reply = lambda function, request: error
    assert _ask(active) == (502, {"error": handler.INVOKE_FAILED})
    assert _latencies(active) == [(names.VARIANT_CHEAPER, 1500.0)]
    assert active.lam.judged() == []


@pytest.mark.parametrize(
    "answer",
    [
        {"answer": "x", "input_tokens": 1, "output_tokens": 2},  # a field missing
        {"answer": "", "input_tokens": 1, "output_tokens": 2, "model_id": names.MODEL_CHEAPER},
        {"answer": "x", "input_tokens": -1, "output_tokens": 2, "model_id": names.MODEL_CHEAPER},
        {"answer": "x", "input_tokens": 1, "output_tokens": 2, "model_id": names.MODEL_CURRENT},
        ["not", "an", "object"],
    ],
    ids=["missing field", "empty answer", "negative tokens", "the other arm's model", "a list"],
)
def test_a_reply_that_is_not_the_served_arms_answer_response_is_502(active, answer):
    active.lam.reply = lambda function, request: answer
    assert _ask(active) == (502, {"error": handler.INVOKE_FAILED})
    assert active.lam.judged() == []


def test_a_failed_handoff_to_the_judge_still_returns_the_answer(active, caplog):
    active.lam.judge_error = ClientError({"Error": {"Code": "ServiceException"}}, "Invoke")
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)

    assert _ask(active)[0] == 200
    assert names.JUDGE_FUNCTION in caplog.text
    assert IDENTITY not in caplog.text and QUESTION not in caplog.text


# ---------------------------------------------------------------------------
# The metric
# ---------------------------------------------------------------------------
def test_every_metric_carries_exactly_the_variant_dimension_set(active):
    _ask(active)
    active.flagsmith.identity_flags = lambda identifier: flags_of(
        experiment_flag(FEATURE_KEY, names.VARIANT_CHEAPER), kill_switch(False)
    )
    _ask(active)

    metric = names.CW_METRIC_ANSWER_LATENCY_MS
    documents = _metrics(active)
    assert len(documents) == 2
    for document in documents:
        # The embedded metric format (docs.aws.amazon.com/AmazonCloudWatch/latest/
        # monitoring/CloudWatch_Embedded_Metric_Format_Specification.html).
        assert set(document) == {"_aws", names.CW_DIMENSION_VARIANT, metric}
        directive = document["_aws"]
        assert isinstance(directive["Timestamp"], int)
        assert directive["CloudWatchMetrics"] == [
            {
                "Namespace": names.CW_NAMESPACE,
                "Dimensions": names.CW_DIMENSION_SETS,
                "Metrics": [{"Name": metric, "Unit": names.CW_METRIC_UNITS[metric]}],
            }
        ]
    assert [doc[names.CW_DIMENSION_VARIANT] for doc in documents] == list(names.VARIANTS[::-1])


def test_the_metric_timestamp_is_the_wall_clock_in_milliseconds(active):
    _ask(active)
    [document] = _metrics(active)
    assert document["_aws"]["Timestamp"] == int(active.time.time() * 1000)


# ---------------------------------------------------------------------------
# Flushing, bad input, and failures before the answer
# ---------------------------------------------------------------------------
def test_flush_events_runs_before_every_return(active):
    def thrown(function, request):
        return function_error("AnswerFailed")

    _ask(active)  # served
    assert active.flagsmith.calls[-1] == "flush"
    active.router.handle({"body": "not json"})  # bad input, once the client exists
    _ask(active, identity=names.run_identity(OTHER_RUN_ID, 1))  # another run's identity
    active.lam.reply = thrown
    _ask(active)  # an answer error
    active.flagsmith.identity_flags = lambda identifier: flags_of(kill_switch(True))
    _ask(active)  # no experiment feature

    assert active.flagsmith.flushes == 5
    assert active.flagsmith.calls.count("exposure") == 2  # each one before its flush
    assert active.flagsmith.calls[-1] == "flush"


class LambdaContext:
    """The part of Lambda's context the router reads: the time it has left."""

    def __init__(self, remaining_ms: int) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


def test_the_flush_join_is_bounded_by_the_time_lambda_has_left(flagsmith_api, caplog):
    """A flush that cannot finish in time is left running: the client gets its answer, and a
    late flush loses at most this request's exposure (contract change c3)."""
    flagsmith_api.hold_events = threading.Event()  # the flush can only time out, 9 s or more
    world = _world(pointer=pointer_item(RUN_ID), flagsmith=flagsmith_api.real_client)
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)

    started = time.perf_counter()
    response = world.router.handle(_event(), LambdaContext(remaining_ms=2500))
    elapsed = time.perf_counter() - started

    assert _reply(response)[0] == 200
    assert 1.5 <= elapsed <= 1.5 + 1.0  # 2.5 s left, less 1 s, then the join gives up
    # How long the join waited, and the time that leaves, not the time Lambda had left.
    assert "the flush was still running after 1.5 s, with about 1 s of Lambda's" in caplog.text
    assert IDENTITY not in caplog.text


def test_without_a_thread_the_answer_returns_at_once_and_nothing_flushes_in_line(
    active, monkeypatch, caplog
):
    """The SDK's flush has no timeout, so an in-line one could outlast Lambda's time. The
    events wait for the SDK's own timer or the next flush instead."""

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    _ask(active)  # the client exists from here on, and its one flush is done
    monkeypatch.setattr(handler.threading.Thread, "start", no_thread)
    active.flagsmith.flush_seconds = 2.0  # an in-line flush would hold the reply this long
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)

    started = time.perf_counter()
    status, _ = _ask(active)
    elapsed = time.perf_counter() - started

    assert status == 200
    assert elapsed < 1.0
    assert active.flagsmith.calls.count("flush") == active.flagsmith.flushes == 1  # the first
    assert "leaving the events to the SDK's timer" in caplog.text
    # One warning: the finish does not try again after step 6's start failed.
    assert caplog.text.count("cannot start the flush thread") == 1


def test_a_failing_flush_is_logged_and_the_answer_still_returned(active, caplog):
    active.flagsmith.flush_error = RuntimeError("the events processor broke")
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)

    assert _ask(active) == (200, {names.ANSWER_RESPONSE_FIELDS[0]: "An answer from cheaper."})
    assert "flush_events failed (RuntimeError)" in caplog.text


@pytest.mark.parametrize(
    "reply",
    [
        function_error("AnswerFailed"),
        function_error("AnswerThrottled"),
        ClientError({"Error": {"Code": "ServiceException"}}, "Invoke"),
    ],
    ids=["502", "503", "an invoke exception"],
)
def test_on_an_answer_error_the_flush_is_joined_before_the_return(active, reply):
    active.flagsmith.flush_seconds = 0.3  # the flush thread outlasts the failed answer
    active.lam.reply = lambda function, request: reply

    status, _ = _ask(active)
    returned = time.monotonic()

    assert status in (502, 503)
    assert active.flagsmith.flushes == 1
    assert active.flagsmith.flush_finished_at <= returned  # joined, not left running


def test_before_the_first_flagsmith_call_there_is_nothing_to_flush(active):
    active.router.handle({"body": "not json"})
    assert active.flagsmith.calls == []


@pytest.mark.parametrize(
    "event",
    [
        _event(raw="not json"),
        _event(raw=""),
        _event(["a", "list"]),
        _event({QUESTION_FIELD: QUESTION}),
        _event({QUESTION_FIELD: QUESTION, IDENTITY_FIELD: IDENTITY, "extra": 1}),
        _event({QUESTION_FIELD: "", IDENTITY_FIELD: IDENTITY}),
        _event({QUESTION_FIELD: QUESTION, IDENTITY_FIELD: names.identity(7)}),
        _event({QUESTION_FIELD: QUESTION, IDENTITY_FIELD: f"{RUN_ID}-user-151"}),
        _event(raw="!!!", encoded=False) | {"isBase64Encoded": True},
        {"version": "2.0"},  # no body
        "not an event",
    ],
)
def test_bad_input_is_400_bad_request_before_anything_else(active, event):
    assert _reply(active.router.handle(event)) == (400, {"error": handler.BAD_REQUEST})
    assert active.dynamo.reads == []
    assert active.flagsmith.calls == []
    assert active.lam.invokes == []


def test_a_base64_encoded_body_is_read(active):
    body = dict(zip(names.ANSWER_REQUEST_FIELDS, (QUESTION, IDENTITY), strict=True))
    status, _ = _reply(active.router.handle(_event(body, encoded=True)))
    assert status == 200


QUESTION_SAMPLES = [
    {QUESTION_FIELD: QUESTION, IDENTITY_FIELD: IDENTITY},
    {QUESTION_FIELD: names.PROBE_QUESTION, IDENTITY_FIELD: names.PROBE_IDENTITY},
    None,
    [],
    {},
    {QUESTION_FIELD: QUESTION},
    {QUESTION_FIELD: 7, IDENTITY_FIELD: IDENTITY},
    {QUESTION_FIELD: QUESTION, IDENTITY_FIELD: 7},
    {QUESTION_FIELD: QUESTION, IDENTITY_FIELD: names.identity(7)},
    {QUESTION_FIELD: QUESTION, IDENTITY_FIELD: IDENTITY, "extra": 1},
]


@pytest.mark.parametrize("body", QUESTION_SAMPLES)
def test_the_routers_request_check_agrees_with_answer_request_json(body):
    try:
        validate("answer_request", body)
        schema_accepts = True
    except ValidationError:
        schema_accepts = False
    assert (handler.request_problem(body) is None) == schema_accepts


def test_a_failed_pointer_read_is_502_upstream_failed_with_no_flagsmith_call(active):
    active.dynamo.error = ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
    assert _ask(active) == (502, {"error": handler.UPSTREAM_FAILED})
    assert active.flagsmith.calls == []
    assert active.lam.invokes == []


def test_a_failed_identity_request_is_502_upstream_failed_and_nothing_is_answered(active):
    active.flagsmith.identity_error = FlagsmithAPIError("Unable to get valid response")
    assert _ask(active) == (502, {"error": handler.UPSTREAM_FAILED})
    assert active.lam.invokes == []
    assert active.flagsmith.flushes == 1


def _no_client():
    raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")


def test_no_flagsmith_client_with_an_active_run_is_502_upstream_failed():
    world = _world(pointer=pointer_item(RUN_ID), flagsmith=_no_client)
    assert _ask(world) == (502, {"error": handler.UPSTREAM_FAILED})
    assert world.lam.invokes == []


def test_no_flagsmith_client_with_no_active_run_serves_current(caplog):
    """No run data is at stake, so the default's default is served (the lead, for c3)."""
    world = _world(pointer=None, flagsmith=_no_client)
    caplog.set_level(logging.WARNING, logger=ROUTER_LOGGER)

    assert _ask(world) == (200, {names.ANSWER_RESPONSE_FIELDS[0]: "An answer from current."})
    assert [a.function for a in world.lam.answers()] == [names.ANSWER_CURRENT_FUNCTION]
    assert names.DEFAULT_MODEL_FEATURE in caplog.text


def test_a_callers_keys_never_reach_the_log(active, caplog):
    """An unexpected key can carry an identity or a question, so the warning names only the
    missing fields and counts the rest."""
    leaked_identity, leaked_question = names.run_identity(RUN_ID, 99), "Who else asked?"
    body = {QUESTION_FIELD: QUESTION, IDENTITY_FIELD: IDENTITY, leaked_identity: leaked_question}
    caplog.set_level(logging.DEBUG, logger=ROUTER_LOGGER)

    assert _reply(active.router.handle(_event(body))) == (400, {"error": handler.BAD_REQUEST})
    for text in (leaked_identity, leaked_question, IDENTITY, QUESTION):
        assert text not in caplog.text
    assert "1 unexpected field(s)" in caplog.text


def test_the_throttled_error_type_is_the_answer_functions_class_name():
    assert handler.THROTTLED == answer_handler.AnswerThrottled.__name__


def test_a_pointer_whose_run_id_is_not_a_run_id_is_502_upstream_failed():
    world = _world(pointer=pointer_item("not a run", feature_key="assistant_model_x"))
    assert _ask(world) == (502, {"error": handler.UPSTREAM_FAILED})
    assert world.lam.invokes == []


# ---------------------------------------------------------------------------
# The real AWS clients, through botocore's Stubber, and the module's wiring
# ---------------------------------------------------------------------------
def test_the_invokes_are_valid_lambda_requests(active):
    client = handler.new_lambda_client()
    stubber = Stubber(client)
    answer = json.dumps(
        {
            "answer": "Stubbed.",
            "input_tokens": 3,
            "output_tokens": 4,
            "model_id": names.MODEL_CHEAPER,
        }
    ).encode()
    request = {QUESTION_FIELD: QUESTION, IDENTITY_FIELD: IDENTITY}
    stubber.add_response(
        "invoke",
        {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(answer), len(answer))},
        expected_params={
            "FunctionName": names.ANSWER_CHEAPER_FUNCTION,
            "InvocationType": "RequestResponse",
            "Payload": json.dumps(request).encode(),
        },
    )
    stubber.add_response("invoke", {"StatusCode": 202})  # the judge's; its payload is above
    active.router.lambda_client = client
    with stubber:
        assert _ask(active) == (200, {names.ANSWER_RESPONSE_FIELDS[0]: "Stubbed."})
        stubber.assert_no_pending_responses()


def test_the_clients_are_in_the_contract_region_and_an_invoke_is_one_attempt():
    lam, dynamo = handler.new_lambda_client(), handler.new_dynamodb_client()  # built only
    assert lam.meta.region_name == dynamo.meta.region_name == names.REGION
    assert lam.meta.config.retries["total_max_attempts"] == 1


def test_the_flagsmith_client_is_built_from_the_server_key_with_events_on(monkeypatch, caplog):
    secrets = boto3.client("secretsmanager", region_name=names.REGION)
    stubber = Stubber(secrets)
    stubber.add_response(
        "get_secret_value",
        {"SecretString": DUMMY_SDK_KEY},
        expected_params={"SecretId": names.SECRET_FLAGSMITH_SERVER_KEY},
    )
    monkeypatch.setattr(handler.boto3, "client", lambda service, **kwargs: secrets)
    built = []
    monkeypatch.setattr(handler, "Flagsmith", lambda **kwargs: built.append(kwargs) or "client")
    caplog.set_level(logging.DEBUG)

    with stubber:
        assert handler.new_flagsmith_client() == "client"
    [kwargs] = built
    retries = kwargs.pop("retries")
    # The contract's limits (design.md section 5), and the literals they stand for.
    assert kwargs == {
        "environment_key": DUMMY_SDK_KEY,
        **names.FLAGSMITH_CLIENT_OPTIONS,
        "request_timeout_seconds": names.FLAGSMITH_SDK_REQUEST_TIMEOUT_SECONDS,
    }
    assert (retries.total, retries.backoff_factor) == (names.FLAGSMITH_SDK_RETRIES, 0)
    assert (names.FLAGSMITH_SDK_REQUEST_TIMEOUT_SECONDS, names.FLAGSMITH_SDK_RETRIES) == (3, 1)
    assert DUMMY_SDK_KEY not in caplog.text


def test_the_lambda_entry_point_uses_one_router_per_container(monkeypatch):
    made = []

    class Spy:
        def handle(self, event, context=None):
            return {"statusCode": 200, "event": event, "context": context}

    monkeypatch.setattr(handler, "_router", None)
    monkeypatch.setattr(handler, "Router", lambda **kwargs: made.append(kwargs) or Spy())
    monkeypatch.setattr(handler, "new_dynamodb_client", lambda: "dynamodb")
    monkeypatch.setattr(handler, "new_lambda_client", lambda: "lambda")
    context = LambdaContext(remaining_ms=29_000)

    assert handler.handler({"n": 1}, context) == {
        "statusCode": 200,
        "event": {"n": 1},
        "context": context,
    }
    assert handler.handler({"n": 2}, None)["event"] == {"n": 2}
    assert len(made) == 1
    assert made[0]["flagsmith_factory"] is handler.new_flagsmith_client


def test_the_router_loads_without_jsonschema():
    probe = "import sys, app.router.handler; print('jsonschema' in sys.modules)"
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# The real Flagsmith SDK against a local Flagsmith API
# ---------------------------------------------------------------------------
@pytest.fixture
def flagsmith_api():
    api = FakeFlagsmithApi().start()
    api.flags_for = lambda identifier: [
        api_flag(
            FEATURE_KEY,
            enabled=True,
            value=names.VARIANT_CHEAPER,
            variant=names.VARIANT_CHEAPER,
            experiment={"id": EXPERIMENT_ID, "name": "router test", "in_experiment": True},
        ),
        api_flag(names.KILL_SWITCH_FEATURE, enabled=True, value=None),
    ]
    clients: list[Flagsmith] = []

    def real_client() -> Flagsmith:
        """The client the router builds in production, pointed at this server."""
        client = handler.flagsmith_client(
            DUMMY_SDK_KEY,
            api_url=api.api_url,
            event_processor_config=EventProcessorConfig(events_api_url=api.events_url),
        )
        clients.append(client)
        return client

    api.real_client = real_client
    api.clients = clients
    yield api
    api.release()  # later sends are answered at once, and the held ones let go
    for client in clients:
        client.close()
    api.stop()


def test_with_the_real_sdk_one_identity_request_and_the_exposure_reach_flagsmith(flagsmith_api):
    world = _world(pointer=pointer_item(RUN_ID), flagsmith=flagsmith_api.real_client)
    assert _ask(world)[0] == 200

    assert [body["identifier"] for body in flagsmith_api.identity_bodies] == [IDENTITY]
    # Flushed before the router returned: the exposure is already at the events API.
    [exposure] = flagsmith_api.events()
    assert exposure["event"] == "$flag_exposure"
    assert exposure["feature_name"] == FEATURE_KEY
    assert exposure["identifier"] == IDENTITY
    assert exposure["value"] == names.VARIANT_CHEAPER
    assert exposure["metadata"][names.EXPOSURE_METADATA_KEY] == EXPERIMENT_ID
    assert [a.function for a in world.lam.answers()] == [names.ANSWER_CHEAPER_FUNCTION]


# A faulted cheaper answer: the 10 s fault plus about 3 s of Sonnet 5 (1.2's one sample: 2.8 s).
FAULTED_ANSWER_SECONDS = names.FIS_DELAY_MS / 1000 + 3


def test_the_flush_runs_alongside_the_answer_and_is_joined_before_the_return(
    flagsmith_api, record_property, capsys
):
    """Contract change c3, option (a). The flush starts right after the exposure, runs
    while the answer does, and is joined before the router returns.

    With a silent events API and a faulted 13 s answer, the request takes about the longer
    of the two: the identity request, plus 13 s, plus a margin. It does not take their sum.
    The identity request answers at once here; the 3 s allowed for it below is the read
    timeout, not design.md section 5's worst case of about 6 s (9 with a retried connect)."""
    flagsmith_api.hold_events = threading.Event()
    times: dict[str, float] = {}

    def watched_client():
        client = flagsmith_api.real_client()
        real_flush = client.flush_events

        def flush_events():
            times["flush_started"] = time.monotonic()
            real_flush()
            times["flush_finished"] = time.monotonic()

        client.flush_events = flush_events
        return client

    world = _world(pointer=pointer_item(RUN_ID), flagsmith=watched_client)
    world.lam.real_seconds = FAULTED_ANSWER_SECONDS

    started = time.perf_counter()
    status, _ = _ask(world)
    returned = time.monotonic()
    elapsed = time.perf_counter() - started

    assert status == 200
    assert FAULTED_ANSWER_SECONDS <= elapsed <= 3 + FAULTED_ANSWER_SECONDS + 1.5
    # Alongside: the flush began before the answer ended, and ended before the return.
    assert times["flush_started"] < world.lam.answer_finished_at
    assert times["flush_finished"] <= returned
    # Every one of the flush's attempts, before the return. The SDK's own 10 s timer may add a
    # send of the kept batch while the answer still runs, independent of the router.
    assert flagsmith_api.event_posts >= EVENTS_MAX_ATTEMPTS
    # One after the other they would have taken at least the answer plus three 3 s attempts.
    sequential_at_least = FAULTED_ANSWER_SECONDS + EVENTS_MAX_ATTEMPTS * (
        EVENTS_REQUEST_TIMEOUT_SECONDS
    )
    assert elapsed < sequential_at_least
    record_property("request_seconds_with_a_13_s_answer_and_a_silent_events_api", round(elapsed, 2))
    with capsys.disabled():
        print(
            f"\n13 s answer and a silent events API: {elapsed:.2f} s in all; the flush took"
            f" {times['flush_finished'] - times['flush_started']:.2f} s alongside it"
            f" (one after the other: at least {sequential_at_least:.0f} s)"
        )


def test_with_c3s_limits_a_silent_identity_request_gives_up_after_one_3_second_attempt(
    flagsmith_api, record_property, capsys
):
    """c3's request_timeout_seconds reaches the identity request. urllib3 never retries a
    POST after a read timeout, so Retry(total=1) adds no second try here; it retries only
    a failed connect. So a Flagsmith that accepts at once and never answers costs one 3 s
    read. requests applies the same 3 s to the connect too, so a slow connect could add
    up to 3 s more, and a retried connect 3 s more again: about 6 s, or about 9
    (design.md section 5)."""
    flagsmith_api.hold_identities = threading.Event()
    world = _world(pointer=pointer_item(RUN_ID), flagsmith=flagsmith_api.real_client)

    started = time.perf_counter()
    assert _ask(world) == (502, {"error": handler.UPSTREAM_FAILED})
    elapsed = time.perf_counter() - started

    assert len(flagsmith_api.identity_bodies) == 1
    assert 3 <= elapsed <= 4  # the 3 s read timeout, plus a little overhead
    assert world.lam.invokes == []  # nothing was answered
    record_property("identity_request_seconds", round(elapsed, 2))
    with capsys.disabled():
        print(f"\nidentity request with a silent Flagsmith, c3's limits: {elapsed:.2f} s, 1 try")


def test_with_a_slow_events_api_the_flush_takes_about_twelve_seconds(
    flagsmith_api, record_property, capsys
):
    """Records how long flush_events() takes when the events API never answers, as task 3.2
    asks. Every attempt then hits the SDK's own timeout.

    c3's limits do not reach the flush. The SDK's events processor keeps its own session and
    posts with its own Final constants, a 3 s timeout and 3 attempts, whatever the client
    was given (flagsmith 6.7.0, analytics.py)."""
    flagsmith_api.hold_events = threading.Event()
    world = _world(pointer=pointer_item(RUN_ID), flagsmith=flagsmith_api.real_client)

    started = time.perf_counter()
    status, _ = _ask(world)
    elapsed = time.perf_counter() - started

    assert status == 200  # the answer still comes back once the flush gives up
    [client] = flagsmith_api.clients
    assert client.request_timeout_seconds == 3  # c3's limit, on the client
    assert client.session.get_adapter(flagsmith_api.api_url).max_retries.total == 1
    assert flagsmith_api.event_posts == EVENTS_MAX_ATTEMPTS == 3  # not 2: retries ignored
    assert flagsmith_api.events() == []  # kept for the next send, never delivered
    # Three attempts of 3 s each, with random backoffs of up to 1 s and then 2 s between them
    # (flagsmith/analytics.py), so between 9 s and 12 s, plus a little overhead.
    shortest = EVENTS_MAX_ATTEMPTS * EVENTS_REQUEST_TIMEOUT_SECONDS
    longest = shortest + sum(
        EVENTS_RETRY_BASE_SECONDS * 2**n for n in range(EVENTS_MAX_ATTEMPTS - 1)
    )
    assert shortest <= elapsed <= longest + 1.5
    # The budget (design.md section 5). One send takes at most about 12 s, as here. A flush
    # queued behind a send already in flight (the SDK's own 10 s timer) waits for it and then
    # sends again, about 24 s in all. So the flush runs alongside the answer, and its join is
    # bounded by the time Lambda has left. The router's 29 s, less the identity request's
    # about 6 s (requests applies the 3 s to the connect and to the read), leaves about
    # 23 s, which one send fits inside. An answer that runs to its own 25 s timeout may meet
    # the router's limit; design.md section 5 accepts that.
    identity_request_seconds = 2 * names.FLAGSMITH_SDK_REQUEST_TIMEOUT_SECONDS
    for_answer_or_flush = names.ROUTER_TIMEOUT_SECONDS - identity_request_seconds
    assert longest < for_answer_or_flush < names.ANSWER_TIMEOUT_SECONDS
    record_property("flush_seconds", round(elapsed, 2))
    with capsys.disabled():
        print(
            f"\nflush with a silent events API: {elapsed:.2f} s (at most {longest:.0f} s);"
            f" alongside the answer, which has up to {for_answer_or_flush} s"
        )
