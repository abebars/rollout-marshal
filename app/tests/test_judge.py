"""Tests for app/judge, rm-judge (task 3.3).

Bedrock and Flagsmith are never called. Each test gives the judge botocore's Stubber on a
real bedrock-runtime client, which checks the request against the service model, or a fake
client, and a fake Flagsmith client or the real SDK pointed at an events API on 127.0.0.1.
Without one the judge's clients are traps that fail the test. The AWS environment is
replaced with dummy credentials and no config or credentials files.

The expected judge request is the one task 1.2 recorded in evidence/1.2-converse.json, with
only the question and the answer swapped in.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import boto3
import botocore.session
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.stub import Stubber
from flagsmith import EventProcessorConfig
from jsonschema import ValidationError

from app.judge import handler
from contracts import names, validate

ROOT = Path(__file__).resolve().parents[2]
RECORDED_JUDGE_CALL = {
    call["name"]: call
    for call in json.loads((ROOT / "evidence/1.2-converse.json").read_text())["calls"]
}["judge_thinking_disabled"]
PRICES = json.loads((ROOT / names.PRICES_FILE).read_text())
PRICING_PAGE = "https://aws.amazon.com/bedrock/pricing/"  # the lead's decision for 3.3

RUN_ID = "rjudgetest1200"
OTHER_RUN_ID = "rjudgetest1201"
IDENTITY = names.run_identity(RUN_ID, 7)
OTHER_IDENTITY = names.run_identity(OTHER_RUN_ID, 7)
QUESTION = "How do I export all of my projects to CSV?"
ANSWER = "Open Settings, choose Export, pick CSV, then select Download."
LATENCY_MS = 1234.5
HELPFUL, NOT_HELPFUL = '{"helpful": true}', '{"helpful": false}'
LEAKED_KEY = names.run_identity(RUN_ID, 99)  # a caller's key that holds an identity
LEAKED_TEXT = "Who else asked about exports?"
REQUEST_ID = "8ea123e4-1db7-4aca-ad10-d9ca1234c1fd"  # Lambda's, the same on each retry
WALL = 1_764_201_600.25
ACCESS_KEY = "testing"
SECRET_KEY = "testing-secret"
DUMMY_SDK_KEY = "dummy-judge-test-environment-key"
JUDGE_LOGGER = "app.judge.handler"
REAL_NEW_BEDROCK_CLIENT = handler.new_bedrock_client  # before any test replaces it
REAL_NEW_FLAGSMITH_CLIENT = handler.new_flagsmith_client
DEFAULT = object()  # World.handle()'s payload when a test gives none; None is a payload


def _payload(served: str = names.VARIANT_CHEAPER, *, assigned: str | None = None, **changes):
    """A judge_payload for one answer of 1000 input and 200 output tokens."""
    values = (
        RUN_ID,
        IDENTITY,
        assigned or served,
        served,
        names.MODEL_BY_VARIANT[served],
        QUESTION,
        ANSWER,
        1000,
        200,
        LATENCY_MS,
    )
    payload = dict(zip(names.JUDGE_PAYLOAD_FIELDS, values, strict=True))
    for field, value in changes.items():
        assert field in payload, field
        payload[field] = value
    return payload


def _without(field: str) -> dict:
    payload = _payload()
    del payload[field]
    return payload


def _reply(text: str = HELPFUL, *, stop_reason: str = "end_turn", content: list | None = None):
    """A Converse response as Bedrock shapes it."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}] if content is None else content,
            }
        },
        "stopReason": stop_reason,
        "usage": {"inputTokens": 91, "outputTokens": 10, "totalTokens": 101},
        "metrics": {"latencyMs": 524},
    }


def _expected_request(question: str = QUESTION, answer: str = ANSWER) -> dict:
    """1.2's recorded judge request, with this question and answer in place of its own."""
    request = copy.deepcopy(RECORDED_JUDGE_CALL["request"])
    request["messages"][0]["content"][0]["text"] = f"Question: {question}\nAnswer: {answer}"
    return request


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"the service says something about {QUESTION}"},
            "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": "bedrock-request-1"},
        },
        "Converse",
    )


def _clean(text: str) -> None:
    """Nothing of the caller's, and no key, in text."""
    for private in (
        QUESTION,
        ANSWER,
        IDENTITY,
        OTHER_IDENTITY,
        LEAKED_KEY,
        LEAKED_TEXT,
        ACCESS_KEY,
        SECRET_KEY,
        DUMMY_SDK_KEY,
    ):
        assert private not in text


