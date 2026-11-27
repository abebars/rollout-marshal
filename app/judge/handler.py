"""rm-judge: grade one answer and send its outcome (task 3.3).

rm-router invokes it asynchronously, once per answer served while a run is active, with a
judge_payload (contracts/schemas/judge_payload.json): {run_id, identity, assigned, served,
model_id, question, answer, input_tokens, output_tokens, latency_ms}. For one answer it:

1. Checks the payload, without jsonschema. It is stricter than the schema in three ways:
   the identity must be one of run_id's, neither ID may end in a newline (the schema's
   pattern is a search, so its $ lets one through), and latency_ms must be finite.
2. Prices the answer: AnswerCostMicroUsd = round(input_tokens * input + output_tokens *
   output), with model_id's prices per million tokens from app/prices.json. They were
   copied from the AWS Bedrock pricing page, whose URL and the date are in the file. A
   price per million tokens times the tokens is already in millionths of a dollar.
3. Asks the judge model, us.anthropic.claude-haiku-5-5, whether the answer is helpful. It
   is one Converse call with thinking disabled (thinking.type disabled inside
   additionalModelRequestFields) and maxTokens 200, the request task 1.2 proved
   (evidence/1.2-converse.json). A reply of exactly {"helpful": true} or
   {"helpful": false} is the verdict. Anything else counts as not helpful, and the raw
   reply is logged.
4. Writes AnswerCostMicroUsd and AnswerHelpful to CloudWatch as one embedded-metric log
   line with exactly the Variant dimension, Variant being the variant served.
5. Tracks answer_helpful, answer_latency_ms and answer_cost_micro_usd for the identity,
   then calls flush_events() before it returns. answer_helpful is 1 or 0, an int and never
   a bool: the SDK sends str(value), so True would travel as "True" (task 1.2).

Failures, the lead's decision for 3.3: all or nothing, in an order that makes Lambda's
retry safe.
- The judge call and the reply's parse come before any write. An unparseable reply is not
  a failure. The call has a 5 s connect and a 10 s read timeout, so it ends well inside the
  function's 30 s; botocore's 60 s defaults would let Lambda kill it with no line logged.
- Nothing is written with under 13 s left: one SDK send takes about 12 s, plus 1 s. The
  judge raises JudgeFailed instead, so the retry starts clean and no flush is cut off by
  the timeout, which could send the events twice.
- CloudWatch is written first, then the Flagsmith events. Any failure up to the events
  raises, so Lambda's asynchronous retries run the answer again. A retry after a failed
  write may count the cost twice in CloudWatch, which is the safe side for the budget check.
- The events are tracked one at a time. Once the first is tracked nothing raises, because
  a retry would track them again: a later track that fails logs one warning. The SDK's
  flush_events() swallows send errors anyway: it returns once the batch is delivered,
  dropped, or kept for its next send (flagsmith 6.7.0, analytics.py).
- Each failed attempt logs one line with the run ID, Lambda's request ID and the error
  type. The request ID is the same on each retry of one event, so its lines are the
  attempts of one answer. Nothing logged names the identity or quotes the question or the
  answer.
- JudgeThrottled: Bedrock answered ThrottlingException. JudgeFailed: any other Bedrock
  error; a max_tokens stop, an error and never a verdict (design.md section 3); a reply of
  an unexpected shape; a payload that is not a judge_payload; an unusable prices file; no
  Flagsmith client.

flush_events() also runs before a failed attempt returns, once a client exists, so events
an earlier answer left in the SDK's buffer still go (design.md section 4, before every
return).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from flagsmith import Flagsmith

from app.bedrock_fields import nested_field, sampling_parameters
from contracts import names

logger = logging.getLogger(__name__)

JUDGE_REPLY_FIELD = "helpful"  # tasks.md 3.3: the judge call returns {"helpful": true|false}
HELPFUL_REPLY = json.dumps({JUDGE_REPLY_FIELD: True})
NOT_HELPFUL_REPLY = json.dumps({JUDGE_REPLY_FIELD: False})
# The system prompt task 1.2 ran (evidence/1.2-converse.json).
SYSTEM_PROMPT = (
    "You grade one support answer for helpfulness. Reply with only JSON:"
    f" {HELPFUL_REPLY} or {NOT_HELPFUL_REPLY}."
)
THROTTLING_CODE = "ThrottlingException"  # Bedrock's error code for a throttled request
# The judge's time limits, the lead's decision for 3.3: its Bedrock call ends well inside
# the function's 30 s, and nothing is written with less time left than a flush needs.
JUDGE_BEDROCK_CONNECT_TIMEOUT_SECONDS = 5  # names.JUDGE_BEDROCK_CONNECT_TIMEOUT_SECONDS after c4
JUDGE_BEDROCK_READ_TIMEOUT_SECONDS = 10  # names.JUDGE_BEDROCK_READ_TIMEOUT_SECONDS after c4
# One SDK send of about 12 s, plus 1 s.
JUDGE_FLUSH_RESERVE_SECONDS = 13  # names.JUDGE_FLUSH_RESERVE_SECONDS after c4

PRICES_PATH = Path(__file__).resolve().parents[2] / names.PRICES_FILE
# The shape of app/prices.json, the lead's decision for 3.3.
PRICES_FIELDS = ("source_url", "retrieved_on", "currency", "per_million_tokens")
PRICE_FIELDS = ("input", "output")
PRICES_CURRENCY = "USD"
PRICED_MODELS = (*names.MODEL_BY_VARIANT.values(), names.MODEL_JUDGE)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class JudgeThrottled(Exception):
    """Bedrock throttled the judge call (ThrottlingException)."""


class JudgeFailed(Exception):
    """No verdict, or nothing to judge with: see the module docstring."""


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    output: float


# ---------------------------------------------------------------------------
# Prices and cost
# ---------------------------------------------------------------------------
def _is_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def prices_problem(document: object) -> str | None:
    """Why document is not a usable app/prices.json, or None when it is one."""
    if not isinstance(document, dict) or set(document) != set(PRICES_FIELDS):
        return f"it must be an object with exactly {', '.join(PRICES_FIELDS)}"
    source, read_on = document["source_url"], document["retrieved_on"]
    if not isinstance(source, str) or not source.startswith("https://"):
        return "source_url must be the pricing page's https URL"
    if not isinstance(read_on, str) or not _DATE.fullmatch(read_on):
        return "retrieved_on must be a date, YYYY-MM-DD"
    try:
        date.fromisoformat(read_on)
    except ValueError:
        return "retrieved_on must be a date, YYYY-MM-DD"
    if document["currency"] != PRICES_CURRENCY:
        return f"currency must be {PRICES_CURRENCY}"
    table = document["per_million_tokens"]
    if not isinstance(table, dict) or set(table) != set(PRICED_MODELS):
        return f"per_million_tokens must price exactly {', '.join(PRICED_MODELS)}"
    for model_id, entry in table.items():
        if not isinstance(entry, dict) or set(entry) != set(PRICE_FIELDS):
            return f"{model_id} must have exactly {', '.join(PRICE_FIELDS)}"
        if not all(_is_number(entry[field]) and entry[field] > 0 for field in PRICE_FIELDS):
            return f"{model_id}'s prices must be positive numbers"
    return None


def load_prices(path: Path | str = PRICES_PATH) -> dict[str, Price]:
    """Each model's price per million tokens from app/prices.json, checked first."""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        raise JudgeFailed(f"{names.PRICES_FILE} cannot be read ({type(err).__name__})") from None
    problem = prices_problem(document)
    if problem:
        raise JudgeFailed(f"{names.PRICES_FILE} is not usable: {problem}")
    return {model_id: Price(**entry) for model_id, entry in document["per_million_tokens"].items()}


