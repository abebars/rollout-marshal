"""Task 1.2 spike: the Flagsmith Python SDK and Bedrock Converse from one Lambda.

Deployed as rm-spike-sdk by infra/spike (arm64, python3.13) and destroyed after the spike.
One action per invocation, chosen by event["action"]:

- "router"    The router's role (design.md section 4). ONE client calls get_identity_flags
              for user-001 to user-150 and tracks an exposure, with design.md's arguments,
              only when flag.experiment.in_experiment. Then flush_events(). Returns every
              identity's arm, and the flag fields remote evaluation reported.
- "judge"     The judge's role. A NEW client sends one spike_event per identity through
              track_event, its value set by the arm the router invocation returned
              (event["assignments"]), so this invocation makes no identity request. Then
              flush_events(). Returns each arm's expected mean.
- "converse"  The Bedrock calls of design.md section 3, through Converse.

The Flagsmith SDK key is read at run time from the Secrets Manager secret whose NAME is in
env FLAGSMITH_KEY_SECRET_NAME, and only that secret. It stays in memory: nothing logged or
returned contains it, and redact() checks the response before it leaves.

HttpRecorder reads two private SDK attributes (client.session, the event processor's
session) to count the HTTP responses the SDK got. That is spike instrumentation only; the
real router and judge (tasks 3.2, 3.3) use the public SDK calls alone.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from contracts import names

# Spike names: design.md section 13 and tasks.md 1.2 give them; names.py has only the prefix.
SPIKE_FLAG = f"{names.SPIKE_NAME_PREFIX}flag"
SPIKE_EVENT = f"{names.SPIKE_NAME_PREFIX}event"
SPIKE_METRIC = f"{names.SPIKE_NAME_PREFIX}metric"  # mean of SPIKE_EVENT; made by the lead

SDK_KEY_SECRET_ENV = "FLAGSMITH_KEY_SECRET_NAME"
SERVER_SIDE_KEY_PREFIX = "ser."  # flagsmith.py checks this prefix for local evaluation
ACTIONS = ("selfcheck", "router", "judge", "converse")
FLAGS_LOGGER = "flagsmith"

# The execution environment: one id per container, and how many invocations it has run.
CONTAINER_ID = uuid.uuid4().hex
_invocations = 0


# ---------------------------------------------------------------------------
# Clients (module-level factories, so tests can swap them)
# ---------------------------------------------------------------------------
def new_flagsmith_client(sdk_key: str, **overrides: Any):
    """A remote-evaluation client with events on (design.md section 4)."""
    from flagsmith import Flagsmith

    return Flagsmith(environment_key=sdk_key, **names.FLAGSMITH_CLIENT_OPTIONS, **overrides)


def lambda_flagsmith_client(sdk_key: str):
    """The client the deployed function uses: Flagsmith's default API and events URLs."""
    return new_flagsmith_client(sdk_key)


def secrets_client():
    import boto3

    return boto3.client("secretsmanager", region_name=names.REGION)


def bedrock_client():
    import boto3
    from botocore.config import Config

    config = Config(read_timeout=120, connect_timeout=10, retries={"mode": "standard"})
    return boto3.client(names.BEDROCK_RUNTIME_SERVICE, region_name=names.REGION, config=config)


