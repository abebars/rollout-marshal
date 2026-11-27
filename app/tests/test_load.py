"""Tests for scripts/load.py and app/questions.json (task 3.4, design.md section 11).

Every request goes to FakeApi, a local HTTP server on 127.0.0.1: nothing here reaches AWS,
Flagsmith or a deployed API. The tests that start the script as a process run a copy of it
inside a temporary git repository with a second worktree. They take that repository's
rm-load.lock, never this one's, which a real load or a test run in another worktree may
hold.

Every request is signed with SigV4 for execute-api (contract change c1). The credentials
are dummies. For this process and the script's, the AWS environment is replaced: test
keys, config and credentials files at /dev/null, instance metadata off. So no test can
find or use real credentials. FakeApi keeps each request as it arrived, and the tests
recompute its signature from those bytes by the algorithm AWS documents.

Identities are <run_id>-user-001 to <run_id>-user-150, so every run's are new to Flagsmith
(the lead's Gate 1 decision, contract change c1). Until that change widens the identity
pattern in contracts/schemas/answer_request.json, which still allows only user-001 to
user-150, a body is checked field by field: its question against the schema's question
rule, its identity against the run-identity spelling.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import inspect
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

import pytest
from botocore.credentials import Credentials
from jsonschema import Draft202012Validator

from contracts import load_schema, names

ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "127.0.0.1"
# How long a test waits for something that should happen at once. Only a failing test
# waits this long.
WAIT_SECONDS = 30
# A refused start exits as soon as Python and git have run; one that is still going after
# this took a lock of its own.
REFUSAL_SECONDS = 10
# Scratch run IDs (design.md section 4's pattern), only ever sent to FakeApi.
RUN_ID = "rloadtest1200"
OTHER_RUN_ID = "rloadtest1201"

# Dummy credentials. Key and secret differ, so a signature made with the wrong one fails.
ACCESS_KEY = "testing"
SECRET_KEY = "testing-secret"
SESSION_TOKEN = "testing-session-token"
TEST_CREDENTIALS = Credentials(ACCESS_KEY, SECRET_KEY)
DUMMY_KEYS = {"AWS_ACCESS_KEY_ID": ACCESS_KEY, "AWS_SECRET_ACCESS_KEY": SECRET_KEY}
# Every other source in botocore's credential chain, pointed at nothing.
NO_AWS_SOURCES = {
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
    "BOTO_CONFIG": os.devnull,
}


def _import_load():
    spec = importlib.util.spec_from_file_location("rm_load_script", ROOT / names.LOAD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while a class is built
    spec.loader.exec_module(module)
    return module


load = _import_load()


def _run_identity_spelling(run_id: str) -> re.Pattern:
    """^<run_id>-user-NNN$, the spelling contract change c1 gives a run's identities."""
    return re.compile(rf"^{re.escape(run_id)}-user-\d{{3}}$")


def _expected_identities(run_id: str, users: int) -> list[str]:
    """Users 1 to users of this run, in order."""
    return [f"{run_id}-{names.identity(n)}" for n in range(1, users + 1)]


QUESTION_RULE = Draft202012Validator(
    load_schema("answer_request")["properties"][names.ANSWER_REQUEST_FIELDS[0]]
)


def _check_body(body: dict, run_id: str) -> None:
    """The answer request's two fields; a question its schema accepts; a run identity."""
    assert set(body) == set(names.ANSWER_REQUEST_FIELDS)
    QUESTION_RULE.validate(body[load.QUESTION_FIELD])
    assert _run_identity_spelling(run_id).fullmatch(body[load.IDENTITY_FIELD]), body


