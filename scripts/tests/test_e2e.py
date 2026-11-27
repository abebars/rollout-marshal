"""scripts/e2e.py, the lead's end-to-end driver (task 5.1).

Nothing here reaches AWS, Bedrock or Flagsmith. The CLI runs for real, through its own
main(), against World, a fake runtime client whose status answers follow a timeline on a
fake clock; the SSM read is a fake too. boto3 reads (STS, GetApis, GetItem) are fakes, and
so are the children, except in the tests that start real stand-in processes. Every test
runs with no AWS credentials, the AWS config files at /dev/null and the HTTP layer refusing
to send, so a request that escaped a fake could not reach AWS.
"""

from __future__ import annotations

import ast
import io
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import botocore.httpsession
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.stub import Stubber

from cli import marshal
from contracts import names
from scripts import e2e

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts" / "e2e.py"
RUN_ID = "r202611271500"
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{names.REGION}:{names.ACCOUNT_ID}:runtime/"
    f"{names.AGENT_RUNTIME_DEPLOYED_NAME}-Abc123XyZ0"
)
API_ID = "zq7api9xk2"
ENDPOINT = f"https://{API_ID}.execute-api.{names.REGION}.amazonaws.com"
ASK_URL = ENDPOINT + "/ask"
GIVEN_URL = f"https://gv4url1ab0.execute-api.{names.REGION}.amazonaws.com/ask"
EPOCH = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
LOAD_PID, FAULT_PID = 4101, 4102
# The plan as start answers it: several lines, one of which reads like a rendered field, so
# a parse of marshal's printed output would go wrong. e2e.py reads the reply itself.
PLAN = (
    f"Planned run {RUN_ID}: move the support assistant to Claude Sonnet 5.\n"
    "awaiting: nothing\n"
    f"Approve with `marshal approve {RUN_ID} launch`, or decline with `marshal decline {RUN_ID}`."
)

P, R, RB, AR, RR, H, PR, AB = (
    names.STATE_PLANNED,
    names.STATE_RUNNING,
    names.STATE_ROLLED_BACK,
    names.STATE_AWAITING_RESUME,
    names.STATE_RESULTS_READY,
    names.STATE_HELD,
    names.STATE_PROMOTED,
    names.STATE_ABORTED,
)
# The fake times (s) of one clean run. start takes 40 s, so the plan returns at 40 and the
# status polls fall every 10 s from there: 300, 340, ... are all poll times.
HAPPY = (
    (0, P, None),
    (300, P, R),  # the human's approve, claimed
    (340, R, None),  # the launch commits
    (640, R, RB),
    (650, RB, None),
    (1150, AR, None),
    (1300, AR, R),  # the human's resume
    (1330, R, None),
    (1900, RR, None),
    (2100, RR, H),  # the human's decline
    (2110, H, None),
)
PASS_HISTORY = (P, R, RB, AR, R, RR, H)
NOTIFIED = {
    "3#ROLLED_BACK": "sns-msg-1",
    "4#AWAITING_RESUME": "sns-msg-2",
    "11#RESULTS_READY": "sns-msg-3",
}

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
)


@pytest.fixture(autouse=True)
def nothing_reaches_aws(monkeypatch):
    for name in _AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    def refuse(*args, **kwargs):
        raise AssertionError("a request reached the HTTP layer")

    monkeypatch.setattr(botocore.httpsession.URLLib3Session, "send", refuse)


