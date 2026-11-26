"""The shared InvokeAgentRuntime client setting (design.md section 7), checked in botocore.

No AWS calls. The clients here are real botocore clients, but a before-send hook answers
every request in place of the network. The requests are unsigned, and conftest.py removes
every credential source besides. That lets the tests count the attempts botocore really makes.
"""

from __future__ import annotations

import io

import boto3
import botocore.endpoint
import pytest
from botocore import UNSIGNED
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError

from contracts import names
from signals.invoker.agent_client import AGENT_CLIENT_SERVICE, agent_client_config, new_agent_client

ENDPOINT = f"https://{AGENT_CLIENT_SERVICE}.{names.REGION}.amazonaws.com"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{names.REGION}:{names.ACCOUNT_ID}:runtime/rm_agent-Test123"
)


class RawBody:
    """The raw stream an AWSResponse reads its body from."""

    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)

    def stream(self, **_):
        yield self._data.read()

    def read(self, *args, **_):
        return self._data.read(*args)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """No backoff sleeps between the attempts the control test counts."""
    monkeypatch.setattr(botocore.endpoint.time, "sleep", lambda _seconds: None)


def counting_client(config: Config, answer):
    """A bedrock-agentcore client with `config` whose every attempt is answered by `answer`."""
    unsigned = config.merge(Config(signature_version=UNSIGNED))
    client = boto3.session.Session().client(
        AGENT_CLIENT_SERVICE, region_name=names.REGION, config=unsigned
    )
    attempts = []

    def before_send(request, **_):
        attempts.append(request.url)
        return answer()

    client.meta.events.register("before-send", before_send)
    return client, attempts


def server_error():
    return AWSResponse(
        ENDPOINT,
        500,
        {"x-amzn-ErrorType": "InternalServerException", "Content-Type": "application/json"},
        RawBody(b'{"message": "try again"}'),
    )


def read_timeout():
    raise ReadTimeoutError(endpoint_url=ENDPOINT)


def invoke(client):
    return client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=names.new_session_id(),
        payload=b'{"action": "tick"}',
    )


def test_config_holds_the_contract_values():
    config = agent_client_config()
    assert config.read_timeout == names.AGENT_CLIENT_READ_TIMEOUT_SECONDS == 290
    assert config.retries == {"total_max_attempts": names.AGENT_CLIENT_MAX_ATTEMPTS}
    assert names.AGENT_CLIENT_MAX_ATTEMPTS == 1
    # The client gives up before rm-invoker's own Lambda timeout stops it.
    assert config.read_timeout < names.INVOKER_TIMEOUT_SECONDS


def test_config_is_a_fresh_object_each_time():
    """botocore normalises the retries dict in place when it builds a client, so a shared
    Config instance would change under its other users."""
    first, second = agent_client_config(), agent_client_config()
    assert first is not second
    assert first.retries is not second.retries


def test_new_agent_client_carries_the_setting(monkeypatch):
    # An environment that asks for more attempts cannot add retries back: an explicit
    # total_max_attempts in the Config wins (botocore args.py, _compute_retry_max_attempts).
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("AWS_RETRY_MODE", "legacy")
    client = new_agent_client()
    assert client.meta.service_model.service_name == AGENT_CLIENT_SERVICE
    assert client.meta.region_name == names.REGION
    assert client.meta.config.read_timeout == names.AGENT_CLIENT_READ_TIMEOUT_SECONDS
    assert client.meta.config.retries["total_max_attempts"] == names.AGENT_CLIENT_MAX_ATTEMPTS
    assert "invoke_agent_runtime" in dir(client)


def test_new_agent_client_uses_the_given_session():
    """A given session is the one used; the region is pinned whatever it says."""

    class RecordingSession:
        def __init__(self):
            self.calls = []

        def client(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return "client"

    session = RecordingSession()
    assert new_agent_client(session) == "client"
    [(args, kwargs)] = session.calls
    assert args == (AGENT_CLIENT_SERVICE,)
    assert kwargs["region_name"] == names.REGION
    assert kwargs["config"].read_timeout == names.AGENT_CLIENT_READ_TIMEOUT_SECONDS
    assert kwargs["config"].retries == {"total_max_attempts": names.AGENT_CLIENT_MAX_ATTEMPTS}


def test_the_attempt_counter_sees_retries():
    """Control: with retries allowed, the same failing answer is attempted three times, so the
    single attempts below are the setting at work, not a counter that cannot see retries."""
    client, attempts = counting_client(Config(retries={"total_max_attempts": 3}), server_error)
    with pytest.raises(ClientError):
        invoke(client)
    assert len(attempts) == 3


def test_a_retryable_server_error_is_attempted_once():
    client, attempts = counting_client(agent_client_config(), server_error)
    with pytest.raises(ClientError) as raised:
        invoke(client)
    assert raised.value.response["Error"]["Code"] == "InternalServerException"
    assert len(attempts) == 1


def test_a_read_timeout_is_attempted_once():
    client, attempts = counting_client(agent_client_config(), read_timeout)
    with pytest.raises(ReadTimeoutError):
        invoke(client)
    assert len(attempts) == 1
