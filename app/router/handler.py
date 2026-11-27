"""rm-router: one question in, the right model's answer out, the experiment recorded (task 3.2).

API Gateway's HTTP API sends each POST /ask here (payload format 2.0: a JSON body, maybe
base64-encoded). The evaluation order is design.md section 4's; the request path and the
metric are section 5's. Per request:

1. The body must be an answer_request, {question, identity}, checked here without
   jsonschema. Anything else is 400 {"error": "bad_request"}.
2. The active run pointer is read from rm-runs, cached 30 seconds. A pointer without
   feature_key counts as no active run.
   - No active run: serve assistant_default_model, read from the environment flags and
     cached 30 seconds. Record no exposure and no event, and do not invoke rm-judge.
   - With an active run, an identity that is not one of that run's gets a 400 before any
     Flagsmith call.
3. get_identity_flags, once. The experiment feature and the kill switch both come from
   that one result; get_experiment_flag is never called, because it makes its own identity
   request.
4. If the experiment flag is enabled, the identity is in the experiment and flag.variant
   is set, the exposure is tracked with flag.variant and the experiment's ID.
5. If assistant_cheaper_enabled is off, current is served; otherwise the assigned
   variation, which is the flag's value.
6. The served answer function is invoked synchronously, and the invoke is timed.
   AnswerLatencyMs, with Variant = the served variant, is emitted as an embedded-metric log
   line for every answer invoke, success or error, so a slow failure still reaches the
   latency alarm.
7. On success, rm-judge gets the judge_payload asynchronously and the reply is 200
   {"answer": ...}. On an error, rm-judge is not invoked and nothing is recorded. An
   AnswerThrottled is 503. Any other function error (Lambda's own timeout included), a
   reply that is not the served arm's answer_response, or a failed invoke is 502. Either
   body is {"error": <errorType, or invoke_failed>}.
8. flush_events() before every return. Where an answer is invoked, it starts on a thread
   just before the invoke, right after the exposure, and is joined before the return. A
   slow events API then overlaps the answer instead of adding to it (contract change c3).
   The SDK client gives up on a request after 3 s and retries it at most once, which bounds
   the identity request. Its events processor ignores both settings, so a flush can take
   up to 12 s.

Also the lead's decision: flags without the experiment feature while a run is active
(just after launch, say) serve current, record nothing, do not invoke rm-judge, and log
one warning that does not name the identity.

Decided here, for the lead to confirm:
- When rm-runs, the Secrets Manager key or the identity request fails, the reply is 502
  {"error": "upstream_failed"} and nothing is answered. A silent fallback would serve
  answers whose run data is lost unseen.
- A pointer whose run_id is not a run ID is treated the same way.
- With no active run, a failed or odd read of assistant_default_model serves current, its
  default.
- A missing kill switch serves current, like a disabled one. An experiment value that is
  neither variant is treated like a missing experiment feature.
- A failed hand-off to rm-judge still returns the answer, with a warning.
- Invokes make one attempt, because a retry would sit inside the measured latency.

No log line names an identity or quotes a question, an answer or a key.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
from dataclasses import dataclass

import boto3
from botocore.config import Config
from flagsmith import Flagsmith
from flagsmith.exceptions import FlagsmithFeatureDoesNotExistError
from urllib3 import Retry

from contracts import names

logger = logging.getLogger(__name__)

QUESTION_FIELD, IDENTITY_FIELD = names.ANSWER_REQUEST_FIELDS
ANSWER_FIELD, INPUT_TOKENS_FIELD, OUTPUT_TOKENS_FIELD, MODEL_ID_FIELD = names.ANSWER_RESPONSE_FIELDS
RUN_ID_ATTRIBUTE, FEATURE_KEY_ATTRIBUTE = names.POINTER_ATTRIBUTES
THROTTLED = "AnswerThrottled"  # app/answer's error for a Bedrock throttle (task 3.1)
BAD_REQUEST = "bad_request"
INVOKE_FAILED = "invoke_failed"
UPSTREAM_FAILED = "upstream_failed"
SYNC, ASYNC = "RequestResponse", "Event"  # Lambda InvocationType values
IDENTITY_USER_SEPARATOR = "-user-"  # <run_id>-user-NNN (names.run_identity)
# The SDK's request limits (contract change c3), so a Flagsmith that never answers cannot
# spend the router's 29 s: each request gives up after 3 s and is retried at most once,
# with no backoff. Until c3 merges, the values are kept here.
FLAGSMITH_REQUEST_TIMEOUT_SECONDS = 3  # names.FLAGSMITH_REQUEST_TIMEOUT_SECONDS after c3
FLAGSMITH_RETRY = {"total": 1, "backoff_factor": 0}  # names.FLAGSMITH_RETRY after c3


class Upstream(Exception):
    """rm-runs, the Flagsmith key or Flagsmith failed before anything was answered."""


@dataclass(frozen=True)
class Run:
    run_id: str
    feature_key: str


@dataclass(frozen=True)
class Judged:
    """What rm-judge needs besides the answer: the run, and what the experiment assigned."""

    run_id: str
    assigned: str


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
def request_problem(body: object) -> str | None:
    """Why body is not an answer_request, or None when it is one. Never quotes the question."""
    if not isinstance(body, dict):
        return f"a {type(body).__name__}, not an object"
    if set(body) != set(names.ANSWER_REQUEST_FIELDS):
        return f"fields {sorted(map(str, body))}, expected {sorted(names.ANSWER_REQUEST_FIELDS)}"
    question, identity = body[QUESTION_FIELD], body[IDENTITY_FIELD]
    if not isinstance(question, str) or not question:
        return f"{QUESTION_FIELD} must be a non-empty string"
    if not isinstance(identity, str) or not names.is_run_identity(identity):
        return f"{IDENTITY_FIELD} must match {names.RUN_IDENTITY_PATTERN}"
    return None


def _body(event: object) -> object:
    """The event's JSON body, or None when there is none to read."""
    if not isinstance(event, dict) or not isinstance(event.get("body"), str):
        return None
    raw = event["body"]
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _run_of(identity: str) -> str:
    """The run ID before -user- in a run identity."""
    return identity.partition(IDENTITY_USER_SEPARATOR)[0]


