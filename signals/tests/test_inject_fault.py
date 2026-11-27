"""scripts/inject_fault.sh (task 4.3) against a stubbed `aws`: no AWS call.

The stub is first on PATH. It logs each call's arguments and answers from environment
variables: the account, the template IDs, and the experiment states that get-experiment
returns in turn. Like the real CLI, it prints times in its own local time zone, and every
run sets TZ=America/New_York, so only the script's own TZ=UTC and `date -u` give UTC.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC as _UTC
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from contracts import names

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / names.INJECT_FAULT_SCRIPT
UTC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"

STUB = r"""#!PYTHON
import json, os, sys
from datetime import datetime, timezone
START = datetime(2026, 11, 27, 4, 0, 20, tzinfo=timezone.utc).timestamp()
END = datetime(2026, 11, 27, 4, 6, 30, tzinfo=timezone.utc).timestamp()
def cli_time(epoch):
    # The CLI prints timestamps in the process's local time zone.
    return datetime.fromtimestamp(epoch).astimezone().isoformat()
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
command = " ".join(args[:2])
if command == "sts get-caller-identity":
    print(os.environ.get("STUB_ACCOUNT", "818928662830"))
elif command == "fis list-experiment-templates":
    print(os.environ.get("STUB_TEMPLATES", "EXT1aaaaaaaaaaaaa"))
elif command == "fis start-experiment":
    if os.environ.get("STUB_FAIL_START"):
        sys.stderr.write("An error occurred (AccessDeniedException) in StartExperiment\n")
        sys.exit(254)
    print("EXP1bbbbbbbbbbbbb\tinitiating")
elif command == "fis get-experiment":
    counter = os.environ["STUB_LOG"] + ".n"
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    if n < int(os.environ.get("STUB_FAIL_POLLS", "0")):
        print("An error occurred (ThrottlingException) when calling GetExperiment", file=sys.stderr)
        sys.exit(254)
    n -= int(os.environ.get("STUB_FAIL_POLLS", "0"))
    states = os.environ.get("STUB_STATES", "running,completed").split(",")
    state = states[min(n, len(states) - 1)]
    ended = state in ("completed", "stopped", "failed", "cancelled")
    end = cli_time(END) if ended else "None"
    print("\t".join([state, cli_time(START), end, f"Experiment {state}."]))
else:
    sys.exit(99)
"""


@pytest.fixture
def run(tmp_path):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    aws = stub_dir / "aws"
    aws.write_text(STUB.replace("PYTHON", sys.executable, 1))
    aws.chmod(0o755)
    log = tmp_path / "aws.log"

    def go(*args, **env):
        environment = {
            **os.environ,
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "RM_FIS_POLL_SECONDS": "0",
            "TZ": "America/New_York",
            **env,
        }
        done = subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return done, calls

    return go


def commands(calls):
    return [" ".join(call[:2]) for call in calls]


def test_starts_the_template_and_waits_for_it_to_complete(run):
    done, calls = run(STUB_STATES="initiating,running,running,completed")
    assert done.returncode == 0, done.stderr
    assert commands(calls) == [
        "sts get-caller-identity",
        "fis list-experiment-templates",
        "fis start-experiment",
        *["fis get-experiment"] * 4,
    ]
    start = next(c for c in calls if c[:2] == ["fis", "start-experiment"])
    assert start[start.index("--experiment-template-id") + 1] == "EXT1aaaaaaaaaaaaa"
    assert all(c[-2:] == ["--region", names.REGION] for c in calls)
    started = re.search(rf"experiment EXP1bbbbbbbbbbbbb started at ({UTC})", done.stdout)
    assert started, done.stdout
    # The script's own clock is UTC (`date -u`), not the TZ=America/New_York the run sets.
    printed = datetime.strptime(started.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC)
    assert abs(datetime.now(_UTC) - printed) < timedelta(minutes=2)
    assert re.search(rf"experiment EXP1bbbbbbbbbbbbb ended at {UTC}: completed", done.stdout)
    # FIS's own start and end, in UTC because the script runs the CLI with TZ=UTC.
    assert "FIS start 2026-11-27T04:00:20+00:00, end 2026-11-27T04:06:30+00:00" in done.stdout


def test_refuses_any_argument(run):
    """There is no --fallback (design.md section 9 builds it only if the timebox runs out)."""
    done, calls = run("--fallback")
    assert done.returncode == 2
    assert calls == []
    assert "takes no arguments" in done.stderr


def test_finds_the_template_by_its_name_tag(run):
    _, calls = run()
    [listing] = [c for c in calls if c[:2] == ["fis", "list-experiment-templates"]]
    query = listing[listing.index("--query") + 1]
    assert f"tags.Name=='{names.FIS_TEMPLATE}'" in query


@pytest.mark.parametrize("final", ["failed", "stopped", "cancelled"])
def test_any_other_end_state_exits_non_zero(run, final):
    done, _ = run(STUB_STATES=f"running,{final}")
    assert done.returncode == 1
    assert re.search(rf"ended at {UTC}: {final}", done.stdout)


def test_stopping_is_not_an_end_state(run):
    done, calls = run(STUB_STATES="running,stopping,stopped")
    assert done.returncode == 1
    assert commands(calls).count("fis get-experiment") == 3
    assert re.search(rf"{UTC} state stopping", done.stdout)
    assert re.search(rf"ended at {UTC}: stopped", done.stdout)


def test_refuses_any_other_account_before_touching_fis(run):
    done, calls = run(STUB_ACCOUNT="111122223333")
    assert done.returncode == 2
    assert commands(calls) == ["sts get-caller-identity"]
    assert "111122223333" in done.stderr


@pytest.mark.parametrize("templates", ["", "None", "EXT1aaaaaaaaaaaaa\tEXT2ccccccccccccc"])
def test_needs_exactly_one_template(run, templates):
    done, calls = run(STUB_TEMPLATES=templates)
    assert done.returncode == 2
    assert "fis start-experiment" not in commands(calls)
    assert names.FIS_TEMPLATE in done.stderr


def test_a_failed_start_exits_with_the_cli_status_and_polls_nothing(run):
    done, calls = run(STUB_FAIL_START="1")
    assert done.returncode == 254
    assert "fis get-experiment" not in commands(calls)
    assert "AccessDeniedException" in done.stderr


def test_a_failed_poll_is_retried_until_the_end(run):
    done, calls = run(STUB_FAIL_POLLS="2", STUB_STATES="running,completed")
    assert done.returncode == 0, done.stderr
    assert commands(calls).count("fis get-experiment") == 4
    assert done.stdout.count("poll failed") == 2
    assert re.search(rf"ended at {UTC}: completed", done.stdout)


def test_gives_up_waiting_without_stopping_the_experiment(run):
    done, calls = run(STUB_STATES="running", RM_FIS_MAX_WAIT_SECONDS="0")
    assert done.returncode == 3
    assert "still running" in done.stderr
    assert not any(c[:2] == ["fis", "stop-experiment"] for c in calls)


def test_never_stops_an_experiment():
    """The fault ends by its own duration (design.md section 9); no agent ends an experiment."""
    assert "stop-experiment" not in SCRIPT.read_text()


def test_constants_match_contracts():
    text = SCRIPT.read_text()
    assert f'ACCOUNT="{names.ACCOUNT_ID}"' in text
    assert f'REGION="{names.REGION}"' in text
    assert f'TEMPLATE_NAME="{names.FIS_TEMPLATE}"' in text


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
