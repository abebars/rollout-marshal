"""Tests for app/answer, rm-answer-current and rm-answer-cheaper (task 3.1).

Bedrock is never called. Each test gives the handler either botocore's Stubber on a real
bedrock-runtime client, which checks the request against the service model and against the
exact parameters the test expects, or a fake client. Without one the handler's client is a
trap that fails the test. The AWS environment is replaced with dummy credentials and no
config or credentials files, so no test can find real credentials.

The expected request is the one task 1.2 recorded in evidence/1.2-converse.json, with only
the question swapped in. So the tests pin the handler to the request that worked.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
import botocore.session
import pytest
from botocore.exceptions import EndpointConnectionError, ReadTimeoutError
from botocore.stub import Stubber
from jsonschema import ValidationError

from app import bedrock_fields
from app.answer import handler
from contracts import names, validate

ROOT = Path(__file__).resolve().parents[2]
RECORDED_CALLS = {
    call["name"]: call
    for call in json.loads((ROOT / "evidence/1.2-converse.json").read_text())["calls"]
}
RECORDED_ANSWER_CALL = {
    names.MODEL_CURRENT: "current_effort_low",
    names.MODEL_CHEAPER: "cheaper_effort_low",
}
ACCESS_KEY = "testing"
SECRET_KEY = "testing-secret"
QUESTION = "How do I export all of my projects to CSV?"
IDENTITY = names.run_identity("ranswertest1200", 7)
ANSWER = "Open Settings, choose Export, pick CSV, then select Download."
REAL_BEDROCK_CLIENT = handler.bedrock_client  # before any test replaces it


def _recorded_request(model_id: str) -> dict:
    return RECORDED_CALLS[RECORDED_ANSWER_CALL[model_id]]["request"]


def _expected_request(model_id: str, question: str = QUESTION) -> dict:
    """1.2's recorded request for this model, with question in place of its question."""
    request = copy.deepcopy(_recorded_request(model_id))
    request["messages"][0]["content"][0]["text"] = question
    return request


