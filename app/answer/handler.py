"""rm-answer-current and rm-answer-cheaper: one support answer from Bedrock (task 3.1).

One handler, deployed twice. Environment variable MODEL_ID names the model it asks:
us.anthropic.claude-fable-5-1 for rm-answer-current and us.anthropic.claude-sonnet-5 for
rm-answer-cheaper (design.md sections 3 and 5).

- Input: an answer_request, {question, identity} (contracts/schemas/answer_request.json),
  checked here without jsonschema. The identity is checked but not used, because the
  answer functions make no Flagsmith call.
- Output: an answer_response, {answer, input_tokens, output_tokens, model_id}. The answer
  is the reply's text blocks; any thinking block is left out.
- The request is the one task 1.2 proved (evidence/1.2-converse.json): a Converse call, a
  system prompt asking for at most 120 words, maxTokens 2000, and thinking effort low as
  output_config.effort inside additionalModelRequestFields. No sampling parameter is ever
  sent; a request carrying one is refused before Bedrock is called.
- One attempt per answer, because botocore's retries are off. A throttle surfaces at once
  as AnswerThrottled, and the latency rm-router measures is one model call, not a retry
  loop.

Errors. The handler raises one of two exceptions. Lambda reports the raised class's simple
name as errorType, and rm-router branches on it. This is the lead's decision, which the
next contract change records:

- AnswerThrottled: Bedrock answered ThrottlingException.
- AnswerFailed: any other Bedrock error; a stopReason of max_tokens, which is an error and
  never a result (design.md section 3); a reply with no answer text; and, before Bedrock is
  called, a MODEL_ID that names neither answer model, or an event that is not an
  answer_request.

Their messages carry the model, the error's code, its HTTP status and request ID. They
never carry the question, the reply, the service's own message, a key, or a caller's field
name.

The chaos_delay_cheaper read (design.md section 9) is not built: task 4.3 adds it only if
its FIS timebox runs out.
"""

from __future__ import annotations

import os

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.bedrock_fields import nested_field, sampling_parameters
from contracts import names

MODEL_ID_ENV = "MODEL_ID"  # tasks.md 3.1
ANSWER_MODELS = tuple(names.MODEL_BY_VARIANT.values())  # answer_response's model_id enum
QUESTION_FIELD, IDENTITY_FIELD = names.ANSWER_REQUEST_FIELDS
THROTTLING_CODE = "ThrottlingException"  # Bedrock's error code for a throttled request
# The system prompt task 1.2 ran (evidence/1.2-converse.json).
SYSTEM_PROMPT = (
    "You are the support assistant for a fictional SaaS product. Answer the customer's"
    f" question in at most {names.ANSWER_MAX_WORDS} words."
)


class AnswerThrottled(Exception):
    """Bedrock throttled the request (ThrottlingException)."""


class AnswerFailed(Exception):
    """No answer: another Bedrock error, a max_tokens stop, no text, or a bad request."""


def new_bedrock_client():
    """A bedrock-runtime client in us-east-1 that makes one attempt per call."""
    return boto3.client(
        names.BEDROCK_RUNTIME_SERVICE,
        region_name=names.REGION,
        config=Config(retries={"total_max_attempts": 1}),
    )


_client = None


def bedrock_client():
    """The container's client, built on first use and kept for later invocations."""
    global _client
    if _client is None:
        _client = new_bedrock_client()
    return _client


def request_problem(event: object) -> str | None:
    """Why event is not an answer_request, or None when it is one. It names only the
    expected fields that are missing, and counts the others: a caller's key can carry an
    identity or a question, so it is never quoted, nor is the question."""
    if not isinstance(event, dict):
        return f"a {type(event).__name__}, not an object"
    expected = set(names.ANSWER_REQUEST_FIELDS)
    if set(event) != expected:
        missing = sorted(expected - set(event))
        unexpected = len(set(event) - expected)
        return f"fields missing {missing}, and {unexpected} unexpected field(s)"
    question, identity = event[QUESTION_FIELD], event[IDENTITY_FIELD]
    if not isinstance(question, str) or not question:
        return f"{QUESTION_FIELD} must be a non-empty string"
    if not isinstance(identity, str) or not names.is_run_identity(identity):
        return f"{IDENTITY_FIELD} must match {names.RUN_IDENTITY_PATTERN}"
    return None


def converse_request(model_id: str, question: str) -> dict:
    """The Converse request task 1.2 proved, for one question."""
    return {
        "modelId": model_id,
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": question}]}],
        "inferenceConfig": {"maxTokens": names.ANSWER_MAX_TOKENS},
        "additionalModelRequestFields": nested_field(
            names.THINKING_EFFORT_FIELD, names.ANSWER_THINKING_EFFORT
        ),
    }


def _service_error(err: ClientError, model_id: str) -> AnswerThrottled | AnswerFailed:
    """The typed error for a Bedrock error reply, without the service's own message."""
    code = err.response.get("Error", {}).get("Code") or "an unnamed error"
    meta = err.response.get("ResponseMetadata", {})
    status, request_id = meta.get("HTTPStatusCode"), meta.get("RequestId")
    where = ", ".join(
        part
        for part in (status and f"HTTP {status}", request_id and f"request {request_id}")
        if part
    )
    detail = f"Bedrock {code} from {model_id}" + (f" ({where})" if where else "")
    return AnswerThrottled(detail) if code == THROTTLING_CODE else AnswerFailed(detail)


def _answer_text(reply: dict) -> str:
    """The reply's text blocks, joined; thinking and any other block left out."""
    content = reply["output"]["message"]["content"]
    return "".join(block["text"] for block in content if "text" in block)


def answer(event: object, client) -> dict:
    """One answer_response for one answer_request, or AnswerThrottled or AnswerFailed."""
    model_id = os.environ.get(MODEL_ID_ENV, "")
    if model_id not in ANSWER_MODELS:
        raise AnswerFailed(
            f"{MODEL_ID_ENV} must be one of {', '.join(ANSWER_MODELS)}; it is {model_id!r}"
        )
    problem = request_problem(event)
    if problem:
        raise AnswerFailed(f"not an answer_request: {problem}")
    request = converse_request(model_id, event[QUESTION_FIELD])
    forbidden = sampling_parameters(request)
    if forbidden:
        raise AnswerFailed(f"refusing to send sampling parameters: {', '.join(forbidden)}")

    try:
        reply = client.converse(**request)
    except ClientError as err:
        raise _service_error(err, model_id) from None
    except BotoCoreError as err:
        raise AnswerFailed(f"Bedrock call to {model_id} failed: {type(err).__name__}") from None

    try:
        stop_reason = reply["stopReason"]
        text = _answer_text(reply)
        tokens = (int(reply["usage"]["inputTokens"]), int(reply["usage"]["outputTokens"]))
    except (KeyError, TypeError, ValueError):
        raise AnswerFailed(f"a reply from {model_id} of an unexpected shape") from None
    if stop_reason == names.STOP_REASON_MAX_TOKENS:
        raise AnswerFailed(
            f"{model_id} stopped at maxTokens {names.ANSWER_MAX_TOKENS} (stopReason"
            f" {names.STOP_REASON_MAX_TOKENS}), so there is no answer"
        )
    if not text.strip():
        raise AnswerFailed(f"{model_id} replied with no text (stopReason {stop_reason})")
    return dict(zip(names.ANSWER_RESPONSE_FIELDS, (text, *tokens, model_id), strict=True))


def handler(event, context):
    """The Lambda entry point: answer() with the container's Bedrock client."""
    return answer(event, bedrock_client())