def read_sdk_key(secrets=None) -> str:
    """The SDK key's value, read by the secret NAME in the environment. Never logged."""
    name = os.environ[SDK_KEY_SECRET_ENV]
    if name != names.SECRET_FLAGSMITH_SERVER_KEY:
        raise ValueError(
            f"{SDK_KEY_SECRET_ENV} must name {names.SECRET_FLAGSMITH_SERVER_KEY}: "
            "this function only reads the SDK key"
        )
    return (secrets or secrets_client()).get_secret_value(SecretId=name)["SecretString"]


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------
class HttpRecorder:
    """Status, path, size and timing of each HTTP response a Flagsmith client gets.

    Request and response headers are never read, so the X-Environment-Key header (the SDK
    key) cannot end up here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self.unavailable: list[str] = []

    @classmethod
    def attach(cls, client) -> HttpRecorder:
        recorder = cls()
        recorder._hook(getattr(client, "session", None), "flags")
        processor = getattr(client, "_event_processor", None)
        recorder._hook(getattr(processor, "_session", None), "events")
        return recorder

    def _hook(self, session, kind: str) -> None:
        if session is None:
            self.unavailable.append(kind)
            return

        def on_response(response, *args, **kwargs):
            entry = {
                "kind": kind,
                "status": response.status_code,
                "path": urlsplit(response.request.url).path,
                "elapsed_ms": round(response.elapsed.total_seconds() * 1000, 1),
            }
            if kind == "events":
                entry["events"] = _count_events(response.request.body)
            with self._lock:
                self._entries.append(entry)
            return response

        session.hooks["response"].append(on_response)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._entries)


def _count_events(body) -> int | None:
    try:
        return len(json.loads(body)["events"])
    except (TypeError, ValueError, KeyError):
        return None


class SdkLogCapture(logging.Handler):
    """Keeps the flagsmith SDK's WARNING-and-above log records while it is entered."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[dict] = []
        self._logger = logging.getLogger(FLAGS_LOGGER)
        self._old_level = self._logger.level

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {"logger": record.name, "level": record.levelname, "message": record.getMessage()}
        )

    def __enter__(self) -> SdkLogCapture:
        self._logger.addHandler(self)
        if self._logger.getEffectiveLevel() > logging.WARNING:
            self._logger.setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._old_level)


def _client_facts(client) -> dict:
    processor = getattr(client, "_event_processor", None)
    return {
        "instance": uuid.uuid4().hex,  # a new one for every client this module builds
        "evaluation": "local" if client.enable_local_evaluation else "remote",
        "api_url": getattr(client, "api_url", None),
        "events_endpoint": getattr(processor, "_batch_endpoint", None),
        "max_buffer_items": getattr(processor, "_max_buffer", None),
        "flush_interval_seconds": getattr(processor, "_flush_interval_seconds", None),
    }


def _delivery(recorder: HttpRecorder, tracked: int) -> dict:
    """What the events API had answered by the time flush_events() returned."""
    entries = recorder.snapshot()
    identity = [e for e in entries if e["kind"] == "flags" and e["path"].endswith("/identities/")]
    posts = [e for e in entries if e["kind"] == "events"]
    delivered = sum(e["events"] or 0 for e in posts if e["status"] < 400)
    return {
        "identity_requests": {
            "count": len(identity),
            "statuses": dict(Counter(str(e["status"]) for e in identity)),
            "max_elapsed_ms": max((e["elapsed_ms"] for e in identity), default=None),
        },
        "event_posts": [
            {"status": e["status"], "events": e["events"], "elapsed_ms": e["elapsed_ms"]}
            for e in posts
        ],
        "events_tracked": tracked,
        "events_delivered": delivered,
        "delivered_before_return": tracked > 0 and delivered == tracked,
        "events_refused": any(e["status"] in (401, 403) for e in posts),
        "instrumentation_unavailable": recorder.unavailable,
    }


# ---------------------------------------------------------------------------
# Invocation 1: the router's role
# ---------------------------------------------------------------------------
def _flag_row(identity: str, flag) -> dict:
    experiment = getattr(flag, "experiment", None)
    return {
        "identity": identity,
        "enabled": flag.enabled,
        "value": flag.value,
        "variant": getattr(flag, "variant", None),
        "reason": getattr(flag, "reason", None),
        "experiment": None
        if experiment is None
        else {
            "id": experiment.id,
            "name": experiment.name,
            "in_experiment": experiment.in_experiment,
        },
        "exposure_tracked": False,
    }


def _in_experiment(row: dict) -> bool:
    return bool(row["experiment"] and row["experiment"]["in_experiment"])