def _response(status: int, body: dict) -> dict:
    """An HTTP API response (payload format 2.0)."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# What came back from the answer function
# ---------------------------------------------------------------------------
def _error_type(payload: bytes) -> str | None:
    """The errorType Lambda put in a failed invoke's payload, if it has one."""
    try:
        error_type = json.loads(payload).get("errorType")
    except (ValueError, AttributeError):
        return None
    return error_type if isinstance(error_type, str) and error_type else None


def _answer(payload: bytes, served: str) -> dict | None:
    """The answer_response in payload, from the served arm's model, or None."""
    try:
        answer = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(answer, dict) or set(answer) != set(names.ANSWER_RESPONSE_FIELDS):
        return None
    text, model_id = answer[ANSWER_FIELD], answer[MODEL_ID_FIELD]
    if not isinstance(text, str) or not text:
        return None
    for field in (INPUT_TOKENS_FIELD, OUTPUT_TOKENS_FIELD):
        tokens = answer[field]
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            return None
    if model_id != names.MODEL_BY_VARIANT[served]:
        return None
    return answer


def _exposed(flag) -> bool:
    """In the experiment, the flag enabled and a variant set, as get_experiment_flag checks."""
    experiment = getattr(flag, "experiment", None)
    return bool(
        flag.enabled
        and experiment is not None
        and experiment.in_experiment
        and getattr(flag, "variant", None)
    )


