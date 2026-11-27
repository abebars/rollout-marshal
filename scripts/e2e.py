"""Drive one Rollout Marshal run end to end (task 5.1; design.md sections 6, 7, 9 and 11).

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=<the engineer's SSO profile in 818928662830>
    uv run python scripts/e2e.py [--users 150] [--questions 10] [--prompt "<text>"]
                                 [--url <the route's URL>] [--record <new file>.json]
                                 [--pause-minutes N]

--pause-minutes lengthens the human's three pauses (launch, resume and the decision), for a
human who will not be at the terminal within the defaults: each waits the longer of its
default and N minutes, so it never shortens one.

An existing --record file is refused while the arguments are read (exit 2). Then, before
anything starts, it checks, each by a read only: that the credentials are for account
818928662830; that the alarm rm-cheaper-latency-p95 exists and is not in ALARM; that
rm-invoker does not run with STUB=1 (invoker_stub must be false, task 5.1); that exactly
one FIS experiment template carries the tag scripts/inject_fault.sh selects by; the route's
URL (--url, or rm-api's endpoint from apigatewayv2 GetApis plus /ask); and that no other load
holds rm-load.lock. A failed check refuses (exit 3), names what to fix, and starts nothing.

Then it drives one run. Each line of its own carries a UTC timestamp; the plan, the pause
blocks and the summary are printed as they are.

1. marshal start "<prompt>", through the CLI's own main(), in this process. It prints the
   plan, the time it sent start and the time the plan returned.
2. A pause: it prints the command that approves the launch (uv run marshal ..., to run
   from the repository root with the same AWS_PROFILE), then reads marshal status every
   10 seconds. It prints when it sees the approve take effect (the launch claimed,
   or the run already RUNNING) and when the run is RUNNING, each to within one poll.
3. scripts/load.py --run-id <run_id> --users N --questions M --url <url> in the
   background, and straight away scripts/inject_fault.sh, both with this shell's
   environment, the engineer's SSO credentials. Their output is shown as it comes, and
   kept in a log directory that outlives this script.
4. It waits for ROLLED_BACK, then AWAITING_RESUME; pauses for the human to resume; waits
   for RUNNING again.
5. It waits for RESULTS_READY; pauses for the human to hold, or to end the experiment in
   the Flagsmith dashboard and promote; waits for HELD or PROMOTED.
6. It waits for the load to stop at its count, then prints the summary for
   evidence/5.1-e2e.md (the run item's history and notified map, read from rm-runs; the
   four timestamps; the answers sent, from load.py's output; the report's S3 key) and
   writes it to --record as JSON.

It never approves, declines or checks anything. It sends only start and status:
call_marshal refuses any other action before the CLI runs, and _Recorder refuses it again
before a request goes out. The human's commands appear only as text it prints.

It never prints the route's URL, and never writes it to the record: the command line it
shows for load.py has <not shown> in its place, and no error quotes it.

Every wait has a timeout (Timeouts) and says what to look at when it runs out. A failed
status call is printed and the wait goes on; STATUS_ERRORS_FATAL failures in a row stop
the script. So does a state off the expected path (HELD before the resume, say), with the
agent's own message, and a child that ends early: a load refused before sending, or a
fault script that ends before its experiment started.

Ctrl-C, SIGTERM and SIGHUP stop it. On every stop, and at the end, it sends SIGTERM to the
process group of each child still running, waits for it (load.py finishes the requests in
flight and prints its counts), then prints the summary. It never stops a FIS experiment
through the API: inject_fault.sh leaves the fault to end by its 6-minute duration. The run
itself stays where it is, and the message says how to look at it and end it. Output that
cannot be written, to a closed terminal (EIO) or a pipe with no reader (EPIPE), is dropped,
so the children still stop and --record is still written.

Exit status: 0 the run ended HELD or PROMOTED and its history reads as task 5.1's Pass;
1 it failed, timed out or stopped on an unexpected state; 2 bad arguments, an existing
--record among them; 3 refused before anything started; 130 stopped by a signal.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # contracts, cli and scripts

import boto3  # noqa: E402
from boto3.dynamodb.types import TypeDeserializer  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from cli import marshal  # noqa: E402
from contracts import dynamo, names  # noqa: E402
from scripts import load as load_script  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2  # argparse's own status for bad arguments
EXIT_REFUSED = 3
EXIT_INTERRUPTED = 130  # 128 + SIGINT, the shell's convention; for SIGTERM and SIGHUP too

# The sample prompt the repo's tests use (KICKOFF.md's first line).
DEFAULT_PROMPT = "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5."
# The only actions this script ever sends. approve, decline and check are the human's.
SENDABLE = (names.ACTION_START, names.ACTION_STATUS)

POLL_SECONDS = 10  # one status call every 10 s, start to start (the lead: every 10 to 15 s)
HEARTBEAT_SECONDS = 120  # a "still waiting" line when nothing has changed for this long
STATUS_ERRORS_FATAL = 5  # failed status calls in a row that stop the script
# load.py, sent SIGTERM, sends nothing more and waits for the requests in flight, each
# bounded by API Gateway's 30 s (its docstring); past this grace a child gets SIGKILL.
CHILD_STOP_GRACE_SECONDS = 2 * names.API_GATEWAY_TIMEOUT_SECONDS
CHILD_POLL_SECONDS = 0.5
CHILD_KILL_WAIT_SECONDS = 10  # after a SIGKILL, real seconds
# inject_fault.sh gives up waiting on its experiment after RM_FIS_MAX_WAIT_SECONDS, 1200 s
# by default, and exits 3; a margin for its polls on top.
FAULT_SCRIPT_MAX_SECONDS = 1200 + 120
# The alarm's ALARM-to-OK lag, measured from the start of its last breaching minute: 7 min
# 20 s in task 4.1's test and 7 min 17 s in 4.3's fault run (evidence/4.3-fis.md).
ALARM_OK_LAG_SECONDS = 7 * 60 + 20
ALARM_OK_LAG = f"{ALARM_OK_LAG_SECONDS // 60} min {ALARM_OK_LAG_SECONDS % 60} s"
PAUSE_MINUTES_MAX = 24 * 60  # --pause-minutes: the longest a human pause may be told to wait
# scripts/inject_fault.sh starts the one template whose tags.Name is TEMPLATE_NAME
# (names.FIS_TEMPLATE); the preflight finds it the same way.
FAULT_TEMPLATE_TAG = "Name"

ROUTE_METHOD, ROUTE_PATH = names.API_ROUTE_KEY.split(" ", 1)  # POST, /ask
# An HTTP API's endpoint is https://{api-id}.execute-api.{region}.amazonaws.com, and its
# $default stage is served from the base of that URL (API Gateway developer guide, "Stages
# for HTTP APIs"); the app module's ask_url output is that endpoint plus /ask.
EXECUTE_API_HOST = re.compile(
    r"[a-z0-9]+\.execute-api\." + re.escape(names.REGION) + r"\.amazonaws\.com"
)
NOT_SHOWN = "<not shown>"

# Task 5.1's Pass: the run item's history reads these, then HELD or PROMOTED.
PASS_PATH = (
    names.STATE_PLANNED,
    names.STATE_RUNNING,
    names.STATE_ROLLED_BACK,
    names.STATE_AWAITING_RESUME,
    names.STATE_RUNNING,
    names.STATE_RESULTS_READY,
)
PASS_ENDS = (names.STATE_HELD, names.STATE_PROMOTED)

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
LOAD_SUMMARY = re.compile(r"sent=(\d+) ok=(\d+) failed=(\d+)")  # load.py's last line
LOAD_FINISHED = (0, 1)  # load.py's exit statuses once it has sent its count
FAULT_STARTED = re.compile(r"experiment (\S+) started at (\S+)")  # inject_fault.sh's line
FAULT_EXITS = {
    0: "the experiment completed",
    1: "the experiment ended in a state other than completed",
    3: "the experiment was still running when the script stopped waiting",
}


class Refused(RuntimeError):
    """A check before anything started failed; nothing was started."""


class Failed(RuntimeError):
    """The run went off its path, a wait ran out, or a child ended early."""


class NotSendable(RuntimeError):
    """An action this script must never send: only start and status are sent."""


def emit(text: str = "", *, end: str = "\n") -> None:
    """print(), dropping what cannot be written: a closed terminal (EIO) or a pipe with no
    reader (EPIPE) must never stop the children's stop or the record's write."""
    with contextlib.suppress(OSError):
        print(text, end=end, flush=True)


def minutes(seconds: float) -> str:
    return f"{seconds / 60:g} min"


def elapsed(seconds: float) -> str:
    return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"


HUMAN_CLI = ("uv", "run", names.CLI_NAME)  # the console script, from the repository root


def human_command(action: str, run_id: str, *rest: str) -> str:
    """A marshal command line for the human to type. This script only prints it."""
    return " ".join((*HUMAN_CLI, action, run_id, *rest))


# ---------------------------------------------------------------------------
# The CLI: its own main(), in this process, start and status only
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reply:
    code: int  # marshal's exit status
    response: dict | None  # the response.json object, or None when the call failed
    stdout: str  # what marshal printed: the response, rendered
    stderr: str  # why it failed, when it did


class _Recorder:
    """The CLI's own InvokeAgentRuntime client (cli.marshal.new_agent_client, the shared
    setting), made on first use, keeping the reply's body for this script to read. It
    refuses any payload whose action is not start or status, before a request goes out."""

    def __init__(self) -> None:
        self.body: bytes | None = None

    def invoke_agent_runtime(self, **kwargs: Any) -> dict:
        action = json.loads(kwargs["payload"]).get("action")
        if action not in SENDABLE:
            raise NotSendable(f"e2e.py never sends {action!r}; that is the human's")
        reply = marshal.new_agent_client().invoke_agent_runtime(**kwargs)
        self.body = reply["response"].read()
        return {**reply, "response": io.BytesIO(self.body)}

    def response(self) -> dict | None:
        if self.body is None:
            return None
        try:
            value = json.loads(self.body)
        except ValueError:
            return None
        return value if marshal.is_response(value) else None


def call_marshal(action: str, *args: str) -> Reply:
    """One marshal command through cli.marshal.main(), its output captured. Only start and
    status: anything else is refused here, before main() runs."""
    if action not in SENDABLE:
        raise NotSendable(f"e2e.py never sends {action!r}; that is the human's")
    recorder = _Recorder()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = marshal.main([action, *args], agent=recorder)
    return Reply(code, recorder.response(), out.getvalue(), err.getvalue())


# ---------------------------------------------------------------------------
# AWS reads: the account, the route's URL, the run item
# ---------------------------------------------------------------------------
def aws_client(service: str, config: Config | None = None) -> Any:
    return boto3.session.Session().client(service, region_name=names.REGION, config=config)


def check_ask_url(url: str) -> str:
    """The route's URL, or ValueError. No message quotes it."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port, user = parts.hostname or "", parts.port, parts.username
    except ValueError:
        raise ValueError("not a URL") from None
    if parts.scheme != "https" or not EXECUTE_API_HOST.fullmatch(host) or port or user:
        raise ValueError(f"not an https URL on an API Gateway execute-api host in {names.REGION}")
    if parts.path != ROUTE_PATH or parts.query or parts.fragment:
        raise ValueError(f"not the route {names.API_ROUTE_KEY}: the path must be {ROUTE_PATH}")
    return url