def run_router(sdk_key: str, make_client: Callable | None = None) -> dict:
    from flagsmith.exceptions import FlagsmithAPIError, FlagsmithFeatureDoesNotExistError

    make_client = make_client or new_flagsmith_client
    rows: list[dict] = []
    aborted = None
    with SdkLogCapture() as log:
        client = make_client(sdk_key)
        recorder = HttpRecorder.attach(client)
        started = time.perf_counter()
        identity = None
        try:
            for n in range(1, names.IDENTITY_COUNT + 1):
                identity = names.identity(n)
                flags = client.get_identity_flags(identity)  # the one identity request
                try:
                    flag = flags.get_flag(SPIKE_FLAG)
                except FlagsmithFeatureDoesNotExistError:
                    aborted = f"{SPIKE_FLAG} is not in the environment's flags"
                    break
                row = _flag_row(identity, flag)
                rows.append(row)
                if n == 1 and row["experiment"] is None:
                    # Stop after one identity request instead of spending 150 on nothing.
                    aborted = f"no running experiment on {SPIKE_FLAG} (flag.experiment is None)"
                    break
                if _in_experiment(row):
                    client.track_exposure_event(
                        SPIKE_FLAG,
                        identifier=identity,
                        value=flag.variant,
                        metadata={names.EXPOSURE_METADATA_KEY: flag.experiment.id},
                    )
                    row["exposure_tracked"] = True
        except FlagsmithAPIError as error:
            aborted = f"identity request for {identity} failed: {error}"
        loop_done = time.perf_counter()
        client.flush_events()  # sends what was tracked, even after an abort
        flushed = time.perf_counter()
        exposures = sum(row["exposure_tracked"] for row in rows)
        delivery = _delivery(recorder, exposures)

    assigned = [r for r in rows if _in_experiment(r) and r["value"] in names.VARIANTS]
    variants_by_arm: dict[str, list[dict]] = {}
    for arm in names.VARIANTS:
        seen = Counter(json.dumps(r["variant"]) for r in assigned if r["value"] == arm)
        variants_by_arm[arm] = [
            {"variant": json.loads(variant), "count": count} for variant, count in seen.items()
        ]
    return {
        "feature": SPIKE_FLAG,
        "aborted": aborted,
        "client": _client_facts(client),
        "assignments": {r["identity"]: r["value"] for r in assigned},
        "arm_counts": dict(Counter(r["value"] for r in assigned)),
        "not_in_experiment": [r["identity"] for r in rows if not _in_experiment(r)],
        "unexpected_values": [r for r in rows if _in_experiment(r) and r not in assigned],
        "samples": {
            arm: next((r for r in assigned if r["value"] == arm), None) for arm in names.VARIANTS
        },
        "variants_by_arm": variants_by_arm,
        "experiment_ids": sorted({r["experiment"]["id"] for r in rows if r["experiment"]}),
        "enabled_counts": dict(Counter(str(r["enabled"]).lower() for r in rows)),
        "exposures_tracked": exposures,
        **delivery,
        "sdk_log": log.records,
        "seconds": {
            "identity_loop": round(loop_done - started, 3),
            "flush_events": round(flushed - loop_done, 3),
        },
        "identities": rows,
    }


# ---------------------------------------------------------------------------
# Invocation 2: the judge's role
# ---------------------------------------------------------------------------
def outcome_value(arm: str, position: int) -> int:
    """The spike_event value for the position-th identity of an arm.

    Mostly 1 for cheaper and mostly 0 for current: every fifth identity in an arm flips, so
    each arm's mean is a known number that is neither 0 nor 1.
    """
    flip = position % 5 == 4
    if arm == names.VARIANT_CHEAPER:
        return 0 if flip else 1
    if arm == names.VARIANT_CURRENT:
        return 1 if flip else 0
    raise ValueError(f"not an arm: {arm!r}")