def _print(line: str) -> None:
    print(line, flush=True)  # Lambda sends stdout to CloudWatch Logs, which reads the metric


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------
class Router:
    """One per container: it keeps the clients, the pointer and the default model."""

    def __init__(
        self,
        *,
        dynamodb,
        lambda_client,
        flagsmith_factory,
        clock=time.monotonic,
        timer=time.perf_counter,
        wall=time.time,
        emit=_print,
    ) -> None:
        self.dynamodb = dynamodb
        self.lambda_client = lambda_client
        self._flagsmith_factory = flagsmith_factory
        self._flagsmith = None
        self.clock, self.timer, self.wall, self.emit = clock, timer, wall, emit
        self._pointer: tuple[float, Run | None] | None = None
        self._default: tuple[float, str] | None = None
        self._flushing: threading.Thread | None = None  # this request's flush, while it runs

    def handle(self, event: object) -> dict:
        self._flushing = None
        try:
            return self._route(event)
        finally:
            self._finish_flush()  # before every return, error paths included

    # -- the steps ----------------------------------------------------------
    def _route(self, event: object) -> dict:
        body = _body(event)
        problem = request_problem(body)
        if problem:
            logger.warning("bad request: %s", problem)
            return _response(400, {"error": BAD_REQUEST})
        question, identity = body[QUESTION_FIELD], body[IDENTITY_FIELD]
        try:
            return self._decide(question, identity)
        except Upstream as err:
            logger.warning("%s failed; nothing was answered", err)
            return _response(502, {"error": UPSTREAM_FAILED})

    def _decide(self, question: str, identity: str) -> dict:
        run = self._active_run()
        if run is None:
            return self._serve(self._default_variant(), question, identity)
        if _run_of(identity) != run.run_id:
            logger.warning("bad request: an identity that is not one of run %s's", run.run_id)
            return _response(400, {"error": BAD_REQUEST})

        flags = self._identity_flags(identity)
        try:
            flag = flags.get_flag(run.feature_key)
        except FlagsmithFeatureDoesNotExistError:
            logger.warning(
                "run %s: the flags have no %s; serving %s and recording nothing",
                run.run_id,
                run.feature_key,
                names.VARIANT_CURRENT,
            )
            return self._serve(names.VARIANT_CURRENT, question, identity)
        assigned = flag.value
        if assigned not in names.VARIANTS:
            logger.warning(
                "run %s: %s holds neither %s; serving %s and recording nothing",
                run.run_id,
                run.feature_key,
                " nor ".join(names.VARIANTS),
                names.VARIANT_CURRENT,
            )
            return self._serve(names.VARIANT_CURRENT, question, identity)

        if _exposed(flag):
            self._flagsmith.track_exposure_event(
                run.feature_key,
                identifier=identity,
                value=flag.variant,
                metadata={names.EXPOSURE_METADATA_KEY: flag.experiment.id},
            )
        served = assigned if self._cheaper_enabled(flags, run) else names.VARIANT_CURRENT
        return self._serve(served, question, identity, Judged(run.run_id, assigned))

    def _serve(self, served: str, question: str, identity: str, judged: Judged | None = None):
        """Invoke the served arm, time it, emit the latency, and judge a good answer."""
        function = names.ANSWER_FUNCTION_BY_VARIANT[served]
        request = json.dumps({QUESTION_FIELD: question, IDENTITY_FIELD: identity}).encode()
        self._start_flush()  # right after the exposure: it runs while the answer does
        started = self.timer()
        try:
            reply = self.lambda_client.invoke(
                FunctionName=function, InvocationType=SYNC, Payload=request
            )
            payload = reply["Payload"].read()
        except Exception as err:
            reply, payload, failure = None, b"", type(err).__name__
        latency_ms = (self.timer() - started) * 1000
        self._emit_latency(served, latency_ms)  # every invoke, success or error

        if reply is None:
            logger.warning("invoking %s failed (%s)", function, failure)
            return _response(502, {"error": INVOKE_FAILED})
        if reply.get("FunctionError"):
            error_type = _error_type(payload)
            logger.warning("%s raised %s", function, error_type or "an error with no type")
            status = 503 if error_type == THROTTLED else 502
            return _response(status, {"error": error_type or INVOKE_FAILED})
        answer = _answer(payload, served)
        if answer is None:
            logger.warning("%s replied with no answer_response from its model", function)
            return _response(502, {"error": INVOKE_FAILED})
        if judged is not None:
            self._judge(judged, identity, served, question, answer, latency_ms)
        return _response(200, {ANSWER_FIELD: answer[ANSWER_FIELD]})

    def _judge(self, judged, identity, served, question, answer, latency_ms) -> None:
        values = (
            judged.run_id,
            identity,
            judged.assigned,
            served,
            answer[MODEL_ID_FIELD],
            question,
            answer[ANSWER_FIELD],
            answer[INPUT_TOKENS_FIELD],
            answer[OUTPUT_TOKENS_FIELD],
            latency_ms,
        )
        payload = dict(zip(names.JUDGE_PAYLOAD_FIELDS, values, strict=True))
        try:
            self.lambda_client.invoke(
                FunctionName=names.JUDGE_FUNCTION,
                InvocationType=ASYNC,
                Payload=json.dumps(payload).encode(),
            )
        except Exception as err:
            logger.warning(
                "handing the answer to %s failed (%s); it goes unjudged",
                names.JUDGE_FUNCTION,
                type(err).__name__,
            )

    # -- what the steps read ------------------------------------------------
    def _client(self):
        if self._flagsmith is None:
            try:
                self._flagsmith = self._flagsmith_factory()
            except Exception as err:
                raise Upstream(f"the Flagsmith client ({type(err).__name__})") from None
        return self._flagsmith

    def _active_run(self) -> Run | None:
        now = self.clock()
        if self._pointer is not None and now - self._pointer[0] < names.POINTER_CACHE_SECONDS:
            return self._pointer[1]
        try:
            reply = self.dynamodb.get_item(
                TableName=names.RUNS_TABLE,
                Key={names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}},
            )
        except Exception as err:
            raise Upstream(
                f"reading the {names.RUNS_TABLE} pointer ({type(err).__name__})"
            ) from None
        run = _run_from(reply.get("Item"))
        self._pointer = (now, run)
        return run

    def _default_variant(self) -> str:
        now = self.clock()
        if self._default is not None and now - self._default[0] < names.DEFAULT_MODEL_CACHE_SECONDS:
            return self._default[1]
        try:
            flags = self._client().get_environment_flags()
            value = flags.get_feature_value(names.DEFAULT_MODEL_FEATURE)
        except Exception as err:
            logger.warning(
                "cannot read %s (%s); serving %s",
                names.DEFAULT_MODEL_FEATURE,
                type(err).__name__,
                names.DEFAULT_MODEL_DEFAULT,
            )
            return names.DEFAULT_MODEL_DEFAULT
        if value not in names.DEFAULT_MODEL_VALUES:
            logger.warning(
                "%s holds neither %s; serving %s",
                names.DEFAULT_MODEL_FEATURE,
                " nor ".join(names.DEFAULT_MODEL_VALUES),
                names.DEFAULT_MODEL_DEFAULT,
            )
            value = names.DEFAULT_MODEL_DEFAULT
        self._default = (now, value)
        return value

    def _identity_flags(self, identity: str):
        client = self._client()
        try:
            return client.get_identity_flags(identity)
        except Exception as err:
            raise Upstream(f"get_identity_flags ({type(err).__name__})") from None

    def _cheaper_enabled(self, flags, run: Run) -> bool:
        try:
            return bool(flags.is_feature_enabled(names.KILL_SWITCH_FEATURE))
        except FlagsmithFeatureDoesNotExistError:
            logger.warning(
                "run %s: the flags have no %s; serving %s",
                run.run_id,
                names.KILL_SWITCH_FEATURE,
                names.VARIANT_CURRENT,
            )
            return False

    def _emit_latency(self, variant: str, latency_ms: float) -> None:
        """AnswerLatencyMs in the embedded metric format, with exactly the Variant dimension
        (docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/
        CloudWatch_Embedded_Metric_Format_Specification.html)."""
        metric = names.CW_METRIC_ANSWER_LATENCY_MS
        document = {
            "_aws": {
                "Timestamp": int(self.wall() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": names.CW_NAMESPACE,
                        "Dimensions": names.CW_DIMENSION_SETS,
                        "Metrics": [{"Name": metric, "Unit": names.CW_METRIC_UNITS[metric]}],
                    }
                ],
            },
            names.CW_DIMENSION_VARIANT: variant,
            metric: latency_ms,
        }
        self.emit(json.dumps(document))

    # -- flushing (contract change c3, option (a)) --------------------------------------
    def _start_flush(self) -> None:
        """Flush on a thread while the answer runs, so a slow events API overlaps the answer
        instead of adding to it: the request takes about the identity request plus the
        longer of the two. Nothing is tracked while it runs: rm-judge is a Lambda invoke."""
        if self._flagsmith is None:
            return  # before the first Flagsmith call, nothing was ever tracked
        self._flushing = threading.Thread(target=self._flush_now, name="flush", daemon=True)
        self._flushing.start()

    def _finish_flush(self) -> None:
        """Join this request's flush thread, or flush now when none was started."""
        thread, self._flushing = self._flushing, None
        if thread is not None:
            thread.join()
        else:
            self._flush_now()

    def _flush_now(self) -> None:
        if self._flagsmith is None:
            return
        try:
            self._flagsmith.flush_events()
        except Exception as err:
            logger.warning("flush_events failed (%s)", type(err).__name__)