class FakeBedrock:
    """A bedrock-runtime stand-in that records each call and returns or raises outcome."""

    def __init__(self, outcome=None) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def converse(self, **params):
        self.calls.append(params)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FakeFlagsmith:
    """The two SDK calls rm-judge makes, recorded in order in a log it may share."""

    def __init__(self, log: list | None = None) -> None:
        self.log = [] if log is None else log
        self.flush_error: Exception | None = None

    def track_event(self, event, identifier=None, value=None, traits=None, metadata=None):
        self.log.append(("track", event, identifier, value))

    def flush_events(self):
        self.log.append(("flush",))
        if self.flush_error is not None:
            raise self.flush_error

    @property
    def tracked(self) -> list[tuple]:
        return [entry[1:] for entry in self.log if entry[0] == "track"]

    @property
    def flushes(self) -> int:
        return sum(1 for entry in self.log if entry[0] == "flush")


class World:
    """A Judge with fakes, recording what it built, wrote and tracked, in order."""

    def __init__(self, reply: str = HELPFUL, *, bedrock=None, flagsmith_factory=None) -> None:
        self.log: list[tuple] = []
        self.lines: list[str] = []
        self.built: list[str] = []
        self.flagsmith = FakeFlagsmith(self.log)
        self.bedrock = FakeBedrock(_reply(reply)) if bedrock is None else bedrock
        self.flagsmith_error: Exception | None = None
        self.emit_error: Exception | None = None
        self.prices_loader = handler.load_prices
        self._flagsmith_factory = flagsmith_factory
        self.judge = handler.Judge(
            bedrock_factory=self._new_bedrock,
            flagsmith_factory=self._new_flagsmith,
            prices_loader=lambda: self.prices_loader(),
            emit=self._emit,
            wall=lambda: WALL,
        )

    def _new_bedrock(self):
        self.built.append("bedrock")
        return self.bedrock

    def _new_flagsmith(self):
        self.built.append("flagsmith")
        if self.flagsmith_error is not None:
            raise self.flagsmith_error
        return self._flagsmith_factory() if self._flagsmith_factory else self.flagsmith

    def _emit(self, line: str) -> None:
        if self.emit_error is not None:
            raise self.emit_error
        self.lines.append(line)
        self.log.append(("cloudwatch",))

    def handle(self, payload=DEFAULT, request_id: str | None = REQUEST_ID):
        payload = _payload() if payload is DEFAULT else payload
        return self.judge.handle(payload, request_id=request_id)

    @property
    def documents(self) -> list[dict]:
        return [json.loads(line) for line in self.lines]

    def nothing_written(self) -> bool:
        return self.lines == [] and self.flagsmith.tracked == []


@pytest.fixture(autouse=True)
def _dummy_aws_and_no_real_clients(monkeypatch):
    for name in [name for name in os.environ if name.startswith("AWS_") or name == "BOTO_CONFIG"]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BOTO_CONFIG", os.devnull)

    def trap(*args, **kwargs):
        raise AssertionError("a test reached for a real client: give the judge a fake")

    monkeypatch.setattr(handler, "new_bedrock_client", trap)
    monkeypatch.setattr(handler, "new_flagsmith_client", trap)


@pytest.fixture
def stubbed():
    """botocore's Stubber on a real bedrock-runtime client."""
    client = boto3.client(names.BEDROCK_RUNTIME_SERVICE, region_name=names.REGION)
    with Stubber(client) as stubber:
        yield SimpleNamespace(client=client, stubber=stubber)
        stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# app/prices.json
# ---------------------------------------------------------------------------
def test_prices_json_has_the_shape_the_lead_set():
    assert set(PRICES) == {"source_url", "retrieved_on", "currency", "per_million_tokens"}
    assert PRICES["currency"] == "USD"
    # One entry per answer model plus the judge's, keyed by the exact us. profile IDs.
    assert set(PRICES["per_million_tokens"]) == {
        *names.MODEL_BY_VARIANT.values(),
        names.MODEL_JUDGE,
    }
    for model_id, price in PRICES["per_million_tokens"].items():
        assert model_id.startswith("us."), model_id
        assert set(price) == {"input", "output"}
        for value in price.values():
            assert type(value) is float and value > 0


def test_prices_json_names_the_pricing_page_and_the_day_it_was_read():
    assert PRICES["source_url"] == PRICING_PAGE
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", PRICES["retrieved_on"])
    assert date.fromisoformat(PRICES["retrieved_on"]) <= datetime.now(UTC).date()