def _check_assignments(assignments) -> dict[str, str]:
    if not isinstance(assignments, dict) or not assignments:
        raise ValueError("assignments must be a non-empty object of identity to arm")
    for identity, arm in assignments.items():
        if not isinstance(identity, str) or not names.is_identity(identity):
            raise ValueError(f"not an identity: {identity!r}")
        if arm not in names.VARIANTS:
            raise ValueError(f"not an arm: {arm!r} for {identity}")
    return assignments


def run_judge(sdk_key: str, assignments, make_client: Callable | None = None) -> dict:
    assignments = _check_assignments(assignments)
    make_client = make_client or new_flagsmith_client
    positions: Counter = Counter()
    sent: list[dict] = []
    with SdkLogCapture() as log:
        client = make_client(sdk_key)  # a new client: nothing is shared with the router's
        recorder = HttpRecorder.attach(client)
        started = time.perf_counter()
        for identity in sorted(assignments, key=lambda i: int(i.split("-")[1])):
            arm = assignments[identity]
            value = outcome_value(arm, positions[arm])
            positions[arm] += 1
            client.track_event(SPIKE_EVENT, identifier=identity, value=value)
            sent.append({"identity": identity, "arm": arm, "value": value})
        loop_done = time.perf_counter()
        client.flush_events()
        flushed = time.perf_counter()
        delivery = _delivery(recorder, len(sent))

    expected = {}
    for arm in names.VARIANTS:
        values = [s["value"] for s in sent if s["arm"] == arm]
        expected[arm] = {
            "identities": len(values),
            "sum": sum(values),
            "mean": sum(values) / len(values) if values else None,
        }
    return {
        "event": SPIKE_EVENT,
        "client": _client_facts(client),
        "expected": expected,
        **delivery,
        "sdk_log": log.records,
        "seconds": {
            "track_loop": round(loop_done - started, 3),
            "flush_events": round(flushed - loop_done, 3),
        },
        "sent": sent,
    }


# ---------------------------------------------------------------------------
# Bedrock Converse (design.md section 3)
# ---------------------------------------------------------------------------
# The Converse spellings of the sampling parameters, beside names.FORBIDDEN_SAMPLING_PARAMETERS
# (see the comment there: adding them to names.py is the lead's contract change).
_FORBIDDEN_KEYS = frozenset(names.FORBIDDEN_SAMPLING_PARAMETERS) | {"topP", "topK"}

# AWS Bedrock user guide, Adaptive thinking: "To turn thinking off where the model allows it,
# send "thinking": {"type": "disabled"}".
THINKING_DISABLED_FIELDS = {"thinking": {"type": "disabled"}}

ANSWER_SYSTEM = (
    "You are the support assistant for a fictional SaaS product. Answer the customer's "
    f"question in at most {names.ANSWER_MAX_WORDS} words."
)
QUESTION = "How do I reset my password?"
SAMPLE_ANSWER = "Open Settings, choose Security, then Reset password, and follow the emailed link."
JUDGE_SYSTEM = (
    "You grade one support answer for helpfulness. Reply with only JSON: "
    '{"helpful": true} or {"helpful": false}.'
)


def effort_fields(effort: str) -> dict:
    """additionalModelRequestFields for an effort, from names.THINKING_EFFORT_FIELD."""
    outer, inner = names.THINKING_EFFORT_FIELD.split(".")
    return {outer: {inner: effort}}


@dataclass(frozen=True)
class ConverseCall:
    name: str
    request: dict
    optional: bool = False
    expect_error: str | None = None  # the error code this call should get, if any
    notes: str = field(default="", compare=False)


def _answer_request(model_id: str, extra_fields: dict) -> dict:
    return {
        "modelId": model_id,
        "system": [{"text": ANSWER_SYSTEM}],
        "messages": [{"role": "user", "content": [{"text": QUESTION}]}],
        "inferenceConfig": {"maxTokens": names.ANSWER_MAX_TOKENS},
        "additionalModelRequestFields": extra_fields,
    }


