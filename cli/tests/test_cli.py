"""marshal, the engineer's CLI (tasks.md 2.6; design.md section 7).

No AWS calls. Most tests pass a fake SSM client and a fake runtime client. The few that use
the CLI's own botocore client answer every request in a before-send hook, and every test runs
with no credentials, no AWS config files and the HTTP layer refusing to send, so a request
that escaped a fake could not reach AWS.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path

import botocore.endpoint
import botocore.httpsession
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
    ResponseStreamingError,
)

import contracts
from cli import marshal
from contracts import names

RUN_ID = "r202611271530"
PROMPT = "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5."
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{names.REGION}:{names.ACCOUNT_ID}:runtime/"
    f"{names.AGENT_RUNTIME_DEPLOYED_NAME}-Abc123XyZ0"
)
ENDPOINT = f"https://bedrock-agentcore.{names.REGION}.amazonaws.com"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

_AWS_ENV = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_MAX_ATTEMPTS",
    "AWS_RETRY_MODE",
)


@pytest.fixture(autouse=True)
def nothing_reaches_aws(monkeypatch):
    """No credentials, no AWS config files, no instance metadata, no backoff sleeps, and an
    HTTP layer that refuses to send."""
    for name in _AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(botocore.endpoint.time, "sleep", lambda _seconds: None)

    def refuse(*args, **kwargs):
        raise AssertionError("a request reached the HTTP layer")

    monkeypatch.setattr(botocore.httpsession.URLLib3Session, "send", refuse)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeSsm:
    """GetParameter, answering the runtime's ARN."""

    def __init__(self, value: str = RUNTIME_ARN, error: Exception | None = None) -> None:
        self.value, self.error = value, error
        self.calls: list[dict] = []

    def get_parameter(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"Parameter": {"Name": kwargs["Name"], "Type": "String", "Value": self.value}}


class FakeRuntime:
    """InvokeAgentRuntime as botocore answers it: the body a stream, the HTTP status an int."""

    def __init__(self, reply=None, *, body: bytes | None = None, status: int = 200, error=None):
        self.reply, self.body, self.status, self.error = reply, body, status, error
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        raw = self.body if self.body is not None else json.dumps(self.reply).encode()
        return {
            "response": io.BytesIO(raw),
            "statusCode": self.status,
            "contentType": "application/json",
            "runtimeSessionId": kwargs.get("runtimeSessionId"),
        }


def response(
    state=names.STATE_PLANNED, *, run_id=RUN_ID, message="Planned.", pending=None, error=None
) -> dict:
    reply = {
        "run_id": run_id,
        "state": state,
        "message": message,
        "awaiting": names.awaiting_for(state),
        "pending": pending,
        "error": error,
    }
    contracts.validate("response", reply)
    return reply


NO_ACTIVE_RUN = {
    "run_id": None,
    "state": None,
    "message": "no active run",
    "awaiting": None,
    "pending": None,
    "error": None,
}


def no_client(*args, **kwargs):
    raise AssertionError("no client may be made")


# ---------------------------------------------------------------------------
# One payload per call, a new session ID each time, the ARN from SSM
# ---------------------------------------------------------------------------
ENGINEER = names.ACTOR_ENGINEER


@pytest.mark.parametrize(
    "argv, payload",
    [
        (["start", PROMPT], {"action": names.ACTION_START, "prompt": PROMPT, "actor": ENGINEER}),
        *(
            (
                ["approve", RUN_ID, awaiting],
                {
                    "action": names.ACTION_APPROVE,
                    "run_id": RUN_ID,
                    "awaiting": awaiting,
                    "actor": ENGINEER,
                },
            )
            for awaiting in names.AWAITING_VALUES
        ),
        (
            ["decline", RUN_ID],
            {"action": names.ACTION_DECLINE, "run_id": RUN_ID, "actor": ENGINEER},
        ),
        (["status"], {"action": names.ACTION_STATUS}),
        (["status", RUN_ID], {"action": names.ACTION_STATUS, "run_id": RUN_ID}),
        (["check", RUN_ID], {"action": names.ACTION_CHECK, "run_id": RUN_ID, "actor": ENGINEER}),
    ],
    ids=[
        "start",
        *(f"approve-{awaiting}" for awaiting in names.AWAITING_VALUES),
        "decline",
        "status",
        "status-run",
        "check",
    ],
)
def test_each_command_sends_one_invoke_payload(argv, payload):
    ssm, runtime = FakeSsm(), FakeRuntime(response())
    assert marshal.main(argv, ssm=ssm, agent=runtime) == names.CLI_EXIT_OK
    (call,) = runtime.calls
    assert set(call) == {"agentRuntimeArn", "runtimeSessionId", "payload"}
    assert call["agentRuntimeArn"] == RUNTIME_ARN
    sent = json.loads(call["payload"])
    assert sent == payload
    contracts.validate("invoke", sent)
    assert ssm.calls == [{"Name": names.AGENT_RUNTIME_ARN_PARAMETER}]