def discover_ask_url(apigateway: Any) -> str:
    """rm-api's endpoint plus /ask, as the app module's ask_url output builds it, from a
    read of every API (GetApis, paged). Refused unless exactly one HTTP API has the name."""
    found = [
        api
        for page in apigateway.get_paginator("get_apis").paginate()
        for api in page.get("Items", [])
        if api.get("Name") == names.API_NAME
    ]
    if len(found) != 1:
        raise Refused(f"expected exactly one API named {names.API_NAME}, found {len(found)}")
    if found[0].get("ProtocolType") != "HTTP":
        raise Refused(f"{names.API_NAME} is not an HTTP API")
    try:
        return check_ask_url(str(found[0].get("ApiEndpoint", "")).rstrip("/") + ROUTE_PATH)
    except ValueError as error:
        raise Refused(f"{names.API_NAME}'s endpoint plus {ROUTE_PATH} is {error}") from None


def check_alarm(cloudwatch: Any) -> str:
    """The alarm's state, from one DescribeAlarms read. Refused unless exactly one metric
    alarm has the name, and while it is in ALARM: a run launched then would roll back on
    its first tick, before the fault."""
    alarms = cloudwatch.describe_alarms(
        AlarmNames=[names.ALARM_NAME], AlarmTypes=["MetricAlarm"]
    ).get("MetricAlarms", [])
    if len(alarms) != 1:
        raise Refused(
            f"expected one alarm named {names.ALARM_NAME}, found {len(alarms)}:"
            f" apply infra/ (task 4.1)"
        )
    state = alarms[0].get("StateValue")
    if state == names.ALARM_STATE_ALARM:
        raise Refused(
            f"the alarm {names.ALARM_NAME} is in ALARM: wait until it is OK again (about"
            f" {ALARM_OK_LAG} after the start of its last breaching minute), then start again"
        )
    return str(state)