def at(seconds: float) -> str:
    return names.utc_iso(EPOCH + timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeClock:
    """Fake time: sleep moves it on. on_sleep, if set, runs after each sleep."""

    def __init__(self, on_sleep=None):
        self.t = 0.0
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.on_sleep is not None:
            self.on_sleep(self)

    def utc(self):
        return at(self.t)


def response(state, pending=None, *, message=None, error=None, run_id=RUN_ID):
    return {
        "run_id": run_id,
        "state": state,
        "message": message or f"Run {run_id} is {state}.",
        "awaiting": names.awaiting_for(state),
        "pending": pending,
        "error": error,
    }


class World:
    """rm_agent as e2e.py sees it, through the real CLI. start answers PLANNED after
    plan_seconds; status answers the timeline's state at the clock's time, unless
    status_fault(n) gives the n-th status call an exception or a reply of its own."""

    def __init__(self, clock, timeline=HAPPY, *, plan_seconds=40, status_fault=None, start=None):
        self.clock, self.timeline, self.plan_seconds = clock, timeline, plan_seconds
        self.status_fault = status_fault or (lambda n: None)
        self.start = start
        self.payloads: list[dict] = []
        self.status_calls = 0

    def now(self):
        current = None
        for moment, state, pending in self.timeline:
            if self.clock.t >= moment:
                current = (state, pending)
        return current

    def invoke_agent_runtime(self, **kwargs):
        payload = json.loads(kwargs["payload"])
        self.payloads.append(payload)
        if payload["action"] == names.ACTION_START:
            self.clock.t += self.plan_seconds
            body = self.start if self.start is not None else response(P, message=PLAN)
        else:
            assert payload == {"action": names.ACTION_STATUS, "run_id": RUN_ID}
            fault = self.status_fault(self.status_calls)
            self.status_calls += 1
            if isinstance(fault, Exception):
                raise fault
            body = fault if fault is not None else response(*self.now())
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return {"response": io.BytesIO(raw), "statusCode": 200, "contentType": "application/json"}


class FakeSsm:
    def get_parameter(self, **kwargs):
        assert kwargs == {"Name": names.AGENT_RUNTIME_ARN_PARAMETER}
        return {"Parameter": {"Name": kwargs["Name"], "Type": "String", "Value": RUNTIME_ARN}}


class FakeProcess:
    """A child on the fake clock: start lines written at spawn, exit lines and the exit
    code once the clock reaches exit_at. SIGTERM ends it at once with term_lines, unless
    it ignores SIGTERM; SIGKILL always ends it."""

    def __init__(self, clock, log, pid, spec):
        self.clock, self.log, self.pid = clock, log, pid
        self.exit_at = spec.get("exit_at", 10**9)
        self.exit_code = spec.get("exit_code", 0)
        self.exit_lines = spec.get("exit_lines", ())
        self.term_lines = spec.get("term_lines", ())
        self.term_code = spec.get("term_code", -signal.SIGTERM)
        self.ignores_term = spec.get("ignores_term", False)
        self.returncode = None
        self.signals: list[int] = []
        self.write(spec.get("start_lines", ()))

    def write(self, lines):
        with open(self.log, "a") as out:
            out.writelines(line + "\n" for line in lines)

    def poll(self):
        if self.returncode is None and self.clock.t >= self.exit_at:
            self.write(self.exit_lines)
            self.returncode = self.exit_code
        return self.returncode

    def wait(self, timeout=None):
        assert self.poll() is not None, "wait() on a child that is still running"
        return self.returncode

    def signal(self, signum):
        self.signals.append(signum)
        if signum == signal.SIGTERM and not self.ignores_term:
            self.exit_at, self.exit_lines, self.exit_code = (
                self.clock.t,
                self.term_lines,
                self.term_code,
            )
        if signum == signal.SIGKILL:
            self.exit_at, self.exit_lines, self.exit_code = self.clock.t, (), -signal.SIGKILL


def load_spec(**changes):
    spec = {
        "start_lines": [
            f"load: 1500 requests, 150 users x 10 questions of run {RUN_ID}, at most 10 at a time"
        ],
        "exit_at": 340 + 1500,
        "exit_code": 0,
        "exit_lines": ["sent=1500 ok=1500 failed=0"],
        "term_lines": ["interrupted: sent 700 of 1500", "sent=700 ok=698 failed=2"],
        "term_code": 130,
    }
    return spec | changes


def fault_spec(**changes):
    spec = {
        "start_lines": [
            "inject_fault: template rm-delay-cheaper is EXTt3mpl4te",
            "inject_fault: experiment EXPf4ult0001 started at 2026-11-27T15:05:41Z"
            " (state initiating)",
        ],
        "exit_at": 340 + 400,
        "exit_code": 0,
        "exit_lines": [
            "inject_fault: experiment EXPf4ult0001 ended at 2026-11-27T15:12:10Z: completed"
            " (FIS start 2026-11-27T15:05:42Z, end 2026-11-27T15:11:55Z: Experiment completed.)"
        ],
    }
    return spec | changes


class Spawner:
    """deps.spawn and deps.killpg: FakeProcesses picked by the script each argv runs."""

    def __init__(self, clock, load=None, fault=None):
        self.clock = clock
        self.specs = {"load": load or load_spec(), "fault": fault or fault_spec()}
        self.argvs: dict[str, list[str]] = {}
        self.processes: dict[str, FakeProcess] = {}
        self.kills: list[tuple[int, int]] = []

    def spawn(self, argv, log):
        name = "load" if str(ROOT / names.LOAD_SCRIPT) in argv else "fault"
        assert name not in self.processes, f"{name} started twice"
        self.argvs[name] = argv
        pid = LOAD_PID if name == "load" else FAULT_PID
        self.processes[name] = FakeProcess(self.clock, log, pid, self.specs[name])
        return self.processes[name]

    def killpg(self, pid, signum):
        self.kills.append((pid, signum))
        for process in self.processes.values():
            if process.pid == pid:
                process.signal(signum)


class FakeSts:
    def __init__(self, account=names.ACCOUNT_ID, error=None):
        self.account, self.error = account, error

    def get_caller_identity(self):
        if self.error is not None:
            raise self.error
        return {"Account": self.account, "Arn": "arn:aws:sts::x:assumed-role/test", "UserId": "u"}


class FakeApis:
    """apigatewayv2's GetApis paginator over the given pages."""

    def __init__(self, *pages):
        self.pages = pages or ([api(names.API_NAME, ENDPOINT)],)
        self.reads = 0

    def get_paginator(self, operation):
        assert operation == "get_apis"
        return self

    def paginate(self):
        self.reads += 1
        for items in self.pages:
            yield {"Items": items}


def api(name, endpoint, protocol="HTTP"):
    return {
        "Name": name,
        "ApiEndpoint": endpoint,
        "ProtocolType": protocol,
        "ApiId": API_ID,
        "RouteSelectionExpression": "$request.method $request.path",
    }


def history(states=PASS_HISTORY):
    entries, previous = [], None
    for n, state in enumerate(states):
        engineer = state in (P, H, PR) or (previous, state) in ((P, R), (AR, R))
        entries.append(
            {
                "at": at(100 * n),
                "from": previous,
                "to": state,
                "actor": names.ACTOR_ENGINEER if engineer else names.ACTOR_AGENT,
                "reason": "start" if n == 0 else f"step {n}",
            }
        )
        previous = state
    return entries


def run_item(states=PASS_HISTORY, notified=None):
    return {
        "pk": names.run_pk(RUN_ID),
        "run_id": RUN_ID,
        "state": states[-1],
        "history": history(states),
        "notified": NOTIFIED if notified is None else notified,
        "report_key": names.report_key(RUN_ID),
        "incidents": [{"started_at": at(650), "resumed_at": at(1330)}],
        "experiment_id": 1042,
    }


class FakeDynamo:
    def __init__(self, item=None, error=None):
        self.item = run_item() if item is None else item
        self.error = error
        self.calls: list[dict] = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        serialize = TypeSerializer().serialize
        if self.item is False:
            return {}
        return {"Item": {key: serialize(value) for key, value in self.item.items()}}


class Harness:
    """One e2e.py run against the fakes. Every part can be changed before run()."""

    def __init__(self, monkeypatch, tmp_path, timeline=HAPPY, **world):
        self.tmp_path = tmp_path
        self.clock = FakeClock()
        self.world = World(self.clock, timeline, **world)
        self.spawner = Spawner(self.clock)
        self.sts, self.apis, self.dynamo = FakeSts(), FakeApis(), FakeDynamo()
        self.lock_checks = 0
        self.lock_error: Exception | None = None
        self.record = tmp_path / "record.json"
        monkeypatch.setattr(marshal, "new_agent_client", lambda: self.world)
        monkeypatch.setattr(marshal, "new_ssm_client", FakeSsm)

    def check_lock(self):
        self.lock_checks += 1
        if self.lock_error is not None:
            raise self.lock_error

    def deps(self, **changes):
        deps = e2e.Deps(
            clock=self.clock,
            spawn=self.spawner.spawn,
            killpg=self.spawner.killpg,
            sts=lambda: self.sts,
            apigateway=lambda: self.apis,
            dynamodb=lambda: self.dynamo,
            check_lock=self.check_lock,
            shield=_no_shield,
            log_dir=self.tmp_path,
        )
        for key, value in changes.items():
            setattr(deps, key, value)
        return deps

    def run(self, *argv, record=True, **changes):
        args = [*argv, *(["--record", str(self.record)] if record else [])]
        self.e2e = e2e.E2E(e2e.parse_args(args), self.deps(**changes))
        return self.e2e.run()

    def recorded(self):
        return json.loads(self.record.read_text())

    def actions(self):
        return [payload["action"] for payload in self.world.payloads]


class _no_shield:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


def make(monkeypatch, tmp_path, timeline=HAPPY, **world):
    return Harness(monkeypatch, tmp_path, timeline, **world)


# ---------------------------------------------------------------------------
# It never approves or declines
# ---------------------------------------------------------------------------
def _docstrings(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _enclosing_functions(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _function_of(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef):
            return node.name
    return None


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


HUMAN_ACTIONS = ("ACTION_APPROVE", "ACTION_DECLINE", "ACTION_CHECK")


def test_the_source_never_invokes_approve_decline_or_check():
    """Statically: the CLI runs in one place, call_marshal, which every caller hands start
    or status; the human's actions only ever reach human_command, which builds text; no
    literal spells them; nothing else runs a program but spawn_child's Popen."""
    tree = ast.parse(SOURCE.read_text())
    parents = _enclosing_functions(tree)
    docstrings = _docstrings(tree)

    mains = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "main"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "marshal"
    ]
    assert [_function_of(node, parents) for node in mains] == ["call_marshal"]

    cli_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) in ("cli", "call_marshal")
    ]
    assert cli_calls, "no CLI calls found"
    for node in cli_calls:
        first = node.args[0]
        assert isinstance(first, ast.Attribute) and first.attr in ("ACTION_START", "ACTION_STATUS")

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in HUMAN_ACTIONS:
            call = parents[node]
            assert isinstance(call, ast.Call) and _call_name(call) == "human_command", (
                f"names.{node.attr} used outside human_command, line {node.lineno}"
            )
            assert call.args[0] is node
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            assert node.value.strip() not in ("approve", "decline", "check"), node.lineno
            assert "marshal approve" not in node.value, node.lineno
            assert "marshal decline" not in node.value, node.lineno
            assert "marshal check" not in node.value, node.lineno

    runs_programs = {
        _call_name(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("subprocess", "os")
        and node.func.attr not in ("killpg",)
    }
    assert runs_programs == {"Popen"}
    popens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == "Popen"
    ]
    assert [_function_of(node, parents) for node in popens] == ["spawn_child"]


