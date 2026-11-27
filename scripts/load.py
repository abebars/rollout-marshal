"""Load generator for the assistant API (design.md section 11, task 3.4).

    uv run python scripts/load.py --run-id <run_id> --users N --questions M --url <api>

One form that never prints the URL, from the repository root: the shell puts terraform's
output straight into the argument, and nothing echoes it.

    uv run python scripts/load.py --run-id <run_id> --users N --questions M \
        --url "$(terraform -chdir=infra output -raw ask_url)"

It sends N x M requests, then stops: identities <run_id>-user-001 to <run_id>-user-N each
ask M different questions from app/questions.json. No identity is used by two runs:
nothing shows that Flagsmith counts only an experiment's own events for an identity, so
each run's are new to it (the lead's Gate 1 decision, contract change c1). Requests go out
round by round (every user's first question, then every user's second, and so on), at most
10 in flight at once. Each is sent once, with no retries. The last line it prints is:

    sent=<n> ok=<n> failed=<n>

ok means an HTTP 2xx reply. Anything else is failed: an error status, no reply within API
Gateway's 30 second limit, a refused connection. A line before the summary counts the
failures by kind. sent counts every request the script started, so it is the number the
answer budget counts (design.md section 11: ask the human before 7,500 in all).

Each request POSTs one answer request, {"question": ..., "identity": ...}
(contracts/schemas/answer_request.json), to --url exactly as given. That URL is the full URL
of the HTTP API's only route, POST /ask, as the app module's output ask_url gives it. The
route uses AWS_IAM authorization, so every request is signed with SigV4 for execute-api in
us-east-1, from the shell's AWS credentials, found the way the AWS CLI finds them (contract
change c1, design.md section 11). Each is signed as it goes out, over the exact bytes sent.
A shell with no usable credentials is refused before anything is sent. A signed request
carries the session token, so plain http is allowed only to a server on this machine. The
script never prints the URL, a key or a signature, so its output can go into a public
evidence file.

One load at a time. Before it sends anything, the script takes rm-load.lock: an exclusive
flock(2) on a file in the repository's git common directory. The directory is found from
where this script lives (git rev-parse --path-format=absolute --git-common-dir), so every
worktree and every working directory shares the one lock. While another load holds it, a
start is refused with exit status 3 and sends nothing; so is a start that cannot take the
lock at all. The kernel drops the lock when the holding process ends, however it ends, so a
killed load leaves no stale lock. The file stays; only the lock on it counts.

Ctrl-C stops a load, and so do SIGTERM (a harness's timeout) and SIGHUP (a closed
terminal): nothing more is sent, the requests in flight finish, and the counts are
printed. Further stop signals during that drain are ignored, so the lock is held until the
last request ends. The script never runs unattended (design.md section 11): someone
watches every load until it ends.

Exit status: 0 every request ok, 1 some failed, 2 bad arguments or no AWS credentials,
3 refused (the lock), 130 stopped by a signal.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import BotoCoreError, ClientError

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT))  # the repo root, for contracts

from contracts import names  # noqa: E402

# The HTTP API's contract, from contract change c1. Until c1 merges, these are kept here
# with the values it gives names.py.
API_ROUTE_KEY = "POST /ask"  # names.API_ROUTE_KEY after c1
API_AUTHORIZATION_TYPE = "AWS_IAM"  # names.API_AUTHORIZATION_TYPE after c1
API_SIGV4_SERVICE = "execute-api"  # names.API_SIGV4_SERVICE after c1
APP_ASK_URL_OUTPUT = "ask_url"  # names.APP_ASK_URL_OUTPUT after c1
API_METHOD = API_ROUTE_KEY.split()[0]
CONTENT_TYPE = "application/json"
# An API Gateway endpoint names its region: https://{api-id}.execute-api.{region}.amazonaws.com
# (docs.aws.amazon.com/apigateway/latest/developerguide/how-to-call-api.html).
EXECUTE_API_HOST = re.compile(r"[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com")

QUESTIONS_PATH = ROOT / names.QUESTIONS_FILE
QUESTION_FIELD, IDENTITY_FIELD = names.ANSWER_REQUEST_FIELDS
# API Gateway ends every request at this limit (design.md section 5), so a reply that has
# not come by then never will.
REQUEST_TIMEOUT_SECONDS = names.API_GATEWAY_TIMEOUT_SECONDS
# How often the main thread wakes while it waits. Python runs signal handlers only in the
# main thread, so this bounds how long Ctrl-C takes to be seen, whichever thread got it.
WAIT_POLL_SECONDS = 0.2

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2  # argparse's own status for bad arguments
EXIT_REFUSED = 3
EXIT_INTERRUPTED = 130  # 128 + SIGINT, the shell's convention; for SIGTERM and SIGHUP too
# Each stops a load the same way (run_load): main installs _stop for each.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_STOPPING = threading.Event()


def _stop(signum: int, frame: object) -> None:
    """The first stop signal stops the load (run_load); every later one does nothing.

    Two different signals back to back, a SIGTERM and then a Ctrl-C before the first is
    handled, would otherwise raise KeyboardInterrupt twice. The second would escape the
    drain and free the lock with requests in flight.
    """
    if not _STOPPING.is_set():
        _STOPPING.set()
        raise KeyboardInterrupt


def install_stop_handlers() -> None:
    """Ctrl-C, SIGTERM and SIGHUP each stop a load (run_load), even when whatever started it
    was ignoring one of them."""
    for stop in STOP_SIGNALS:
        signal.signal(stop, _stop)


class LoadRefused(RuntimeError):
    """The load must not start: another load holds the lock, or the lock cannot be taken."""


class NoCredentials(RuntimeError):
    """The shell has no usable AWS credentials, so no request could be signed."""


# ---------------------------------------------------------------------------
# What to send
# ---------------------------------------------------------------------------
def load_questions(path: Path = QUESTIONS_PATH) -> list[str]:
    """The support questions in app/questions.json, a JSON list of non-empty strings."""
    questions = json.loads(path.read_text())
    if not (
        isinstance(questions, list)
        and questions
        and all(isinstance(question, str) and question.strip() for question in questions)
    ):
        raise ValueError(f"{names.QUESTIONS_FILE} must be a JSON list of non-empty strings")
    return questions


def request_body(question: str, identity: str) -> dict[str, str]:
    """One request's body, the answer request's two fields."""
    return {QUESTION_FIELD: question, IDENTITY_FIELD: identity}


def _require_run_id(run_id: str) -> str:
    if not names.is_run_id(run_id):
        raise ValueError(f"not a run ID (expected {names.RUN_ID_PATTERN}): {run_id!r}")
    return run_id


def run_identity(run_id: str, n: int) -> str:  # names.run_identity after c1
    """<run_id>-user-NNN, identity n of a run.

    Contract change c1 adds names.run_identity, which does the same; this then calls it.
    """
    return f"{_require_run_id(run_id)}-{names.identity(n)}"


def plan(run_id: str, users: int, per_user: int, questions: list[str]) -> list[dict[str, str]]:
    """Every request of a load, in the order they go out.

    Round r (0 to per_user - 1) asks each of the run's users 1 to <users> one question. User
    n asks questions[((n - 1) * per_user + r) % len(questions)]: the questions are dealt out
    in order, so a user's questions are all different, and each question is asked as often
    as any other whenever users x per_user is a multiple of the list's length (300 and
    1,500 are).
    """
    _require_run_id(run_id)
    if not 1 <= users <= names.IDENTITY_COUNT:
        raise ValueError(
            f"--users must be 1 to {names.IDENTITY_COUNT}, for the identities "
            f"{run_identity(run_id, 1)} to {run_identity(run_id, names.IDENTITY_COUNT)}; "
            f"got {users}"
        )
    if not 1 <= per_user <= len(questions):
        raise ValueError(
            f"--questions must be 1 to {len(questions)}, the number of questions in "
            f"{names.QUESTIONS_FILE}; got {per_user}"
        )
    return [
        request_body(questions[((n - 1) * per_user + r) % len(questions)], run_identity(run_id, n))
        for r in range(per_user)
        for n in range(1, users + 1)
    ]


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------
def git_common_dir(start: Path = SCRIPT_DIR) -> Path:
    """The git common directory of the repository that holds start, shared by its worktrees."""
    command = ["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as err:
        raise LoadRefused(
            f"cannot find the git common directory for {start}: {err.stderr.strip()}"
        ) from None
    except OSError as err:
        raise LoadRefused(f"cannot find the git common directory for {start}: {err}") from None
    return Path(done.stdout.strip())


def lock_path(start: Path = SCRIPT_DIR) -> Path:
    """rm-load.lock in the git common directory."""
    return git_common_dir(start) / names.LOAD_LOCK_NAME


@dataclass
class HeldLock:
    path: Path
    fd: int

    def release(self) -> None:
        os.close(self.fd)  # the kernel drops the flock with the file's last descriptor

    def __enter__(self) -> HeldLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def take_lock(path: Path) -> HeldLock:
    """Take rm-load.lock without waiting, or raise LoadRefused.

    It is an flock(2), not a POSIX record lock (lockf): an flock belongs to the open file,
    so it holds against every other open of the file, even one in this process, and the
    kernel drops it when the process ends. The holder writes its process ID into the file
    so that a refused start can name it. The file is never deleted: deleting a lock file
    lets two processes each lock a different file under the same name.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as err:
        raise LoadRefused(f"cannot open {path}: {err.strerror}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
        os.close(fd)
        named = f" (pid {holder})" if holder else ""
        raise LoadRefused(f"another load holds {path}{named}") from None
    except OSError as err:
        os.close(fd)
        raise LoadRefused(f"cannot lock {path}: {err.strerror}") from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return HeldLock(path, fd)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
@dataclass
class Tally:
    """What a load sent and how each request ended. Safe to update from many threads."""

    sent: int = 0
    ok: int = 0
    failed: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    interrupted: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def started(self) -> None:
        with self._lock:
            self.sent += 1

    def finished(self, failure: str | None) -> None:
        with self._lock:
            if failure is None:
                self.ok += 1
            else:
                self.failed += 1
                self.reasons[failure] += 1

    def summary(self) -> str:
        return f"sent={self.sent} ok={self.ok} failed={self.failed}"


def _kind(error: object) -> str:
    """A failure's kind: never the error's text, which can hold the URL."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, BaseException):
        return type(error).__name__
    return "URLError"


def signing_region(url: str) -> str:
    """The region requests to url are signed for: the one an execute-api host names, else
    names.REGION. Any other region is refused, as this project runs only in us-east-1."""
    match = EXECUTE_API_HOST.fullmatch(urllib.parse.urlsplit(url).hostname or "")
    region = match.group(1) if match else names.REGION
    if region != names.REGION:
        raise ValueError(
            f"the URL's host is in {region}, and this project runs only in {names.REGION}"
        )
    return region


def aws_credentials() -> Credentials:
    """The shell's AWS credentials, found the way the AWS CLI finds them.

    They are loaded once here, so a missing or expired sign-in stops the load before it
    sends anything. NoCredentials names only the error's kind, never its text.
    """
    try:
        credentials = botocore.session.Session().get_credentials()
        if credentials is not None:
            credentials.get_frozen_credentials()
    except (BotoCoreError, ClientError) as err:
        raise NoCredentials(f"cannot load AWS credentials ({type(err).__name__})") from None
    if credentials is None:
        raise NoCredentials("no AWS credentials in this shell")
    return credentials


def signed_request(
    url: str, data: bytes, credentials: Credentials, region: str
) -> urllib.request.Request:
    """A POST of data to url, signed with SigV4 for execute-api in region.

    The signature covers the Host header urllib sends, the content type, X-Amz-Date, the
    session token when there is one, and the SHA-256 of data, the exact bytes sent. A
    frozen copy of the credentials keeps key, secret and token together, refreshed first if
    they are about to expire.
    """
    request = urllib.request.Request(url, data=data, method=API_METHOD)
    to_sign = AWSRequest(
        method=API_METHOD,
        url=url,
        data=data,
        headers={"Host": request.host, "Content-Type": CONTENT_TYPE},
    )
    SigV4Auth(credentials.get_frozen_credentials(), API_SIGV4_SERVICE, region).add_auth(to_sign)
    for name, value in to_sign.headers.items():
        request.add_header(name, value)
    return request


def send(
    url: str, body: dict[str, str], *, credentials: Credentials, region: str, timeout: float
) -> str | None:
    """Sign one request as it goes out and POST it. None when the reply is HTTP 2xx, else
    why it failed."""
    try:
        request = signed_request(url, json.dumps(body).encode(), credentials, region)
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            reply.read()
            status = reply.status
    except urllib.error.HTTPError as err:
        err.close()
        return f"HTTP {err.code}"
    except urllib.error.URLError as err:  # no reply: refused, unreachable, timed out
        return _kind(err.reason)
    except Exception as err:  # every request ends as ok or failed, whatever went wrong
        return _kind(err)
    return None if 200 <= status < 300 else f"HTTP {status}"


def run_load(
    url: str,
    bodies: list[dict[str, str]],
    *,
    credentials: Credentials,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Tally:
    """Send every body once, signed, at most LOAD_MAX_CONCURRENCY at a time, and count the
    outcomes.

    A stop signal (Ctrl-C, SIGTERM, SIGHUP) cancels every request not yet sent, ignores any
    further stop signal, waits for the ones in flight, and returns the tally marked
    interrupted.
    """
    region = signing_region(url)
    tally = Tally()

    def one(body: dict[str, str]) -> None:
        tally.started()
        tally.finished(send(url, body, credentials=credentials, region=region, timeout=timeout))

    pool = ThreadPoolExecutor(max_workers=names.LOAD_MAX_CONCURRENCY, thread_name_prefix="load")
    try:
        pending = {pool.submit(one, body) for body in bodies}
        while pending:
            _, pending = wait(pending, timeout=WAIT_POLL_SECONDS)
    except KeyboardInterrupt:
        # First, before anything else: a second signal must not cut the drain short, which
        # would free the lock while requests are still in flight.
        for stop in STOP_SIGNALS:
            signal.signal(stop, signal.SIG_IGN)
        tally.interrupted = True
        pool.shutdown(wait=False, cancel_futures=True)  # nothing more is sent from here
        print(
            "interrupted: sending nothing more; waiting for the requests in flight",
            file=sys.stderr,
            flush=True,
        )
    finally:
        # Whatever was raised above, even a notice that could not be printed to a closed
        # terminal, nothing leaves here (and frees the lock) while a request is in flight.
        pool.shutdown(wait=True, cancel_futures=True)
    return tally


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
# No argument error quotes the value it refuses, so a URL pasted in the wrong place is
# never printed.
def _count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _api_url(value: str) -> str:
    """An --url value. No error quotes it: the URL is never printed."""
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise argparse.ArgumentTypeError("not a URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise argparse.ArgumentTypeError("must be an http:// or https:// URL")
    if parts.scheme == "http" and not _is_loopback(host):
        raise argparse.ArgumentTypeError(
            "must be https://, because a signed request carries the session token;"
            " plain http is only for a test server on this machine"
        )
    try:
        signing_region(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from None
    return value


def _run_id(value: str) -> str:
    if not names.is_run_id(value):
        raise argparse.ArgumentTypeError(f"not a run ID (expected {names.RUN_ID_PATTERN})")
    return value


# Anything that looks like a URL or an AWS host, wherever argparse might quote it.
_URLISH = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\",;]+|[^\s'\",;]*amazonaws\.com[^\s'\",;]*")


class _QuietParser(argparse.ArgumentParser):
    """An ArgumentParser whose errors never print a URL. Some of argparse's own messages
    quote what was typed, an explicit value given to --help for one, so every error has
    anything URL-shaped taken out before it is printed."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {_URLISH.sub('<not shown>', message)}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _QuietParser(
        prog="load.py",
        description="Send users x questions requests to the assistant API, then stop.",
        allow_abbrev=False,  # argparse quotes an ambiguous abbreviation, value and all
    )
    parser.add_argument(
        "--run-id",
        type=_run_id,
        required=True,
        help=f"the run whose identities to use, matching {names.RUN_ID_PATTERN}",
    )
    parser.add_argument(
        "--users",
        type=_count,
        required=True,
        help=f"identities <run_id>-user-001 to <run_id>-user-<N>, N at most {names.IDENTITY_COUNT}",
    )
    parser.add_argument(
        "--questions",
        type=_count,
        required=True,
        help=f"different questions each user asks, from {names.QUESTIONS_FILE}",
    )
    parser.add_argument(
        "--url",
        type=_api_url,
        required=True,
        help=f"the full URL of the API's route, {API_ROUTE_KEY}, as the app module's output"
        f" {APP_ASK_URL_OUTPUT} gives it; requests go to it as given",
    )
    return parser