def test_the_commands_are_the_cli_actions(capsys):
    """Exactly CLI_ACTIONS, in order: alarm and tick are rm-invoker's, never the engineer's."""
    assert marshal.main(["--help"], ssm=FakeSsm(), agent=FakeRuntime()) == names.CLI_EXIT_OK
    usage = capsys.readouterr().out
    assert usage.startswith(f"usage: {names.CLI_NAME} ")
    listed = re.findall(r"^ {4}(\w+) ", usage, re.MULTILINE)
    assert tuple(listed) == names.CLI_ACTIONS


def test_every_call_has_a_new_session_id():
    runtime = FakeRuntime(response())
    for _ in range(3):
        assert marshal.main(["status"], ssm=FakeSsm(), agent=runtime) == names.CLI_EXIT_OK
    sessions = [call["runtimeSessionId"] for call in runtime.calls]
    assert len(set(sessions)) == 3
    for session in sessions:
        assert session.startswith(names.SESSION_ID_PREFIX)
        assert uuid.UUID(session.removeprefix(names.SESSION_ID_PREFIX)).version == 4
        assert len(session) >= names.SESSION_ID_MIN_LENGTH


def test_main_makes_the_engineers_clients_when_none_are_passed(monkeypatch):
    ssm, runtime = FakeSsm(), FakeRuntime(response())
    monkeypatch.setattr(marshal, "new_ssm_client", lambda: ssm)
    monkeypatch.setattr(marshal, "new_agent_client", lambda: runtime)
    assert marshal.main(["check", RUN_ID]) == names.CLI_EXIT_OK
    assert len(ssm.calls) == 1 and len(runtime.calls) == 1


# ---------------------------------------------------------------------------
# Exit codes: 0 when error is null, 1 when it carries a code, 2 on a usage error
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", names.ERROR_CODES)
def test_a_response_with_an_error_code_exits_1(code, capsys):
    reply = response(error={"code": code, "message": "Not now."})
    assert marshal.main(["approve", RUN_ID, "launch"], ssm=FakeSsm(), agent=FakeRuntime(reply)) == (
        names.CLI_EXIT_ERROR
    )
    assert f"{code}: Not now." in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--bogus"],
        ["launch", RUN_ID],
        ["alarm"],
        ["tick"],
        ["start"],
        ["start", ""],
        ["start", " \n\t"],
        ["start", PROMPT, "extra"],
        ["approve"],
        ["approve", "R202611271530", "launch"],
        ["approve", "r123", "launch"],
        ["approve", RUN_ID],  # what it approves is required (contract change c5)
        ["approve", RUN_ID, "bogus"],
        ["approve", RUN_ID, "Launch"],
        ["approve", RUN_ID, "launch", "extra"],
        ["decline", RUN_ID, "launch"],
        ["decline", "not-a-run"],
        ["check"],
        ["check", RUN_ID, RUN_ID],
        ["status", "x"],
        ["status", RUN_ID, RUN_ID],
    ],
)
def test_a_usage_error_exits_2_makes_no_client_and_calls_nothing(argv, monkeypatch, capsys):
    monkeypatch.setattr(marshal, "new_ssm_client", no_client)
    monkeypatch.setattr(marshal, "new_agent_client", no_client)
    assert marshal.main(argv) == names.CLI_EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"usage: {names.CLI_NAME}")


@pytest.mark.parametrize(
    "argv", [["--help"], ["-h"], *([action, "--help"] for action in names.CLI_ACTIONS)]
)
def test_help_exits_0_and_makes_no_client(argv, monkeypatch, capsys):
    monkeypatch.setattr(marshal, "new_ssm_client", no_client)
    monkeypatch.setattr(marshal, "new_agent_client", no_client)
    assert marshal.main(argv) == names.CLI_EXIT_OK
    assert capsys.readouterr().out.startswith(f"usage: {names.CLI_NAME}")