def test_load_prices_reads_each_models_price_per_million_tokens():
    prices = handler.load_prices()

    assert set(prices) == set(PRICES["per_million_tokens"])
    for model_id, price in PRICES["per_million_tokens"].items():
        assert prices[model_id] == handler.Price(input=price["input"], output=price["output"])


def _broken(change) -> dict:
    document = copy.deepcopy(PRICES)
    change(document)
    return document


BROKEN_PRICES = {
    "a model missing": _broken(lambda d: d["per_million_tokens"].pop(names.MODEL_CHEAPER)),
    "a model extra": _broken(
        lambda d: d["per_million_tokens"].update(
            {"us.anthropic.claude-x": {"input": 1.0, "output": 2.0}}
        )
    ),
    "a bool price": _broken(
        lambda d: d["per_million_tokens"][names.MODEL_CURRENT].update(input=True)
    ),
    "a zero price": _broken(
        lambda d: d["per_million_tokens"][names.MODEL_CURRENT].update(output=0.0)
    ),
    "a string price": _broken(
        lambda d: d["per_million_tokens"][names.MODEL_JUDGE].update(input="0.11")
    ),
    "a price field extra": _broken(
        lambda d: d["per_million_tokens"][names.MODEL_CHEAPER].update(batch_input=1.1)
    ),
    "no source": _broken(lambda d: d.pop("source_url")),
    "no date": _broken(lambda d: d.update(retrieved_on="27 Nov 2026")),
    "another currency": _broken(lambda d: d.update(currency="EUR")),
    "a key extra": _broken(lambda d: d.update(region="us-east-1")),
}


@pytest.mark.parametrize("document", list(BROKEN_PRICES.values()), ids=list(BROKEN_PRICES))
def test_load_prices_refuses_a_file_it_cannot_use(tmp_path, document):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(document))
    with pytest.raises(handler.JudgeFailed, match="prices.json"):
        handler.load_prices(path)


def test_load_prices_refuses_a_file_that_is_not_json(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text("{not json")
    with pytest.raises(handler.JudgeFailed, match="prices.json"):
        handler.load_prices(path)


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("price", "tokens", "expected"),
    [
        (handler.Price(input=2.2, output=11.0), (1000, 200), 4400),
        (handler.Price(input=11.0, output=55.0), (91, 10), 1551),
        (handler.Price(input=2.2, output=11.0), (3, 0), 7),  # 6.6 rounds up
        (handler.Price(input=2.2, output=11.0), (0, 0), 0),
        (handler.Price(input=0.11, output=0.55), (1_000_000, 1_000_000), 660_000),
    ],
)
def test_cost_is_tokens_times_the_price_per_million_rounded_to_an_int(price, tokens, expected):
    """A price per million tokens times the tokens is already millionths of a dollar."""
    cost = handler.cost_micro_usd(price, *tokens)
    assert cost == expected
    assert type(cost) is int


@pytest.mark.parametrize(
    ("served", "expected"),
    [
        (names.VARIANT_CHEAPER, 1000 * 2.2 + 200 * 11.0),  # Sonnet 5: 4400 micro-USD
        (names.VARIANT_CURRENT, 1000 * 11.0 + 200 * 55.0),  # Fable 5.1: 22000 micro-USD
    ],
)
def test_an_answers_cost_uses_its_own_models_prices(served, expected):
    world = World()
    outcome = world.handle(_payload(served))

    [document] = world.documents
    assert document[names.CW_METRIC_ANSWER_COST_MICRO_USD] == round(expected)
    assert outcome[names.EVENT_COST_MICRO_USD] == round(expected)
    assert (names.EVENT_COST_MICRO_USD, IDENTITY, round(expected)) in world.flagsmith.tracked


# ---------------------------------------------------------------------------
# The judge call
# ---------------------------------------------------------------------------
def test_the_judge_request_is_exactly_the_one_task_1_2_recorded():
    recorded = RECORDED_JUDGE_CALL["request"]
    question, answer = (
        recorded["messages"][0]["content"][0]["text"].removeprefix("Question: ").split("\nAnswer: ")
    )
    assert handler.converse_request(question, answer) == recorded


def test_the_judge_call_has_thinking_disabled_and_no_sampling_parameter():
    request = handler.converse_request(QUESTION, ANSWER)

    assert request["modelId"] == names.MODEL_JUDGE
    assert request["inferenceConfig"] == {"maxTokens": names.JUDGE_MAX_TOKENS}
    assert request["additionalModelRequestFields"] == {
        "thinking": {"type": names.THINKING_DISABLED_VALUE}
    }
    text = json.dumps(request)
    for parameter in names.FORBIDDEN_SAMPLING_PARAMETERS:
        assert f'"{parameter}"' not in text


