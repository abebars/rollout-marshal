"""marshal: the engineer's command line for rm_agent (tasks.md 2.6; design.md section 7).

    marshal start "<prompt>"
    marshal approve <run_id>
    marshal decline <run_id>
    marshal status [run_id]
    marshal check <run_id>

Each call reads the runtime's ARN from SSM parameter /rm/agent-runtime-arn with the
engineer's own AWS credentials, and sends one payload (contracts/schemas/invoke.json) in one
InvokeAgentRuntime call with a new session ID. It prints the response (response.json): the
run, its state, the message, what it waits for, any pending target and any error code.

The client setting is the one design.md section 7 gives the CLI and rm-invoker: read timeout
290 seconds and one attempt. The CLI builds its own botocore Config from contracts.names, and
imports nothing from signals/.

Exit codes (contracts.names): 0 when the response's error is null, 1 when it carries an error
code, 2 on a usage error. A call that fails, or a reply that is not a response, also exits 1,
with the reason on stderr and nothing on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

from contracts import names

AGENT_CLIENT_SERVICE = "bedrock-agentcore"  # the boto3 client that has invoke_agent_runtime
# Only a runtime in this account and region is invoked (CLAUDE.md: one account, us-east-1).
RUNTIME_ARN_PREFIX = f"arn:aws:bedrock-agentcore:{names.REGION}:{names.ACCOUNT_ID}:runtime/"
_RUN_ACTIONS = (names.ACTION_APPROVE, names.ACTION_DECLINE, names.ACTION_CHECK)
_HELP = {
    names.ACTION_START: "plan a run from a prompt; nothing launches until you approve",
    names.ACTION_APPROVE: "approve the step the run waits for: launch, resume or promotion",
    names.ACTION_DECLINE: "decline the step the run waits for",
    names.ACTION_STATUS: "show a run, or the active run when none is named",
    names.ACTION_CHECK: "do now what the alarm or the next tick would do for the run",
}
_REPLY_SHOWN = 500  # characters of a reply that is not a response, shown on stderr


class ReplyError(Exception):
    """The runtime answered something that is not a response.json object."""


# ---------------------------------------------------------------------------
# Clients: the engineer's credentials, us-east-1, the shared setting
# ---------------------------------------------------------------------------
def agent_client_config() -> Config:
    """design.md section 7's InvokeAgentRuntime setting, as a new object each call: botocore
    normalises the retries dict in place when it builds a client. An explicit
    total_max_attempts wins over AWS_MAX_ATTEMPTS and the config file."""
    return Config(
        read_timeout=names.AGENT_CLIENT_READ_TIMEOUT_SECONDS,
        retries={"total_max_attempts": names.AGENT_CLIENT_MAX_ATTEMPTS},
    )


def new_agent_client():
    return boto3.session.Session().client(
        AGENT_CLIENT_SERVICE, region_name=names.REGION, config=agent_client_config()
    )


def new_ssm_client():
    return boto3.session.Session().client("ssm", region_name=names.REGION)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=names.CLI_NAME,
        description="Drive a Rollout Marshal run: each command is one call to rm_agent.",
        epilog="Exit codes: 0 when the response has no error, 1 when it carries an error "
        "code or the call failed, 2 on a usage error.",
    )
    commands = parser.add_subparsers(dest="action", required=True, metavar="command")
    for action in names.CLI_ACTIONS:
        command = commands.add_parser(action, help=_HELP[action], description=_HELP[action])
        if action == names.ACTION_START:
            command.add_argument("prompt", help="what to test, in plain words")
        elif action == names.ACTION_STATUS:
            command.add_argument("run_id", nargs="?", help="a run ID; omit it for the active run")
        else:
            command.add_argument("run_id", help="the run's ID, for example r202611271530")
    return parser


def payload_for(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict:
    """The invoke.json payload for the command. parser.error() on a value the agent would
    refuse anyway: an empty prompt, or a run ID that does not match RUN_ID_PATTERN."""
    action = args.action
    if action == names.ACTION_START:
        if not args.prompt.strip():
            parser.error("the prompt is empty")
        return {"action": action, "prompt": args.prompt, "actor": names.ACTOR_ENGINEER}
    run_id = args.run_id
    if run_id is not None and not names.is_run_id(run_id):
        parser.error(f"{run_id!r} is not a run ID (expected {names.RUN_ID_PATTERN})")
    if action == names.ACTION_STATUS:
        return {"action": action} if run_id is None else {"action": action, "run_id": run_id}
    if action not in _RUN_ACTIONS:  # argparse offers only CLI_ACTIONS
        parser.error(f"no command {action!r}")
    return {"action": action, "run_id": run_id, "actor": names.ACTOR_ENGINEER}


# ---------------------------------------------------------------------------
# The call and its reply
# ---------------------------------------------------------------------------
def is_response(value: Any) -> bool:
    """contracts/schemas/response.json, checked by hand so the CLI needs no jsonschema. A test
    checks the two agree."""
    if not isinstance(value, dict) or set(value) != set(names.RESPONSE_FIELDS):
        return False
    run_id, state, pending, error = (
        value["run_id"],
        value["state"],
        value["pending"],
        value["error"],
    )
    return (
        (run_id is None or (isinstance(run_id, str) and names.is_run_id(run_id)))
        and (state is None or (isinstance(state, str) and state in names.STATES))
        and (pending is None or (isinstance(pending, str) and pending in names.STATES))
        and value["awaiting"] == names.awaiting_for(state)
        and isinstance(value["message"], str)
        and (
            error is None
            or (
                isinstance(error, dict)
                and set(error) == set(names.ERROR_FIELDS)
                and error["code"] in names.ERROR_CODES
                and isinstance(error["message"], str)
                and len(error["message"]) >= 1
            )
        )
    )


def read_runtime_arn(ssm: Any) -> str:
    return ssm.get_parameter(Name=names.AGENT_RUNTIME_ARN_PARAMETER)["Parameter"]["Value"]


def invoke(agent: Any, runtime_arn: str, payload: dict, session_id: str) -> dict:
    """One InvokeAgentRuntime call: the ARN, the session ID and the JSON payload as bytes, as
    docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html shows.
    botocore's errors pass through; ReplyError when the reply is not a response."""
    reply = agent.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    body = reply["response"].read()
    status = reply.get("statusCode", 200)
    try:
        response = json.loads(body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError
        response = None
    if status != 200 or not is_response(response):
        raise ReplyError(
            f"rm_agent's reply is not a response (HTTP {status}): {body[:_REPLY_SHOWN]!r}"
        )
    return response


def render(response: dict) -> str:
    """The run, its state, the message, what it waits for, and any pending target and error."""
    lines = [
        ("run", response["run_id"] or "none"),
        ("state", response["state"] or "none"),
        ("message", response["message"]),
        ("awaiting", response["awaiting"] or "nothing"),
    ]
    if response["pending"]:
        lines.append(("pending", response["pending"]))
    if response["error"]:
        lines.append(("error", f"{response['error']['code']}: {response['error']['message']}"))
    return "\n".join(f"{label + ':':<10}{value}" for label, value in lines)


def _failed(what: str, detail: Any = None, *, session_id: str | None = None) -> int:
    reason = what if detail is None else f"{what}: {type(detail).__name__}: {detail}"
    session = f" (session {session_id})" if session_id else ""
    print(f"{names.CLI_NAME}: {reason}{session}", file=sys.stderr)
    return names.CLI_EXIT_ERROR


def main(argv: list[str] | None = None, *, ssm: Any = None, agent: Any = None) -> int:
    """The console script cli.marshal:main. Returns the exit code. Tests pass ssm and agent;
    otherwise they are made from the engineer's credentials, and only after the command
    parses, so --help and usage errors make no client."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        payload = payload_for(args, parser)
    except SystemExit as stop:  # argparse exits 0 after --help and 2 after a usage error
        return names.CLI_EXIT_OK if stop.code in (0, None) else names.CLI_EXIT_USAGE

    ssm = ssm if ssm is not None else new_ssm_client()
    agent = agent if agent is not None else new_agent_client()
    try:
        runtime_arn = read_runtime_arn(ssm)
    except (BotoCoreError, ClientError) as error:
        return _failed(f"could not read {names.AGENT_RUNTIME_ARN_PARAMETER}", error)
    if not isinstance(runtime_arn, str) or not runtime_arn.startswith(RUNTIME_ARN_PREFIX):
        return _failed(
            f"{names.AGENT_RUNTIME_ARN_PARAMETER} holds {runtime_arn!r}, not a runtime in "
            f"account {names.ACCOUNT_ID}, {names.REGION}; nothing was sent"
        )

    session_id = names.new_session_id()
    try:
        response = invoke(agent, runtime_arn, payload, session_id)
    except ReadTimeoutError as error:
        look = f"{names.CLI_NAME} {names.ACTION_STATUS}"
        if payload.get("run_id"):
            look += f" {payload['run_id']}"
        return _failed(
            f"no reply within {names.AGENT_CLIENT_READ_TIMEOUT_SECONDS} s; the action may still "
            f"have run, so look with `{look}` before repeating it",
            error,
            session_id=session_id,
        )
    except (BotoCoreError, ClientError) as error:
        return _failed("InvokeAgentRuntime failed", error, session_id=session_id)
    except ReplyError as error:
        return _failed(str(error), session_id=session_id)

    print(render(response))
    return names.CLI_EXIT_OK if response["error"] is None else names.CLI_EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