def check_invoker(functions: Any) -> None:
    """Refused while rm-invoker runs with STUB=1 (invoker_stub true): it then only logs the
    alarm and every tick, so nothing would roll the run back. One GetFunctionConfiguration
    read; only the STUB variable is looked at, and the environment is never shown."""
    configuration = functions.get_function_configuration(FunctionName=names.INVOKER_FUNCTION)
    variables = (configuration.get("Environment") or {}).get("Variables") or {}
    if variables.get(names.INVOKER_STUB_ENV) == names.INVOKER_STUB_ON:
        raise Refused(
            f"{names.INVOKER_FUNCTION} runs with {names.INVOKER_STUB_ENV}={names.INVOKER_STUB_ON}:"
            f" set {names.INVOKER_STUB_VARIABLE} = false in {names.TFVARS_FILE} and apply"
            " infra/ (task 5.1)"
        )


def check_fault_template(fis: Any) -> str:
    """The template scripts/inject_fault.sh will start, found as it finds one: every page of
    ListExperimentTemplates, kept when tags.Name is rm-delay-cheaper. Refused unless exactly
    one, as the script refuses."""
    found = [
        template
        for page in fis.get_paginator("list_experiment_templates").paginate()
        for template in page.get("experimentTemplates", [])
        if (template.get("tags") or {}).get(FAULT_TEMPLATE_TAG) == names.FIS_TEMPLATE
    ]
    if len(found) != 1:
        raise Refused(
            f"{names.INJECT_FAULT_SCRIPT} needs exactly one FIS experiment template tagged"
            f" {FAULT_TEMPLATE_TAG}={names.FIS_TEMPLATE}, found {len(found)}:"
            " apply infra/ (task 4.3)"
        )
    return str(found[0].get("id"))


def check_load_lock() -> None:
    """Refused while another load holds rm-load.lock, since load.py would then refuse to
    start after the launch. It takes the lock as load.py does and lets it go at once."""
    try:
        with load_script.take_lock(load_script.lock_path()):
            pass
    except load_script.LoadRefused as error:
        raise Refused(f"load.py could not start: {error}") from None


def read_run_item(dynamodb: Any, run_id: str) -> dict | None:
    """The run item from rm-runs: one consistent GetItem, numbers as contracts.dynamo
    gives them. None when there is none."""
    reply = dynamodb.get_item(
        TableName=names.RUNS_TABLE,
        Key={names.PARTITION_KEY: {"S": names.run_pk(run_id)}},
        ConsistentRead=True,
    )
    attributes = reply.get("Item")
    if not attributes:
        return None
    deserialize = TypeDeserializer().deserialize
    return dynamo.from_dynamo({key: deserialize(value) for key, value in attributes.items()})


# ---------------------------------------------------------------------------
# Children: the load and the fault
# ---------------------------------------------------------------------------
def load_argv(run_id: str, users: int, questions: int, url: str) -> list[str]:
    return [
        sys.executable,
        str(ROOT / names.LOAD_SCRIPT),
        *("--run-id", run_id, "--users", str(users), "--questions", str(questions)),
        *("--url", url),
    ]


def fault_argv() -> list[str]:
    return ["bash", str(ROOT / names.INJECT_FAULT_SCRIPT)]