def _parse(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """parse_args, except that unrecognized arguments are counted, not quoted."""
    args, unrecognized = parser.parse_known_args(argv)
    if unrecognized:
        parser.error(f"{len(unrecognized)} unrecognized argument(s), not shown")
    return args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _parse(build_parser(), argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = _parse(parser, argv)
    try:
        bodies = plan(args.run_id, args.users, args.questions, load_questions())
    except ValueError as err:
        parser.error(str(err))  # exits with EXIT_USAGE

    try:
        credentials = aws_credentials()
    except NoCredentials as err:
        print(
            f"load.py: refused to start: {err}. The route takes {API_AUTHORIZATION_TYPE}"
            " authorization, so every request is signed with SigV4: sign in (for example,"
            " set AWS_PROFILE and run aws sso login), then start again.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        lock = take_lock(lock_path())
    except LoadRefused as err:
        print(f"load.py: refused to start: {err}", file=sys.stderr)
        return EXIT_REFUSED

    install_stop_handlers()
    with lock:
        print(
            f"load: {len(bodies)} requests, {args.users} users x {args.questions} questions"
            f" of run {args.run_id}, at most {names.LOAD_MAX_CONCURRENCY} at a time",
            flush=True,
        )
        tally = run_load(args.url, bodies, credentials=credentials)

    if tally.reasons:
        print("failed: " + ", ".join(f"{kind} x{n}" for kind, n in tally.reasons.most_common()))
    if tally.interrupted:
        print(f"interrupted: sent {tally.sent} of {len(bodies)}")
    print(tally.summary(), flush=True)
    if tally.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK if tally.failed == 0 else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