def _judge_request(model_id: str) -> dict:
    return {
        "modelId": model_id,
        "system": [{"text": JUDGE_SYSTEM}],
        "messages": [
            {
                "role": "user",
                "content": [{"text": f"Question: {QUESTION}\nAnswer: {SAMPLE_ANSWER}"}],
            }
        ],
        "inferenceConfig": {"maxTokens": names.JUDGE_MAX_TOKENS},
        "additionalModelRequestFields": THINKING_DISABLED_FIELDS,
    }


def converse_calls(include_optional: bool = True) -> list[ConverseCall]:
    low = effort_fields(names.ANSWER_THINKING_EFFORT)
    calls = [
        ConverseCall("current_effort_low", _answer_request(names.MODEL_CURRENT, low)),
        ConverseCall("cheaper_effort_low", _answer_request(names.MODEL_CHEAPER, low)),
        ConverseCall("judge_thinking_disabled", _judge_request(names.MODEL_JUDGE)),
        ConverseCall(
            "cheaper_thinking_disabled",
            _judge_request(names.MODEL_CHEAPER),
            notes="Sonnet 5 with the judge's thinking-disabled request, as a proven fallback",
        ),
        ConverseCall(
            "current_thinking_disabled",
            _answer_request(names.MODEL_CURRENT, THINKING_DISABLED_FIELDS),
            optional=True,
            expect_error="ValidationException",
            notes="Fable 5.1 cannot disable thinking (AWS model card); one attempt for the 400",
        ),
    ]
    return [c for c in calls if include_optional or not c.optional]


def assert_no_sampling(request: dict) -> None:
    """Refuse a request that carries temperature, top_p, top_k, topP or topK anywhere."""

    def walk(value, path: str) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in _FORBIDDEN_KEYS:
                    raise ValueError(f"sampling parameter {key} at {path}.{key}")
                walk(inner, f"{path}.{key}")
        elif isinstance(value, list):
            for i, inner in enumerate(value):
                walk(inner, f"{path}[{i}]")

    walk(request, "request")