def test_the_sendable_actions_are_start_and_status():
    assert e2e.SENDABLE == (names.ACTION_START, names.ACTION_STATUS)


@pytest.mark.parametrize(
    "action",
    [
        names.ACTION_APPROVE,
        names.ACTION_DECLINE,
        names.ACTION_CHECK,
        names.ACTION_TICK,
        names.ACTION_ALARM,
    ],
)
def test_call_marshal_refuses_every_other_action_before_the_cli_runs(action, monkeypatch):
    def no_cli(*args, **kwargs):
        raise AssertionError("marshal.main ran")

    monkeypatch.setattr(marshal, "main", no_cli)
    with pytest.raises(e2e.NotSendable, match="the human's"):
        e2e.call_marshal(action, RUN_ID, names.AWAITS_LAUNCH)


@pytest.mark.parametrize("action", [names.ACTION_APPROVE, names.ACTION_DECLINE, "check"])
def test_the_recorder_refuses_a_payload_it_may_not_send_before_any_client(action, monkeypatch):
    def no_client():
        raise AssertionError("a client was made")

    monkeypatch.setattr(marshal, "new_agent_client", no_client)
    payload = json.dumps({"action": action, "run_id": RUN_ID, "actor": "engineer"}).encode()
    with pytest.raises(e2e.NotSendable):
        e2e._Recorder().invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, runtimeSessionId="s", payload=payload
        )


def test_a_full_run_sends_only_start_and_status(harness, capsys):
    assert harness.run() == e2e.EXIT_OK
    actions = harness.actions()
    assert actions[0] == names.ACTION_START and actions.count(names.ACTION_START) == 1
    assert set(actions[1:]) == {names.ACTION_STATUS}
    out = capsys.readouterr().out
    assert "marshal approve" in out and "marshal decline" in out  # printed, never sent


def test_call_marshal_goes_through_the_clis_own_main_and_keeps_the_reply(harness):
    harness.clock.t = 700
    reply = e2e.call_marshal(names.ACTION_STATUS, RUN_ID)
    assert reply.code == names.CLI_EXIT_OK
    assert reply.response == response(RB)
    assert reply.stdout == marshal.render(response(RB)) + "\n"
    assert reply.stderr == ""
    assert harness.world.payloads == [{"action": names.ACTION_STATUS, "run_id": RUN_ID}]


def test_call_marshal_keeps_the_clis_failure_on_stderr(harness):
    harness.world.status_fault = lambda n: ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeAgentRuntime"
    )
    reply = e2e.call_marshal(names.ACTION_STATUS, RUN_ID)
    assert reply.code == names.CLI_EXIT_ERROR and reply.response is None
    assert "ThrottlingException" in reply.stderr and reply.stdout == ""


# ---------------------------------------------------------------------------
# The timestamps
# ---------------------------------------------------------------------------
def test_the_four_timestamps_are_printed_and_recorded(harness, capsys):
    assert harness.run() == e2e.EXIT_OK
    out = capsys.readouterr().out
    prompt = json.dumps(e2e.DEFAULT_PROMPT)
    assert f"{at(0)}  start sent: marshal start {prompt}\n" in out
    assert f"{at(40)}  plan returned: run {RUN_ID} is PLANNED\n" in out
    assert f"{at(300)}  approve seen: the launch is claimed\n" in out
    assert f"{at(340)}  RUNNING: run {RUN_ID} is running\n" in out
    assert harness.recorded()["timestamps"] == {
        "start_sent": at(0),
        "plan_returned": at(40),
        "approve_seen": at(300),
        "running": at(340),
    }


def test_an_approve_whose_claim_falls_between_polls_is_seen_with_running(
    monkeypatch, tmp_path, capsys
):
    timeline = ((0, P, None), (305, R, None), *HAPPY[3:])
    h = make(monkeypatch, tmp_path, timeline)
    assert h.run() == e2e.EXIT_OK
    stamps = h.recorded()["timestamps"]
    assert stamps["approve_seen"] == stamps["running"] == at(310)
    assert f"{at(310)}  approve seen: the launch is committed\n" in capsys.readouterr().out


def test_every_state_change_is_printed_with_its_time_and_recorded(harness, capsys):
    harness.run()
    out = capsys.readouterr().out
    assert f"{at(640)}  status: RUNNING, RUNNING -> ROLLED_BACK pending\n" in out
    assert f"{at(650)}  status: ROLLED_BACK\n" in out
    seen = harness.recorded()["seen"]
    assert [(s["at"], s["state"], s["pending"]) for s in seen] == [
        (at(40) if moment == 0 else at(moment), state, pending) for moment, state, pending in HAPPY
    ]


def test_a_heartbeat_is_printed_while_nothing_changes(harness, capsys):
    harness.run()
    assert f"{at(460)}  still RUNNING; waiting for ROLLED_BACK, 2 min 00 s of 15 min\n" in (
        capsys.readouterr().out
    )


# ---------------------------------------------------------------------------
# The pauses
# ---------------------------------------------------------------------------
def test_each_pause_prints_exactly_what_the_human_types(harness, capsys):
    harness.run()
    out = capsys.readouterr().out
    launch = "\n".join(
        [
            "PAUSE: review the plan above. To launch it, type in another terminal:",
            f"    marshal approve {RUN_ID} launch",
            f"e2e.py approves nothing itself; it reads `marshal status {RUN_ID}` every 10 s"
            " until the run moves.",
            e2e.HUMAN_RULE,
        ]
    )
    resume = "\n".join(
        [
            "PAUSE: the cheaper model is healthy again. To resume the experiment, type:",
            f"    marshal approve {RUN_ID} resume",
            e2e.HUMAN_RULE,
        ]
    )
    decision = "\n".join(
        [
            "PAUSE: results are ready. Choose one, in another terminal.",
            "  To hold the run, type:",
            f"    marshal decline {RUN_ID}",
            "    then end the experiment in the Flagsmith dashboard: a hold leaves it running.",
            "  Or, to promote it, end the experiment in the Flagsmith dashboard, then type:",
            f"    marshal approve {RUN_ID} promotion",
            "  Note which metric the dashboard shows as primary, and the rollout decision you"
            " choose: evidence/5.1-e2e.md records both.",
            e2e.HUMAN_RULE,
        ]
    )
    for block in (launch, resume, decision):
        assert f"\n\n{block}\n\n" in out
    assert out.index(launch) < out.index(resume) < out.index(decision)
    assert f"{at(40)}  waiting for the human, run {RUN_ID}\n" in out
    assert f"{at(650)}  waiting for the human, run {RUN_ID}\n" not in out
    assert f"{at(1150)}  waiting for the human, run {RUN_ID}\n" in out
    assert f"{at(1900)}  waiting for the human, run {RUN_ID}\n" in out


