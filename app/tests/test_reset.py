"""Tests for scripts/reset.py (task 3.5).

Flagsmith is app/tests/flagsmith_fake.py, rm-runs is app/tests/aws_fakes.FakeDynamo, and
the tick is botocore's Stubber on a real Scheduler client, which checks UpdateSchedule's
parameters against the service model.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber

from app.tests.aws_fakes import FakeDynamo, pointer, run_item
from app.tests.flagsmith_fake import DEV_KEY, FakeFlagsmith
from contracts import names
from scripts import bootstrap_flagsmith as bootstrap
from scripts import reset

ORG_KEY = "dummyOrganisationKeyForTests"
RUN_ID = "rresettest0601"
KILL, DEFAULT, CHAOS = names.KILL_SWITCH_FEATURE, names.DEFAULT_MODEL_FEATURE, names.CHAOS_FEATURE
TICK = {
    "Arn": f"arn:aws:scheduler:us-east-1:{names.ACCOUNT_ID}:schedule/default/{names.TICK_SCHEDULE}",
    "Name": names.TICK_SCHEDULE,
    "GroupName": names.TICK_SCHEDULE_GROUP,
    "ScheduleExpression": f"rate({names.TICK_INTERVAL_MINUTES} minutes)",
    "ScheduleExpressionTimezone": "UTC",
    "Description": "Wakes rm_agent every 2 minutes while a run is live.",
    "State": "ENABLED",
    "FlexibleTimeWindow": {"Mode": "OFF"},
    "ActionAfterCompletion": "NONE",
    "Target": {
        "Arn": f"arn:aws:lambda:us-east-1:{names.ACCOUNT_ID}:function:{names.INVOKER_FUNCTION}",
        "RoleArn": f"arn:aws:iam::{names.ACCOUNT_ID}:role/{names.SCHEDULER_ROLE}",
        "Input": json.dumps({"action": names.ACTION_TICK}),
    },
    "CreationDate": datetime(2026, 11, 26, 12, tzinfo=UTC),
    "LastModificationDate": datetime(2026, 11, 26, 12, tzinfo=UTC),
}


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    for name in [name for name in os.environ if name.startswith("AWS_") or name == "BOTO_CONFIG"]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing-secret")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BOTO_CONFIG", os.devnull)


@pytest.fixture
def flagsmith():
    """Everything left as a rolled-back, promoted, faulted run would leave it."""
    fake = FakeFlagsmith()
    fake.add_feature(KILL, enabled=False)
    fake.add_feature(DEFAULT, value=names.VARIANT_CHEAPER)
    fake.add_feature(CHAOS, enabled=True)
    fake.experiments.append({"id": 830, "name": "assistant_model_rlive0101", "status": "running"})
    fake.experiments.append({"id": 829, "name": "assistant_model_rold0101", "status": "completed"})
    return fake


@pytest.fixture
def admin(flagsmith):
    return bootstrap.Admin(ORG_KEY, transport=flagsmith)


@pytest.fixture
def scheduler():
    client = boto3.client("scheduler", region_name=names.REGION)
    with Stubber(client) as stubber:
        client.stubber = stubber
        yield client
        stubber.assert_no_pending_responses()


def _expect_tick(scheduler, schedule=TICK, *, disabled=True):
    get = {"Name": names.TICK_SCHEDULE, "GroupName": names.TICK_SCHEDULE_GROUP}
    scheduler.stubber.add_response("get_schedule", copy.deepcopy(schedule), expected_params=get)
    if disabled:
        written = {
            k: v
            for k, v in schedule.items()
            if k not in ("Arn", "CreationDate", "LastModificationDate")
        }
        written["State"] = "DISABLED"  # botocore adds ClientToken after the stub checks
        scheduler.stubber.add_response("update_schedule", {"ScheduleArn": TICK["Arn"]}, written)


def _run(admin, dynamodb, scheduler, *argv):
    lines = []
    code = reset.main(
        list(argv), admin=admin, dynamodb=dynamodb, scheduler=scheduler, out=lines.append
    )
    return code, "\n".join(lines)


def test_it_resets_everything_and_lists_the_running_experiment(admin, flagsmith, scheduler):
    dynamodb = FakeDynamo(pointer(RUN_ID))  # a seeded run: no run item
    _expect_tick(scheduler)

    code, text = _run(admin, dynamodb, scheduler)

    assert code == bootstrap.EXIT_OK, text
    assert names.ACTIVE_POINTER_PK not in dynamodb.items
    assert flagsmith.state(KILL)[0] is True
    assert flagsmith.state(DEFAULT) == (True, names.VARIANT_CURRENT)
    assert flagsmith.state(CHAOS)[0] is False
    assert "830" in text and "assistant_model_rlive0101" in text and "dashboard" in text
    assert "829" not in text  # ended already


def test_the_pointer_is_deleted_only_if_it_still_names_the_run_read(admin, scheduler):
    dynamodb = FakeDynamo(pointer(RUN_ID))
    _expect_tick(scheduler)
    _run(admin, dynamodb, scheduler)
    [(_, params)] = [c for c in dynamodb.calls if c[0] == "delete_item"]
    assert params["Key"] == {names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}}
    assert params["ExpressionAttributeValues"] == {":run_id": {"S": RUN_ID}}


def test_it_never_ends_an_experiment(admin, flagsmith, scheduler):
    _expect_tick(scheduler)
    _run(admin, FakeDynamo(), scheduler)
    assert not [
        p for _, p, _ in flagsmith.requests if p.endswith(names.FLAGSMITH_ADMIN_FORBIDDEN_PATHS)
    ]
    assert {e["status"] for e in flagsmith.experiments} == {"running", "completed"}


@pytest.mark.parametrize("state", sorted(set(names.STATES) - set(names.TERMINAL_STATES)))
def test_it_refuses_while_the_pointer_names_a_live_run(admin, flagsmith, scheduler, state):
    dynamodb = FakeDynamo(pointer(RUN_ID), run_item(RUN_ID, state))

    code, text = _run(admin, dynamodb, scheduler)

    assert code == bootstrap.EXIT_REFUSED
    assert RUN_ID in text and state in text and "--force" in text
    assert dynamodb.writes() == [] and flagsmith.writes() == []  # scheduler: no stubbed call


def test_force_resets_a_live_run_anyway(admin, flagsmith, scheduler):
    dynamodb = FakeDynamo(pointer(RUN_ID), run_item(RUN_ID, names.STATE_RUNNING))
    _expect_tick(scheduler)

    code, _ = _run(admin, dynamodb, scheduler, "--force")

    assert code == bootstrap.EXIT_OK
    assert names.ACTIVE_POINTER_PK not in dynamodb.items
    assert dynamodb.items[names.run_pk(RUN_ID)]["state"]["S"] == names.STATE_RUNNING  # untouched


@pytest.mark.parametrize("state", names.TERMINAL_STATES)
def test_a_pointer_left_by_a_finished_run_needs_no_force(admin, scheduler, state):
    dynamodb = FakeDynamo(pointer(RUN_ID), run_item(RUN_ID, state))
    _expect_tick(scheduler)
    assert _run(admin, dynamodb, scheduler)[0] == bootstrap.EXIT_OK


def test_a_second_reset_changes_nothing(admin, flagsmith, scheduler):
    _expect_tick(scheduler)
    _run(admin, FakeDynamo(), scheduler)
    before = list(flagsmith.writes())
    _expect_tick(scheduler, {**TICK, "State": "DISABLED"}, disabled=False)

    code, _ = _run(admin, FakeDynamo(), scheduler)

    assert code == bootstrap.EXIT_OK
    assert flagsmith.writes() == before  # every state was already the reset one


def test_a_missing_chaos_flag_is_already_off(admin, flagsmith, scheduler):
    del flagsmith.features[flagsmith.feature(CHAOS)["id"]]
    _expect_tick(scheduler)
    code, text = _run(admin, FakeDynamo(), scheduler)
    assert code == bootstrap.EXIT_OK and CHAOS in text


def test_a_schedule_that_is_not_the_tick_is_never_written(admin, scheduler):
    wrong = copy.deepcopy(TICK)
    wrong["Target"]["Input"] = json.dumps({"action": "start"})
    _expect_tick(scheduler, wrong, disabled=False)

    code, text = _run(admin, FakeDynamo(), scheduler)

    assert code == bootstrap.EXIT_FAILED and names.TICK_SCHEDULE in text


def test_the_written_fields_are_those_get_schedule_returns_and_update_schedule_takes():
    client = boto3.client("scheduler", region_name=names.REGION)
    fields = set(reset.tick_update_fields(client))
    assert {
        "Name",
        "GroupName",
        "ScheduleExpression",
        "Target",
        "FlexibleTimeWindow",
        "State",
    } <= fields
    assert not fields & {"Arn", "CreationDate", "LastModificationDate"}


def test_nothing_printed_holds_a_key(admin, scheduler):
    _expect_tick(scheduler)
    _, text = _run(admin, FakeDynamo(pointer(RUN_ID)), scheduler)
    assert ORG_KEY not in text and DEV_KEY not in text