def test_a_sampling_parameter_is_refused_before_bedrock(monkeypatch):
    real = handler.converse_request

    def with_top_k(question, answer):
        request = real(question, answer)
        request["additionalModelRequestFields"]["top_k"] = 5
        return request

    monkeypatch.setattr(handler, "converse_request", with_top_k)
    world = World()
    with pytest.raises(handler.JudgeFailed, match="top_k") as raised:
        world.handle()
    _clean(str(raised.value))
    assert world.bedrock.calls == []
    assert world.nothing_written()


def test_the_judge_makes_one_call_with_the_recorded_request(stubbed):
    stubbed.stubber.add_response("converse", _reply(), expected_params=_expected_request())
    world = World(bedrock=stubbed.client)

    world.handle()  # Stubber checks the request against bedrock-runtime's model and 1.2's


# ---------------------------------------------------------------------------
# The outcome
# ---------------------------------------------------------------------------
def test_a_judged_answer_writes_cloudwatch_then_three_events_then_flushes():
    world = World(HELPFUL)

    outcome = world.handle()

    assert world.log == [
        ("cloudwatch",),
        ("track", names.EVENT_HELPFUL, IDENTITY, 1),
        ("track", names.EVENT_LATENCY_MS, IDENTITY, LATENCY_MS),
        ("track", names.EVENT_COST_MICRO_USD, IDENTITY, 4400),
        ("flush",),  # before handle() returned
    ]
    assert outcome == {
        names.EVENT_HELPFUL: 1,
        names.EVENT_LATENCY_MS: LATENCY_MS,
        names.EVENT_COST_MICRO_USD: 4400,
    }
    assert len(world.bedrock.calls) == 1


@pytest.mark.parametrize(
    ("reply", "expected"),
    [(HELPFUL, 1), (NOT_HELPFUL, 0), ("Yes, very helpful.", 0)],
    ids=["helpful", "not helpful", "unparseable"],
)
def test_answer_helpful_is_an_int_in_0_or_1_never_a_bool(reply, expected):
    """1.2 showed the SDK sends a value as str(value), so True would travel as "True"."""
    world = World(reply)
    world.handle()

    [helpful] = [
        value for event, _, value in world.flagsmith.tracked if event == names.EVENT_HELPFUL
    ]
    assert type(helpful) is int and helpful == expected
    [document] = world.documents
    assert type(document[names.CW_METRIC_ANSWER_HELPFUL]) is int
    assert document[names.CW_METRIC_ANSWER_HELPFUL] == expected


UNPARSEABLE = [
    "Yes, it is helpful.",
    '{"helpful": "true"}',
    '{"helpful": 1}',
    '{"helpful": true, "reason": "clear steps"}',
    '{"Helpful": true}',
    "",
    '```json\n{"helpful": true}\n```',
    "[true]",
    "null",
    '{"helpful": true}{"helpful": false}',
]


@pytest.mark.parametrize("reply", UNPARSEABLE)
def test_an_unparseable_reply_counts_as_not_helpful_and_is_logged(reply, caplog):
    caplog.set_level(logging.WARNING, logger=JUDGE_LOGGER)
    world = World(reply)

    outcome = world.handle()

    assert outcome[names.EVENT_HELPFUL] == 0
    assert [event for event, _, _ in world.flagsmith.tracked] == list(names.EVENTS)
    assert world.flagsmith.flushes == 1
    [record] = [r for r in caplog.records if r.name == JUDGE_LOGGER]
    assert record.levelno == logging.WARNING
    assert repr(reply) in record.getMessage()  # the raw reply, as the spec says
    assert RUN_ID in record.getMessage()
    _clean(record.getMessage())


@pytest.mark.parametrize(
    ("reply", "expected"),
    [(' {"helpful": true}\n', 1), ('{"helpful":false}', 0), ('\n\n{"helpful": true}', 1)],
)
def test_whitespace_around_the_json_still_counts(reply, expected, caplog):
    caplog.set_level(logging.WARNING, logger=JUDGE_LOGGER)
    world = World(reply)

    assert world.handle()[names.EVENT_HELPFUL] == expected
    assert caplog.records == []