def cost_micro_usd(price: Price, input_tokens: float, output_tokens: float) -> int:
    """An answer's cost in millionths of a US dollar, the lead's decision for 3.3."""
    return round(input_tokens * price.input + output_tokens * price.output)


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------
def _is_count(value: object) -> bool:
    """A whole number, 0 or more, as JSON Schema's integer reads one (1000.0 is one)."""
    if type(value) is int:
        return value >= 0
    return type(value) is float and value.is_integer() and value >= 0


def payload_problem(payload: object) -> str | None:
    """Why payload is not a judge_payload, or None when it is one. It quotes no value, and
    no caller's key: a key can carry an identity or a question."""
    if not isinstance(payload, dict):
        return f"a {type(payload).__name__}, not an object"
    expected = set(names.JUDGE_PAYLOAD_FIELDS)
    if set(payload) != expected:
        missing = sorted(expected - set(payload))
        unexpected = len(set(payload) - expected)
        return f"fields missing {missing}, and {unexpected} unexpected field(s)"
    run_id, identity = payload["run_id"], payload["identity"]
    if not isinstance(run_id, str) or not names.is_run_id(run_id):
        return f"run_id must match {names.RUN_ID_PATTERN}"
    if not isinstance(identity, str) or not names.is_run_identity(identity):
        return f"identity must match {names.RUN_IDENTITY_PATTERN}"
    if not identity.startswith(f"{run_id}-") or not names.is_identity(identity[len(run_id) + 1 :]):
        return "identity must be one of run_id's"
    for field in ("assigned", "served"):
        if not isinstance(payload[field], str) or payload[field] not in names.VARIANTS:
            return f"{field} must be one of {', '.join(names.VARIANTS)}"
    served_model = names.MODEL_BY_VARIANT[payload["served"]]
    if payload["model_id"] != served_model:
        return f"model_id must be the served variant's model, {served_model}"
    if not isinstance(payload["question"], str) or not payload["question"]:
        return "question must be a non-empty string"
    if not isinstance(payload["answer"], str):
        return "answer must be a string"
    for field in ("input_tokens", "output_tokens"):
        if not _is_count(payload[field]):
            return f"{field} must be a whole number, 0 or more"
    latency = payload["latency_ms"]
    if not _is_number(latency) or latency < 0:
        return "latency_ms must be a finite number, 0 or more"
    return None


