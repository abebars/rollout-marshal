"""Put the project back to its resting state between runs (task 3.5).

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=<the human's profile in 818928662830>
    uv run python scripts/reset.py [--force]

It refuses while the pointer names a run in a state that is not terminal (design.md
section 6), unless --force. A pointer whose run item is missing, as after
scripts/seed_experiment.py, is no live run. Then, in this order:

1. clears the pointer, on condition it still names the run it read;
2. disables the rm-tick schedule;
3. enables assistant_cheaper_enabled;
4. sets assistant_default_model to current;
5. turns chaos_delay_cheaper off, when it exists;
6. lists every experiment still running, for the human to end in the Flagsmith dashboard.

The tick and the pointer come first: they stop the agent and the router's experiment
traffic. Each Flagsmith state changes through a v2 version, only when it differs.

The tick is disabled by 2.4's set_tick (agent/runtime/rm_agent/tools/schedule.py). It
checks that the schedule is rm-tick's, then writes back every field GetSchedule returns
that UpdateSchedule takes, changing only State. The script never ends an experiment, and
never calls an end, pause or rollout path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "agent" / "runtime"):  # contracts, scripts, and 2.4's rm_agent
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from rm_agent.tools.schedule import set_tick  # noqa: E402

from contracts import names  # noqa: E402
from scripts.bootstrap_flagsmith import (  # noqa: E402
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    Admin,
    FlagsmithError,
    Refused,
    check_account,
    live_state,
    organisation_key,
    path_of,
)

POINTER_KEY = {names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}}
MAX_PAGES = 50  # of the experiments list: far more than one project ever holds


# ---------------------------------------------------------------------------
# The pointer
# ---------------------------------------------------------------------------
def live_run(dynamodb) -> tuple[str | None, str | None]:
    """The pointer's run ID and that run's state: (None, None) with no pointer, and a
    state of None when the run item is missing."""
    item = dynamodb.get_item(TableName=names.RUNS_TABLE, Key=POINTER_KEY, ConsistentRead=True)
    found = item.get("Item")
    if not found:
        return None, None
    run_id = found.get("run_id", {}).get("S")
    if not run_id or not names.is_run_id(run_id):
        return run_id, None
    run = dynamodb.get_item(
        TableName=names.RUNS_TABLE,
        Key={names.PARTITION_KEY: {"S": names.run_pk(run_id)}},
        ConsistentRead=True,
    ).get("Item")
    return run_id, (run or {}).get("state", {}).get("S")


def clear_pointer(dynamodb, run_id: str) -> None:
    """Delete the pointer if it still names run_id (a start since the read keeps its own)."""
    dynamodb.delete_item(
        TableName=names.RUNS_TABLE,
        Key=POINTER_KEY,
        ConditionExpression="attribute_not_exists(#pk) OR run_id = :run_id",
        ExpressionAttributeNames={"#pk": names.PARTITION_KEY},
        ExpressionAttributeValues={":run_id": {"S": run_id}},
    )


# ---------------------------------------------------------------------------
# Flagsmith
# ---------------------------------------------------------------------------
def reset_flags(admin: Admin, out=print) -> None:
    """The kill switch on, the default model current, the chaos flag off: each only when it
    differs, through a v2 version."""
    kill_switch = admin.find_feature(names.KILL_SWITCH_FEATURE)
    if kill_switch is None:
        out(f"{names.KILL_SWITCH_FEATURE} does not exist; run {names.BOOTSTRAP_FLAGSMITH_SCRIPT}")
    elif live_state(kill_switch)[0] is not True:
        admin.set_state(kill_switch, enabled=True)
        out(f"enabled {names.KILL_SWITCH_FEATURE}")
    else:
        out(f"{names.KILL_SWITCH_FEATURE} was already enabled")

    default = admin.find_feature(names.DEFAULT_MODEL_FEATURE)
    if default is None:
        out(f"{names.DEFAULT_MODEL_FEATURE} does not exist; run {names.BOOTSTRAP_FLAGSMITH_SCRIPT}")
    elif live_state(default)[1] != names.DEFAULT_MODEL_DEFAULT:
        admin.set_state(default, value=names.DEFAULT_MODEL_DEFAULT)
        out(f"set {names.DEFAULT_MODEL_FEATURE} to {names.DEFAULT_MODEL_DEFAULT}")
    else:
        out(f"{names.DEFAULT_MODEL_FEATURE} was already {names.DEFAULT_MODEL_DEFAULT}")

    chaos = admin.find_feature(names.CHAOS_FEATURE)
    if chaos is None:
        out(f"{names.CHAOS_FEATURE} does not exist, so it is off")
    elif live_state(chaos)[0] is not False:
        admin.set_state(chaos, enabled=False)
        out(f"turned {names.CHAOS_FEATURE} off")
    else:
        out(f"{names.CHAOS_FEATURE} was already off")


def running_experiments(admin: Admin) -> list[dict]:
    """Every running experiment, from every page of the unfiltered list. The status filter
    was never run, and 1.3's recorded schema spells the statuses otherwise, so the check is
    on each experiment's own status, as 1.4's end_status.py did (PR #27's review)."""
    running, path = [], admin.environment_path("experiments/")
    for _ in range(MAX_PAGES):
        reply = admin.request("GET", path) or {}
        running += [
            e
            for e in reply.get("results", [])
            if e.get("status") == names.EXPERIMENT_STATUS_RUNNING
        ]
        path = path_of(reply.get("next"))
        if path is None:
            return running
    raise FlagsmithError(f"the experiments list ran past {MAX_PAGES} pages")


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------
def reset(*, admin: Admin, dynamodb, force: bool = False, out=print) -> None:
    run_id, state = live_run(dynamodb)
    if run_id is not None and state is not None and state not in names.TERMINAL_STATES:
        if not force:
            raise Refused(
                f"the pointer names run {run_id}, which is {state}; finish or decline it, or"
                " pass --force"
            )
        out(f"--force: resetting while run {run_id} is {state}")

    if run_id is not None:
        clear_pointer(dynamodb, run_id)
        out(f"cleared the pointer (run {run_id})")
    else:
        out("no pointer to clear")
    try:
        wrote = set_tick(False)  # 2.4's: ValueError when the schedule is not rm-tick's
        out(
            f"disabled {names.TICK_SCHEDULE}"
            if wrote
            else f"{names.TICK_SCHEDULE} was already disabled"
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        out(f"no {names.TICK_SCHEDULE} schedule to disable")
    reset_flags(admin, out)

    running = running_experiments(admin)
    if not running:
        out("no experiment is running")
    for experiment in running:
        out(
            f"still running: experiment {experiment.get('id')} ({experiment.get('name')});"
            " end it in the Flagsmith dashboard"
        )


def main(
    argv: list[str] | None = None,
    *,
    admin: Admin | None = None,
    dynamodb=None,
    out=print,
) -> int:
    """The tick's Scheduler client is 2.4's own, made on first use in us-east-1."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--force", action="store_true", help="reset even while a run is live")
    args = parser.parse_args(argv)
    try:
        if admin is None or dynamodb is None:
            check_account()
            dynamodb = dynamodb or boto3.client("dynamodb", region_name=names.REGION)
            admin = admin or Admin(organisation_key())
        reset(admin=admin, dynamodb=dynamodb, force=args.force, out=out)
    except Refused as err:
        out(f"refused: {err}")
        return EXIT_REFUSED
    except (FlagsmithError, ClientError, ValueError) as err:  # ValueError: 2.4's tick check
        out(f"failed: {err}")
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