def spawn_child(argv: list[str], log: Path) -> subprocess.Popen:
    """A child in a session of its own, so a Ctrl-C at the terminal reaches this script
    only, which then stops the child's whole process group. Its output, stdout and stderr,
    goes to log; its environment is this shell's."""
    with open(log, "wb") as out:
        return subprocess.Popen(
            argv,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


@dataclass
class Child:
    name: str  # load or fault
    shown: str  # its command line, as printed: never the URL
    process: Any  # subprocess.Popen, or a test's fake
    log: Path
    started: float  # monotonic
    started_at: str  # UTC
    offset: int = 0
    partial: str = ""
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    signalled: list[str] = field(default_factory=list)


@contextlib.contextmanager
def shield_signals() -> Iterator[None]:
    """Further stop signals are ignored while the children stop, as load.py does in its
    drain, so a second Ctrl-C cannot cut the stop short and leave a load unwatched."""
    saved = {stop: signal.getsignal(stop) for stop in STOP_SIGNALS}
    for stop in STOP_SIGNALS:
        signal.signal(stop, signal.SIG_IGN)
    try:
        yield
    finally:
        for stop, handler in saved.items():
            if handler is not None:
                signal.signal(stop, handler)


class Clock:
    """Real time. Tests pass a fake whose sleep moves its time on."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def utc(self) -> str:
        return names.utc_iso()


@dataclass(frozen=True)
class Timeouts:
    """How long each wait lasts, in seconds."""

    # The human reads the plan and approves the launch.
    launch_approval: float = 30 * 60
    # A claimed approve finishing: the approve's own call waits up to 290 s, and an effect
    # that failed is taken up again by a tick (every 2 min) or marshal check once its claim
    # is older than 360 s.
    effects: float = 15 * 60
    # From the fault's start to ROLLED_BACK: the fault ramps within a minute and the alarm
    # needs two one-minute periods, so ALARM comes 3 to 5 minutes in (design.md section 9),
    # then the alarm rule or the next tick rolls the run back.
    rollback: float = 15 * 60
    # From ROLLED_BACK to AWAITING_RESUME: the 6-minute fault ends, the alarm goes back to
    # OK about ALARM_OK_LAG after the start of its last breaching minute, then a tick (every
    # 2 min) probes the cheaper model: about 10 to 16 minutes. 25 leaves room.
    resume_request: float = 25 * 60
    # The human approves the resume.
    resume_approval: float = 30 * 60
    # From the resume to RESULTS_READY: a tick every 2 min reads results, which refresh at
    # most once every 5 min and lag the events by about 2 min (design.md section 4), so a
    # few refreshes may pass before a read is current and ready.
    results: float = 45 * 60
    # The human ends the experiment in the Flagsmith dashboard and approves, or declines.
    decision: float = 60 * 60


@dataclass
class Deps:
    """What the script reaches outside itself. Tests replace any of it."""

    clock: Any = field(default_factory=Clock)
    cli: Callable[..., Reply] = call_marshal
    spawn: Callable[[list[str], Path], Any] = spawn_child
    killpg: Callable[[int, int], None] = os.killpg
    sts: Callable[[], Any] = lambda: aws_client("sts")
    cloudwatch: Callable[[], Any] = lambda: aws_client("cloudwatch")
    lambda_: Callable[[], Any] = lambda: aws_client("lambda")
    fis: Callable[[], Any] = lambda: aws_client("fis")  # ListExperimentTemplates only
    apigateway: Callable[[], Any] = lambda: aws_client("apigatewayv2")
    # The summary's read: short timeouts, so a stop is not held up by it.
    dynamodb: Callable[[], Any] = lambda: aws_client(
        "dynamodb",
        Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 3}),
    )
    check_lock: Callable[[], None] = check_load_lock
    shield: Callable[[], Any] = shield_signals
    load_argv: Callable[[str, int, int, str], list[str]] = load_argv
    fault_argv: Callable[[], list[str]] = fault_argv
    log_dir: Path | None = None  # None: a new temporary directory
    timeouts: Timeouts = field(default_factory=Timeouts)


# ---------------------------------------------------------------------------
# What the human types, printed at each pause
# ---------------------------------------------------------------------------
HUMAN_RULE = "These are the human's steps (CLAUDE.md): e2e.py and the agents take none of them."
HUMAN_WHERE = "Run it from the repository root, with the same AWS_PROFILE as this shell."


def launch_pause(run_id: str) -> list[str]:
    return [
        "PAUSE: review the plan above. To launch it, type in another terminal:",
        f"    {human_command(names.ACTION_APPROVE, run_id, names.AWAITS_LAUNCH)}",
        HUMAN_WHERE,
        f"e2e.py approves nothing itself; it reads the run's status every {POLL_SECONDS} s"
        " until the run moves.",
        HUMAN_RULE,
    ]


def resume_pause(run_id: str) -> list[str]:
    return [
        "PAUSE: the cheaper model is healthy again. To resume the experiment, type:",
        f"    {human_command(names.ACTION_APPROVE, run_id, names.AWAITS_RESUME)}",
        HUMAN_WHERE,
        HUMAN_RULE,
    ]


def decision_pause(run_id: str) -> list[str]:
    return [
        "PAUSE: results are ready. Choose one, in another terminal.",
        "  To hold the run, type:",
        f"    {human_command(names.ACTION_DECLINE, run_id)}",
        "    then end the experiment in the Flagsmith dashboard: a hold leaves it running.",
        "  Or, to promote it, end the experiment in the Flagsmith dashboard, then type:",
        f"    {human_command(names.ACTION_APPROVE, run_id, names.AWAITS_PROMOTION)}",
        HUMAN_WHERE,
        "  Note which metric the dashboard shows as primary, and the rollout decision you"
        " choose: evidence/5.1-e2e.md records both.",
        HUMAN_RULE,
    ]


def stop_line_lines(record: dict) -> list[str]:
    """CLAUDE.md's checks after every load (Gate 2's G6), printed with the summary."""
    sent = record["answers_sent"]
    this_run = f"this run sent {sent}" if sent is not None else "this run's count is unknown"
    return [
        "Stop lines (CLAUDE.md): read both now, before any further load.",
        "  cost:  uv run python evidence/3.6-scripts/smoke_reads.py cost"
        f" --answers-sent <all answers since the cutoff; {this_run}>"
        " --remaining-planned <answers still planned>",
        "  usage: uv run python evidence/3.6-scripts/smoke_reads.py usage",
        "         The total lags (more than 25 minutes at Gate 1, and it may wait for the day"
        " to close): also read usage-data/'s daily rows, or the Usage page in the dashboard.",
    ]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class E2E:
    def __init__(self, args: argparse.Namespace, deps: Deps) -> None:
        self.args, self.deps, self.clock = args, deps, deps.clock
        self.timeouts = deps.timeouts
        if args.pause_minutes is not None:  # it lengthens the human's pauses, never shortens one
            wait, default = args.pause_minutes * 60, deps.timeouts
            self.timeouts = replace(
                default,
                launch_approval=max(default.launch_approval, wait),
                resume_approval=max(default.resume_approval, wait),
                decision=max(default.decision, wait),
            )
        self.url: str | None = None  # never printed
        self.run_id: str | None = None
        self.stamps: dict[str, str | None] = dict.fromkeys(
            ("start_sent", "plan_returned", "approve_seen", "running")
        )
        self.seen: list[dict] = []  # every state change status showed
        self.last: dict | None = None  # the last good status response
        self.polled_at: str | None = None
        self.status_errors = 0
        self.load: Child | None = None
        self.fault: Child | None = None
        self.ended = False  # the run reached HELD or PROMOTED

    # -- output ------------------------------------------------------------
    def say(self, text: str, *, at: str | None = None) -> None:
        stamp = at or self.clock.utc()
        for line in text.splitlines() or [""]:
            emit(f"{stamp}  {line}")

    def pause(self, lines: list[str]) -> None:
        self.say(f"waiting for the human, run {self.run_id}")
        emit("\n" + "\n".join(lines) + "\n")

    # -- the whole thing ---------------------------------------------------
    def run(self) -> int:
        try:
            self.preflight()
        except Refused as error:
            self.say(f"refused: {error}; nothing was started")
            return EXIT_REFUSED
        except KeyboardInterrupt:
            self.say("interrupted before anything started")
            return EXIT_INTERRUPTED
        outcome, reason = "completed", None
        try:
            self.drive()
        except Failed as error:
            outcome, reason = "failed", str(error)
        except KeyboardInterrupt:
            if self.ended:  # the run is over; only the wait for the children is cut short
                self.say("stop signal: the run has ended, so only the children stop now")
            else:
                outcome, reason = "interrupted", "stopped by Ctrl-C or a stop signal"
        except Exception as error:  # a bug: the children still stop and the summary prints
            with contextlib.suppress(OSError):
                traceback.print_exc()
            outcome, reason = "failed", f"e2e.py itself failed: {type(error).__name__}: {error}"
        finally:
            # The shield first: nothing printed, and no second signal, may come between a
            # stop and the children's SIGTERM.
            with self.deps.shield():
                if reason is not None:
                    self.say(f"stopping: {reason}")
                self.stop_children()
        return self.finish(outcome, reason)

    def preflight(self) -> None:
        try:
            account = self.deps.sts().get_caller_identity()["Account"]
        except (BotoCoreError, ClientError) as error:
            raise Refused(
                f"could not read the caller identity ({type(error).__name__}: {error})"
            ) from None
        if account != names.ACCOUNT_ID:
            raise Refused(f"the credentials are for account {account}, not {names.ACCOUNT_ID}")
        self.say(f"account {account}: ok")
        state = self.read(f"the alarm {names.ALARM_NAME}", check_alarm, self.deps.cloudwatch)
        self.say(f"alarm {names.ALARM_NAME}: {state}")
        self.read(names.INVOKER_FUNCTION, check_invoker, self.deps.lambda_)
        self.say(
            f"{names.INVOKER_FUNCTION}: {names.INVOKER_STUB_ENV} is not {names.INVOKER_STUB_ON}"
        )
        template = self.read("the FIS experiment templates", check_fault_template, self.deps.fis)
        self.say(f"fault template: {template}, tagged {FAULT_TEMPLATE_TAG}={names.FIS_TEMPLATE}")
        if self.args.url is not None:
            self.url = self.args.url
            self.say("route: --url as given (never shown)")
        else:
            try:
                self.url = discover_ask_url(self.deps.apigateway())
            except (BotoCoreError, ClientError) as error:
                raise Refused(
                    f"could not read the APIs ({type(error).__name__}: {error})"
                ) from None
            self.say(f"route: {names.API_NAME}'s endpoint plus {ROUTE_PATH} (never shown)")
        self.deps.check_lock()
        self.say(f"{names.LOAD_LOCK_NAME}: no other load holds it")
        if self.args.record is not None:
            self.say(f"record: {self.args.record}")

    @staticmethod
    def read(what: str, check: Callable[[Any], Any], client: Callable[[], Any]) -> Any:
        """One preflight read: a failed call refuses, naming what could not be read."""
        try:
            return check(client())
        except (BotoCoreError, ClientError) as error:
            raise Refused(f"could not read {what} ({type(error).__name__}: {error})") from None

    def drive(self) -> None:
        t = self.timeouts
        self.start()
        rid = self.run_id

        # The launch: the human approves it.
        self.pause(launch_pause(rid))
        self.approved(
            waiting_in=names.STATE_PLANNED,
            targets=(names.STATE_RUNNING,),
            goal="the launch approval",
            timeout=t.launch_approval,
            hint=f"The run stays {names.STATE_PLANNED}, and e2e.py will not start the load:"
            f" `{human_command(names.ACTION_DECLINE, rid)}` drops it before a new start.",
            stamp="approve_seen",
        )
        claimed = "claimed" if self.last["state"] == names.STATE_PLANNED else "committed"
        self.say(f"approve seen: the launch is {claimed}", at=self.stamps["approve_seen"])
        self.finished(
            waiting_in=names.STATE_PLANNED,
            targets=(names.STATE_RUNNING,),
            goal=names.STATE_RUNNING,
            stamp="running",
        )
        self.say(f"RUNNING: run {rid} is running", at=self.stamps["running"])

        # Load and fault together.
        self.start_children()

        self.wait(
            goal=names.STATE_ROLLED_BACK,
            done=lambda s: s["state"] in (names.STATE_ROLLED_BACK, names.STATE_AWAITING_RESUME),
            allowed=(names.STATE_RUNNING,),
            timeout=t.rollback,
            hint=f"The alarm {names.ALARM_NAME} should reach ALARM 3 to 5 minutes into the"
            f" fault (design.md section 9), and {names.INVOKER_FUNCTION} hands it, and every"
            f" tick, to the agent, unless {names.INVOKER_STUB_VARIABLE} is true"
            f" ({names.INVOKER_STUB_ENV}={names.INVOKER_STUB_ON}: it only logs them). Look at the"
            f" fault's output above, the alarm, {names.INVOKER_FUNCTION}'s log, and"
            f" `{human_command(names.ACTION_STATUS, rid)}`.",
        )
        self.wait(
            goal=names.STATE_AWAITING_RESUME,
            done=lambda s: s["state"] == names.STATE_AWAITING_RESUME,
            allowed=(names.STATE_ROLLED_BACK,),
            timeout=t.resume_request,
            hint=f"The resume request needs the {names.FIS_DURATION_MINUTES}-minute fault over,"
            f" the alarm back to OK (about {ALARM_OK_LAG} after the start of its last breaching"
            f" minute; evidence/4.3-fis.md), then a tick (every"
            f" {names.TICK_INTERVAL_MINUTES} min) whose probe passes."
            f" `{human_command(names.ACTION_STATUS, rid)}` says why not yet.",
        )

        # The resume: the human approves it.
        self.pause(resume_pause(rid))
        resumed = (names.STATE_RUNNING, names.STATE_RESULTS_READY)
        self.approved(
            waiting_in=names.STATE_AWAITING_RESUME,
            targets=resumed,
            goal="the resume approval",
            timeout=t.resume_approval,
            hint=f"The run stays {names.STATE_AWAITING_RESUME}:"
            f" `{human_command(names.ACTION_APPROVE, rid, names.AWAITS_RESUME)}` resumes it,"
            f" `{human_command(names.ACTION_DECLINE, rid)}` holds it.",
        )
        claimed = "claimed" if self.last["state"] == names.STATE_AWAITING_RESUME else "committed"
        self.say(f"resume seen: the resume is {claimed}")
        self.finished(
            waiting_in=names.STATE_AWAITING_RESUME, targets=resumed, goal=names.STATE_RUNNING
        )
        self.say(f"RUNNING again: run {rid} is resumed")

        self.wait(
            goal=names.STATE_RESULTS_READY,
            done=lambda s: s["state"] == names.STATE_RESULTS_READY,
            allowed=(names.STATE_RUNNING,),
            timeout=t.results,
            hint=f"A tick every {names.TICK_INTERVAL_MINUTES} min reads results, which refresh"
            f" at most once every {names.RESULTS_REFRESH_MIN_SECONDS // 60} min and lag the"
            f" events; `{human_command(names.ACTION_STATUS, rid)}` shows the run, and"
            f" `{human_command(names.ACTION_CHECK, rid)}` does what the next tick would.",
        )
        self.say(f"RESULTS_READY: {self.last['message']}")

        # The decision: the human holds, or ends the experiment and promotes.
        self.pause(decision_pause(rid))
        self.approved(
            waiting_in=names.STATE_RESULTS_READY,
            targets=PASS_ENDS,
            goal="the decision",
            timeout=t.decision,
            hint=f"The run stays {names.STATE_RESULTS_READY}:"
            f" `{human_command(names.ACTION_DECLINE, rid)}` holds it; or end the experiment"
            f" in the Flagsmith dashboard, then"
            f" `{human_command(names.ACTION_APPROVE, rid, names.AWAITS_PROMOTION)}`.",
        )
        self.finished(
            waiting_in=names.STATE_RESULTS_READY, targets=PASS_ENDS, goal="HELD or PROMOTED"
        )
        self.ended = True
        self.say(f"{self.last['state']}: {self.last['message']}")
        self.wait_for_children()

    # -- start ------------------------------------------------------------
    def start(self) -> None:
        prompt = self.args.prompt
        self.stamps["start_sent"] = self.clock.utc()
        self.say(f"start sent: marshal start {json.dumps(prompt)}", at=self.stamps["start_sent"])
        reply = self.deps.cli(names.ACTION_START, prompt)
        self.stamps["plan_returned"] = self.polled_at = self.clock.utc()
        response = reply.response
        if response is None:
            raise Failed(f"marshal start failed: {reply.stderr.strip() or reply.code}")
        emit(reply.stdout, end="" if reply.stdout.endswith("\n") else "\n")
        if response["error"] is not None:
            error = response["error"]
            raise Failed(f"marshal start returned {error['code']}: {error['message']}")
        if response["state"] != names.STATE_PLANNED or response["run_id"] is None:
            raise Failed(f"marshal start returned state {response['state']}, not PLANNED")
        self.run_id = response["run_id"]
        self.note(response)
        self.say(f"plan returned: run {self.run_id} is PLANNED", at=self.stamps["plan_returned"])

    # -- polling ----------------------------------------------------------
    def poll(self) -> dict | None:
        """One marshal status call. A good response is returned; a failure is printed and
        counted, and the STATUS_ERRORS_FATAL-th in a row stops the script."""
        reply = self.deps.cli(names.ACTION_STATUS, self.run_id)
        self.polled_at = self.clock.utc()
        response = reply.response
        if response is None:
            problem = (reply.stderr.strip() or f"marshal exited {reply.code}").splitlines()[0]
        elif response["error"] is not None:
            problem = f"{response['error']['code']}: {response['error']['message']}"
        elif response["run_id"] != self.run_id or response["state"] is None:
            problem = f"status named run {response['run_id']}, state {response['state']}"
        else:
            self.status_errors = 0
            self.note(response)
            return response
        self.status_errors += 1
        self.say(
            f"status error {self.status_errors} of {STATUS_ERRORS_FATAL} in a row: {problem}",
            at=self.polled_at,
        )
        if self.status_errors >= STATUS_ERRORS_FATAL:
            raise Failed(
                f"`{human_command(names.ACTION_STATUS, self.run_id)}` failed"
                f" {STATUS_ERRORS_FATAL} times in a row; the last: {problem}"
            )
        return None

    def note(self, response: dict) -> None:
        state, pending = response["state"], response["pending"]
        if self.last is None or (self.last["state"], self.last["pending"]) != (state, pending):
            self.seen.append({"at": self.polled_at, "state": state, "pending": pending})
            shown = state if pending is None else f"{state}, {state} -> {pending} pending"
            self.say(f"status: {shown}", at=self.polled_at)
        self.last = response

    def wait(
        self,
        *,
        goal: str,
        done: Callable[[dict], bool],
        allowed: tuple[str, ...],
        timeout: float,
        hint: str,
    ) -> dict:
        """Poll status every POLL_SECONDS until done(response). A state outside allowed
        stops the script, and so does the timeout, with hint."""
        start = self.clock.monotonic()
        deadline, beat = start + timeout, start
        while True:
            began = self.clock.monotonic()
            response = self.poll()
            self.watch_children()
            if response is not None:
                if done(response):
                    return response
                if response["state"] not in allowed:
                    raise Failed(
                        f"run {self.run_id} is {response['state']} while e2e.py waited for"
                        f" {goal}: {response['message']}"
                    )
            now = self.clock.monotonic()
            if now >= deadline:
                raise Failed(f"timed out after {minutes(timeout)} waiting for {goal}. {hint}")
            if now - beat >= HEARTBEAT_SECONDS:
                state = self.last["state"] if self.last else "unknown"
                self.say(
                    f"still {state}; waiting for {goal}, {elapsed(now - start)} of"
                    f" {minutes(timeout)}"
                )
                beat = now
            self.clock.sleep(max(0.0, min(began + POLL_SECONDS, deadline) - now))

    def approved(
        self,
        *,
        waiting_in: str,
        targets: tuple[str, ...],
        goal: str,
        timeout: float,
        hint: str,
        stamp: str | None = None,
    ) -> None:
        """Wait for the human's command to take effect: its transition claimed (pending)
        or already committed."""
        self.wait(
            goal=goal,
            done=lambda s: s["pending"] in targets or s["state"] in targets,
            allowed=(waiting_in,),
            timeout=timeout,
            hint=hint,
        )
        if stamp:
            self.stamps[stamp] = self.polled_at

    def finished(
        self, *, waiting_in: str, targets: tuple[str, ...], goal: str, stamp: str | None = None
    ) -> None:
        """Wait for a claimed transition to commit. No call when the last status showed it."""
        rid = self.run_id
        if self.last is None or self.last["state"] not in targets:
            self.wait(
                goal=goal,
                done=lambda s: s["state"] in targets,
                allowed=(waiting_in,),
                timeout=self.timeouts.effects,
                hint=f"`{human_command(names.ACTION_STATUS, rid)}` shows what is pending, and"
                f" `{human_command(names.ACTION_CHECK, rid)}` finishes a stalled transition once"
                f" its claim is older than {names.PENDING_STALE_SECONDS} s.",
            )
        if stamp:
            self.stamps[stamp] = self.polled_at

    # -- children ---------------------------------------------------------
    def start_children(self) -> None:
        log_dir = self.deps.log_dir or Path(tempfile.mkdtemp(prefix="rm-e2e-"))
        self.say(f"child output is also kept in {log_dir}")
        users, questions = self.args.users, self.args.questions
        self.load = self.spawn(
            "load",
            self.deps.load_argv(self.run_id, users, questions, self.url),
            f"{names.LOAD_SCRIPT} --run-id {self.run_id} --users {users}"
            f" --questions {questions} --url {NOT_SHOWN}",
            log_dir / "load.log",
        )
        self.fault = self.spawn(
            "fault", self.deps.fault_argv(), names.INJECT_FAULT_SCRIPT, log_dir / "fault.log"
        )

    def spawn(self, name: str, argv: list[str], shown: str, log: Path) -> Child:
        try:
            process = self.deps.spawn(argv, log)
        except OSError as error:
            raise Failed(f"could not start {shown}: {error.strerror or error}") from None
        child = Child(name, shown, process, log, self.clock.monotonic(), self.clock.utc())
        self.say(f"started {shown} (pid {process.pid})", at=child.started_at)
        return child

    def children(self) -> list[Child]:
        return [child for child in (self.load, self.fault) if child is not None]

    def pump(self, child: Child, *, last: bool = False) -> None:
        """Show what the child wrote since the last look, line by line."""
        try:
            with open(child.log, "rb") as log:
                log.seek(child.offset)
                data = log.read()
        except OSError:
            data = b""
        child.offset += len(data)
        *lines, child.partial = (child.partial + data.decode(errors="replace")).split("\n")
        if last and child.partial:
            lines, child.partial = [*lines, child.partial], ""
        for line in lines:
            child.lines.append(line)
            self.say(f"[{child.name}] {line}")

    def reap(self, child: Child) -> bool:
        """True when the child has just been seen to end."""
        if child.exit_code is not None:
            return False
        code = child.process.poll()
        if code is None:
            return False
        child.exit_code = code
        self.pump(child, last=True)
        return True

    def watch_children(self) -> None:
        """Show their output; stop the script when one ends early."""
        for child in self.children():
            self.pump(child)
        load, fault = self.load, self.fault
        if load is not None and self.reap(load):
            counts = load_counts(load)
            self.say(f"load.py ended, exit {load.exit_code}: {counts_text(counts)}")
            if load.exit_code not in LOAD_FINISHED or counts is None:
                raise Failed(f"load.py ended early, exit {load.exit_code}; see its output above")
            if counts["ok"] == 0:
                raise Failed(f"none of load.py's {counts['sent']} requests succeeded")
        if fault is not None and self.reap(fault):
            if fault_experiment(fault) is None:
                raise Failed(
                    f"inject_fault.sh ended, exit {fault.exit_code}, before its experiment"
                    " started: no fault was injected"
                )
            meaning = FAULT_EXITS.get(fault.exit_code, "see its output above")
            self.say(f"inject_fault.sh ended, exit {fault.exit_code}: {meaning}")

    def wait_for_children(self) -> None:
        """At the end: the load stops at its count, the fault script when its experiment
        ends. Each gets until its own worst case, then the stop below."""
        load_worst = (
            math.ceil(self.args.users * self.args.questions / names.LOAD_MAX_CONCURRENCY)
            * names.API_GATEWAY_TIMEOUT_SECONDS
            + CHILD_STOP_GRACE_SECONDS
        )
        for child, worst in ((self.load, load_worst), (self.fault, FAULT_SCRIPT_MAX_SECONDS)):
            if child is None or child.exit_code is not None:
                continue
            if child.process.poll() is None:
                self.say(
                    f"waiting for {child.shown} to end by itself; Ctrl-C stops it now and the"
                    " summary still prints"
                )
            while child.process.poll() is None:
                if self.clock.monotonic() >= child.started + worst:
                    self.say(f"{child.shown} is still running after {minutes(worst)}")
                    break
                self.pump(child)
                self.clock.sleep(POLL_SECONDS)
            self.watch_children()

    def stop_children(self) -> None:
        """SIGTERM to each running child's process group, then wait for it. load.py sends
        nothing more and finishes the requests in flight; inject_fault.sh stops waiting,
        and its experiment runs on to its end. Past the grace, SIGKILL."""
        live = [c for c in self.children() if c.exit_code is None and c.process.poll() is None]
        for child in live:
            self.say(f"stopping {child.shown} (pid {child.process.pid}) with SIGTERM")
            self.send_signal(child, signal.SIGTERM)
        deadline = self.clock.monotonic() + CHILD_STOP_GRACE_SECONDS
        for child in live:
            while child.process.poll() is None and self.clock.monotonic() < deadline:
                self.pump(child)
                self.clock.sleep(CHILD_POLL_SECONDS)
            if child.process.poll() is None:
                self.say(
                    f"{child.shown} did not stop within {CHILD_STOP_GRACE_SECONDS} s;"
                    " sending SIGKILL"
                )
                self.send_signal(child, signal.SIGKILL)
                try:
                    child.process.wait(timeout=CHILD_KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    self.say(
                        f"{child.shown} (pid {child.process.pid}) is still running; stop it by hand"
                    )
        for child in self.children():
            self.reap(child)
        if self.fault is not None and self.fault in live:
            self.say(
                "the FIS experiment, if it started, runs on to its"
                f" {names.FIS_DURATION_MINUTES}-minute end; e2e.py never stops it"
            )

    def send_signal(self, child: Child, signum: int) -> None:
        try:
            self.deps.killpg(child.process.pid, signum)
            child.signalled.append(signal.Signals(signum).name)
        except ProcessLookupError:
            pass  # it ended in between
        except OSError as error:
            self.say(f"could not signal {child.shown}: {error}")

    # -- the end ------------------------------------------------------------
    def finish(self, outcome: str, reason: str | None) -> int:
        item, item_error = None, None
        if self.run_id is not None:
            try:
                item = read_run_item(self.deps.dynamodb(), self.run_id)
                if item is None:
                    item_error = f"no item {names.run_pk(self.run_id)} in {names.RUNS_TABLE}"
            except (BotoCoreError, ClientError) as error:
                item_error = f"{type(error).__name__}: {error}"
            except KeyboardInterrupt:
                item_error = "the read was interrupted"
        record = build_record(self, outcome, reason, item, item_error)
        emit("\n".join(summary_lines(record)))
        self.write_record(record)
        if reason is not None:
            self.say(f"stopped: {reason}")
        if self.last is not None and self.last["state"] not in names.TERMINAL_STATES:
            rid = self.run_id
            self.say(
                f"run {rid} is still {self.last['state']}:"
                f" `{human_command(names.ACTION_STATUS, rid)}` shows it,"
                f" `{human_command(names.ACTION_DECLINE, rid)}` ends it."
            )
        if outcome == "interrupted":
            return EXIT_INTERRUPTED
        if outcome == "completed" and record["history_matches_pass"]:
            return EXIT_OK
        if outcome == "completed" and item_error is not None:
            self.say(f"could not read the run item ({item_error}); check its history by hand")
        elif outcome == "completed":
            self.say("the run item's history does not read as task 5.1's Pass; see above")
        return EXIT_FAILED

    def write_record(self, record: dict) -> None:
        path = self.args.record
        if path is None:
            return
        text = json.dumps(record, indent=2, default=_jsonable) + "\n"
        try:
            with open(path, "x") as out:  # never over an earlier record
                out.write(text)
        except OSError as error:
            self.say(f"could not write {path} ({error.strerror or error}); the record follows")
            emit(text)
            return
        self.say(f"record written to {path}")


def _jsonable(value: Any) -> Any:
    return sorted(value) if isinstance(value, set | frozenset) else str(value)


def load_counts(child: Child | None) -> dict[str, int] | None:
    """load.py's last line, sent=<n> ok=<n> failed=<n>, as numbers."""
    for line in reversed(child.lines if child else []):
        match = LOAD_SUMMARY.fullmatch(line.strip())
        if match:
            return dict(zip(("sent", "ok", "failed"), map(int, match.groups()), strict=True))
    return None


