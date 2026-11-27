"""Load generator for the assistant API (design.md section 11, task 3.4).

    uv run python scripts/load.py --run-id <run_id> --users N --questions M --url <api>

It sends N x M requests, then stops: identities <run_id>-user-001 to <run_id>-user-N each
ask M different questions from app/questions.json. No identity is used by two runs:
nothing shows that Flagsmith counts only an experiment's own events for an identity, so
each run's are new to it (the lead's Gate 1 decision, contract change c). Requests go out
round by round (every user's first question, then every user's second, and so on), at most
10 in flight at once. Each is sent once, with no retries. The last line it prints is:

    sent=<n> ok=<n> failed=<n>

ok means an HTTP 2xx reply. Anything else is failed: an error status, no reply within API
Gateway's 30 second limit, a refused connection. A line before the summary counts the
failures by kind. sent counts every request the script started, so it is the number the
answer budget counts (design.md section 11: ask the human before 7,500 in all).

Each request POSTs {"question": ..., "identity": ...} to --url exactly as given, route
included. Those are the answer request's two fields (contracts/schemas/answer_request.json),
which rm-router passes to the answer function. The script never prints the URL, so its
output can go into a public evidence file.

One load at a time. Before it sends anything, the script takes rm-load.lock: an exclusive
flock(2) on a file in the repository's git common directory. The directory is found from
where this script lives (git rev-parse --path-format=absolute --git-common-dir), so every
worktree and every working directory shares the one lock. While another load holds it, a
start is refused with exit status 3 and sends nothing; so is a start that cannot take the
lock at all. The kernel drops the lock when the holding process ends, however it ends, so a
killed load leaves no stale lock. The file stays; only the lock on it counts.

Ctrl-C stops a load: nothing more is sent, the requests in flight finish, and the counts
are printed. The script never runs unattended (design.md section 11): someone watches every
load until it ends.

Exit status: 0 every request ok, 1 some failed, 2 bad arguments, 3 refused (the lock),
130 interrupted.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
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

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT))  # the repo root, for contracts

from contracts import names  # noqa: E402

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
EXIT_INTERRUPTED = 130  # 128 + SIGINT, the shell's convention


class LoadRefused(RuntimeError):
    """The load must not start: another load holds the lock, or the lock cannot be taken."""


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


def run_identity(run_id: str, n: int) -> str:
    """<run_id>-user-NNN, identity n of a run.

    Contract change c adds names.run_identity; this then becomes a call to it.
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


def send(url: str, body: dict[str, str], timeout: float) -> str | None:
    """POST one request. None when the reply is HTTP 2xx, else why it failed."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
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
    url: str, bodies: list[dict[str, str]], *, timeout: float = REQUEST_TIMEOUT_SECONDS
) -> Tally:
    """Send every body once, at most LOAD_MAX_CONCURRENCY at a time, and count the outcomes.

    Ctrl-C cancels every request not yet sent, waits for the ones in flight, and returns the
    tally marked interrupted.
    """
    tally = Tally()

    def one(body: dict[str, str]) -> None:
        tally.started()
        tally.finished(send(url, body, timeout))

    pool = ThreadPoolExecutor(max_workers=names.LOAD_MAX_CONCURRENCY, thread_name_prefix="load")
    try:
        pending = {pool.submit(one, body) for body in bodies}
        while pending:
            _, pending = wait(pending, timeout=WAIT_POLL_SECONDS)
    except KeyboardInterrupt:
        tally.interrupted = True
        pool.shutdown(wait=False, cancel_futures=True)  # nothing more is sent from here
        print(
            "interrupted: sending nothing more; waiting for the requests in flight",
            file=sys.stderr,
            flush=True,
        )
    pool.shutdown(wait=True)
    return tally


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------
def _count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {number}")
    return number


def _http_url(value: str) -> str:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise argparse.ArgumentTypeError("must be an http:// or https:// URL")
    return value


def _run_id(value: str) -> str:
    try:
        return _require_run_id(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="load.py",
        description="Send users x questions requests to the assistant API, then stop.",
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
        type=_http_url,
        required=True,
        help="the assistant API's URL, route included; requests go to it as given",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        bodies = plan(args.run_id, args.users, args.questions, load_questions())
    except ValueError as err:
        parser.error(str(err))  # exits with EXIT_USAGE

    try:
        lock = take_lock(lock_path())
    except LoadRefused as err:
        print(f"load.py: refused to start: {err}", file=sys.stderr)
        return EXIT_REFUSED

    # Ctrl-C stops a load even when whatever started it was ignoring SIGINT.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    with lock:
        print(
            f"load: {len(bodies)} requests, {args.users} users x {args.questions} questions"
            f" of run {args.run_id}, at most {names.LOAD_MAX_CONCURRENCY} at a time",
            flush=True,
        )
        tally = run_load(args.url, bodies)

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