def _run_id_of(event: object) -> str:
    """The payload's run ID for a log line, or "?" when it has none to show."""
    run_id = event.get("run_id") if isinstance(event, dict) else None
    return run_id if isinstance(run_id, str) and names.is_run_id(run_id) else "?"


# ---------------------------------------------------------------------------
# The judge call and its reply
# ---------------------------------------------------------------------------
def converse_request(question: str, answer: str) -> dict:
    """The Converse request task 1.2 proved, for one answer to one question."""
    return {
        "modelId": names.MODEL_JUDGE,
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": [
            {"role": "user", "content": [{"text": f"Question: {question}\nAnswer: {answer}"}]}
        ],
        "inferenceConfig": {"maxTokens": names.JUDGE_MAX_TOKENS},
        "additionalModelRequestFields": nested_field(
            names.THINKING_DISABLED_FIELD, names.THINKING_DISABLED_VALUE
        ),
    }


def verdict(text: str) -> bool | None:
    """True or False when text is the JSON the judge is asked for, give or take whitespace
    around it; None for anything else."""
    try:
        reply = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if isinstance(reply, dict) and set(reply) == {JUDGE_REPLY_FIELD}:
        helpful = reply[JUDGE_REPLY_FIELD]
        if isinstance(helpful, bool):
            return helpful
    return None


def _service_error(err: ClientError) -> JudgeThrottled | JudgeFailed:
    """The typed error for a Bedrock error reply, without the service's own message."""
    code = err.response.get("Error", {}).get("Code") or "an unnamed error"
    meta = err.response.get("ResponseMetadata", {})
    status, request_id = meta.get("HTTPStatusCode"), meta.get("RequestId")
    where = ", ".join(
        part
        for part in (status and f"HTTP {status}", request_id and f"request {request_id}")
        if part
    )
    detail = f"Bedrock {code} from {names.MODEL_JUDGE}" + (f" ({where})" if where else "")
    return JudgeThrottled(detail) if code == THROTTLING_CODE else JudgeFailed(detail)


def _error_name(err: Exception) -> str:
    """A ClientError's code, or the exception's class name: never its message."""
    if isinstance(err, ClientError):
        return err.response.get("Error", {}).get("Code") or type(err).__name__
    return type(err).__name__