def counts_text(counts: dict[str, int] | None) -> str:
    if counts is None:
        return "no sent=/ok=/failed= line"
    return f"sent={counts['sent']} ok={counts['ok']} failed={counts['failed']}"


def fault_experiment(child: Child | None) -> str | None:
    """The FIS experiment ID inject_fault.sh printed when it started one."""
    for line in child.lines if child else []:
        match = FAULT_STARTED.search(line)
        if match:
            return match.group(1)
    return None


def build_record(
    e2e: E2E, outcome: str, reason: str | None, item: dict | None, item_error: str | None
) -> dict:
    """What evidence/5.1-e2e.md needs, and what --record holds. Never the URL."""
    history = item.get("history") if item else None
    states = [entry.get("to") for entry in history] if history else None
    load, fault = e2e.load, e2e.fault
    counts = load_counts(load)
    report_key = item.get("report_key") if item else None
    return {
        "run_id": e2e.run_id,
        "outcome": outcome,
        "reason": reason,
        "last_status": e2e.last["state"] if e2e.last else None,
        "prompt": e2e.args.prompt,
        "load_size": {"users": e2e.args.users, "questions": e2e.args.questions},
        "timestamps": dict(e2e.stamps),
        "seen": e2e.seen,
        "history": history,
        "history_states": states,
        "history_matches_pass": bool(
            states and tuple(states[:-1]) == PASS_PATH and states[-1] in PASS_ENDS
        ),
        "answers_sent": counts["sent"] if counts else None,
        "load": None
        if load is None
        else {
            "command": load.shown,
            "started_at": load.started_at,
            "exit_code": load.exit_code,
            "signalled": load.signalled,
            **(counts or {}),
            "output": load.lines,
            "log": str(load.log),
        },
        "fault": None
        if fault is None
        else {
            "command": fault.shown,
            "started_at": fault.started_at,
            "exit_code": fault.exit_code,
            "signalled": fault.signalled,
            "experiment_id": fault_experiment(fault),
            "output": fault.lines,
            "log": str(fault.log),
        },
        "state": item.get("state") if item else None,
        "incidents": item.get("incidents") if item else None,
        "report_key": report_key,
        "report": f"s3://{names.REPORTS_BUCKET}/{report_key}" if report_key else None,
        "notified": item.get("notified") if item else None,
        "run_item_error": item_error,
    }