def _is_aws_variable(name: str) -> bool:
    return name.startswith("AWS_") or name == "BOTO_CONFIG"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No proxy for 127.0.0.1, and only the dummy AWS credentials anywhere."""
    monkeypatch.setenv("NO_PROXY", LOOPBACK)
    monkeypatch.setenv("no_proxy", LOOPBACK)
    for name in [name for name in os.environ if _is_aws_variable(name)]:
        monkeypatch.delenv(name)
    for name, value in {**NO_AWS_SOURCES, **DUMMY_KEYS}.items():
        monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# The fake assistant API
# ---------------------------------------------------------------------------
class Received(NamedTuple):
    method: str
    path: str
    headers: dict[str, str]  # names in lower case
    raw: bytes  # the body exactly as it arrived
    body: dict


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64  # ten connects at once must not overflow the listen backlog


class FakeApi:
    """Stands in for the assistant API: answers every POST and keeps what it was sent.

    status_for picks each reply's status from the request body. With overlap set, a request
    is held until that many are in flight (or HOLD_SECONDS pass), then LINGER_SECONDS more,
    so a load that had more requests out at once would be caught doing it. With gate set,
    every reply waits until the test sets the gate. in_flight drops before the reply is
    written, so the server never counts a request the client has already finished.
    """

    HOLD_SECONDS = 1.0
    LINGER_SECONDS = 0.05

    def __init__(self) -> None:
        self.received: list[Received] = []
        self.status_for: Callable[[dict], int] = lambda body: 200
        self.overlap: int | None = None
        self.gate: threading.Event | None = None
        self.in_flight = 0
        self.max_in_flight = 0
        self._cond = threading.Condition()
        self._server = _Server((LOOPBACK, 0), self._handler_class())
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def url(self) -> str:
        """A URL with a route of its own, to show load.py posts to exactly what it is given."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/route/under/test?stage=fake"

    def start(self) -> FakeApi:
        self._thread.start()
        return self

    def stop(self) -> None:
        if self.gate is not None:
            self.gate.set()
        self._server.shutdown()
        self._server.server_close()

    def wait_for(self, count: int) -> bool:
        """True once at least count requests have arrived."""
        with self._cond:
            return self._cond.wait_for(lambda: len(self.received) >= count, WAIT_SECONDS)

    def _handler_class(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep test output quiet
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                headers = {name.lower(): value for name, value in self.headers.items()}
                received = Received(self.command, self.path, headers, raw, json.loads(raw))
                with fake._cond:
                    fake.received.append(received)
                    fake.in_flight += 1
                    fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
                    fake._cond.notify_all()
                    if fake.overlap is not None:
                        fake._cond.wait_for(
                            lambda: fake.in_flight >= fake.overlap, fake.HOLD_SECONDS
                        )
                try:
                    if fake.overlap is not None:
                        time.sleep(fake.LINGER_SECONDS)
                    if fake.gate is not None:
                        fake.gate.wait(WAIT_SECONDS)
                finally:
                    with fake._cond:
                        fake.in_flight -= 1
                        fake._cond.notify_all()
                status = fake.status_for(received.body)
                data = json.dumps({"status": status}).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client is gone: a killed load, or one that timed out

        return Handler


@pytest.fixture
def fake_api() -> Iterator[FakeApi]:
    fake = FakeApi().start()
    yield fake
    fake.stop()


def _run(url: str, bodies: list[dict], **kwargs):
    """run_load in this process, with the dummy credentials unless a test passes its own."""
    kwargs.setdefault("credentials", TEST_CREDENTIALS)
    return load.run_load(url, bodies, **kwargs)


# ---------------------------------------------------------------------------
# SigV4, recomputed from the request as the server received it
# ---------------------------------------------------------------------------
def _authorization(received: Received) -> dict[str, str]:
    """Credential, SignedHeaders and Signature from an AWS4-HMAC-SHA256 Authorization header."""
    algorithm, _, fields = received.headers["authorization"].partition(" ")
    assert algorithm == "AWS4-HMAC-SHA256"
    return dict(field.strip().split("=", 1) for field in fields.split(","))


def _expected_signature(received: Received, secret_key: str, body: bytes | None = None) -> str:
    """The SigV4 signature for the request as it arrived, by the algorithm in
    docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html:
    a canonical request, a string to sign, a key derived from the secret, then HMAC-SHA256.
    It uses no AWS library. body, when given, replaces the body that arrived.

    The canonical query is the query's pairs sorted, which is right for FakeApi's URL: one
    pair of unreserved characters.
    """
    authorization = _authorization(received)
    scope = authorization["Credential"].split("/", 1)[1]  # YYYYMMDD/region/service/aws4_request
    signed_headers = authorization["SignedHeaders"]
    path, _, query = received.path.partition("?")
    canonical_headers = "".join(
        f"{name}:{' '.join(received.headers[name].split())}\n" for name in signed_headers.split(";")
    )
    payload = received.raw if body is None else body
    canonical_request = "\n".join(
        [
            received.method,
            path,
            "&".join(sorted(query.split("&"))) if query else "",
            canonical_headers,
            signed_headers,
            hashlib.sha256(payload).hexdigest(),
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            received.headers["x-amz-date"],
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = f"AWS4{secret_key}".encode()
    for part in scope.split("/"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def _check_signed(received: Received, token: str | None = None) -> None:
    """Signed with SigV4 by the dummy credentials, for execute-api in the contract's region,
    over the exact bytes that arrived."""
    authorization = _authorization(received)
    amz_date = received.headers["x-amz-date"]
    assert re.fullmatch(r"\d{8}T\d{6}Z", amz_date)
    assert authorization["Credential"] == (
        # The service spelled out, not load's constant, so a wrong constant fails here.
        # It becomes names.API_SIGV4_SERVICE after c1.
        f"{ACCESS_KEY}/{amz_date[:8]}/{names.REGION}/execute-api/aws4_request"
    )
    signed = authorization["SignedHeaders"].split(";")
    assert signed == sorted(signed)
    assert {"content-type", "host", "x-amz-date"} <= set(signed)
    if token is None:
        assert "x-amz-security-token" not in received.headers
    else:
        assert received.headers["x-amz-security-token"] == token
        assert "x-amz-security-token" in signed
    assert authorization["Signature"] == _expected_signature(received, SECRET_KEY)


# ---------------------------------------------------------------------------
# A temporary repository with two worktrees, and copies of load.py run as processes
# ---------------------------------------------------------------------------
class TempRepo(NamedTuple):
    common_dir: Path  # <repo>/.git, which both worktrees share
    main: Path  # the repository's own worktree
    second: Path  # a second worktree, from git worktree add


class Finished(NamedTuple):
    code: int
    out: str
    err: str


GIT_SETTINGS = (
    "-c",
    "user.name=load test",
    "-c",
    "user.email=load-test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def _env(**extra: str | None) -> dict[str, str]:
    """The environment for git and for the script's copies.

    No GIT_* variable passes, so no git command here can act on this repository. The AWS
    environment is the dummy one, and 127.0.0.1 is never sent through a proxy. An extra
    value of None removes that variable.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and not _is_aws_variable(key)
    }
    env.update(NO_PROXY=LOOPBACK, no_proxy=LOOPBACK, PYTHONDONTWRITEBYTECODE="1")
    env.update(NO_AWS_SOURCES)
    env.update(DUMMY_KEYS)
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *GIT_SETTINGS, *args],
        cwd=cwd,
        env=_env(),
        capture_output=True,
        text=True,
        check=True,
        timeout=WAIT_SECONDS,
    )
    return done.stdout.strip()


def _copy_checkout(dest: Path) -> None:
    """The files load.py runs from: the script, the contracts package and the questions."""
    (dest / names.LOAD_SCRIPT).parent.mkdir(parents=True)
    shutil.copy2(ROOT / names.LOAD_SCRIPT, dest / names.LOAD_SCRIPT)
    shutil.copytree(
        ROOT / "contracts",
        dest / "contracts",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    (dest / names.QUESTIONS_FILE).parent.mkdir(parents=True)
    shutil.copy2(ROOT / names.QUESTIONS_FILE, dest / names.QUESTIONS_FILE)


@pytest.fixture
def temp_repo(tmp_path) -> TempRepo:
    main = tmp_path / "repo"
    second = tmp_path / "second-worktree"
    main.mkdir()
    _git(main, "init", "-q")
    _copy_checkout(main)
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "load.py test fixture")
    _git(main, "worktree", "add", "-q", "--detach", str(second))
    common_dir = Path(_git(second, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    assert common_dir.resolve() == (main / ".git").resolve()
    return TempRepo(common_dir.resolve(), main, second)


def _finish(proc: subprocess.Popen, timeout: float = WAIT_SECONDS) -> Finished:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"load.py still running after {timeout} s; stdout {out!r}, stderr {err!r}")
    return Finished(proc.returncode, out, err)


def _summary(out: str) -> str:
    """The last line load.py prints: sent=<n> ok=<n> failed=<n>."""
    return out.strip().splitlines()[-1]


class Loads:
    """Starts copies of load.py as processes; kills any still running at teardown."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.started: list[subprocess.Popen] = []

    def start(
        self,
        checkout: Path,
        *,
        users: int,
        questions: int,
        run_id: str = RUN_ID,
        cwd: Path | None = None,
        **env: str | None,
    ) -> subprocess.Popen:
        args = [sys.executable, str(checkout / names.LOAD_SCRIPT)]
        args += ["--users", str(users), "--questions", str(questions), "--url", self.url]
        args += ["--run-id", run_id]
        proc = subprocess.Popen(
            args,
            cwd=cwd or checkout,
            env=_env(**env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.started.append(proc)
        return proc

    def run(self, checkout: Path, *, timeout: float = WAIT_SECONDS, **kwargs) -> Finished:
        return _finish(self.start(checkout, **kwargs), timeout)

    def refused(self, checkout: Path, **kwargs) -> Finished:
        """A start that must be refused. One that is not would hang on a gated reply, so it
        gets REFUSAL_SECONDS, and the failure shows what it printed."""
        return self.run(checkout, timeout=REFUSAL_SECONDS, **kwargs)

    def kill_all(self) -> None:
        for proc in self.started:
            if proc.poll() is None:
                proc.kill()
            if proc.stderr is not None and proc.stderr.closed:
                proc.stderr = None  # a test closed it on purpose
            proc.communicate()


@pytest.fixture
def loads(fake_api) -> Iterator[Loads]:
    started = Loads(fake_api.url)
    yield started
    started.kill_all()


def _nothing_secret_printed(done: Finished, url: str) -> None:
    """No URL, key, secret or token in what the script printed."""
    printed = done.out + done.err
    for value in (url, ACCESS_KEY, SECRET_KEY, SESSION_TOKEN, "Signature="):
        assert value not in printed


# ---------------------------------------------------------------------------
# app/questions.json and the request plan
# ---------------------------------------------------------------------------
def test_questions_file_holds_thirty_distinct_support_questions():
    questions = json.loads((ROOT / names.QUESTIONS_FILE).read_text())
    assert isinstance(questions, list)
    assert len(questions) == names.QUESTIONS_COUNT
    assert len(set(questions)) == len(questions)
    for question in questions:
        assert isinstance(question, str)
        assert question and question == question.strip()
        QUESTION_RULE.validate(question)
    assert load.load_questions() == questions


@pytest.mark.parametrize("load_plan", [names.LOAD_SMOKE, names.LOAD_FAULT, names.LOAD_FULL])
def test_plan_gives_each_user_different_questions_and_spreads_them_evenly(load_plan):
    users, per_user = load_plan
    questions = load.load_questions()
    bodies = load.plan(RUN_ID, users, per_user, questions)

    assert len(bodies) == users * per_user
    for body in bodies:
        _check_body(body, RUN_ID)
    asked = defaultdict(list)
    for body in bodies:
        asked[body[load.IDENTITY_FIELD]].append(body[load.QUESTION_FIELD])
    # Exactly users 1 to N of this run, each with per_user different questions.
    identities = _expected_identities(RUN_ID, users)
    assert sorted(asked) == identities
    assert all(len(set(mine)) == per_user for mine in asked.values())
    # Every question in the file is asked, each as often as the others.
    times_asked = Counter(body[load.QUESTION_FIELD] for body in bodies)
    assert set(times_asked) == set(questions)
    assert len(set(times_asked.values())) == 1
    # Round by round: the first round asks every identity once, in order.
    assert [body[load.IDENTITY_FIELD] for body in bodies[:users]] == identities


def test_each_run_id_gets_identities_that_no_other_run_uses():
    questions = load.load_questions()
    users, per_user = names.LOAD_FULL

    def identities(run_id):
        return {body[load.IDENTITY_FIELD] for body in load.plan(run_id, users, per_user, questions)}

    first, second = identities(RUN_ID), identities(OTHER_RUN_ID)
    assert first == set(_expected_identities(RUN_ID, users))
    assert second == set(_expected_identities(OTHER_RUN_ID, users))
    assert first.isdisjoint(second)
    assert load.run_identity(RUN_ID, 7) == f"{RUN_ID}-user-007"


@pytest.mark.parametrize(
    ("run_id", "users", "per_user"),
    [
        (RUN_ID, 0, 1),
        (RUN_ID, names.IDENTITY_COUNT + 1, 1),
        (RUN_ID, 1, 0),
        (RUN_ID, 1, names.QUESTIONS_COUNT + 1),
        ("loadtest1200", 1, 1),  # no leading r
        ("rload12", 1, 1),  # fewer than four digits
        ("rLOAD1200", 1, 1),  # upper case
        ("", 1, 1),
    ],
)
def test_plan_refuses_what_the_run_ids_identities_or_questions_cannot_cover(
    run_id, users, per_user
):
    with pytest.raises(ValueError):
        load.plan(run_id, users, per_user, load.load_questions())


GOOD_ARGS = ["--users", "1", "--questions", "1", "--url", "http://127.0.0.1:9/"]
OTHER_REGION_URL = "https://abc1234567.execute-api.eu-west-1.amazonaws.com/ask"


@pytest.mark.parametrize(
    "argv",
    [
        ["--users", "0", "--questions", "1", "--url", "http://127.0.0.1:9/", "--run-id", RUN_ID],
        ["--users", "1", "--questions", "two", "--url", "http://127.0.0.1:9/", "--run-id", RUN_ID],
        ["--users", "1", "--questions", "1", "--url", "ftp://127.0.0.1/", "--run-id", RUN_ID],
        ["--users", "1", "--questions", "1", "--url", "not a url", "--run-id", RUN_ID],
        # Plain http only to this machine: a signed request carries the session token.
        ["--users", "1", "--questions", "1", "--url", "http://api.example.invalid/ask"]
        + ["--run-id", RUN_ID],
        # An API in another region: this project runs only in us-east-1.
        ["--users", "1", "--questions", "1", "--url", OTHER_REGION_URL, "--run-id", RUN_ID],
        ["--users", "1", "--questions", "1", "--run-id", RUN_ID],  # no --url
        GOOD_ARGS,  # no --run-id
        [*GOOD_ARGS, "--run-id", "smoke0412"],
        [*GOOD_ARGS, "--run-id", "rsmoke12"],
        [*GOOD_ARGS, "--run-id", f"{RUN_ID}-user-001"],
    ],
)
def test_bad_arguments_exit_as_a_usage_error(argv, capsys):
    with pytest.raises(SystemExit) as exited:
        load.parse_args(argv)
    assert exited.value.code == load.EXIT_USAGE
    err = capsys.readouterr().err
    if "--url" in argv:
        assert argv[argv.index("--url") + 1] not in err  # never printed, even when refused


PASTED_URL = "https://abc1234567.execute-api.us-east-1.amazonaws.com/ask"


@pytest.mark.parametrize(
    "argv",
    [
        ["--run-id", PASTED_URL, "--users", "1", "--questions", "1", "--url", PASTED_URL],
        ["--run-id", RUN_ID, "--users", PASTED_URL, "--questions", "1", "--url", PASTED_URL],
        ["--run-id", RUN_ID, "--users", "1", "--questions", PASTED_URL, "--url", PASTED_URL],
        ["--run-id", RUN_ID, "--users", "1", "--questions", "1", "--url", PASTED_URL, PASTED_URL],
        ["--run-id", RUN_ID, "--users", "1", "--questions", "1", f"--u={PASTED_URL}"],
        [f"--help={PASTED_URL}"],  # argparse's own "ignored explicit argument" message
    ],
    ids=[
        "as --run-id",
        "as --users",
        "as --questions",
        "a stray argument",
        "an abbreviation",
        "as --help's value",
    ],
)
def test_a_url_in_the_wrong_place_is_refused_and_never_printed(argv, capsys):
    with pytest.raises(SystemExit) as exited:
        load.parse_args(argv)  # parsing only: nothing is sent
    assert exited.value.code == load.EXIT_USAGE
    printed = capsys.readouterr()
    assert "abc1234567" not in printed.out + printed.err


def test_good_arguments_parse():
    args = load.parse_args([*GOOD_ARGS, "--run-id", RUN_ID])
    assert (args.users, args.questions, args.run_id) == (1, 1, RUN_ID)
    assert names.is_run_id(RUN_ID) and names.is_run_id(OTHER_RUN_ID)
    # Parsing only: nothing is sent to these.
    for url in (
        "https://abc1234567.execute-api.us-east-1.amazonaws.com/ask",
        "http://localhost:9/",
    ):
        assert load.parse_args(
            ["--users", "1", "--questions", "1", "--url", url, "--run-id", RUN_ID]
        )


@pytest.mark.parametrize(
    ("url", "region"),
    [
        ("https://abc1234567.execute-api.us-east-1.amazonaws.com/ask", "us-east-1"),
        ("http://127.0.0.1:9/route", names.REGION),  # no region in the host
        ("https://api.example.invalid/ask", names.REGION),
    ],
)
def test_the_signing_region_comes_from_the_host_or_the_contract(url, region):
    assert load.signing_region(url) == region == names.REGION


def test_a_host_in_another_region_is_refused():
    with pytest.raises(ValueError, match="eu-west-1"):
        load.signing_region(OTHER_REGION_URL)


def test_the_credentials_come_from_the_shell():
    frozen = load.aws_credentials().get_frozen_credentials()
    assert (frozen.access_key, frozen.secret_key, frozen.token) == (ACCESS_KEY, SECRET_KEY, None)


@pytest.mark.parametrize(
    "setting",
    [
        {"AWS_ACCESS_KEY_ID": None, "AWS_SECRET_ACCESS_KEY": None},  # none at all
        {"AWS_PROFILE": "rm-no-such-profile"},  # a profile that cannot be loaded
    ],
)
def test_without_usable_credentials_there_is_nothing_to_sign_with(monkeypatch, setting):
    for name, value in setting.items():
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(load.NoCredentials):
        load.aws_credentials()


# ---------------------------------------------------------------------------
# Sending: the count, the concurrency, the signature, failures
# ---------------------------------------------------------------------------
def test_sends_each_planned_request_once_to_the_url_as_given(fake_api):
    bodies = load.plan(RUN_ID, 7, 4, load.load_questions())
    tally = _run(fake_api.url, bodies)

    assert (tally.sent, tally.ok, tally.failed, tally.interrupted) == (28, 28, 0, False)
    assert len(fake_api.received) == 28

    def key(body):
        return json.dumps(body, sort_keys=True)

    assert Counter(key(r.body) for r in fake_api.received) == Counter(key(b) for b in bodies)
    assert {(r.method, r.path, r.headers["content-type"]) for r in fake_api.received} == {
        ("POST", "/route/under/test?stage=fake", "application/json")
    }
    assert load.API_ROUTE_KEY.split()[0] == "POST"


def test_every_request_is_signed_for_execute_api_over_the_bytes_sent(fake_api):
    _run(fake_api.url, load.plan(RUN_ID, 5, 2, load.load_questions()))

    assert len(fake_api.received) == 10
    for received in fake_api.received:
        _check_signed(received)


def test_a_session_token_is_sent_and_signed(fake_api):
    credentials = Credentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN)
    _run(fake_api.url, load.plan(RUN_ID, 3, 1, load.load_questions()), credentials=credentials)

    assert len(fake_api.received) == 3
    for received in fake_api.received:
        _check_signed(received, token=SESSION_TOKEN)


def test_a_changed_body_fails_the_signature_check(fake_api):
    _run(fake_api.url, load.plan(RUN_ID, 1, 1, load.load_questions()))
    [received] = fake_api.received
    signature = _authorization(received)["Signature"]

    assert signature == _expected_signature(received, SECRET_KEY)
    changed = received.raw.replace(b"user-001", b"user-002")
    assert changed != received.raw
    assert signature != _expected_signature(received, SECRET_KEY, body=changed)
    assert signature != _expected_signature(received, "another-secret")


def test_each_request_is_signed_when_it_is_sent_not_when_it_is_planned(fake_api):
    """A signature is good for minutes and a full load takes about fifteen, so each request
    is signed as it goes out. Here the second ten wait more than a second for the first."""
    fake_api.gate = threading.Event()
    bodies = load.plan(RUN_ID, 20, 1, load.load_questions())
    runner = threading.Thread(target=_run, args=(fake_api.url, bodies))
    runner.start()
    assert fake_api.wait_for(names.LOAD_MAX_CONCURRENCY)
    time.sleep(1.1)
    fake_api.gate.set()
    runner.join(WAIT_SECONDS)

    first, rest = fake_api.received[:10], fake_api.received[10:]
    assert len(rest) == 10
    assert max(r.headers["x-amz-date"] for r in first) < min(r.headers["x-amz-date"] for r in rest)
    for received in fake_api.received:
        _check_signed(received)


def test_never_more_requests_in_flight_than_the_contract_allows(fake_api):
    fake_api.overlap = names.LOAD_MAX_CONCURRENCY
    bodies = load.plan(RUN_ID, 15, 2, load.load_questions())  # 30 requests: 3 rounds of 10

    tally = _run(fake_api.url, bodies)

    assert tally.sent == tally.ok == len(fake_api.received) == len(bodies)
    # Reached, so the server could see overlap, and never passed.
    assert fake_api.max_in_flight == names.LOAD_MAX_CONCURRENCY


def test_error_replies_count_as_failed_and_are_never_retried(fake_api):
    def odd_users_fail(body):
        return 502 if int(body[load.IDENTITY_FIELD].rsplit("-", 1)[1]) % 2 else 200

    fake_api.status_for = odd_users_fail
    tally = _run(fake_api.url, load.plan(RUN_ID, 10, 3, load.load_questions()))

    assert (tally.sent, tally.ok, tally.failed) == (30, 15, 15)
    assert tally.reasons == Counter({"HTTP 502": 15})
    assert len(fake_api.received) == 30


def test_an_unreachable_api_fails_every_request_exactly_once():
    with socket.socket() as spare:
        spare.bind((LOOPBACK, 0))
        port = spare.getsockname()[1]
    # Nothing listens on that port any more.
    bodies = load.plan(RUN_ID, 3, 2, load.load_questions())
    tally = _run(f"http://{LOOPBACK}:{port}/", bodies)

    assert (tally.sent, tally.ok, tally.failed) == (6, 0, 6)
    assert tally.reasons == Counter({"ConnectionRefusedError": 6})


def test_a_request_with_no_reply_times_out_and_counts_as_failed(fake_api):
    fake_api.gate = threading.Event()  # set only at teardown: no reply comes in the run
    bodies = load.plan(RUN_ID, 2, 1, load.load_questions())
    tally = _run(fake_api.url, bodies, timeout=0.2)

    assert (tally.sent, tally.ok, tally.failed) == (2, 0, 2)
    assert tally.reasons == Counter({"timeout": 2})
    assert load.REQUEST_TIMEOUT_SECONDS == names.API_GATEWAY_TIMEOUT_SECONDS
    # And that is what a load waits when no timeout is passed.
    default = inspect.signature(load.run_load).parameters["timeout"].default
    assert default == names.API_GATEWAY_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# The command line, run as a process from a temporary repository
# ---------------------------------------------------------------------------
def test_full_run_sends_exactly_users_times_questions_and_prints_the_counts(
    temp_repo, fake_api, loads
):
    users, per_user = names.LOAD_FULL  # the spec's scenario: 150 x 10 = 1,500
    done = loads.run(temp_repo.main, users=users, questions=per_user)

    assert done.code == load.EXIT_OK, done.err
    assert _summary(done.out) == f"sent={users * per_user} ok={users * per_user} failed=0"
    assert f"run {RUN_ID}" in done.out
    _nothing_secret_printed(done, fake_api.url)
    assert len(fake_api.received) == users * per_user == 1500
    for received in fake_api.received:
        _check_body(received.body, RUN_ID)
        _check_signed(received)  # with the keys from the shell's environment
    # Exactly users 1 to 150 of this run, each asking 10 questions.
    per_identity = Counter(r.body[load.IDENTITY_FIELD] for r in fake_api.received)
    assert per_identity == dict.fromkeys(_expected_identities(RUN_ID, users), per_user)


def test_a_session_token_from_the_shell_is_sent_and_signed(temp_repo, fake_api, loads):
    done = loads.run(temp_repo.main, users=2, questions=1, AWS_SESSION_TOKEN=SESSION_TOKEN)

    assert done.code == load.EXIT_OK, done.err
    _nothing_secret_printed(done, fake_api.url)
    assert len(fake_api.received) == 2
    for received in fake_api.received:
        _check_signed(received, token=SESSION_TOKEN)


def test_without_credentials_it_refuses_to_start_and_sends_nothing(temp_repo, fake_api, loads):
    done = loads.run(
        temp_repo.main, users=1, questions=1, AWS_ACCESS_KEY_ID=None, AWS_SECRET_ACCESS_KEY=None
    )

    assert done.code == load.EXIT_USAGE
    assert "no AWS credentials" in done.err
    assert done.out == ""
    _nothing_secret_printed(done, fake_api.url)
    assert fake_api.received == []
    # Refused before the lock was even taken.
    assert not (temp_repo.common_dir / names.LOAD_LOCK_NAME).exists()


def test_failures_print_their_reasons_and_exit_non_zero(temp_repo, fake_api, loads):
    fake_api.status_for = lambda body: 500
    done = loads.run(temp_repo.main, users=2, questions=1)

    assert done.code == load.EXIT_FAILED
    assert _summary(done.out) == "sent=2 ok=0 failed=2"
    assert "HTTP 500 x2" in done.out
    _nothing_secret_printed(done, fake_api.url)
    assert len(fake_api.received) == 2


def test_more_users_than_identities_is_refused_before_anything_is_sent(temp_repo, fake_api, loads):
    done = loads.run(temp_repo.main, users=names.IDENTITY_COUNT + 1, questions=1)

    assert done.code == load.EXIT_USAGE
    assert f"{RUN_ID}-{names.IDENTITY_LAST}" in done.err
    assert fake_api.received == []


def test_a_bad_run_id_is_refused_before_anything_is_sent(temp_repo, fake_api, loads):
    done = loads.run(temp_repo.main, users=1, questions=1, run_id="smoke0412")

    assert done.code == load.EXIT_USAGE
    assert names.RUN_ID_PATTERN in done.err
    assert fake_api.received == []


def test_a_second_start_is_refused_from_any_worktree_or_directory(
    temp_repo, fake_api, loads, tmp_path
):
    fake_api.gate = threading.Event()
    first = loads.start(temp_repo.main, users=1, questions=1)
    assert fake_api.wait_for(1)  # the first load is sending, so it holds the lock

    elsewhere = tmp_path / "outside-any-repo"
    elsewhere.mkdir()
    attempts = [
        (temp_repo.second, temp_repo.second),  # the other worktree's copy, from there
        (temp_repo.main, temp_repo.main / "app"),  # the same copy, from a subdirectory
        (temp_repo.second, elsewhere),  # the other copy, from outside any repository
    ]
    for checkout, cwd in attempts:
        refused = loads.refused(checkout, users=1, questions=1, cwd=cwd)
        assert refused.code == load.EXIT_REFUSED, refused
        assert "another load holds" in refused.err
        assert names.LOAD_LOCK_NAME in refused.err
        assert f"pid {first.pid}" in refused.err
        assert refused.out == ""
        _nothing_secret_printed(refused, fake_api.url)
    assert len(fake_api.received) == 1  # the refused starts sent nothing

    # One lock for the whole repository, in the git common directory, not one per worktree.
    assert (temp_repo.common_dir / names.LOAD_LOCK_NAME).is_file()
    assert list(temp_repo.common_dir.rglob(names.LOAD_LOCK_NAME)) == [
        temp_repo.common_dir / names.LOAD_LOCK_NAME
    ]

    fake_api.gate.set()
    done = _finish(first)
    assert done.code == load.EXIT_OK, done.err
    assert _summary(done.out) == "sent=1 ok=1 failed=0"

    again = loads.run(temp_repo.second, users=1, questions=1)
    assert again.code == load.EXIT_OK, again.err  # a load that finished let the lock go


def test_the_lock_frees_itself_when_the_holding_load_is_killed(temp_repo, fake_api, loads):
    fake_api.gate = threading.Event()
    holder = loads.start(temp_repo.main, users=1, questions=1)
    assert fake_api.wait_for(1)
    assert loads.refused(temp_repo.second, users=1, questions=1).code == load.EXIT_REFUSED

    holder.send_signal(signal.SIGKILL)  # no handler runs: only the kernel can drop the lock
    holder.wait(WAIT_SECONDS)
    assert holder.returncode == -signal.SIGKILL
    # The file stays behind; the lock went with the process.
    assert (temp_repo.common_dir / names.LOAD_LOCK_NAME).is_file()

    fake_api.gate.set()
    after = loads.run(temp_repo.second, users=1, questions=1)
    assert after.code == load.EXIT_OK, after.err
    assert _summary(after.out) == "sent=1 ok=1 failed=0"


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@pytest.mark.parametrize("first", STOP_SIGNALS, ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_stop_signal_drains_the_load_holds_the_lock_and_prints_what_was_sent(
    temp_repo, fake_api, loads, first
):
    """Ctrl-C, a harness's SIGTERM and a closed terminal's SIGHUP each stop a load the same
    way: nothing more is sent, the ten in flight finish, and the counts are printed. More
    signals during that drain change nothing, and the lock stays held until it ends."""
    fake_api.gate = threading.Event()
    proc = loads.start(temp_repo.main, users=20, questions=1)
    assert fake_api.wait_for(names.LOAD_MAX_CONCURRENCY)  # ten held at the server

    stderr_lines: list[str] = []
    stopping = threading.Event()

    def read_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
            if line.startswith("interrupted"):
                stopping.set()

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    proc.send_signal(first)
    assert stopping.wait(WAIT_SECONDS), stderr_lines  # it has cancelled what was not sent

    # Every stop signal again, mid-drain: none may cut the drain short or free the lock.
    for again in STOP_SIGNALS:
        proc.send_signal(again)
    time.sleep(0.5)
    assert proc.poll() is None, stderr_lines  # still draining
    refused = loads.refused(temp_repo.second, users=1, questions=1)
    assert refused.code == load.EXIT_REFUSED, refused  # the lock is still held

    fake_api.gate.set()
    proc.wait(WAIT_SECONDS)
    out = proc.stdout.read()
    reader.join(WAIT_SECONDS)

    sent = names.LOAD_MAX_CONCURRENCY
    assert proc.returncode == load.EXIT_INTERRUPTED, stderr_lines
    assert _summary(out) == f"sent={sent} ok={sent} failed=0"
    assert f"interrupted: sent {sent} of 20" in out
    assert len(fake_api.received) == sent  # the refused start sent nothing


def test_only_the_first_stop_signal_raises():
    """Two different stop signals back to back (SIGTERM, then Ctrl-C before the first is
    handled) must stop the load once. A second KeyboardInterrupt would escape the drain and
    free the lock with requests in flight. A process test catches that race only now and
    then, so this checks the handler itself."""
    load._STOPPING.clear()
    try:
        with pytest.raises(KeyboardInterrupt):
            load._stop(signal.SIGTERM, None)
        for later in (signal.SIGINT, signal.SIGHUP):
            try:
                assert load._stop(later, None) is None
            except KeyboardInterrupt:  # a test failure, not an interrupted test run
                pytest.fail(f"{signal.Signals(later).name} after SIGTERM raised again")
    finally:
        load._STOPPING.clear()


def test_the_stop_handler_is_what_catches_every_stop_signal():
    before = {stop: signal.getsignal(stop) for stop in load.STOP_SIGNALS}
    try:
        load.install_stop_handlers()
        assert {stop: signal.getsignal(stop) for stop in load.STOP_SIGNALS} == dict.fromkeys(
            (signal.SIGINT, signal.SIGTERM, signal.SIGHUP), load._stop
        )
    finally:
        for stop, handler in before.items():
            signal.signal(stop, handler)


def test_with_stderr_gone_a_stop_signal_still_holds_the_lock_through_the_drain(
    temp_repo, fake_api, loads
):
    """A closed terminal (SIGHUP's own case) or a closed stderr makes the drain's notice
    fail to print. The load must still wait for the requests in flight before it lets the
    lock go."""
    fake_api.gate = threading.Event()
    proc = loads.start(temp_repo.main, users=20, questions=1)
    assert fake_api.wait_for(names.LOAD_MAX_CONCURRENCY)

    proc.stderr.close()  # nothing reads stderr now, so writing to it fails
    proc.send_signal(signal.SIGHUP)
    time.sleep(1)
    assert proc.poll() is None  # still draining
    refused = loads.refused(temp_repo.second, users=1, questions=1)
    assert refused.code == load.EXIT_REFUSED, refused  # the lock is still held

    fake_api.gate.set()
    proc.wait(WAIT_SECONDS)
    assert len(fake_api.received) == names.LOAD_MAX_CONCURRENCY  # nothing more was sent


def test_outside_a_git_repository_it_refuses_to_start(tmp_path, fake_api, loads):
    plain = tmp_path / "not-a-repo"
    _copy_checkout(plain)
    done = loads.run(plain, users=1, questions=1, GIT_CEILING_DIRECTORIES=str(tmp_path))

    assert done.code == load.EXIT_REFUSED
    assert "git common directory" in done.err
    assert fake_api.received == []
