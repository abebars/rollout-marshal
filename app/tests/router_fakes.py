"""Stand-ins for everything rm-router talks to, so its tests reach no AWS and no Flagsmith.

- FakeTime: one controllable clock behind the router's monotonic, perf_counter and wall
  clocks.
- FakeDynamo: the rm-runs pointer read.
- FakeLambda: the answer functions (synchronous) and rm-judge (asynchronous). It advances
  FakeTime while an answer "runs", so the router's measured latency is known.
- FakeFlagsmith: the SDK client's surface the router uses. It returns the real SDK's Flags
  and Flag objects, so a missing feature raises exactly as the SDK does, and it fails the
  test if get_experiment_flag is called.
- FakeFlagsmithApi: a local HTTP server on 127.0.0.1 for the real SDK. It answers the
  identities endpoint and records event batches, and can hold every events POST open to
  play a slow events API.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple

from flagsmith.models import ExperimentMetadata, Flag, Flags

from contracts import names

EXPERIMENT_ID = 4242
DUMMY_SDK_KEY = "dummy-sdk-key-for-router-tests"
LOOPBACK = "127.0.0.1"

ANSWER_FUNCTIONS = {
    function: variant for variant, function in names.ANSWER_FUNCTION_BY_VARIANT.items()
}


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
class FakeTime:
    """One clock for the caches (monotonic), the latency (perf_counter) and EMF (time)."""

    def __init__(self, start: float = 1_000.0, wall: float = 1_800_000_000.0) -> None:
        self.now = start
        self._wall_offset = wall - start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now + self._wall_offset


# ---------------------------------------------------------------------------
# rm-runs
# ---------------------------------------------------------------------------
def pointer_item(run_id: str, *, feature_key: str | None = "from run") -> dict:
    """The ACTIVE pointer as DynamoDB returns it; feature_key None leaves it out (PLANNED)."""
    run_attr, feature_attr = names.POINTER_ATTRIBUTES
    item = {names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}, run_attr: {"S": run_id}}
    if feature_key == "from run":
        feature_key = names.experiment_feature_key(run_id)
    if feature_key is not None:
        item[feature_attr] = {"S": feature_key}
    return item


class FakeDynamo:
    def __init__(self, item: dict | None = None) -> None:
        self.item = item
        self.reads: list[dict] = []
        self.error: Exception | None = None

    def get_item(self, **params):
        self.reads.append(params)
        if self.error is not None:
            raise self.error
        return {} if self.item is None else {"Item": self.item}


# ---------------------------------------------------------------------------
# Lambda: the answer functions and rm-judge
# ---------------------------------------------------------------------------
class Invoke(NamedTuple):
    function: str
    invocation_type: str
    payload: dict


def answer_for(function: str, request: dict) -> dict:
    """A valid answer_response from the answer function named function."""
    variant = ANSWER_FUNCTIONS[function]
    return dict(
        zip(
            names.ANSWER_RESPONSE_FIELDS,
            (f"An answer from {variant}.", 51, 197, names.MODEL_BY_VARIANT[variant]),
            strict=True,
        )
    )


def function_error(error_type: str) -> dict:
    """What Lambda's invoke returns when the function raised error_type."""
    body = {
        "errorMessage": "no answer",
        "errorType": error_type,
        "requestId": "r",
        "stackTrace": [],
    }
    return {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(json.dumps(body).encode()),
    }


class FakeLambda:
    """reply(function, request) gives a synchronous invoke's response: by default a valid
    answer, a dict from function_error(), or an exception to raise. Each answer takes
    answer_seconds on FakeTime."""

    def __init__(self, time: FakeTime, answer_seconds: float = 1.5) -> None:
        self.time = time
        self.answer_seconds = answer_seconds
        self.invokes: list[Invoke] = []
        self.reply: Callable[[str, dict], object] = answer_for
        self.judge_error: Exception | None = None

    def invoke(self, *, FunctionName: str, InvocationType: str, Payload: bytes):  # noqa: N803
        payload = json.loads(Payload)
        self.invokes.append(Invoke(FunctionName, InvocationType, payload))
        if InvocationType == "Event":
            if self.judge_error is not None:
                raise self.judge_error
            return {"StatusCode": 202, "Payload": io.BytesIO(b"")}
        self.time.advance(self.answer_seconds)
        outcome = self.reply(FunctionName, payload)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, dict) and "StatusCode" in outcome:
            return outcome
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(outcome).encode())}

    def answers(self) -> list[Invoke]:
        return [i for i in self.invokes if i.invocation_type == "RequestResponse"]

    def judged(self) -> list[Invoke]:
        return [i for i in self.invokes if i.invocation_type == "Event"]