def test_the_exit_codes_are_the_contracts():
    assert (names.CLI_EXIT_OK, names.CLI_EXIT_ERROR, names.CLI_EXIT_USAGE) == (0, 1, 2)


# ---------------------------------------------------------------------------
# What it prints
# ---------------------------------------------------------------------------
def test_it_prints_the_run_state_message_awaiting_pending_and_error(capsys):
    reply = response(
        names.STATE_RESULTS_READY,
        message="Results are ready.",
        pending=names.STATE_PROMOTED,
        error={"code": names.ERROR_TRANSITION_PENDING, "message": "Another call holds it."},
    )
    marshal.main(["approve", RUN_ID, "promotion"], ssm=FakeSsm(), agent=FakeRuntime(reply))
    assert capsys.readouterr().out.splitlines() == [
        f"run:      {RUN_ID}",
        "state:    RESULTS_READY",
        "message:  Results are ready.",
        "awaiting: promotion",
        "pending:  PROMOTED",
        "error:    TRANSITION_PENDING: Another call holds it.",
    ]


def test_it_prints_no_pending_or_error_line_when_there_is_none(capsys):
    reply = response(message="Planned. Approve to launch.")
    assert marshal.main(["start", PROMPT], ssm=FakeSsm(), agent=FakeRuntime(reply)) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"run:      {RUN_ID}",
        "state:    PLANNED",
        "message:  Planned. Approve to launch.",
        "awaiting: launch",
    ]


def test_status_with_no_active_run_prints_none_and_exits_0(capsys):
    assert marshal.main(["status"], ssm=FakeSsm(), agent=FakeRuntime(NO_ACTIVE_RUN)) == 0
    assert capsys.readouterr().out.splitlines() == [
        "run:      none",
        "state:    none",
        "message:  no active run",
        "awaiting: nothing",
    ]


# ---------------------------------------------------------------------------
# The reply must be a response; failures before one exit 1 with the reason on stderr
# ---------------------------------------------------------------------------
def test_the_reply_check_agrees_with_response_json():
    good = response()
    replies = [
        good,
        response(names.STATE_RUNNING),
        response(names.STATE_AWAITING_RESUME),
        response(names.STATE_RESULTS_READY, pending=names.STATE_PROMOTED),
        response(names.STATE_HELD, error={"code": names.ERROR_NOT_FOUND, "message": "x"}),
        NO_ACTIVE_RUN,
        {**good, "awaiting": None},  # PLANNED awaits launch
        {**response(names.STATE_RUNNING), "awaiting": names.AWAITS_LAUNCH},
        {**good, "state": "BOGUS"},
        {**good, "state": ["PLANNED"]},
        {**good, "pending": "SOON"},
        {**good, "run_id": "R1"},
        {**good, "run_id": True},
        {**good, "message": None},
        {**good, "error": {"code": "NOPE", "message": "x"}},
        {**good, "error": {"code": names.ERROR_NOT_FOUND, "message": ""}},
        {**good, "error": {"code": names.ERROR_NOT_FOUND}},
        {**good, "error": {"code": names.ERROR_NOT_FOUND, "message": "x", "extra": 1}},
        {**good, "error": "NOT_FOUND"},
        {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "pending"},
        [good],
        "PLANNED",
        None,
    ]
    validator = contracts.validator("response")
    for value in replies:
        assert marshal.is_response(value) is validator.is_valid(value), value


@pytest.mark.parametrize(
    "runtime",
    [
        FakeRuntime(body=b"not json"),
        FakeRuntime(body=b"\xff\xfe"),
        FakeRuntime(body=b""),
        FakeRuntime({"message": "hello"}),
        FakeRuntime({**response(), "awaiting": None}),
        FakeRuntime(response(), status=500),
    ],
    ids=["text", "bytes", "empty", "not-a-response", "inconsistent", "status-500"],
)
def test_a_reply_that_is_not_a_response_exits_1(runtime, capsys):
    assert marshal.main(["status"], ssm=FakeSsm(), agent=runtime) == names.CLI_EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a response" in captured.err