def _run_from(item: dict | None) -> Run | None:
    """The run a pointer item names, or None when there is no active run."""
    if not item:
        return None
    run_id = item.get(RUN_ID_ATTRIBUTE, {}).get("S")
    feature_key = item.get(FEATURE_KEY_ATTRIBUTE, {}).get("S")
    if not feature_key:
        return None  # PLANNED: not launched, so not an active run to the router
    if not (isinstance(run_id, str) and names.is_run_id(run_id)):
        raise Upstream(f"the {names.RUNS_TABLE} pointer (its run_id is not a run ID)")
    return Run(run_id, feature_key)


# ---------------------------------------------------------------------------
# The Lambda entry point
# ---------------------------------------------------------------------------
def new_dynamodb_client():
    return boto3.client("dynamodb", region_name=names.REGION)


def new_lambda_client():
    """One attempt per invoke: a retry would sit inside the latency the alarm reads."""
    return boto3.client(
        "lambda", region_name=names.REGION, config=Config(retries={"total_max_attempts": 1})
    )


def flagsmith_client(environment_key: str, **overrides) -> Flagsmith:
    """The SDK client rm-router uses: remote evaluation with events on, and c3's request
    limits. Tests pass a local server's URLs in overrides."""
    return Flagsmith(
        environment_key=environment_key,
        **names.FLAGSMITH_CLIENT_OPTIONS,
        request_timeout_seconds=FLAGSMITH_REQUEST_TIMEOUT_SECONDS,
        retries=Retry(**FLAGSMITH_RETRY),
        **overrides,
    )


def new_flagsmith_client() -> Flagsmith:
    """The SDK client, built with the server key read from Secrets Manager at run time. The
    key stays in memory: nothing logs or returns it (design.md section 8)."""
    secrets = boto3.client("secretsmanager", region_name=names.REGION)
    key = secrets.get_secret_value(SecretId=names.SECRET_FLAGSMITH_SERVER_KEY)["SecretString"]
    return flagsmith_client(key)


_router: Router | None = None


def handler(event, context):
    """The Lambda entry point: one Router per container, so its caches outlive a request."""
    global _router
    if _router is None:
        _router = Router(
            dynamodb=new_dynamodb_client(),
            lambda_client=new_lambda_client(),
            flagsmith_factory=new_flagsmith_client,
        )
    return _router.handle(event)