# ---------------------------------------------------------------------------
# CloudWatch
# ---------------------------------------------------------------------------
def metric_line(variant: str, cost_micro_usd: int, helpful: int, timestamp_ms: int) -> str:
    """AnswerCostMicroUsd and AnswerHelpful as one embedded-metric log line. Its one
    dimension set, exactly Variant, applies to both metrics (docs.aws.amazon.com/
    AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html)."""
    metrics = {
        names.CW_METRIC_ANSWER_COST_MICRO_USD: cost_micro_usd,
        names.CW_METRIC_ANSWER_HELPFUL: helpful,
    }
    document = {
        "_aws": {
            "Timestamp": timestamp_ms,
            "CloudWatchMetrics": [
                {
                    "Namespace": names.CW_NAMESPACE,
                    "Dimensions": names.CW_DIMENSION_SETS,
                    "Metrics": [
                        {"Name": name, "Unit": names.CW_METRIC_UNITS[name]} for name in metrics
                    ],
                }
            ],
        },
        names.CW_DIMENSION_VARIANT: variant,
        **metrics,
    }
    return json.dumps(document)


def _print(line: str) -> None:
    print(line, flush=True)  # Lambda sends stdout to CloudWatch Logs, which reads the metrics


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------
class Judge:
    """One rm-judge container: its clients and prices, each built on first use and kept."""

    def __init__(
        self,
        *,
        bedrock_factory,
        flagsmith_factory,
        prices_loader=load_prices,
        emit=_print,
        wall=time.time,
    ) -> None:
        self._bedrock_factory = bedrock_factory
        self._flagsmith_factory = flagsmith_factory
        self._prices_loader = prices_loader
        self.emit, self.wall = emit, wall
        self._bedrock_client = None
        self._flagsmith = None
        self._prices: dict[str, Price] | None = None

    def handle(self, event: object, context: object = None) -> dict:
        """Judge one answer and send its outcome, which it returns. Or it logs one line and
        raises, having sent no event. context is Lambda's: its request ID, kept across
        Lambda's retries, and the time the function has left."""
        request_id = getattr(context, "aws_request_id", None)
        try:
            return self._judge(event, context, request_id)
        except Exception as err:
            detail = f": {err}" if isinstance(err, JudgeThrottled | JudgeFailed) else ""
            logger.error(
                "run %s, request %s: attempt failed with %s%s; no outcome sent",
                _run_id_of(event),
                request_id,
                type(err).__name__,
                detail,
            )
            raise
        finally:
            self._flush(event, request_id)  # before every return, error paths included

    def _judge(self, event: object, context: object, request_id: str | None) -> dict:
        problem = payload_problem(event)
        if problem:
            raise JudgeFailed(f"not a judge_payload: {problem}")
        cost = cost_micro_usd(
            self._price(event["model_id"]), event["input_tokens"], event["output_tokens"]
        )
        client = self._client()
        helpful = int(self._verdict(event))  # 1 or 0: an int, never a bool
        outcome = dict(zip(names.EVENTS, (helpful, event["latency_ms"], cost), strict=True))
        if context is not None:
            left = context.get_remaining_time_in_millis() / 1000
            if left < JUDGE_FLUSH_RESERVE_SECONDS:
                raise JudgeFailed(
                    f"{left:.1f} s left, under the {JUDGE_FLUSH_RESERVE_SECONDS} s a flush"
                    " needs; nothing is written, so Lambda's retry starts clean"
                )
        # The writes: CloudWatch, then the Flagsmith events, one at a time. Once the first
        # event is tracked nothing raises, because a retry would track them all again.
        self.emit(metric_line(event["served"], cost, helpful, int(self.wall() * 1000)))
        for count, (name, value) in enumerate(outcome.items()):
            try:
                client.track_event(name, identifier=event["identity"], value=value)
            except Exception as err:
                if count == 0:
                    raise  # nothing tracked: the failure line, and Lambda's retry
                logger.warning(
                    "run %s, request %s: tracking %s failed with %s after %d of %d events;"
                    " not retried, so no event is sent twice",
                    event["run_id"],
                    request_id,
                    name,
                    type(err).__name__,
                    count,
                    len(outcome),
                )
                break
        return outcome

    def _price(self, model_id: str) -> Price:
        if self._prices is None:
            self._prices = self._prices_loader()
        return self._prices[model_id]

    def _client(self):
        if self._flagsmith is None:
            try:
                self._flagsmith = self._flagsmith_factory()
            except Exception as err:
                raise JudgeFailed(f"no Flagsmith client ({_error_name(err)})") from None
        return self._flagsmith

    def _bedrock(self):
        if self._bedrock_client is None:
            self._bedrock_client = self._bedrock_factory()
        return self._bedrock_client

    def _verdict(self, event: dict) -> bool:
        """The judge's verdict on one answer; an unparseable reply is False, and logged."""
        request = converse_request(event["question"], event["answer"])
        forbidden = sampling_parameters(request)
        if forbidden:
            raise JudgeFailed(f"refusing to send sampling parameters: {', '.join(forbidden)}")
        try:
            reply = self._bedrock().converse(**request)
        except ClientError as err:
            raise _service_error(err) from None
        except BotoCoreError as err:
            raise JudgeFailed(
                f"Bedrock call to {names.MODEL_JUDGE} failed: {type(err).__name__}"
            ) from None
        try:
            stop_reason = reply["stopReason"]
            content = reply["output"]["message"]["content"]
            text = "".join(block["text"] for block in content if "text" in block)
        except (KeyError, TypeError):
            raise JudgeFailed(f"a reply from {names.MODEL_JUDGE} of an unexpected shape") from None
        if stop_reason == names.STOP_REASON_MAX_TOKENS:
            raise JudgeFailed(
                f"{names.MODEL_JUDGE} stopped at maxTokens {names.JUDGE_MAX_TOKENS} (stopReason"
                f" {names.STOP_REASON_MAX_TOKENS}), so there is no verdict"
            )
        helpful = verdict(text)
        if helpful is None:
            logger.warning(
                "run %s: the judge's reply is not %s or %s, so it counts as not helpful;"
                " the reply: %r",
                event["run_id"],
                HELPFUL_REPLY,
                NOT_HELPFUL_REPLY,
                text,
            )
            return False
        return helpful

    def _flush(self, event: object, request_id: str | None) -> None:
        """flush_events() before every return, once a client exists (design.md section 4).
        It never raises: the events may be tracked already, and a retry would send them
        again."""
        if self._flagsmith is None:
            return
        try:
            self._flagsmith.flush_events()
        except Exception as err:
            logger.warning(
                "run %s, request %s: flush_events failed with %s",
                _run_id_of(event),
                request_id,
                type(err).__name__,
            )