def test_a_missing_parameter_exits_1_and_invokes_nothing(capsys):
    missing = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "Parameter not found."}},
        "GetParameter",
    )
    runtime = FakeRuntime(response())
    assert marshal.main(["status"], ssm=FakeSsm(error=missing), agent=runtime) == 1
    assert runtime.calls == []
    err = capsys.readouterr().err
    assert names.AGENT_RUNTIME_ARN_PARAMETER in err and "ParameterNotFound" in err


@pytest.mark.parametrize(
    "arn",
    [
        RUNTIME_ARN.replace(names.ACCOUNT_ID, "111122223333"),
        RUNTIME_ARN.replace(names.REGION, "us-west-2"),
        f"arn:aws:lambda:{names.REGION}:{names.ACCOUNT_ID}:function:{names.INVOKER_FUNCTION}",
        "",
    ],
    ids=["another-account", "another-region", "not-a-runtime", "empty"],
)
def test_a_runtime_arn_outside_this_account_and_region_exits_1(arn, capsys):
    runtime = FakeRuntime(response())
    assert marshal.main(["status"], ssm=FakeSsm(value=arn), agent=runtime) == 1
    assert runtime.calls == []
    assert names.ACCOUNT_ID in capsys.readouterr().err


@pytest.mark.parametrize(
    "error, said",
    [
        (
            ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Not allowed."}},
                "InvokeAgentRuntime",
            ),
            "AccessDeniedException",
        ),
        (NoCredentialsError(), "credentials"),
    ],
    ids=["denied", "no-credentials"],
)
def test_an_invoke_that_fails_exits_1(error, said, capsys):
    assert marshal.main(["status"], ssm=FakeSsm(), agent=FakeRuntime(error=error)) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and said in captured.err


LOOK = f"look with `{names.CLI_NAME} {names.ACTION_STATUS} {RUN_ID}`"


@pytest.mark.parametrize(
    "error",
    [
        ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Not allowed."}},
            "InvokeAgentRuntime",
        ),
        ConnectionClosedError(endpoint_url=ENDPOINT),
        ResponseStreamingError(error="the stream broke"),
        ReadTimeoutError(endpoint_url=ENDPOINT),
    ],
    ids=["client-error", "connection-closed", "response-streaming", "read-timeout"],
)
def test_a_failure_once_the_request_went_out_says_to_look_first(error, capsys):
    argv = ["approve", RUN_ID, "promotion"]
    assert marshal.main(argv, ssm=FakeSsm(), agent=FakeRuntime(error=error)) == 1
    assert LOOK in capsys.readouterr().err


def test_a_reply_that_is_not_a_response_says_to_look_first(capsys):
    argv = ["approve", RUN_ID, "promotion"]
    assert marshal.main(argv, ssm=FakeSsm(), agent=FakeRuntime(body=b"oops")) == 1
    assert LOOK in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url=ENDPOINT),
        ConnectTimeoutError(endpoint_url=ENDPOINT),
        NoCredentialsError(),
    ],
    ids=["endpoint-connection", "connect-timeout", "no-credentials"],
)
def test_a_failure_before_the_request_went_out_gives_no_hint(error, capsys):
    argv = ["approve", RUN_ID, "promotion"]
    assert marshal.main(argv, ssm=FakeSsm(), agent=FakeRuntime(error=error)) == 1
    err = capsys.readouterr().err
    assert err and "look with" not in err


def test_a_missing_profile_exits_1_cleanly(monkeypatch, capsys):
    """The clients are made inside the try, so ProfileNotFound is a failure, not a
    traceback."""
    monkeypatch.setenv("AWS_PROFILE", "no-such-profile")
    assert marshal.main(["status"]) == names.CLI_EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == "" and "ProfileNotFound" in captured.err


def test_a_read_timeout_says_the_action_may_have_run_and_how_to_look(capsys):
    timeout = ReadTimeoutError(endpoint_url=ENDPOINT)
    argv = ["approve", RUN_ID, "promotion"]
    assert marshal.main(argv, ssm=FakeSsm(), agent=FakeRuntime(error=timeout)) == 1
    err = capsys.readouterr().err
    assert f"{names.AGENT_CLIENT_READ_TIMEOUT_SECONDS} s" in err
    assert f"{names.CLI_NAME} {names.ACTION_STATUS} {RUN_ID}" in err