def test_thinking_or_other_blocks_are_left_out_of_the_reply():
    content = [{"reasoningContent": {"reasoningText": {"text": "hmm"}}}, {"text": HELPFUL}]
    world = World(bedrock=FakeBedrock(_reply(content=content)))
    assert world.handle()[names.EVENT_HELPFUL] == 1


@pytest.mark.parametrize(
    ("assigned", "served"),
    [
        (names.VARIANT_CURRENT, names.VARIANT_CURRENT),
        (names.VARIANT_CHEAPER, names.VARIANT_CHEAPER),
        (names.VARIANT_CHEAPER, names.VARIANT_CURRENT),  # the kill switch off
    ],
)
def test_both_metrics_carry_exactly_the_variant_dimension_set(assigned, served):
    world = World()
    world.handle(_payload(served, assigned=assigned))

    [document] = world.documents  # one line per answer, holding both metrics
    metrics = (names.CW_METRIC_ANSWER_COST_MICRO_USD, names.CW_METRIC_ANSWER_HELPFUL)
    assert document["_aws"] == {
        "Timestamp": int(WALL * 1000),
        "CloudWatchMetrics": [
            {
                "Namespace": names.CW_NAMESPACE,
                "Dimensions": names.CW_DIMENSION_SETS,
                "Metrics": [{"Name": m, "Unit": names.CW_METRIC_UNITS[m]} for m in metrics],
            }
        ],
    }
    assert set(document) == {"_aws", names.CW_DIMENSION_VARIANT, *metrics}
    assert document[names.CW_DIMENSION_VARIANT] == served  # the variant actually served
    for metric in metrics:
        assert names.CW_METRIC_EMITTER[metric] == names.JUDGE_FUNCTION
    _clean(world.lines[0])


# ---------------------------------------------------------------------------
# Errors: the typed error is raised so Lambda retries, and nothing is written before it
# ---------------------------------------------------------------------------
def test_the_error_classes_are_named_for_lambdas_error_type():
    assert handler.JudgeThrottled.__name__ == "JudgeThrottled"
    assert handler.JudgeFailed.__name__ == "JudgeFailed"
    assert not issubclass(handler.JudgeThrottled, handler.JudgeFailed)
    assert not issubclass(handler.JudgeFailed, handler.JudgeThrottled)


def test_a_throttled_judge_raises_judge_throttled_and_writes_nothing(stubbed):
    stubbed.stubber.add_client_error(
        "converse",
        service_error_code="ThrottlingException",
        service_message="Too many requests, please wait before trying again.",
        http_status_code=429,
        expected_params=_expected_request(),
    )
    world = World(bedrock=stubbed.client)

    with pytest.raises(handler.JudgeThrottled) as raised:
        world.handle()

    assert "ThrottlingException" in str(raised.value)
    assert "Too many requests" not in str(raised.value)
    _clean(str(raised.value))
    assert world.nothing_written()


def _converse_errors() -> dict[str, int]:
    """The errors AWS's API model documents for Converse, with their HTTP status."""
    model = botocore.session.get_session().get_service_model(names.BEDROCK_RUNTIME_SERVICE)
    return {
        shape.name: shape.metadata["error"]["httpStatusCode"]
        for shape in model.operation_model("Converse").error_shapes
    }


CONVERSE_ERRORS = _converse_errors()


def test_the_throttle_code_is_one_converse_documents():
    assert handler.THROTTLING_CODE == "ThrottlingException"
    assert CONVERSE_ERRORS[handler.THROTTLING_CODE] == 429


@pytest.mark.parametrize(
    ("code", "status"),
    sorted((c, s) for c, s in CONVERSE_ERRORS.items() if c != "ThrottlingException"),
)
def test_any_other_bedrock_error_raises_judge_failed_and_writes_nothing(stubbed, code, status):
    stubbed.stubber.add_client_error(
        "converse",
        service_error_code=code,
        service_message=f"the service says something about {QUESTION}",
        http_status_code=status,
        expected_params=_expected_request(),
    )
    world = World(bedrock=stubbed.client)

    with pytest.raises(handler.JudgeFailed) as raised:
        world.handle()

    assert code in str(raised.value)
    _clean(str(raised.value))
    assert world.nothing_written()


@pytest.mark.parametrize(
    "error",
    [
        ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com/x"),
        EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com/x"),
    ],
)
def test_a_failed_call_that_is_not_a_service_error_raises_judge_failed(error):
    world = World(bedrock=FakeBedrock(error))
    with pytest.raises(handler.JudgeFailed, match=type(error).__name__) as raised:
        world.handle()
    assert "amazonaws.com" not in str(raised.value)
    assert world.nothing_written()