# ---------------------------------------------------------------------------
# Flagsmith: the SDK client the router uses, with the real SDK's flag objects
# ---------------------------------------------------------------------------
def experiment_flag(
    feature_key: str,
    value: object,
    *,
    variant: str | None = "from value",
    enabled: bool = True,
    in_experiment: bool | None = True,
) -> Flag:
    """The experiment feature's flag for one identity. variant defaults to what remote
    evaluation reports for the value (control for current, task 1.2); in_experiment None
    leaves the experiment metadata out."""
    if variant == "from value":
        variant = names.CONTROL_LABEL if value == names.VARIANT_CURRENT else value
    experiment = (
        None
        if in_experiment is None
        else ExperimentMetadata(id=EXPERIMENT_ID, name="router test", in_experiment=in_experiment)
    )
    return Flag(
        enabled=enabled,
        value=value,
        feature_id=301,
        feature_name=feature_key,
        variant=variant,
        reason="SPLIT; weight=50",
        experiment=experiment,
    )


def kill_switch(enabled: bool) -> Flag:
    return Flag(enabled=enabled, value=None, feature_id=302, feature_name=names.KILL_SWITCH_FEATURE)


def default_model(value: object) -> Flag:
    return Flag(enabled=True, value=value, feature_id=303, feature_name=names.DEFAULT_MODEL_FEATURE)


def flags_of(*flags: Flag) -> Flags:
    return Flags(flags={flag.feature_name: flag for flag in flags})


class FakeFlagsmith:
    """identity_flags(identifier) gives that identity's flags; environment gives the
    environment's. calls is the order things happened in."""

    def __init__(self) -> None:
        self.identity_flags: Callable[[str], Flags] = lambda identifier: flags_of()
        self.environment: Flags = flags_of(default_model(names.VARIANT_CURRENT))
        self.identity_error: Exception | None = None
        self.calls: list[str] = []
        self.identity_requests: list[str] = []
        self.environment_requests = 0
        self.exposures: list[dict] = []
        self.events: list[dict] = []
        self.flushes = 0

    def get_identity_flags(self, identifier, traits=None, *, transient=False):
        self.calls.append("identity")
        self.identity_requests.append(identifier)
        if self.identity_error is not None:
            raise self.identity_error
        return self.identity_flags(identifier)

    def get_environment_flags(self):
        self.calls.append("environment")
        self.environment_requests += 1
        return self.environment

    def get_experiment_flag(self, *args, **kwargs):
        raise AssertionError("get_experiment_flag makes its own identity request")

    def track_exposure_event(
        self, feature_name, identifier=None, value=None, traits=None, metadata=None
    ):
        self.calls.append("exposure")
        self.exposures.append(
            {
                "feature_name": feature_name,
                "identifier": identifier,
                "value": value,
                "metadata": metadata,
            }
        )

    def track_event(self, event, identifier=None, value=None, traits=None, metadata=None):
        self.calls.append("event")
        self.events.append({"event": event, "identifier": identifier, "value": value})

    def flush_events(self):
        self.calls.append("flush")
        self.flushes += 1


# ---------------------------------------------------------------------------
# A local Flagsmith API for the real SDK
# ---------------------------------------------------------------------------
def api_flag(
    name: str, *, enabled: bool, value: object, variant: str | None = None, experiment=None
):
    """One flag as Flagsmith's identities endpoint returns it (as task 1.2's fake did)."""
    flag = {
        "enabled": enabled,
        "feature_state_value": value,
        "feature": {"id": abs(hash(name)) % 10_000, "name": name, "type": "STANDARD"},
    }
    if variant is not None:
        flag["variant"] = variant
        flag["feature"]["type"] = "MULTIVARIATE"
    if experiment is not None:
        flag["metadata"] = {"experiment": experiment}
    return flag


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class FakeFlagsmithApi:
    """POST /api/v1/identities/ returns flags_for(identifier); POST /v1/events records the
    batch. With hold_events set, every events POST waits until it is set (or
    HOLD_SECONDS), long past the SDK's 3 second timeout."""

    HOLD_SECONDS = 15

    def __init__(self) -> None:
        self.flags_for: Callable[[str], list[dict]] = lambda identifier: []
        self.hold_events: threading.Event | None = None
        self.identity_bodies: list[dict] = []
        self.event_batches: list[list[dict]] = []
        self.event_posts = 0
        self._lock = threading.Lock()
        self._server = _Server((LOOPBACK, 0), self._handler_class())
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def api_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/api/v1/"

    @property
    def events_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def events(self) -> list[dict]:
        with self._lock:
            return [event for batch in self.event_batches for event in batch]

    def start(self) -> FakeFlagsmithApi:
        self._thread.start()
        return self

    def stop(self) -> None:
        if self.hold_events is not None:
            self.hold_events.set()
        self._server.shutdown()
        self._server.server_close()

    def _handler_class(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _reply(self, status: int, body: object | None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the SDK gave up on this attempt

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"null")
                if self.path == "/api/v1/identities/":
                    with fake._lock:
                        fake.identity_bodies.append(body)
                    self._reply(200, {"flags": fake.flags_for(body["identifier"]), "traits": []})
                elif self.path == "/v1/events":
                    with fake._lock:
                        fake.event_posts += 1
                    if fake.hold_events is not None:
                        fake.hold_events.wait(fake.HOLD_SECONDS)
                        self._reply(503, None)
                        return
                    with fake._lock:
                        fake.event_batches.append(body["events"])
                    self._reply(202, None)
                else:
                    self._reply(404, {"detail": "not found"})

        return Handler