def test_the_plan_is_printed_as_the_cli_renders_it(harness, capsys):
    harness.run()
    rendered = marshal.render(response(P, message=PLAN))
    out = capsys.readouterr().out
    assert rendered in out
    assert out.index(rendered) < out.index("PAUSE: review the plan above")


def test_the_pause_helpers_build_the_commands_from_names():
    assert e2e.launch_pause(RUN_ID)[1] == f"    marshal approve {RUN_ID} launch"
    assert e2e.resume_pause(RUN_ID)[1] == f"    marshal approve {RUN_ID} resume"
    decision = e2e.decision_pause(RUN_ID)
    assert decision[2] == f"    marshal decline {RUN_ID}"
    assert decision[5] == f"    marshal approve {RUN_ID} promotion"


def test_a_hold_says_to_end_the_experiment_and_every_pause_states_the_rule():
    """design.md section 6: a hold leaves Flagsmith as it is, so the experiment still runs."""
    decision = e2e.decision_pause(RUN_ID)
    assert "end the experiment in the Flagsmith dashboard" in decision[3]
    for pause in (e2e.launch_pause(RUN_ID), e2e.resume_pause(RUN_ID), decision):
        assert pause[-1] == e2e.HUMAN_RULE
    assert "CLAUDE.md" in e2e.HUMAN_RULE


def test_the_printed_summary_carries_the_stop_line_reads(harness, capsys):
    harness.run()
    out = capsys.readouterr().out
    assert out.index("==== Summary for evidence/5.1-e2e.md ====") < out.index(
        "Stop lines (CLAUDE.md): read both now, before any further load."
    )


@pytest.mark.parametrize(("sent", "phrase"), [(1500, "this run sent 1500"), (None, "unknown")])
def test_the_summary_ends_with_the_stop_line_reads(sent, phrase):
    """Gate 2's G6: CLAUDE.md's cost and usage checks after every load."""
    lines = e2e.stop_line_lines({"answers_sent": sent})
    assert lines[0].startswith("Stop lines (CLAUDE.md)")
    assert "smoke_reads.py cost --answers-sent" in lines[1] and phrase in lines[1]
    assert "--remaining-planned" in lines[1]
    assert lines[2].endswith("smoke_reads.py usage")
    assert "lags" in lines[3]


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
def _upto(moment):
    return tuple(step for step in HAPPY if step[0] <= moment)


TIMEOUTS = [
    # (case, timeline, when it fires, the message's start, a phrase of its hint)
    (
        "launch approval",
        _upto(0),
        40 + 30 * 60,
        "timed out after 30 min waiting for the launch approval.",
        f"`marshal decline {RUN_ID}` drops it before a new start",
    ),
    (
        "launch",
        _upto(300),
        300 + 15 * 60,
        "timed out after 15 min waiting for RUNNING.",
        f"`marshal check {RUN_ID}` finishes a stalled transition once its claim is older"
        " than 360 s",
    ),
    (
        "rollback",
        _upto(340),
        340 + 15 * 60,
        "timed out after 15 min waiting for ROLLED_BACK.",
        "should reach ALARM 3 to 5 minutes into the fault",
    ),
    (
        "resume request",
        _upto(650),
        650 + 25 * 60,
        "timed out after 25 min waiting for AWAITING_RESUME.",
        "the alarm back to OK (about 7 minutes after the last cheaper datapoint; 6 min 36 s"
        " in evidence/4.1-alarm.md)",
    ),
    (
        "resume approval",
        _upto(1150),
        1150 + 30 * 60,
        "timed out after 30 min waiting for the resume approval.",
        f"`marshal approve {RUN_ID} resume` resumes it",
    ),
    (
        "resume",
        _upto(1300),
        1300 + 15 * 60,
        "timed out after 15 min waiting for RUNNING.",
        f"`marshal status {RUN_ID}` shows what is pending",
    ),
    (
        "results",
        _upto(1330),
        1330 + 45 * 60,
        "timed out after 45 min waiting for RESULTS_READY.",
        "refresh at most once every 5 min",
    ),
    (
        "decision",
        _upto(1900),
        1900 + 60 * 60,
        "timed out after 60 min waiting for the decision.",
        f"end the experiment in the Flagsmith dashboard, then `marshal approve {RUN_ID} promotion`",
    ),
    (
        "decision effects",
        (*_upto(1900), (2100, RR, PR)),
        2100 + 15 * 60,
        "timed out after 15 min waiting for HELD or PROMOTED.",
        f"`marshal check {RUN_ID}` finishes a stalled transition",
    ),
]


@pytest.mark.parametrize(
    ("timeline", "fires", "said", "hint"),
    [case[1:] for case in TIMEOUTS],
    ids=[case[0] for case in TIMEOUTS],
)
def test_each_wait_times_out_with_a_clear_message(
    timeline, fires, said, hint, monkeypatch, tmp_path, capsys
):
    h = make(monkeypatch, tmp_path, timeline)
    h.spawner.specs["load"] = load_spec(exit_at=10**9)
    h.spawner.specs["fault"] = fault_spec(exit_at=10**9)
    assert h.run() == e2e.EXIT_FAILED
    assert h.clock.t == fires
    record = h.recorded()
    assert record["outcome"] == "failed"
    assert record["reason"].startswith(said) and hint in record["reason"]
    out = capsys.readouterr().out
    assert f"{at(fires)}  stopping: {said}" in out
    for process in h.spawner.processes.values():
        assert process.signals == [signal.SIGTERM]  # every child still running is stopped


def test_the_resume_request_wait_allows_for_the_fault_and_the_alarms_return_to_ok():
    # The fault's 6 minutes, the alarm's 6 min 36 s back to OK, then a 2-minute tick: even
    # end to end, that fits in the lead's 25 minutes.
    floor = names.FIS_DURATION_MINUTES * 60 + 396 + names.TICK_INTERVAL_MINUTES * 60
    assert e2e.Timeouts().resume_request == 25 * 60 > floor
    assert e2e.ALARM_OK_LAG == "6 min 36 s"


# ---------------------------------------------------------------------------
# Status errors and unexpected states
# ---------------------------------------------------------------------------
def _upstream(n):
    return response(R, error={"code": names.ERROR_UPSTREAM_FAILED, "message": "rm-runs: slow"})


def test_a_status_error_is_printed_and_the_wait_goes_on(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path, status_fault=lambda n: _upstream(n) if n == 40 else None)
    assert h.run() == e2e.EXIT_OK
    assert "status error 1 of 5 in a row: UPSTREAM_FAILED: rm-runs: slow\n" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("fault", "said"),
    [
        (_upstream, "UPSTREAM_FAILED: rm-runs: slow"),
        (
            lambda n: ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "InvokeAgentRuntime",
            ),
            "InvokeAgentRuntime failed",
        ),
        (lambda n: b"<html>busy</html>", "not a response"),
        (
            lambda n: response(None, run_id=None, message="no run", error=None),
            "status named run None, state None",
        ),
    ],
    ids=["error code", "call failed", "not a response", "no run"],
)
def test_five_status_errors_in_a_row_stop_the_script(fault, said, monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path, status_fault=lambda n: fault(n) if n >= 3 else None)
    assert h.run() == e2e.EXIT_FAILED
    out = capsys.readouterr().out
    for k in range(1, 6):
        assert f"status error {k} of 5 in a row: " in out
    assert "status error 6" not in out
    reason = h.recorded()["reason"]
    assert reason.startswith(f"`marshal status {RUN_ID}` failed 5 times in a row; the last: ")
    assert said in reason
    assert h.world.status_calls == 3 + 5