def summary_lines(record: dict) -> list[str]:
    rid = record["run_id"]
    stamps = record["timestamps"]
    lines = [
        "",
        "==== Summary for evidence/5.1-e2e.md ====",
        f"run:      {rid or 'none'}",
        f"outcome:  {record['outcome']}" + (f" ({record['reason']})" if record["reason"] else ""),
        "timestamps (UTC):",
        f"  start sent:     {stamps['start_sent'] or '-'}",
        f"  plan returned:  {stamps['plan_returned'] or '-'}",
        f"  approve seen:   {stamps['approve_seen'] or '-'}",
        f"  RUNNING:        {stamps['running'] or '-'}",
    ]
    if record["history"] is not None:
        lines.append(f"history ({names.RUNS_TABLE}, {names.run_pk(rid)}):")
        for n, entry in enumerate(record["history"], 1):
            lines.append(
                f"  {n}. {entry.get('at')}  {entry.get('from') or '(none)'} -> {entry.get('to')}"
                f"  {entry.get('actor')}: {entry.get('reason')}"
            )
        verdict = "yes" if record["history_matches_pass"] else "NO"
        lines.append(
            f"  reads as 5.1's Pass ({', '.join(PASS_PATH)}, then HELD or PROMOTED): {verdict}"
        )
    elif rid is not None:
        lines.append(f"history: not read ({record['run_item_error']})")
    load = record["load"]
    if load is None:
        lines.append("answers sent: 0 (load.py did not start)")
    elif record["answers_sent"] is None:
        lines.append(
            f"answers sent: unknown, load.py printed no counts (exit {load['exit_code']});"
            f" count all {record['load_size']['users'] * record['load_size']['questions']}"
        )
    else:
        lines.append(
            f"answers sent: {record['answers_sent']}"
            f" (load.py: sent={load['sent']} ok={load['ok']} failed={load['failed']},"
            f" exit {load['exit_code']})"
        )
    fault = record["fault"]
    if fault is not None:
        lines.append(
            f"fault: inject_fault.sh exit {fault['exit_code']},"
            f" FIS experiment {fault['experiment_id'] or 'not started'}"
        )
    lines.append(f"report: {record['report'] or 'none'}")
    notified = record["notified"]
    lines.append("SNS message IDs (notified):" if notified else "SNS message IDs: none")
    for key, message_id in sorted((notified or {}).items(), key=lambda kv: _key_order(kv[0])):
        lines.append(f"  {key}: {message_id}")
    lines.extend(stop_line_lines(record))
    lines.append("")
    return lines


