"""Local stand-ins for Flagsmith and Bedrock, so the spike's tests need no network or AWS.

FakeFlagsmith listens on 127.0.0.1 and answers the two endpoints the SDK uses in task 1.2:

- POST /api/v1/identities/   remote evaluation (get_identity_flags), one flag per identity
- POST /v1/events            the events API (track_event, track_exposure_event, flush_events)

It keeps every request body, so a test can assert exactly what the real SDK sent. The SDK
key in these tests is a dummy; no test reads a real secret, reaches Flagsmith or calls AWS.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from contracts import names

DUMMY_SDK_KEY = "dummy-sdk-key-for-tests"
EXPERIMENT_ID = 4242
# What the human creates in the Development environment (tasks.md 1.2, design.md section 13).
SPIKE_FLAG = "spike_flag"


def default_flag(identifier: str) -> dict:
    """Odd identities in the cheaper variation, even ones in control, all in the experiment."""
    number = int(identifier.split("-")[1])
    cheaper = number % 2 == 1
    return {
        "enabled": True,
        "feature_state_value": names.VARIANT_CHEAPER if cheaper else names.VARIANT_CURRENT,
        "feature": {"id": 99, "name": SPIKE_FLAG, "type": "MULTIVARIATE"},
        "variant": names.VARIANT_CHEAPER if cheaper else "control",
        "reason": "SPLIT; weight=50",
        "metadata": {"experiment": {"id": EXPERIMENT_ID, "name": "spike", "in_experiment": True}},
    }


class FakeFlagsmith:
    def __init__(self) -> None:
        self.flag_for: Callable[[str], dict | None] = default_flag
        self.events_status = 202
        # When set, the first events POST waits until the test sets this event.
        self.events_gate: threading.Event | None = None
        self.identity_bodies: list[dict] = []
        self.event_batches: list[list[dict]] = []
        self.event_headers: list[dict] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    @property
    def api_url(self) -> str:
        return self.base_url + "api/v1/"

    @property
    def events_url(self) -> str:
        return self.base_url

    def events(self) -> list[dict]:
        with self._lock:
            return [event for batch in self.event_batches for event in batch]

    def start(self) -> FakeFlagsmith:
        self._thread.start()
        return self

    def stop(self) -> None:
        if self.events_gate is not None:
            self.events_gate.set()
        self._server.shutdown()
        self._server.server_close()

    def _handler_class(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep test output quiet
                pass

            def _reply(self, status: int, body: object | None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"null")
                if self.path == "/api/v1/identities/":
                    with fake._lock:
                        fake.identity_bodies.append(body)
                    flag = fake.flag_for(body["identifier"])
                    flags = [] if flag is None else [flag]
                    self._reply(200, {"flags": flags, "traits": []})
                elif self.path == "/v1/events":
                    gate = fake.events_gate
                    if gate is not None:
                        gate.wait(timeout=10)
                    with fake._lock:
                        fake.event_batches.append(body["events"])
                        fake.event_headers.append(dict(self.headers))
                    self._reply(fake.events_status, None)
                else:
                    self._reply(404, {"detail": "not found"})

        return Handler


def text_response(text: str, *, stop_reason: str = "end_turn", reasoning: bool = True) -> dict:
    """A Converse response shaped like boto3's, with an optional empty reasoning block."""
    content = []
    if reasoning:
        content.append({"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}})
    if text:
        content.append({"text": text})
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 40, "outputTokens": 90, "totalTokens": 130},
        "metrics": {"latencyMs": 812},
        "ResponseMetadata": {"RequestId": "req-1", "HTTPStatusCode": 200, "HTTPHeaders": {}},
    }


class FakeBedrock:
    """Records every converse(**request); answers with respond(request)."""

    def __init__(self, respond: Callable[[dict], dict]) -> None:
        self.respond = respond
        self.requests: list[dict] = []

    def converse(self, **request) -> dict:
        self.requests.append(request)
        return self.respond(request)


class FakeSecrets:
    """Secrets Manager with one secret; records which names were asked for."""

    def __init__(self, name: str, value: str) -> None:
        self._name, self._value = name, value
        self.asked: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict:  # noqa: N803 (boto3's spelling)
        self.asked.append(SecretId)
        if SecretId != self._name:
            raise KeyError(SecretId)
        return {"Name": self._name, "SecretString": self._value}