# ---------------------------------------------------------------------------
# Clients and the entry point
# ---------------------------------------------------------------------------
def new_bedrock_client():
    """A bedrock-runtime client in us-east-1 that makes one attempt per call, bounded well
    inside the function's 30 s. botocore's defaults, 60 s each, would outlive it."""
    return boto3.client(
        names.BEDROCK_RUNTIME_SERVICE,
        region_name=names.REGION,
        config=Config(
            connect_timeout=JUDGE_BEDROCK_CONNECT_TIMEOUT_SECONDS,
            read_timeout=JUDGE_BEDROCK_READ_TIMEOUT_SECONDS,
            retries={"total_max_attempts": 1},
        ),
    )


def flagsmith_client(environment_key: str, **overrides) -> Flagsmith:
    """The SDK client rm-judge uses: remote evaluation with events on, as task 1.2 proved.
    Tests pass a local server's URLs in overrides."""
    return Flagsmith(environment_key=environment_key, **names.FLAGSMITH_CLIENT_OPTIONS, **overrides)


def new_flagsmith_client(secrets=None) -> Flagsmith:
    """The SDK client, built with the server key read from Secrets Manager at run time. The
    key stays in memory: nothing logs or returns it (design.md section 8)."""
    if secrets is None:
        secrets = boto3.client("secretsmanager", region_name=names.REGION)
    key = secrets.get_secret_value(SecretId=names.SECRET_FLAGSMITH_SERVER_KEY)["SecretString"]
    return flagsmith_client(key)


_judge: Judge | None = None


def handler(event, context):
    """The Lambda entry point: one Judge per container, so its clients and prices outlive
    an invocation."""
    global _judge
    if _judge is None:
        _judge = Judge(
            bedrock_factory=new_bedrock_client,
            flagsmith_factory=new_flagsmith_client,
            prices_loader=load_prices,
        )
    return _judge.handle(event, context)