def test_a_max_tokens_stop_is_an_error_never_a_verdict():
    reply = _reply(HELPFUL, stop_reason=names.STOP_REASON_MAX_TOKENS)
    world = World(bedrock=FakeBedrock(reply))

    with pytest.raises(handler.JudgeFailed, match=names.STOP_REASON_MAX_TOKENS):
        world.handle()
    assert world.nothing_written()


@pytest.mark.parametrize("reply", [{"stopReason": "end_turn"}, None], ids=["no output", "None"])
def test_a_reply_of_an_unexpected_shape_raises_judge_failed(reply):
    world = World(bedrock=FakeBedrock(reply))
    with pytest.raises(handler.JudgeFailed):
        world.handle()
    assert world.nothing_written()


def test_no_flagsmith_client_raises_judge_failed_before_bedrock():
    world = World()
    world.flagsmith_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": DUMMY_SDK_KEY}}, "GetSecretValue"
    )

    with pytest.raises(handler.JudgeFailed, match="AccessDeniedException") as raised:
        world.handle()

    _clean(str(raised.value))
    assert world.bedrock.calls == []
    assert world.nothing_written()


def test_a_failed_cloudwatch_write_raises_before_any_event():
    world = World()
    world.emit_error = BrokenPipeError("stdout is gone")

    with pytest.raises(BrokenPipeError):
        world.handle()
    assert world.flagsmith.tracked == []


VALID = [
    _payload(),
    _payload(names.VARIANT_CURRENT),
    _payload(names.VARIANT_CURRENT, assigned=names.VARIANT_CHEAPER),
    _payload(answer=""),
    _payload(input_tokens=0, output_tokens=0, latency_ms=0),
    _payload(input_tokens=1000.0),  # JSON Schema's integer takes a float with no fraction
]
INVALID = [
    None,
    [],
    "a judge payload",
    {},
    _without(names.JUDGE_PAYLOAD_FIELDS[6]),
    {**_payload(), LEAKED_KEY: LEAKED_TEXT},
    _payload(run_id="rjudge12"),
    _payload(identity=names.identity(7)),  # a user part with no run ID
    _payload(identity=f"{RUN_ID}-user-151"),
    _payload(assigned="control"),
    {**_payload(), "served": "Cheaper"},
    _payload(model_id=names.MODEL_JUDGE),
    _payload(names.VARIANT_CURRENT, model_id=names.MODEL_CHEAPER),
    _payload(question=""),
    _payload(question=7),
    _payload(answer=None),
    _payload(input_tokens=-1),
    _payload(input_tokens=True),
    _payload(output_tokens="200"),
    _payload(output_tokens=1.5),
    _payload(latency_ms=-1),
    _payload(latency_ms="1234"),
    _payload(latency_ms=True),
]
# judge_payload.json accepts these, and the judge does not.
STRICTER = {
    "another run's identity": _payload(identity=OTHER_IDENTITY),
    # JSON Schema's pattern is a search, and its $ matches before a final newline.
    "a run ID with a newline": _payload(run_id=RUN_ID + "\n"),
    "an identity with a newline": _payload(identity=IDENTITY + "\n"),
    # Either would poison Flagsmith's mean latency.
    "a NaN latency": _payload(latency_ms=float("nan")),
    "an infinite latency": _payload(latency_ms=float("inf")),
}


def _schema_accepts(payload) -> bool:
    try:
        validate("judge_payload", payload)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("payload", VALID + INVALID)
def test_the_judge_reads_a_payload_as_judge_payload_json_does(payload):
    assert (handler.payload_problem(payload) is None) == _schema_accepts(payload)


@pytest.mark.parametrize("payload", list(STRICTER.values()), ids=list(STRICTER))
def test_the_judge_is_stricter_than_the_schema_where_it_must_be(payload):
    assert _schema_accepts(payload)
    assert handler.payload_problem(payload) is not None


@pytest.mark.parametrize("payload", INVALID + list(STRICTER.values()))
def test_a_payload_that_is_not_a_judge_payload_is_refused_before_any_call(payload, caplog):
    caplog.set_level(logging.DEBUG, logger=JUDGE_LOGGER)
    world = World()

    with pytest.raises(handler.JudgeFailed, match="judge_payload") as raised:
        world.handle(payload)

    _clean(str(raised.value))
    _clean(caplog.text)
    assert world.built == []
    assert world.nothing_written()