def test_a_good_status_resets_the_count(monkeypatch, tmp_path, capsys):
    bad = set(range(30, 34)) | set(range(35, 39))  # four, one good, four
    h = make(monkeypatch, tmp_path, status_fault=lambda n: _upstream(n) if n in bad else None)
    assert h.run() == e2e.EXIT_OK
    out = capsys.readouterr().out
    assert out.count("status error 4 of 5 in a row") == 2 and "status error 5" not in out


UNEXPECTED = [
    (
        "declined plan",
        ((0, P, None), (200, AB, None)),
        AB,
        "the launch approval",
    ),
    (
        "results before any rollback",
        (*_upto(340), (900, RR, None)),
        RR,
        "ROLLED_BACK",
    ),
    (
        "held before the resume",
        (*_upto(650), (700, H, None)),
        H,
        "AWAITING_RESUME",
    ),
    (
        "the resume's probe failed",
        (*_upto(1150), (1300, AR, RB), (1310, RB, None)),
        RB,
        "the resume approval",
    ),
    (
        "rolled back after results",
        (*_upto(1900), (2000, RB, None)),
        RB,
        "the decision",
    ),
]


@pytest.mark.parametrize(
    ("timeline", "state", "goal"),
    [case[1:] for case in UNEXPECTED],
    ids=[case[0] for case in UNEXPECTED],
)
def test_an_unexpected_state_stops_with_a_clear_message(
    timeline, state, goal, monkeypatch, tmp_path, capsys
):
    h = make(monkeypatch, tmp_path, timeline)
    assert h.run() == e2e.EXIT_FAILED
    reason = h.recorded()["reason"]
    assert (
        reason
        == f"run {RUN_ID} is {state} while e2e.py waited for {goal}: Run {RUN_ID} is {state}."
    )
    out = capsys.readouterr().out
    if state in names.TERMINAL_STATES:
        assert "is still" not in out
    else:
        assert (
            f"run {RUN_ID} is still {state}: `marshal status {RUN_ID}` shows it,"
            f" `marshal decline {RUN_ID}` ends it.\n"
        ) in out


def test_a_declined_plan_starts_no_child(monkeypatch, tmp_path):
    h = make(monkeypatch, tmp_path, UNEXPECTED[0][1])
    h.run()
    assert h.spawner.processes == {} and h.spawner.kills == []
    assert h.recorded()["answers_sent"] is None and h.recorded()["load"] is None


def test_a_start_that_fails_stops_before_any_wait(monkeypatch, tmp_path, capsys):
    active = response(
        R,
        run_id="r202611271400",
        error={"code": names.ERROR_RUN_ACTIVE, "message": "run r202611271400 is active"},
    )
    h = make(monkeypatch, tmp_path, start=active)
    assert h.run() == e2e.EXIT_FAILED
    assert h.actions() == [names.ACTION_START]
    record = h.recorded()
    assert record["reason"] == "marshal start returned RUN_ACTIVE: run r202611271400 is active"
    assert record["run_id"] is None and h.dynamo.calls == []


# ---------------------------------------------------------------------------
# The children: started together, watched, stopped
# ---------------------------------------------------------------------------
def test_load_and_fault_start_straight_away_once_running(harness, capsys):
    harness.run()
    out = capsys.readouterr().out
    assert harness.spawner.argvs["load"] == [
        sys.executable,
        str(ROOT / names.LOAD_SCRIPT),
        *("--run-id", RUN_ID, "--users", "150", "--questions", "10", "--url", ASK_URL),
    ]
    assert harness.spawner.argvs["fault"] == ["bash", str(ROOT / names.INJECT_FAULT_SCRIPT)]
    shown = f"scripts/load.py --run-id {RUN_ID} --users 150 --questions 10 --url <not shown>"
    assert f"{at(340)}  started {shown} (pid {LOAD_PID})\n" in out
    assert f"{at(340)}  started scripts/inject_fault.sh (pid {FAULT_PID})\n" in out
    assert out.index(f"started {shown}") < out.index("started scripts/inject_fault.sh")
    assert "[fault] inject_fault: experiment EXPf4ult0001 started at" in out


def test_the_load_size_is_passed_through(monkeypatch, tmp_path):
    h = make(monkeypatch, tmp_path)
    h.run("--users", "150", "--questions", "2")
    argv = h.spawner.argvs["load"]
    assert argv[argv.index("--users") + 1] == "150" and argv[argv.index("--questions") + 1] == "2"
    assert h.recorded()["load_size"] == {"users": 150, "questions": 2}


def test_ctrl_c_stops_both_children_with_sigterm_and_still_summarises(harness, capsys):
    def interrupt(clock):
        if clock.t >= 500:
            clock.on_sleep = None
            raise KeyboardInterrupt

    harness.clock.on_sleep = interrupt
    assert harness.run() == e2e.EXIT_INTERRUPTED
    assert harness.spawner.kills == [(LOAD_PID, signal.SIGTERM), (FAULT_PID, signal.SIGTERM)]
    out = capsys.readouterr().out
    shown = f"scripts/load.py --run-id {RUN_ID} --users 150 --questions 10 --url <not shown>"
    assert f"stopping {shown} (pid {LOAD_PID}) with SIGTERM\n" in out
    assert f"stopping scripts/inject_fault.sh (pid {FAULT_PID}) with SIGTERM\n" in out
    assert "the FIS experiment, if it started, runs on to its 6-minute end; e2e.py never" in out
    assert "[load] sent=700 ok=698 failed=2\n" in out
    assert "answers sent: 700 (load.py: sent=700 ok=698 failed=2, exit 130)" in out
    record = harness.recorded()
    assert record["outcome"] == "interrupted" and record["answers_sent"] == 700
    assert record["load"]["signalled"] == ["SIGTERM"] and record["fault"]["signalled"] == [
        "SIGTERM"
    ]
    assert f"run {RUN_ID} is still RUNNING" in out


def test_a_child_that_ignores_sigterm_gets_sigkill_after_the_grace(harness, capsys):
    harness.spawner.specs["load"] = load_spec(ignores_term=True)

    def interrupt(clock):
        if clock.t >= 500:
            clock.on_sleep = None
            raise KeyboardInterrupt

    harness.clock.on_sleep = interrupt
    assert harness.run() == e2e.EXIT_INTERRUPTED
    assert harness.spawner.kills == [
        (LOAD_PID, signal.SIGTERM),
        (FAULT_PID, signal.SIGTERM),
        (LOAD_PID, signal.SIGKILL),
    ]
    out = capsys.readouterr().out
    assert "did not stop within 60 s; sending SIGKILL" in out
    assert "answers sent: unknown, load.py printed no counts (exit -9); count all 1500" in out


def test_ctrl_c_at_a_pause_stops_no_child_and_names_the_run(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path, _upto(0))

    def interrupt(clock):
        if clock.t >= 100:
            clock.on_sleep = None
            raise KeyboardInterrupt

    h.clock.on_sleep = interrupt
    assert h.run() == e2e.EXIT_INTERRUPTED
    assert h.spawner.kills == []
    out = capsys.readouterr().out
    assert f"run {RUN_ID} is still PLANNED: `marshal status {RUN_ID}` shows it" in out
    assert "answers sent: 0 (load.py did not start)" in out