def _reply(text: str = ANSWER, *, stop_reason: str = "end_turn", content: list | None = None):
    """A Converse response as Bedrock shapes it."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}] if content is None else content,
            }
        },
        "stopReason": stop_reason,
        "usage": {"inputTokens": 51, "outputTokens": 197, "totalTokens": 248},
        "metrics": {"latencyMs": 1234},
    }


def _event(question: str = QUESTION, identity: str = IDENTITY) -> dict:
    return dict(zip(names.ANSWER_REQUEST_FIELDS, (question, identity), strict=True))


def _sampling_keys(node) -> list[str]:
    """Every forbidden sampling key anywhere in node: the test's own walk, not the code's."""
    if isinstance(node, dict):
        found = [key for key in node if key in names.FORBIDDEN_SAMPLING_PARAMETERS]
        return found + [key for value in node.values() for key in _sampling_keys(value)]
    if isinstance(node, list | tuple):
        return [key for item in node for key in _sampling_keys(item)]
    return []


def _clean_message(err: Exception) -> None:
    """No question and no key in what the error says."""
    for secret in (QUESTION, ACCESS_KEY, SECRET_KEY):
        assert secret not in str(err)


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


@pytest.fixture(autouse=True)
def _dummy_aws_and_no_bedrock(monkeypatch):
    for name in [name for name in os.environ if name.startswith("AWS_") or name == "BOTO_CONFIG"]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BOTO_CONFIG", os.devnull)

    def trap():
        raise AssertionError("a test reached for the real Bedrock client: stub it")

    monkeypatch.setattr(handler, "bedrock_client", trap)


@pytest.fixture(params=[names.MODEL_CURRENT, names.MODEL_CHEAPER], ids=["current", "cheaper"])
def model_id(request, monkeypatch) -> str:
    """One handler, deployed twice: each test runs as rm-answer-current and as rm-answer-cheaper."""
    monkeypatch.setenv(handler.MODEL_ID_ENV, request.param)
    return request.param


@pytest.fixture
def bedrock(monkeypatch):
    """botocore's Stubber on a real bedrock-runtime client, given to the handler."""
    client = boto3.client(names.BEDROCK_RUNTIME_SERVICE, region_name=names.REGION)
    monkeypatch.setattr(handler, "bedrock_client", lambda: client)
    with Stubber(client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def _use(monkeypatch, fake: FakeBedrock) -> FakeBedrock:
    monkeypatch.setattr(handler, "bedrock_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
def test_the_model_comes_from_env_model_id():
    assert handler.MODEL_ID_ENV == "MODEL_ID"  # tasks.md 3.1
    assert set(handler.ANSWER_MODELS) == {names.MODEL_CURRENT, names.MODEL_CHEAPER}


def test_the_request_is_exactly_the_one_1_2_recorded(model_id):
    recorded = _recorded_request(model_id)
    assert recorded["messages"][0]["content"][0]["text"] == names.PROBE_QUESTION

    assert handler.converse_request(model_id, names.PROBE_QUESTION) == recorded
    # The contract's values, spelled out.
    assert recorded["modelId"] == model_id
    assert recorded["inferenceConfig"] == {"maxTokens": names.ANSWER_MAX_TOKENS}
    assert recorded["additionalModelRequestFields"] == {
        "output_config": {"effort": names.ANSWER_THINKING_EFFORT}
    }
    assert f"at most {names.ANSWER_MAX_WORDS} words" in recorded["system"][0]["text"]


def test_no_sampling_parameter_is_sent_at_any_depth(bedrock, model_id):
    expected = _expected_request(model_id)
    assert _sampling_keys(expected) == []
    assert _sampling_keys(handler.converse_request(model_id, QUESTION)) == []
    assert {"temperature", "top_p", "topP", "top_k", "topK"} <= set(
        names.FORBIDDEN_SAMPLING_PARAMETERS
    )
    # The Stubber refuses any request that differs from expected, so what is sent has none.
    bedrock.add_response("converse", _reply(), expected_params=expected)
    handler.handler(_event(), None)


def test_a_sampling_parameter_is_refused_before_bedrock(monkeypatch, model_id):
    fake = _use(monkeypatch, FakeBedrock(_reply()))
    built = handler.converse_request

    def with_top_k(model, question):
        request = built(model, question)
        request["additionalModelRequestFields"]["top_k"] = 5
        return request

    monkeypatch.setattr(handler, "converse_request", with_top_k)
    with pytest.raises(handler.AnswerFailed, match="top_k"):
        handler.handler(_event(), None)
    assert fake.calls == []


def test_the_dotted_field_helper_builds_exactly_the_fields_1_2_recorded():
    effort = bedrock_fields.nested_field(names.THINKING_EFFORT_FIELD, names.ANSWER_THINKING_EFFORT)
    disabled = bedrock_fields.nested_field(
        names.THINKING_DISABLED_FIELD, names.THINKING_DISABLED_VALUE
    )

    for call in ("current_effort_low", "cheaper_effort_low"):
        assert effort == RECORDED_CALLS[call]["request"]["additionalModelRequestFields"]
    for call in ("judge_thinking_disabled", "cheaper_thinking_disabled"):
        assert disabled == RECORDED_CALLS[call]["request"]["additionalModelRequestFields"]
    assert effort == {"output_config": {"effort": "low"}}
    assert disabled == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize(
    ("dotted", "value", "expected"),
    [("a", 1, {"a": 1}), ("a.b.c", "v", {"a": {"b": {"c": "v"}}})],
)
def test_the_dotted_field_helper_nests_any_depth(dotted, value, expected):
    assert bedrock_fields.nested_field(dotted, value) == expected


@pytest.mark.parametrize("dotted", ["", ".", "a.", ".b", "a..b"])
def test_the_dotted_field_helper_refuses_an_empty_part(dotted):
    with pytest.raises(ValueError):
        bedrock_fields.nested_field(dotted, 1)


def test_the_sampling_walk_finds_keys_at_any_depth():
    request = {"inferenceConfig": {"maxTokens": 1, "topP": 0.9}, "x": [{"y": {"top_k": 3}}]}
    assert bedrock_fields.sampling_parameters(request) == ["topP", "top_k"]
    assert bedrock_fields.sampling_parameters({"inferenceConfig": {"maxTokens": 1}}) == []


# ---------------------------------------------------------------------------
# Input and output
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "event",
    [
        _event(),
        _event(names.PROBE_QUESTION, names.PROBE_IDENTITY),  # the agent's health probe
        _event("Can I pay in euros (€)?", names.run_identity("r202611271530", 150)),
    ],
)
def test_input_and_output_validate_against_the_answer_schemas(bedrock, model_id, event):
    validate("answer_request", event)
    question = event[names.ANSWER_REQUEST_FIELDS[0]]
    bedrock.add_response(
        "converse", _reply(), expected_params=_expected_request(model_id, question)
    )

    result = handler.handler(event, None)

    validate("answer_response", result)
    assert result == dict(
        zip(names.ANSWER_RESPONSE_FIELDS, (ANSWER, 51, 197, model_id), strict=True)
    )


VALID = [_event(), _event(names.PROBE_QUESTION, names.PROBE_IDENTITY)]
INVALID = [
    None,
    [],
    "How do I reset my password?",
    {},
    {names.ANSWER_REQUEST_FIELDS[0]: QUESTION},
    {names.ANSWER_REQUEST_FIELDS[1]: IDENTITY},
    _event(question=""),
    _event(question=7),
    _event(identity=names.identity(7)),  # a user part with no run ID
    _event(identity="ranswertest1200-user-151"),
    _event(identity=7),
    {**_event(), "extra": 1},
]


@pytest.mark.parametrize("event", VALID + INVALID)
def test_the_handler_judges_a_request_as_answer_request_json_does(event):
    try:
        validate("answer_request", event)
        schema_accepts = True
    except ValidationError:
        schema_accepts = False
    assert (handler.request_problem(event) is None) == schema_accepts


@pytest.mark.parametrize("event", INVALID)
def test_a_request_that_is_not_an_answer_request_is_refused_before_bedrock(
    monkeypatch, model_id, event
):
    fake = _use(monkeypatch, FakeBedrock(_reply()))
    with pytest.raises(handler.AnswerFailed, match="answer_request") as raised:
        handler.handler(event, None)
    _clean_message(raised.value)
    assert fake.calls == []


def test_a_callers_key_is_counted_and_never_quoted(monkeypatch, model_id):
    """Lambda logs the message as errorMessage, and a key can carry an identity or a
    question."""
    leaked_identity, leaked_question = names.run_identity("ranswertest1200", 99), "Who else?"
    fake = _use(monkeypatch, FakeBedrock(_reply()))
    with pytest.raises(handler.AnswerFailed) as raised:
        handler.handler({**_event(), leaked_identity: leaked_question}, None)

    message = str(raised.value)
    assert "1 unexpected field(s)" in message
    for text in (leaked_identity, leaked_question, IDENTITY):
        assert text not in message
    _clean_message(raised.value)
    assert fake.calls == []


@pytest.mark.parametrize("value", [None, "", names.MODEL_JUDGE, "us.anthropic.claude-unknown"])
def test_model_id_must_name_one_of_the_two_answer_models(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(handler.MODEL_ID_ENV, raising=False)
    else:
        monkeypatch.setenv(handler.MODEL_ID_ENV, value)
    fake = _use(monkeypatch, FakeBedrock(_reply()))
    with pytest.raises(handler.AnswerFailed, match=handler.MODEL_ID_ENV):
        handler.handler(_event(), None)
    assert fake.calls == []


def test_thinking_blocks_are_never_part_of_the_answer(bedrock, model_id):
    content = [
        {"reasoningContent": {"reasoningText": {"text": "Let me think.", "signature": "s1"}}},
        {"text": "Open Settings. "},
        {"text": "Choose Export."},
    ]
    bedrock.add_response(
        "converse", _reply(content=content), expected_params=_expected_request(model_id)
    )
    result = handler.handler(_event(), None)
    assert result[names.ANSWER_RESPONSE_FIELDS[0]] == "Open Settings. Choose Export."


@pytest.mark.parametrize(
    "content",
    [
        [],
        [{"text": ""}],
        [{"text": "  \n"}],
        [{"reasoningContent": {"reasoningText": {"text": "thinking only", "signature": "s"}}}],
    ],
)
def test_a_reply_with_no_answer_text_raises_answer_failed(bedrock, model_id, content):
    bedrock.add_response(
        "converse", _reply(content=content), expected_params=_expected_request(model_id)
    )
    with pytest.raises(handler.AnswerFailed, match="no text") as raised:
        handler.handler(_event(), None)
    _clean_message(raised.value)


# ---------------------------------------------------------------------------
# The typed errors rm-router branches on
# ---------------------------------------------------------------------------
def test_the_error_classes_are_named_exactly_as_the_router_expects():
    # Lambda reports the raised class's simple name as the invoke response's errorType.
    assert handler.AnswerThrottled.__name__ == "AnswerThrottled"
    assert handler.AnswerFailed.__name__ == "AnswerFailed"
    assert not issubclass(handler.AnswerThrottled, handler.AnswerFailed)
    assert not issubclass(handler.AnswerFailed, handler.AnswerThrottled)


def test_a_bedrock_throttle_raises_answer_throttled(bedrock, model_id):
    bedrock.add_client_error(
        "converse",
        service_error_code="ThrottlingException",
        service_message="Too many requests, please wait before trying again.",
        http_status_code=429,
        expected_params=_expected_request(model_id),
    )
    with pytest.raises(handler.AnswerThrottled) as raised:
        handler.handler(_event(), None)

    assert type(raised.value).__name__ == "AnswerThrottled"
    assert "ThrottlingException" in str(raised.value)
    assert "Too many requests" not in str(raised.value)  # the service's own text is left out
    _clean_message(raised.value)


def _converse_errors() -> dict[str, int]:
    """The errors AWS's API model documents for Converse, with their HTTP status."""
    model = botocore.session.get_session().get_service_model(names.BEDROCK_RUNTIME_SERVICE)
    return {
        shape.name: shape.metadata["error"]["httpStatusCode"]
        for shape in model.operation_model("Converse").error_shapes
    }


CONVERSE_ERRORS = _converse_errors()


def test_the_throttle_code_is_one_converse_documents():
    assert handler.THROTTLING_CODE == "ThrottlingException"  # the lead's decision
    assert CONVERSE_ERRORS[handler.THROTTLING_CODE] == 429
    # Another documented error shares the status, so the handler goes by the code, not 429.
    assert {code for code, status in CONVERSE_ERRORS.items() if status == 429} == {
        "ThrottlingException",
        "ModelNotReadyException",
    }


@pytest.mark.parametrize(
    ("code", "status"),
    sorted(
        (code, status) for code, status in CONVERSE_ERRORS.items() if code != "ThrottlingException"
    ),
)
def test_any_other_bedrock_error_raises_answer_failed(bedrock, model_id, code, status):
    bedrock.add_client_error(
        "converse",
        service_error_code=code,
        service_message=f"the service says something about {QUESTION}",
        http_status_code=status,
        expected_params=_expected_request(model_id),
    )
    with pytest.raises(handler.AnswerFailed) as raised:
        handler.handler(_event(), None)

    assert type(raised.value).__name__ == "AnswerFailed"
    assert code in str(raised.value)
    _clean_message(raised.value)


@pytest.mark.parametrize(
    "error",
    [
        ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com/x"),
        EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com/x"),
    ],
)
def test_a_failed_bedrock_call_that_is_not_a_service_error_raises_answer_failed(
    monkeypatch, model_id, error
):
    _use(monkeypatch, FakeBedrock(error))
    with pytest.raises(handler.AnswerFailed, match=type(error).__name__) as raised:
        handler.handler(_event(), None)
    assert "amazonaws.com" not in str(raised.value)
    _clean_message(raised.value)


def test_a_max_tokens_stop_raises_answer_failed(bedrock, model_id):
    bedrock.add_response(
        "converse",
        _reply("A cut-off answ", stop_reason=names.STOP_REASON_MAX_TOKENS),
        expected_params=_expected_request(model_id),
    )
    with pytest.raises(handler.AnswerFailed, match=names.STOP_REASON_MAX_TOKENS) as raised:
        handler.handler(_event(), None)

    assert type(raised.value).__name__ == "AnswerFailed"
    assert "A cut-off answ" not in str(raised.value)
    _clean_message(raised.value)


def test_a_reply_of_an_unexpected_shape_raises_answer_failed(monkeypatch, model_id):
    _use(monkeypatch, FakeBedrock({"stopReason": "end_turn"}))
    with pytest.raises(handler.AnswerFailed):
        handler.handler(_event(), None)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------
def test_the_client_is_bedrock_runtime_in_the_contract_region_with_one_attempt():
    client = handler.new_bedrock_client()  # built only: nothing is sent

    assert client.meta.service_model.service_name == names.BEDROCK_RUNTIME_SERVICE
    assert client.meta.region_name == names.REGION
    assert client.meta.config.retries["total_max_attempts"] == 1


def test_the_client_is_built_once_per_container(monkeypatch):
    built = []
    monkeypatch.setattr(handler, "new_bedrock_client", lambda: built.append(1) or object())
    monkeypatch.setattr(handler, "_client", None)

    first, second = REAL_BEDROCK_CLIENT(), REAL_BEDROCK_CLIENT()
    assert first is second
    assert built == [1]


def test_the_handler_loads_without_jsonschema_or_flagsmith():
    """The function's package needs app/ and contracts/ beside boto3, and nothing for
    Flagsmith: the answer functions make no Flagsmith call (the chaos read is not built)."""
    probe = (
        "import sys, app.answer.handler;"
        " print([m for m in ('jsonschema', 'flagsmith') if m in sys.modules])"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "[]"