# ---------------------------------------------------------------------------
# One line per failed attempt
# ---------------------------------------------------------------------------
def _throttling(world: World) -> None:
    world.bedrock.outcome = _client_error("ThrottlingException", 429)


def _bedrock_failing(world: World) -> None:
    world.bedrock.outcome = _client_error("ValidationException", 400)


def _no_client(world: World) -> None:
    world.flagsmith_error = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue"
    )


def _no_prices(world: World) -> None:
    def broken():
        raise handler.JudgeFailed(f"{names.PRICES_FILE} cannot be read")

    world.prices_loader = broken


def _no_stdout(world: World) -> None:
    world.emit_error = BrokenPipeError("stdout is gone")


FAILURES = {
    "a throttle": (_throttling, "JudgeThrottled"),
    "a Bedrock error": (_bedrock_failing, "JudgeFailed"),
    "no Flagsmith client": (_no_client, "JudgeFailed"),
    "no prices": (_no_prices, "JudgeFailed"),
    "no CloudWatch write": (_no_stdout, "BrokenPipeError"),
}


@pytest.mark.parametrize(("break_it", "error_type"), list(FAILURES.values()), ids=list(FAILURES))
def test_each_failed_attempt_logs_one_line_with_the_run_the_attempt_and_the_error_type(
    break_it, error_type, caplog
):
    """Lambda keeps the request ID across its retries of one event, so the request ID names
    the answer, and its lines count the attempts."""
    caplog.set_level(logging.DEBUG, logger=JUDGE_LOGGER)
    world = World()
    break_it(world)

    with pytest.raises(Exception) as raised:
        world.handle()

    assert type(raised.value).__name__ == error_type
    [line] = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert RUN_ID in line and REQUEST_ID in line and error_type in line
    _clean(caplog.text)
    assert world.flagsmith.tracked == []


def test_a_payload_without_a_run_id_logs_its_line_without_one(caplog):
    caplog.set_level(logging.DEBUG, logger=JUDGE_LOGGER)
    with pytest.raises(handler.JudgeFailed):
        World().handle({**_payload(run_id=LEAKED_TEXT), LEAKED_KEY: 1})

    [line] = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert REQUEST_ID in line and "JudgeFailed" in line
    _clean(caplog.text)


# ---------------------------------------------------------------------------
# Flushing
# ---------------------------------------------------------------------------
def test_a_failure_after_an_earlier_answer_still_flushes_before_raising():
    world = World()
    world.handle()  # the client exists from here on
    _throttling(world)

    with pytest.raises(handler.JudgeThrottled):
        world.handle()

    assert world.flagsmith.flushes == 2  # flush_events() before every return
    assert len(world.flagsmith.tracked) == len(names.EVENTS)  # the first answer's only


def test_once_the_events_are_tracked_nothing_raises(caplog):
    """A retry would track them again, and the SDK keeps unsent events for its next send,
    so a raise here could send them twice."""
    caplog.set_level(logging.WARNING, logger=JUDGE_LOGGER)
    world = World()
    world.flagsmith.flush_error = RuntimeError("the worker is gone")

    outcome = world.handle()

    assert outcome[names.EVENT_HELPFUL] == 1
    assert len(world.flagsmith.tracked) == len(names.EVENTS)
    [line] = [r.getMessage() for r in caplog.records if r.name == JUDGE_LOGGER]
    assert RUN_ID in line and REQUEST_ID in line and "RuntimeError" in line


# ---------------------------------------------------------------------------
# The real SDK against an events API on 127.0.0.1
# ---------------------------------------------------------------------------
class EventsApi:
    """POST /v1/events records the batch and answers status (202 unless a test sets it)."""

    def __init__(self) -> None:
        self.status = 202
        self.posts: list[tuple[int, list[dict]]] = []
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"null")
                status = api.status if self.path == "/v1/events" else 404
                if self.path == "/v1/events":
                    with api._lock:
                        api.posts.append((status, body["events"]))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def delivered(self) -> list[tuple[str, str, str]]:
        with self._lock:
            return [
                (event["event"], event["identifier"], event["value"])
                for status, events in self.posts
                if status < 300
                for event in events
            ]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def events_api():
    api = EventsApi()
    clients = []

    def real_client():
        """The client rm-judge builds in production, pointed at this server."""
        client = handler.flagsmith_client(
            DUMMY_SDK_KEY,
            api_url=f"{api.url}api/v1/",
            event_processor_config=EventProcessorConfig(events_api_url=api.url),
        )
        clients.append(client)
        return client

    api.real_client = real_client
    yield api
    api.status = 202
    for client in clients:
        client.close()
    api.stop()