def _call_converse(bedrock, call: ConverseCall) -> dict:
    from botocore.exceptions import BotoCoreError, ClientError

    assert_no_sampling(call.request)
    record: dict[str, Any] = {"name": call.name, "optional": call.optional, "request": call.request}
    if call.notes:
        record["notes"] = call.notes
    started = time.perf_counter()
    try:
        response = bedrock.converse(**call.request)
    except (ClientError, BotoCoreError) as error:
        record["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if isinstance(error, ClientError):
            meta = error.response.get("ResponseMetadata", {})
            record["error"] = {
                "code": error.response.get("Error", {}).get("Code"),
                "message": error.response.get("Error", {}).get("Message"),
                "http_status": meta.get("HTTPStatusCode"),
                "request_id": meta.get("RequestId"),
            }
        else:  # no response at all: a timeout or a connection failure
            record["error"] = {"code": type(error).__name__, "message": str(error)}
        record["ok"] = (
            call.expect_error is not None and record["error"]["code"] == call.expect_error
        )
        return record
    record["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    content = response.get("output", {}).get("message", {}).get("content", [])
    text = "".join(block.get("text", "") for block in content)
    record.update(
        {
            "stop_reason": response.get("stopReason"),
            "text": text,
            "content_blocks": [next(iter(block), None) for block in content],
            "usage": response.get("usage"),
            "bedrock_latency_ms": response.get("metrics", {}).get("latencyMs"),
            "request_id": response.get("ResponseMetadata", {}).get("RequestId"),
        }
    )
    succeeded = record["stop_reason"] == "end_turn" and bool(text.strip())
    record["ok"] = succeeded and call.expect_error is None
    return record


def run_converse(bedrock, include_optional: bool = True) -> dict:
    calls = [_call_converse(bedrock, call) for call in converse_calls(include_optional)]
    return {
        "region": names.REGION,
        "calls": calls,
        "required_ok": all(c["ok"] for c in calls if not c["optional"]),
    }


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------
def redact(result: dict, secret: str | None) -> dict:
    """Fail-safe: if the key's value appears anywhere in the result, replace it and say so."""
    if not secret:
        return result
    blob = json.dumps(result, default=str)
    escaped = json.dumps(secret)[1:-1]
    if secret not in blob and escaped not in blob:
        return result
    safe = json.loads(blob.replace(escaped, "[redacted]").replace(secret, "[redacted]"))
    safe["redaction_applied"] = True
    return safe


def _versions() -> dict:
    from importlib import metadata

    found = {"python": platform.python_version(), "machine": platform.machine()}
    for package in ("flagsmith", "flagsmith-flag-engine", "requests", "urllib3"):
        try:
            found[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            found[package] = None
    try:
        import boto3
        import botocore

        found["boto3"], found["botocore"] = boto3.__version__, botocore.__version__
    except ImportError:
        found["boto3"] = found["botocore"] = None
    return found


def run_selfcheck() -> dict:
    """Load the SDK and its compiled dependencies and use each once. No secret, no network.

    Phase 2 runs this first, so the arm64 wheels are proven before any Flagsmith request.
    """
    import importlib
    from importlib.machinery import EXTENSION_SUFFIXES

    uses = {
        "flagsmith": lambda m: m.Flagsmith,
        "flag_engine": lambda m: importlib.import_module("flag_engine.engine"),
        "regex": lambda m: m.compile(r"\p{L}+").fullmatch("spike"),
        "iregexp_check": lambda m: m.check("[a-z]+"),
        "charset_normalizer": lambda m: m.from_bytes(b"rollout marshal spike"),
    }
    # The compiled module inside each package that ships one (an import error here means a
    # wheel for the wrong platform).
    compiled = {
        "regex": "regex._regex",
        "iregexp_check": "iregexp_check.iregexp_check",
        "charset_normalizer": "charset_normalizer.md",
    }
    modules = {}
    for name, use in uses.items():
        use(importlib.import_module(name))  # raises if the package cannot load or run
        entry: dict[str, Any] = {"loaded": True}
        if name in compiled:
            path = importlib.import_module(compiled[name]).__file__ or ""
            entry["compiled_file"] = path.rsplit("/", 1)[-1]
            entry["compiled"] = path.endswith(tuple(EXTENSION_SUFFIXES))
        modules[name] = entry
    ok = all(entry.get("compiled", True) for entry in modules.values())
    return {"modules": modules, "ok": ok}


def handler(event, context=None) -> dict:
    global _invocations
    _invocations += 1
    action = (event or {}).get("action")
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {ACTIONS}")
    started_at = datetime.now(UTC).isoformat()
    secret = None
    if action == "selfcheck":
        result = run_selfcheck()
    elif action == "converse":
        result = run_converse(
            bedrock_client(), include_optional=event.get("include_optional", True)
        )
    else:
        secret = read_sdk_key()
        if action == "router":
            result = run_router(secret, make_client=lambda_flagsmith_client)
        else:
            result = run_judge(
                secret, event.get("assignments"), make_client=lambda_flagsmith_client
            )
        result["key_is_server_side"] = secret.startswith(SERVER_SIDE_KEY_PREFIX)
    result.update(
        {
            "action": action,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "aws_request_id": getattr(context, "aws_request_id", None),
            "function_name": getattr(context, "function_name", None),
            "container": {"id": CONTAINER_ID, "invocation": _invocations},
            "versions": _versions(),
        }
    )
    result = redact(result, secret)
    summary = {
        k: result.get(k)
        for k in ("action", "aborted", "exposures_tracked", "events_delivered", "required_ok")
    }
    print(json.dumps({"spike_summary": summary}))  # counts only; never the key
    return result