def _key_order(key: str) -> tuple[int, str]:
    count, _, state = key.partition("#")
    return (int(count) if count.isdigit() else 0, state)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
# Anything that looks like a URL or an AWS host, wherever argparse might quote it.
_URLISH = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\",;]+|[^\s'\",;]*amazonaws\.com[^\s'\",;]*")


class _QuietParser(argparse.ArgumentParser):
    """An ArgumentParser whose errors never print a URL."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {_URLISH.sub(NOT_SHOWN, message)}\n")


def _count(low: int, high: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("not a whole number") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be {low} to {high}")
        return number

    return parse


def _prompt(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("the prompt is empty")
    return value


def _url(value: str) -> str:
    try:
        return check_ask_url(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = _QuietParser(
        prog="e2e.py",
        description="Drive one Rollout Marshal run end to end; the human approves every step.",
        allow_abbrev=False,  # argparse quotes an ambiguous abbreviation, value and all
    )
    parser.add_argument(
        "--users",
        type=_count(1, names.IDENTITY_COUNT),
        default=names.LOAD_FULL.users,
        help=f"load.py's --users (default {names.LOAD_FULL.users})",
    )
    parser.add_argument(
        "--questions",
        type=_count(1, names.QUESTIONS_COUNT),
        default=names.LOAD_FULL.questions,
        help=f"load.py's --questions (default {names.LOAD_FULL.questions})",
    )
    parser.add_argument(
        "--prompt", type=_prompt, default=DEFAULT_PROMPT, help="what marshal start sends"
    )
    parser.add_argument(
        "--url",
        type=_url,
        help=f"the route's full URL, as the app module's {names.APP_ASK_URL_OUTPUT} output"
        f" gives it; by default {names.API_NAME}'s endpoint plus {ROUTE_PATH}. Never printed",
    )
    parser.add_argument("--record", type=Path, help="a new JSON file for the summary")
    parser.add_argument(
        "--pause-minutes",
        type=_count(1, PAUSE_MINUTES_MAX),
        default=None,
        help="lengthens the human's three pauses: each waits the longer of its default and"
        f" this many minutes (defaults: the launch {Timeouts.launch_approval / 60:g}, the"
        f" resume {Timeouts.resume_approval / 60:g}, the decision {Timeouts.decision / 60:g})",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args, unrecognized = parser.parse_known_args(argv)
    if unrecognized:
        parser.error(f"{len(unrecognized)} unrecognized argument(s), not shown")
    if args.record is not None:
        if args.record.exists():
            parser.error(f"--record: {args.record} exists; give a new file")
        if not args.record.parent.is_dir():
            parser.error(f"--record: {args.record.parent} is not a directory")
    return args


def run(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    """Parse and drive one run, with no signal handlers installed (tests call this)."""
    return E2E(parse_args(argv), deps or Deps()).run()


def _stop(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    for stop in (signal.SIGTERM, signal.SIGHUP):  # each stops the script as Ctrl-C does
        signal.signal(stop, _stop)
    code = E2E(args, Deps()).run()
    release_dead_stdout()
    return code


def release_dead_stdout() -> None:
    """When stdout is gone (EIO, EPIPE), output that emit() dropped still sits in its buffer,
    and Python's flush at exit would fail and exit 120. Point stdout at /dev/null instead, as
    the signal module's note on SIGPIPE does, so the exit status stays this script's."""
    try:
        sys.stdout.flush()
    except OSError:
        with contextlib.suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)


if __name__ == "__main__":
    sys.exit(main())