OUTCOME_ON_THE_WIRE = [
    (names.EVENT_HELPFUL, IDENTITY, "1"),  # an int, so "1", never "True"
    (names.EVENT_LATENCY_MS, IDENTITY, str(LATENCY_MS)),
    (names.EVENT_COST_MICRO_USD, IDENTITY, "4400"),
]


def test_with_the_real_sdk_the_events_are_delivered_before_the_handler_returns(events_api):
    world = World(flagsmith_factory=events_api.real_client)

    world.handle()

    assert events_api.delivered() == OUTCOME_ON_THE_WIRE


def test_with_the_real_sdk_a_rejected_send_does_not_raise(events_api):
    """flush_events() swallows send errors: it returns once the batch is delivered, dropped
    or kept for the next send (flagsmith 6.7.0, analytics.py). So the judge cannot raise
    for one, and Lambda never retries an answer whose events may have gone."""
    events_api.status = 400  # rejected: the SDK drops the batch
    world = World(flagsmith_factory=events_api.real_client)

    world.handle()  # returns

    assert [status for status, _ in events_api.posts] == [400]
    assert events_api.delivered() == []


def test_with_the_real_sdk_a_kept_batch_goes_once_with_the_next_answer(events_api):
    events_api.status = 503  # retried three times, then kept for the next send
    world = World(flagsmith_factory=events_api.real_client)
    world.handle()  # returns: nothing raises, so Lambda does not retry
    assert events_api.delivered() == []

    events_api.status = 202
    world.handle()

    assert [status for status, _ in events_api.posts] == [503, 503, 503, 202]
    assert events_api.delivered() == OUTCOME_ON_THE_WIRE * 2  # each answer's events once


# ---------------------------------------------------------------------------
# The clients and the entry point
# ---------------------------------------------------------------------------
def test_the_bedrock_client_is_bedrock_runtime_in_the_contract_region_with_one_attempt():
    client = REAL_NEW_BEDROCK_CLIENT()  # built only: nothing is sent

    assert client.meta.service_model.service_name == names.BEDROCK_RUNTIME_SERVICE
    assert client.meta.region_name == names.REGION
    assert client.meta.config.retries["total_max_attempts"] == 1


def test_the_flagsmith_client_reads_the_server_key_and_turns_events_on(monkeypatch):
    made = []
    monkeypatch.setattr(handler, "Flagsmith", lambda **kwargs: made.append(kwargs) or "client")
    secrets = boto3.client("secretsmanager", region_name=names.REGION)

    with Stubber(secrets) as stubber:
        stubber.add_response(
            "get_secret_value",
            {"Name": names.SECRET_FLAGSMITH_SERVER_KEY, "SecretString": DUMMY_SDK_KEY},
            expected_params={"SecretId": names.SECRET_FLAGSMITH_SERVER_KEY},
        )
        assert REAL_NEW_FLAGSMITH_CLIENT(secrets) == "client"

    assert made == [{"environment_key": DUMMY_SDK_KEY, **names.FLAGSMITH_CLIENT_OPTIONS}]


def test_the_lambda_entry_point_uses_one_judge_per_container_and_passes_the_request_id(
    monkeypatch,
):
    made, seen = [], []

    class Spy:
        def handle(self, event, request_id=None):
            seen.append((event, request_id))
            return {"handled": True}

    monkeypatch.setattr(handler, "_judge", None)
    monkeypatch.setattr(handler, "Judge", lambda **kwargs: made.append(kwargs) or Spy())

    assert handler.handler({"n": 1}, SimpleNamespace(aws_request_id=REQUEST_ID)) == {
        "handled": True
    }
    handler.handler({"n": 2}, None)

    assert len(made) == 1
    assert made[0]["bedrock_factory"] is handler.new_bedrock_client
    assert made[0]["flagsmith_factory"] is handler.new_flagsmith_client
    assert seen == [({"n": 1}, REQUEST_ID), ({"n": 2}, None)]


def test_the_judge_loads_without_the_answer_package_or_jsonschema():
    """The judge's package needs app/judge, app/bedrock_fields.py, app/prices.json and
    contracts/, beside boto3 and flagsmith: not app/answer (3.1's review)."""
    probe = (
        "import sys, app.judge.handler;"
        " print([m for m in ('app.answer', 'jsonschema') if m in sys.modules])"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "[]"