def test_it_never_stops_a_fis_experiment_through_the_api():
    source = SOURCE.read_text()
    tree = ast.parse(source)
    services = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_name(node) in ("aws_client", "client")
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert services == {"sts", "apigatewayv2", "dynamodb"}
    for word in ("stop_experiment", "StopExperiment", '"fis"', "'fis'"):
        assert word not in source


def test_the_load_ending_early_stops_the_run(harness, capsys):
    harness.spawner.specs["load"] = load_spec(
        exit_at=340,
        exit_code=3,
        start_lines=["load.py: refused to start: another load holds rm-load.lock (pid 77)"],
        exit_lines=[],
    )
    assert harness.run() == e2e.EXIT_FAILED
    assert harness.recorded()["reason"] == "load.py ended early, exit 3; see its output above"
    assert harness.spawner.kills == [(FAULT_PID, signal.SIGTERM)]
    assert "[load] load.py: refused to start: another load holds" in capsys.readouterr().out


def test_a_load_whose_requests_all_failed_stops_the_run(harness):
    harness.spawner.specs["load"] = load_spec(
        exit_at=400,
        exit_code=1,
        exit_lines=["failed: HTTP 403 x1500", "sent=1500 ok=0 failed=1500"],
    )
    assert harness.run() == e2e.EXIT_FAILED
    assert harness.recorded()["reason"] == "none of load.py's 1500 requests succeeded"
    assert harness.recorded()["answers_sent"] == 1500


def test_a_fault_script_that_ends_before_its_experiment_started_stops_the_run(harness):
    harness.spawner.specs["fault"] = fault_spec(
        exit_at=340,
        exit_code=2,
        start_lines=["inject_fault: the credentials are for account 1, not 818928662830"],
        exit_lines=[],
    )
    assert harness.run() == e2e.EXIT_FAILED
    assert harness.recorded()["reason"] == (
        "inject_fault.sh ended, exit 2, before its experiment started: no fault was injected"
    )
    assert harness.spawner.kills == [(LOAD_PID, signal.SIGTERM)]


def test_a_load_that_finishes_before_the_rollback_is_fine(harness, capsys):
    harness.spawner.specs["load"] = load_spec(exit_at=600, exit_lines=["sent=300 ok=300 failed=0"])
    assert harness.run("--questions", "2") == e2e.EXIT_OK
    out = capsys.readouterr().out
    assert f"{at(600)}  load.py ended, exit 0: sent=300 ok=300 failed=0\n" in out
    assert f"{at(740)}  inject_fault.sh ended, exit 0: the experiment completed\n" in out
    assert harness.spawner.kills == []


def test_at_the_end_it_waits_for_the_load_to_stop_at_its_count(harness, capsys):
    harness.spawner.specs["load"] = load_spec(exit_at=2500)
    assert harness.run() == e2e.EXIT_OK
    out = capsys.readouterr().out
    assert "waiting for scripts/load.py --run-id" in out and "to end by itself" in out
    assert harness.clock.t == 2500  # polled every 10 s from HELD at 2110
    assert harness.spawner.kills == []
    assert harness.recorded()["answers_sent"] == 1500


def test_ctrl_c_after_the_run_ended_stops_the_load_and_keeps_the_outcome(harness, capsys):
    harness.spawner.specs["load"] = load_spec(exit_at=10**9)

    def interrupt(clock):
        if clock.t >= 2200:  # HELD came at 2110; the load is still sending
            clock.on_sleep = None
            raise KeyboardInterrupt

    harness.clock.on_sleep = interrupt
    assert harness.run() == e2e.EXIT_OK
    assert harness.spawner.kills == [(LOAD_PID, signal.SIGTERM)]
    out = capsys.readouterr().out
    assert "stop signal: the run has ended, so only the children stop now" in out
    record = harness.recorded()
    assert record["outcome"] == "completed" and record["reason"] is None
    assert record["answers_sent"] == 700 and record["load"]["signalled"] == ["SIGTERM"]


def test_a_load_past_its_worst_case_is_stopped_at_the_end(harness, capsys):
    harness.spawner.specs["load"] = load_spec(exit_at=10**9)
    assert harness.run("--users", "10", "--questions", "1") == e2e.EXIT_OK
    out = capsys.readouterr().out
    # 10 requests, 10 at a time: one round of 30 s, plus the 60 s grace, from 340.
    assert "is still running after 1.5 min" in out
    assert harness.spawner.kills == [(LOAD_PID, signal.SIGTERM)]


# ---------------------------------------------------------------------------
# Real processes: the stop reaches each child's whole group
# ---------------------------------------------------------------------------
LOAD_STANDIN = """\
import signal, sys, time
def stop(signum, frame):
    print("interrupted: sent 3 of 10", flush=True)
    print("sent=3 ok=3 failed=0", flush=True)
    sys.exit(130)
signal.signal(signal.SIGTERM, stop)
print("load: warming up", file=sys.stderr, flush=True)
print("load: ready", flush=True)
while True:
    time.sleep(0.05)
"""
FAULT_STANDIN = """\
trap 'echo "inject_fault: stopped by SIGTERM"; exit 143' TERM
echo "inject_fault: experiment EXPstandin01 started at 2026-11-27T15:00:00Z (state initiating)"
while true; do sleep 0.05; done
"""


class SteppingClock(FakeClock):
    """Fake time that lets a little real time pass in each sleep, for real children."""

    def sleep(self, seconds):
        time.sleep(0.05)
        super().sleep(seconds)


def test_real_children_get_sigterm_in_their_own_sessions(monkeypatch, tmp_path, capsys):
    load_script, fault_script = tmp_path / "load_standin.py", tmp_path / "fault_standin.sh"
    load_script.write_text(LOAD_STANDIN)
    fault_script.write_text(FAULT_STANDIN)
    h = make(monkeypatch, tmp_path, ((0, P, None), (100, R, None)))
    h.clock = SteppingClock()
    h.world.clock = h.clock
    sessions = {}
    began = time.monotonic()

    def interrupt(clock):
        children = h.e2e.load, h.e2e.fault
        ready = all(c is not None and c.log.exists() and c.log.read_text() for c in children)
        if ready or time.monotonic() - began > 20:
            for child in children:
                if child is not None:
                    sessions[child.name] = os.getsid(child.process.pid) == child.process.pid
            clock.on_sleep = None
            raise KeyboardInterrupt

    h.clock.on_sleep = interrupt
    code = h.run(
        clock=h.clock,
        spawn=e2e.spawn_child,
        killpg=os.killpg,
        shield=e2e.shield_signals,
        load_argv=lambda run_id, users, questions, url: [sys.executable, str(load_script)],
        fault_argv=lambda: ["bash", str(fault_script)],
        timeouts=e2e.Timeouts(rollback=10**6),
    )
    assert code == e2e.EXIT_INTERRUPTED
    assert sessions == {"load": True, "fault": True}
    assert h.e2e.load.process.returncode == 130 and h.e2e.fault.process.returncode == 143
    out = capsys.readouterr().out
    assert "[load] load: warming up\n" in out  # stderr goes to the log too
    assert "[load] sent=3 ok=3 failed=0\n" in out
    assert "[fault] inject_fault: stopped by SIGTERM\n" in out
    assert "SIGKILL" not in out
    record = h.recorded()
    assert record["answers_sent"] == 3
    assert record["fault"]["experiment_id"] == "EXPstandin01"
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # the shield restored it