# ---------------------------------------------------------------------------
# The client setting, in the CLI's own botocore client
# ---------------------------------------------------------------------------
def test_the_cli_builds_the_shared_client_setting_from_names(monkeypatch):
    """design.md section 7: read timeout 290 s and one attempt. The environment cannot add
    retries back: an explicit total_max_attempts wins."""
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("AWS_RETRY_MODE", "legacy")
    client = marshal.new_agent_client()
    assert client.meta.service_model.service_name == marshal.AGENT_CLIENT_SERVICE
    assert client.meta.region_name == names.REGION
    assert client.meta.config.read_timeout == names.AGENT_CLIENT_READ_TIMEOUT_SECONDS == 290
    assert client.meta.config.retries["total_max_attempts"] == names.AGENT_CLIENT_MAX_ATTEMPTS
    assert names.AGENT_CLIENT_MAX_ATTEMPTS == 1
    ssm = marshal.new_ssm_client()
    assert (ssm.meta.service_model.service_name, ssm.meta.region_name) == ("ssm", names.REGION)


def test_the_config_is_a_new_object_each_time():
    """botocore normalises the retries dict in place when it builds a client."""
    first, second = marshal.agent_client_config(), marshal.agent_client_config()
    assert first is not second and first.retries is not second.retries


class RawBody:
    """The raw stream an AWSResponse reads its body from."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def stream(self, **_):
        yield self._data.read()

    def read(self, *args, **_):
        return self._data.read(*args)


@pytest.fixture
def signed(monkeypatch):
    """Dummy credentials, so the CLI's real client can sign. Every request is answered in a
    before-send hook, and the HTTP layer still refuses to send."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    def answer_with(client, answer):
        attempts = []

        def before_send(request, **_):
            attempts.append(request)
            return answer()

        client.meta.events.register("before-send", before_send)
        return attempts

    return answer_with


def _header(request, name: str) -> str:
    value = request.headers[name]
    return value.decode() if isinstance(value, bytes) else value


def test_the_real_client_sends_the_payload_and_its_reply_is_read(signed, capsys):
    client = marshal.new_agent_client()
    reply = response(names.STATE_RUNNING, message="Launched.")
    attempts = signed(
        client,
        lambda: AWSResponse(
            ENDPOINT, 200, {"Content-Type": "application/json"}, RawBody(json.dumps(reply).encode())
        ),
    )
    argv = ["approve", RUN_ID, names.AWAITS_RESUME]
    assert marshal.main(argv, ssm=FakeSsm(), agent=client) == names.CLI_EXIT_OK
    (request,) = attempts
    assert json.loads(request.body) == {
        "action": names.ACTION_APPROVE,
        "run_id": RUN_ID,
        "awaiting": names.AWAITS_RESUME,
        "actor": names.ACTOR_ENGINEER,
    }
    assert urllib.parse.quote(RUNTIME_ARN, safe="") in request.url
    assert _header(request, SESSION_HEADER).startswith(names.SESSION_ID_PREFIX)
    assert "state:    RUNNING" in capsys.readouterr().out


def _server_error():
    return AWSResponse(
        ENDPOINT,
        500,
        {"x-amzn-ErrorType": "InternalServerException", "Content-Type": "application/json"},
        RawBody(b'{"message": "try again"}'),
    )


def _read_timeout():
    raise ReadTimeoutError(endpoint_url=ENDPOINT)


@pytest.mark.parametrize("answer", [_server_error, _read_timeout], ids=["500", "read-timeout"])
def test_the_real_client_makes_one_attempt(signed, answer, monkeypatch, capsys):
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "5")  # cannot add retries back
    client = marshal.new_agent_client()
    attempts = signed(client, answer)
    assert marshal.main(["status", RUN_ID], ssm=FakeSsm(), agent=client) == names.CLI_EXIT_ERROR
    assert len(attempts) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and LOOK in captured.err  # a 500 and a timeout both went out


def test_the_cli_imports_nothing_from_signals():
    """The CLI builds its own Config from contracts.names; rm-invoker's module is not shared."""
    tree = ast.parse(Path(marshal.__file__).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not [module for module in imported if module and module.split(".")[0] == "signals"]
    assert {module.split(".")[0] for module in imported if module} <= {
        "__future__",
        "argparse",
        "json",
        "sys",
        "typing",
        "boto3",
        "botocore",
        "contracts",
    }
