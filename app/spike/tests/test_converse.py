"""Task 1.2 spike: the Bedrock Converse requests and how their results are judged.

design.md section 3: effort low on both answer models at maxTokens 2000; thinking disabled on
the judge's model (Haiku 5.5) at maxTokens 200 with no effort field; thinking disabled on
Sonnet 5 as well. Fable 5.1 cannot disable thinking. No sampling parameter on any model.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.spike import handler
from app.spike.tests.fakes import FakeBedrock, text_response
from contracts import names

FORBIDDEN = {"temperature", "top_p", "top_k", "topP", "topK"}


def keys_anywhere(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_anywhere(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_anywhere(v)}
    return set()


def calls_by_name() -> dict[str, handler.ConverseCall]:
    return {call.name: call for call in handler.converse_calls()}


def test_no_request_carries_a_sampling_parameter_at_any_depth():
    for call in handler.converse_calls():
        assert not keys_anywhere(call.request) & FORBIDDEN, call.name
        handler.assert_no_sampling(call.request)


def test_the_guard_refuses_each_spelling(monkeypatch):
    base = calls_by_name()["cheaper_effort_low"].request
    for where, key in [
        ("inferenceConfig", "temperature"),
        ("inferenceConfig", "topP"),
        ("additionalModelRequestFields", "top_k"),
        ("additionalModelRequestFields", "top_p"),
        ("additionalModelRequestFields", "topK"),
    ]:
        request = {**base, where: {**base[where], key: 0.5}}
        with pytest.raises(ValueError, match=key):
            handler.assert_no_sampling(request)


def test_effort_low_on_both_answer_models():
    calls = calls_by_name()
    for name, model in [
        ("current_effort_low", names.MODEL_CURRENT),
        ("cheaper_effort_low", names.MODEL_CHEAPER),
    ]:
        request = calls[name].request
        assert request["modelId"] == model
        assert request["inferenceConfig"] == {"maxTokens": names.ANSWER_MAX_TOKENS}
        # THINKING_EFFORT_FIELD is "output_config.effort": the AWS adaptive thinking page puts
        # effort in its own output_config object inside additionalModelRequestFields.
        assert request["additionalModelRequestFields"] == {
            "output_config": {"effort": names.ANSWER_THINKING_EFFORT}
        }
        assert str(names.ANSWER_MAX_WORDS) in request["system"][0]["text"]
        assert calls[name].optional is False


def test_thinking_disabled_on_the_judge_model_and_on_sonnet_5_with_no_effort_field():
    calls = calls_by_name()
    for name, model in [
        ("judge_thinking_disabled", names.MODEL_JUDGE),
        ("cheaper_thinking_disabled", names.MODEL_CHEAPER),
    ]:
        request = calls[name].request
        assert request["modelId"] == model
        assert request["inferenceConfig"] == {"maxTokens": names.JUDGE_MAX_TOKENS}
        assert request["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
        assert "output_config" not in keys_anywhere(request)
        assert calls[name].optional is False


def test_fable_disabled_is_one_optional_attempt_that_expects_a_validation_error():
    call = calls_by_name()["current_thinking_disabled"]
    assert call.optional is True
    assert call.expect_error == "ValidationException"
    assert call.request["modelId"] == names.MODEL_CURRENT
    assert call.request["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
    assert [c.name for c in handler.converse_calls(include_optional=False)] == [
        "current_effort_low",
        "cheaper_effort_low",
        "judge_thinking_disabled",
        "cheaper_thinking_disabled",
    ]


def validation_error() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "ValidationException", "Message": "thinking cannot be disabled"},
            "ResponseMetadata": {"HTTPStatusCode": 400, "RequestId": "req-400"},
        },
        "Converse",
    )


def respond_like_bedrock(request: dict) -> dict:
    fields = request["additionalModelRequestFields"]
    if request["modelId"] == names.MODEL_CURRENT and "thinking" in fields:
        raise validation_error()
    if "thinking" in fields:
        return text_response('{"helpful": true}', reasoning=False)
    return text_response("Open Settings, then Security, then Reset password.")


def test_run_converse_records_each_call():
    bedrock = FakeBedrock(respond_like_bedrock)
    result = handler.run_converse(bedrock)

    assert [r["name"] for r in result["calls"]] == [c.name for c in handler.converse_calls()]
    assert bedrock.requests == [c.request for c in handler.converse_calls()]
    first = result["calls"][0]
    assert first["ok"] is True
    assert first["stop_reason"] == "end_turn"
    assert first["text"].startswith("Open Settings")
    assert first["usage"] == {"inputTokens": 40, "outputTokens": 90, "totalTokens": 130}
    assert first["bedrock_latency_ms"] == 812
    assert first["wall_ms"] >= 0
    assert first["content_blocks"] == ["reasoningContent", "text"]
    assert first["request_id"] == "req-1"
    assert first["request"] == handler.converse_calls()[0].request

    fable_disabled = result["calls"][-1]
    assert fable_disabled["ok"] is True
    assert fable_disabled["error"] == {
        "code": "ValidationException",
        "message": "thinking cannot be disabled",
        "http_status": 400,
        "request_id": "req-400",
    }
    assert result["required_ok"] is True


@pytest.mark.parametrize(
    "response",
    [
        text_response("cut off", stop_reason="max_tokens"),
        text_response("", stop_reason="end_turn"),
    ],
)
def test_a_max_tokens_stop_or_a_reply_without_text_fails_the_call(response):
    bedrock = FakeBedrock(lambda request: response)
    result = handler.run_converse(bedrock, include_optional=False)

    assert all(call["ok"] is False for call in result["calls"])
    assert result["required_ok"] is False


def test_an_unexpected_error_fails_the_call_and_is_recorded():
    def throttle(request):
        raise ClientError(
            {
                "Error": {"Code": "ThrottlingException", "Message": "slow down"},
                "ResponseMetadata": {"HTTPStatusCode": 429, "RequestId": "req-429"},
            },
            "Converse",
        )

    result = handler.run_converse(FakeBedrock(throttle), include_optional=False)
    assert result["calls"][0]["ok"] is False
    assert result["calls"][0]["error"]["code"] == "ThrottlingException"
    assert result["required_ok"] is False


def test_a_transport_error_is_recorded_and_the_other_calls_still_run():
    def flaky(request):
        if request["modelId"] == names.MODEL_CURRENT:
            raise EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1")
        return text_response("fine")

    bedrock = FakeBedrock(flaky)
    result = handler.run_converse(bedrock, include_optional=False)

    assert len(bedrock.requests) == 4
    assert result["calls"][0]["ok"] is False
    assert result["calls"][0]["error"]["code"] == "EndpointConnectionError"
    assert all(call["ok"] for call in result["calls"][1:])
    assert result["required_ok"] is False


def test_an_optional_call_that_unexpectedly_succeeds_is_not_ok_but_does_not_fail_the_run():
    bedrock = FakeBedrock(lambda request: text_response("fine"))
    result = handler.run_converse(bedrock)

    assert result["calls"][-1]["ok"] is False
    assert result["required_ok"] is True