# ---------------------------------------------------------------------------
# The end: summary and --record
# ---------------------------------------------------------------------------
def test_the_record_holds_what_the_evidence_needs(harness):
    assert harness.run() == e2e.EXIT_OK
    record = harness.recorded()
    assert record["run_id"] == RUN_ID and record["outcome"] == "completed"
    assert record["reason"] is None and record["last_status"] == H
    assert record["prompt"] == e2e.DEFAULT_PROMPT
    assert record["history"] == history()
    assert record["history_states"] == list(PASS_HISTORY)
    assert record["history_matches_pass"] is True
    assert record["answers_sent"] == 1500
    assert record["load"] | {"log": None} == {
        "command": f"scripts/load.py --run-id {RUN_ID} --users 150 --questions 10"
        " --url <not shown>",
        "started_at": at(340),
        "exit_code": 0,
        "signalled": [],
        "sent": 1500,
        "ok": 1500,
        "failed": 0,
        "output": load_spec()["start_lines"] + load_spec()["exit_lines"],
        "log": None,
    }
    assert record["fault"]["experiment_id"] == "EXPf4ult0001"
    assert record["fault"]["exit_code"] == 0
    assert record["report_key"] == f"runs/{RUN_ID}/report.md"
    assert record["report"] == f"s3://{names.REPORTS_BUCKET}/runs/{RUN_ID}/report.md"
    assert record["notified"] == NOTIFIED
    assert record["incidents"] == [{"started_at": at(650), "resumed_at": at(1330)}]
    assert record["run_item_error"] is None


def test_the_run_item_is_one_consistent_read_of_rm_runs(harness):
    harness.run()
    assert harness.dynamo.calls == [
        {
            "TableName": names.RUNS_TABLE,
            "Key": {names.PARTITION_KEY: {"S": f"RUN#{RUN_ID}"}},
            "ConsistentRead": True,
        }
    ]
    assert harness.recorded()["history"][0]["from"] is None


def test_the_summary_prints_history_timestamps_answers_report_and_sns_ids(harness, capsys):
    harness.run()
    out = capsys.readouterr().out
    summary = out[out.index("==== Summary for evidence/5.1-e2e.md ====") :]
    expected = [
        f"run:      {RUN_ID}",
        "outcome:  completed",
        f"  start sent:     {at(0)}",
        f"  plan returned:  {at(40)}",
        f"  approve seen:   {at(300)}",
        f"  RUNNING:        {at(340)}",
        f"history (rm-runs, RUN#{RUN_ID}):",
        f"  1. {at(0)}  (none) -> PLANNED  engineer: start",
        f"  7. {at(600)}  RESULTS_READY -> HELD  engineer: step 6",
        "  reads as 5.1's Pass (PLANNED, RUNNING, ROLLED_BACK, AWAITING_RESUME, RUNNING,"
        " RESULTS_READY, then HELD or PROMOTED): yes",
        "answers sent: 1500 (load.py: sent=1500 ok=1500 failed=0, exit 0)",
        "fault: inject_fault.sh exit 0, FIS experiment EXPf4ult0001",
        f"report: s3://rm-reports-818928662830/runs/{RUN_ID}/report.md",
        "SNS message IDs (notified):",
        "  3#ROLLED_BACK: sns-msg-1\n  4#AWAITING_RESUME: sns-msg-2\n  11#RESULTS_READY: sns-msg-3",
    ]
    for line in expected:
        assert line in summary
    assert summary.index("approve seen") < summary.index("history (") < summary.index("report:")


def test_a_history_off_the_pass_exits_1(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path)
    h.dynamo = FakeDynamo(run_item((P, R, RB, AR, RB, AR, R, RR, H)))
    assert h.run() == e2e.EXIT_FAILED
    assert h.recorded()["history_matches_pass"] is False
    assert "does not read as task 5.1's Pass" in capsys.readouterr().out


def test_a_run_item_that_cannot_be_read_still_gives_the_summary(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path)
    h.dynamo = FakeDynamo(error=NoCredentialsError())
    assert h.run() == e2e.EXIT_FAILED
    record = h.recorded()
    assert record["run_item_error"].startswith("NoCredentialsError")
    assert record["answers_sent"] == 1500
    assert "history: not read (NoCredentialsError" in capsys.readouterr().out


def test_without_record_nothing_is_written(harness, tmp_path):
    assert harness.run(record=False) == e2e.EXIT_OK
    assert not harness.record.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fault.log", "load.log"]


def test_record_refuses_an_existing_file(tmp_path, capsys):
    existing = tmp_path / "old.json"
    existing.write_text("{}")
    with pytest.raises(SystemExit) as stop:
        e2e.parse_args(["--record", str(existing)])
    assert stop.value.code == e2e.EXIT_USAGE
    assert "exists; give a new file" in capsys.readouterr().err
    assert existing.read_text() == "{}"


def test_record_needs_an_existing_directory(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        e2e.parse_args(["--record", str(tmp_path / "missing" / "r.json")])
    assert stop.value.code == e2e.EXIT_USAGE
    assert "is not a directory" in capsys.readouterr().err


def test_a_record_that_cannot_be_written_is_printed_instead(harness, capsys):
    e2e_args = e2e.parse_args(["--record", str(harness.record)])
    harness.record.write_text("taken meanwhile")
    run = e2e.E2E(e2e_args, harness.deps())
    assert run.run() == e2e.EXIT_OK
    out = capsys.readouterr().out
    assert f"could not write {harness.record}" in out and f'"run_id": "{RUN_ID}"' in out
    assert harness.record.read_text() == "taken meanwhile"


# ---------------------------------------------------------------------------
# The URL: found read-only, never printed
# ---------------------------------------------------------------------------
def _everything_printed(capsys, harness):
    captured = capsys.readouterr()
    logs = "".join(p.read_text() for p in harness.tmp_path.glob("*.log"))
    record = harness.record.read_text() if harness.record.exists() else ""
    return captured.out + captured.err + logs + record


def test_the_url_is_found_by_name_on_any_page_and_never_printed(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path)
    h.apis = FakeApis(
        [api("other-api", "https://other1abc.execute-api.us-east-1.amazonaws.com")],
        [api(names.API_NAME, ENDPOINT + "/")],
    )
    assert h.run() == e2e.EXIT_OK
    argv = h.spawner.argvs["load"]
    assert argv[argv.index("--url") + 1] == ASK_URL
    assert h.apis.reads == 1
    printed = _everything_printed(capsys, h)
    for secret in (ASK_URL, ENDPOINT, API_ID):
        assert secret not in printed
    assert "route: rm-api's endpoint plus /ask (never shown)" in printed


def test_a_given_url_is_used_without_reading_the_apis_and_never_printed(
    monkeypatch, tmp_path, capsys
):
    h = make(monkeypatch, tmp_path)
    assert h.run("--url", GIVEN_URL) == e2e.EXIT_OK
    argv = h.spawner.argvs["load"]
    assert argv[argv.index("--url") + 1] == GIVEN_URL
    assert h.apis.reads == 0
    printed = _everything_printed(capsys, h)
    assert GIVEN_URL not in printed and "gv4url1ab0" not in printed


@pytest.mark.parametrize(
    ("pages", "said"),
    [
        (([],), "expected exactly one API named rm-api, found 0"),
        (
            ([api(names.API_NAME, ENDPOINT)], [api(names.API_NAME, ENDPOINT)]),
            "expected exactly one API named rm-api, found 2",
        ),
        (([api(names.API_NAME, ENDPOINT, "WEBSOCKET")],), "rm-api is not an HTTP API"),
        (
            ([api(names.API_NAME, f"https://{API_ID}.execute-api.us-west-2.amazonaws.com")],),
            "rm-api's endpoint plus /ask is not an https URL on an API Gateway execute-api host"
            " in us-east-1",
        ),
    ],
    ids=["none", "two", "not http", "other region"],
)
def test_without_exactly_one_rm_api_nothing_starts(pages, said, monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path)
    h.apis = FakeApis(*pages)
    assert h.run() == e2e.EXIT_REFUSED
    assert h.world.payloads == [] and h.spawner.processes == {}
    printed = _everything_printed(capsys, h)
    assert f"refused: {said}; nothing was started" in printed
    assert API_ID not in printed and not h.record.exists()


def test_the_discovery_is_one_paged_getapis_read():
    client = boto3.session.Session().client("apigatewayv2", region_name=names.REGION)
    stubber = Stubber(client)
    stubber.add_response(
        "get_apis",
        {
            "Items": [api("other", "https://o1.execute-api.us-east-1.amazonaws.com")],
            "NextToken": "t",
        },
        {},
    )
    stubber.add_response("get_apis", {"Items": [api(names.API_NAME, ENDPOINT)]}, {"NextToken": "t"})
    with stubber:
        assert e2e.discover_ask_url(client) == ASK_URL
    stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "url",
    [
        f"http://{API_ID}.execute-api.us-east-1.amazonaws.com/ask",
        f"https://{API_ID}.execute-api.us-west-2.amazonaws.com/ask",
        f"https://{API_ID}.execute-api.us-east-1.amazonaws.com/",
        f"https://{API_ID}.execute-api.us-east-1.amazonaws.com/ask?x={API_ID}",
        f"https://{API_ID}.execute-api.us-east-1.amazonaws.com:8443/ask",
        f"https://user@{API_ID}.execute-api.us-east-1.amazonaws.com/ask",
        f"https://{API_ID}.example.com/ask",
    ],
)
def test_a_bad_url_is_refused_without_quoting_it(url, capsys):
    with pytest.raises(SystemExit) as stop:
        e2e.parse_args(["--url", url])
    assert stop.value.code == e2e.EXIT_USAGE
    err = capsys.readouterr().err
    assert "argument --url: not" in err and API_ID not in err


def test_a_stray_url_is_counted_not_quoted(capsys):
    with pytest.raises(SystemExit):
        e2e.parse_args([ASK_URL])
    err = capsys.readouterr().err
    assert "1 unrecognized argument(s), not shown" in err and API_ID not in err


# ---------------------------------------------------------------------------
# Arguments and the checks before anything starts
# ---------------------------------------------------------------------------
def test_the_defaults_are_the_full_run_and_the_sample_prompt():
    args = e2e.parse_args([])
    assert (args.users, args.questions) == (150, 10) == tuple(names.LOAD_FULL)
    assert args.prompt == e2e.DEFAULT_PROMPT
    assert args.prompt == (
        "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5."
    )
    assert args.url is None and args.record is None


def test_the_prompt_is_sent_as_given(monkeypatch, tmp_path):
    h = make(monkeypatch, tmp_path)
    h.run("--prompt", "Move us to Claude Sonnet 5 from Claude Fable 5.1.")
    assert h.world.payloads[0] == {
        "action": names.ACTION_START,
        "prompt": "Move us to Claude Sonnet 5 from Claude Fable 5.1.",
        "actor": names.ACTOR_ENGINEER,
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["--users", "0"],
        ["--users", "151"],
        ["--questions", "0"],
        ["--questions", "31"],
        ["--users", "ten"],
        ["--prompt", "  "],
    ],
)
def test_bad_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as stop:
        e2e.parse_args(argv)
    assert stop.value.code == e2e.EXIT_USAGE


def test_credentials_for_another_account_start_nothing(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path)
    h.sts = FakeSts(account="111111111111")
    assert h.run() == e2e.EXIT_REFUSED
    assert h.world.payloads == [] and h.lock_checks == 0
    assert (
        "refused: the credentials are for account 111111111111, not 818928662830;"
        " nothing was started"
    ) in capsys.readouterr().out


def test_no_credentials_start_nothing(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path)
    h.sts = FakeSts(error=NoCredentialsError())
    assert h.run() == e2e.EXIT_REFUSED
    assert h.world.payloads == []
    assert "refused: could not read the caller identity (NoCredentialsError" in (
        capsys.readouterr().out
    )


def test_a_held_load_lock_starts_nothing(monkeypatch, tmp_path, capsys):
    h = make(monkeypatch, tmp_path)
    h.lock_error = e2e.Refused("load.py could not start: another load holds rm-load.lock")
    assert h.run() == e2e.EXIT_REFUSED
    assert h.world.payloads == []
    assert "refused: load.py could not start: another load holds rm-load.lock" in (
        capsys.readouterr().out
    )


def test_the_lock_check_uses_load_pys_own_lock(monkeypatch, tmp_path):
    taken = []

    class Held:
        def __enter__(self):
            taken.append("in")
            return self

        def __exit__(self, *exc_info):
            taken.append("out")

    monkeypatch.setattr(e2e.load_script, "lock_path", lambda: tmp_path / names.LOAD_LOCK_NAME)
    monkeypatch.setattr(e2e.load_script, "take_lock", lambda path: Held())
    e2e.check_load_lock()
    assert taken == ["in", "out"]

    def refused(path):
        raise e2e.load_script.LoadRefused(f"another load holds {path} (pid 9)")

    monkeypatch.setattr(e2e.load_script, "take_lock", refused)
    with pytest.raises(e2e.Refused, match=r"load.py could not start: another load holds .* 9"):
        e2e.check_load_lock()


def test_the_real_lock_check_refuses_while_a_load_holds_the_lock(monkeypatch, tmp_path):
    lock = tmp_path / names.LOAD_LOCK_NAME
    monkeypatch.setattr(e2e.load_script, "lock_path", lambda: lock)
    with e2e.load_script.take_lock(lock):
        with pytest.raises(e2e.Refused, match="another load holds"):
            e2e.check_load_lock()
    e2e.check_load_lock()  # free again


def test_main_installs_the_stop_handlers(monkeypatch):
    seen = {}

    class Stub:
        def __init__(self, args, deps):
            pass

        def run(self):
            seen.update({s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)})
            return 0

    saved = {s: signal.getsignal(s) for s in e2e.STOP_SIGNALS}
    monkeypatch.setattr(e2e, "E2E", Stub)
    try:
        assert e2e.main([]) == 0
    finally:
        for stop, handler in saved.items():
            signal.signal(stop, handler)
    assert seen == {signal.SIGTERM: e2e._stop, signal.SIGHUP: e2e._stop}
    with pytest.raises(KeyboardInterrupt):
        e2e._stop(signal.SIGTERM, None)
