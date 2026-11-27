"""The action handlers (tasks.md 2.5; design.md sections 6 and 7).

The state store is 2.1's, over moto DynamoDB, so every claim, takeover and commit is real.
The notify and report tools are 2.4's, over moto SNS and S3, so dedupe keys and reports are
real too. Everything else is a fake that records into one ordered log: Flagsmith, the alarm,
the probe, the tick schedule, the planner and the model's prose. No AWS, Bedrock or Flagsmith
call is made: dummy credentials, the AWS config files at /dev/null.
"""

from __future__ import annotations

import copy
import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from rm_agent import handlers, planner, state
from rm_agent.tools import flagsmith, notify, report, signals

import contracts
from contracts import names

START = datetime(2026, 11, 27, 15, 30, 5, tzinfo=UTC)
RUN_ID = names.run_id_for(START)
EXPERIMENT_ID = 4321
METRIC_IDS = {name: 858 + n for n, name in enumerate(names.METRICS)}
THRESHOLD_MS = 10_000.0
PROSE = "The cheaper model answered as helpfully, for a quarter of the cost."
PROMPT = "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5."
EMBARGO = names.RESULTS_EMBARGO_SECONDS
STALE = names.PENDING_STALE_SECONDS

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


def make_plan() -> dict:
    """A plan as 2.3's planner makes one: the model's two fields, every other a constant."""
    plan = {
        "hypothesis": "Sonnet 5 answers support questions as helpfully as Fable 5.1, for less.",
        "summary": "Split identities 50/50 between Fable 5.1 and Sonnet 5 and compare.",
        "current_model": names.MODEL_CURRENT,
        "cheaper_model": names.MODEL_CHEAPER,
        "primary_metric": names.PRIMARY_METRIC,
        "guardrail_metrics": list(names.GUARDRAIL_METRICS),
        "split": {
            names.VARIANT_CURRENT: 100 - names.TREATMENT_PERCENT,
            names.VARIANT_CHEAPER: names.TREATMENT_PERCENT,
        },
        "min_identities_per_variation": names.MIN_IDENTITIES_PER_VARIATION,
        "stop_rules": {
            "rollback_alarm": names.ALARM_NAME,
            "results_embargo_seconds": names.RESULTS_EMBARGO_SECONDS,
            "promote_requires_experiment_ended": True,
        },
        "guardrails": {
            "max_latency_ratio": names.GUARDRAIL_MAX_LATENCY_RATIO,
            "max_cost_ratio": names.GUARDRAIL_MAX_COST_RATIO,
        },
        "recommendation_rule": {
            "after_incident": names.RECOMMEND_HOLD_RERUN,
            "promote_min_helpful_lift_ci_lower": names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER,
        },
    }
    contracts.validate("plan", plan)
    return plan


def make_results(*, ready: bool = True) -> dict:
    def variation(latency: float, cost: float) -> dict:
        return {
            "identities": 75 if ready else 20,
            "helpful_rate": 0.8,
            "mean_latency_ms": latency,
            "mean_cost_micro_usd": cost,
        }

    results = {
        "ready": ready,
        "variations": {
            names.VARIANT_CURRENT: variation(1000.0, 2000.0),
            names.VARIANT_CHEAPER: variation(900.0, 500.0),
        },
        "helpful_lift_ci_lower": -0.03,
    }
    contracts.validate("results", results)
    return results


# ---------------------------------------------------------------------------
# The world: moto for state, notify and report; fakes for the rest, one ordered log
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Spy:
    """A client that records the calls of each operation and passes them on."""

    def __init__(self, client) -> None:
        self._client = client
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        real = getattr(self._client, name)
        if not callable(real) or name.startswith("_"):
            return real

        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            return real(*args, **kwargs)

        return call


class FakeFlagsmith:
    """2.2's Flagsmith, as the handlers use it. fail[name] raises once; on[name] runs a hook
    after the call is logged."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.status = names.EXPERIMENT_STATUS_ENDED
        self.results = make_results()
        self.not_current = False
        self.fail: dict[str, Exception] = {}
        self.on: dict[str, object] = {}

    def _call(self, name: str, *args) -> None:
        self.world.log.append((name, *args))
        hook = self.on.pop(name, None)
        if hook is not None:
            hook()
        if name in self.fail:
            raise self.fail.pop(name)

    def ensure_permanent_features(self):
        self._call("ensure_permanent_features")
        return {names.KILL_SWITCH_FEATURE: 1, names.DEFAULT_MODEL_FEATURE: 2}

    def set_kill_switch(self, enabled: bool):
        self._call("set_kill_switch", enabled)
        return {"changed": True}

    def ensure_experiment(self, run_id: str, plan: dict):
        self._call("ensure_experiment", run_id)
        return {
            "feature_key": names.experiment_feature_key(run_id),
            "experiment_id": EXPERIMENT_ID,
            "metric_ids": dict(METRIC_IDS),
        }

    def start_experiment(self, experiment_id: int):
        self._call("start_experiment", experiment_id)
        return {"started": True}

    def refresh_results(self, experiment_id: int, last_refresh_at: str | None):
        self._call("refresh_results", experiment_id, last_refresh_at)
        now = self.world.clock()
        if last_refresh_at and now - datetime.fromisoformat(last_refresh_at) < timedelta(
            seconds=names.RESULTS_REFRESH_MIN_SECONDS
        ):
            return None
        return names.utc_iso(now)

    def get_results(self, experiment_id: int, metric_ids: dict, refreshed_at: str | None = None):
        self._call("get_results", experiment_id, refreshed_at)
        assert metric_ids == METRIC_IDS
        if self.not_current:
            raise flagsmith.ResultsNotCurrent("not yet")
        return copy.deepcopy(self.results)

    def experiment_status(self, experiment_id: int) -> str:
        self._call("experiment_status", experiment_id)
        return self.status

    def set_default_model(self, value: str):
        self._call("set_default_model", value)
        return {"changed": True}


class World:
    def __init__(self, clock: Clock, dynamodb, sns: Spy, s3) -> None:
        self.clock, self.dynamodb, self.sns, self.s3 = clock, dynamodb, sns, s3
        self.log: list[tuple] = []
        self.alarm: str | Exception = names.ALARM_STATE_OK
        self.probe: dict | Exception = {"passed": True, "max_ms": 900.0, "reason": "ok"}
        self.prose: str | Exception = PROSE
        self.plan: dict | Exception = make_plan()
        self.requests: list[dict] = []
        self.flagsmith = FakeFlagsmith(self)
        self.tools = handlers.Tools(
            flagsmith=self.flagsmith,
            alarm_state=self._alarm_state,
            probe_cheaper=self._probe_cheaper,
            set_tick=self._set_tick,
            notify=self._notify,
            write_report=self._write_report,
            make_plan=self._make_plan,
            converse=self._converse,
        )

    # -- the fakes ---------------------------------------------------------------
    def _alarm_state(self, name: str):
        self.log.append(("alarm_state",))
        assert name == names.ALARM_NAME
        if isinstance(self.alarm, Exception):
            raise self.alarm
        return signals.Alarm(self.alarm, THRESHOLD_MS)

    def _probe_cheaper(self, threshold_ms: float):
        self.log.append(("probe",))
        assert threshold_ms == THRESHOLD_MS  # from the same DescribeAlarms read
        if isinstance(self.probe, Exception):
            raise self.probe
        return dict(self.probe)

    def _set_tick(self, enabled: bool) -> bool:
        self.log.append(("set_tick", enabled))
        return True

    def _notify(self, run_id: str, subject: str, body: str, dedupe_key: str) -> str:
        self.log.append(("notify", dedupe_key))
        return notify.notify(run_id, subject, body, dedupe_key)

    def _write_report(self, run, results, prose=None) -> str:
        self.log.append(("write_report",))
        return report.write_report(run, results, prose)

    def _make_plan(self, prompt: str) -> dict:
        self.log.append(("make_plan",))
        if isinstance(self.plan, Exception):
            raise self.plan
        return copy.deepcopy(self.plan)

    def _converse(self, request: dict) -> str:
        self.log.append(("converse",))
        for forbidden in names.FORBIDDEN_SAMPLING_PARAMETERS:
            assert forbidden not in str(request)
        assert request["modelId"] == names.MODEL_AGENT
        self.requests.append(request)
        if isinstance(self.prose, Exception):
            raise self.prose
        return self.prose

    # -- driving -----------------------------------------------------------------
    def handle(self, payload: dict) -> dict:
        contracts.validate("invoke", payload)
        response = handlers.handle(payload, self.tools)
        contracts.validate("response", response)
        return response

    def start(self) -> dict:
        return self.handle({"action": "start", "prompt": PROMPT, "actor": "engineer"})

    def engineer(self, action: str, run_id: str = RUN_ID) -> dict:
        return self.handle({"action": action, "run_id": run_id, "actor": "engineer"})

    def tick(self) -> dict:
        return self.handle({"action": "tick"})

    def status(self, run_id: str | None = None) -> dict:
        return self.handle({"action": "status", **({"run_id": run_id} if run_id else {})})

    def alarm_event(self, at: datetime | None = None) -> dict:
        return self.handle(
            {
                "action": "alarm",
                "alarm_name": names.ALARM_NAME,
                "alarm_state": names.ALARM_STATE_ALARM,
                "at": names.utc_iso(at or self.clock()),
            }
        )

    # -- reading -----------------------------------------------------------------
    def run(self, run_id: str = RUN_ID) -> dict:
        item = state.load_run(run_id).item
        contracts.validate("run_item", item)
        return item

    def pointer(self):
        return state.get_active()

    def publishes(self) -> list[dict]:
        return [kwargs for name, kwargs in self.sns.calls if name == "publish"]

    def report_body(self, run_id: str = RUN_ID) -> str:
        got = self.s3.get_object(Bucket=names.REPORTS_BUCKET, Key=names.report_key(run_id))
        return got["Body"].read().decode()

    def cleared(self) -> list[tuple]:
        done, self.log[:] = list(self.log), []
        return done

    # -- reaching a state ------------------------------------------------------------
    def to(self, target: str) -> None:
        """Drive a fresh run to target along the table's main path, then clear the log."""
        self.start()
        assert self.run()["state"] == names.STATE_PLANNED
        if target != names.STATE_PLANNED:
            assert self.engineer("approve")["state"] == names.STATE_RUNNING
            self.clock.advance(1)
        if target in (names.STATE_ROLLED_BACK, names.STATE_AWAITING_RESUME):
            assert self.alarm_event()["state"] == names.STATE_ROLLED_BACK
            self.clock.advance(1)
        if target == names.STATE_AWAITING_RESUME:
            assert self.tick()["state"] == names.STATE_AWAITING_RESUME
            self.clock.advance(1)
        if target == names.STATE_RESULTS_READY:
            self.clock.advance(EMBARGO + 1)
            assert self.tick()["state"] == names.STATE_RESULTS_READY
            self.clock.advance(1)
        assert self.run()["state"] == target
        self.log.clear()


@pytest.fixture
def world(monkeypatch):
    for name in _AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", names.REGION)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("MOTO_ACCOUNT_ID", names.ACCOUNT_ID)
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=names.REGION)
        dynamodb.create_table(
            TableName=names.RUNS_TABLE,
            KeySchema=[{"AttributeName": names.PARTITION_KEY, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": names.PARTITION_KEY, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sns = Spy(boto3.client("sns", region_name=names.REGION))
        sns.create_topic(Name=names.INCIDENTS_TOPIC)
        sns.calls.clear()
        s3 = boto3.client("s3", region_name=names.REGION)
        s3.create_bucket(Bucket=names.REPORTS_BUCKET)
        clock = Clock(START)
        monkeypatch.setattr(state, "_client", lambda: dynamodb)
        monkeypatch.setattr(notify, "_sns", lambda: sns)
        monkeypatch.setattr(notify, "_dynamodb", lambda: dynamodb)
        monkeypatch.setattr(report, "_client", lambda: s3)
        for module in (state, handlers, report):
            monkeypatch.setattr(module, "_now", clock)
        yield World(clock, dynamodb, sns, s3)


def drop_pointer(world: World) -> None:
    world.dynamodb.delete_item(
        TableName=names.RUNS_TABLE, Key={names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}}
    )


def put_pointer(world: World, run_id: str) -> None:
    world.dynamodb.put_item(
        TableName=names.RUNS_TABLE,
        Item={names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}, "run_id": {"S": run_id}},
    )


def failed(code: str = "ThrottlingException", operation: str = "DescribeAlarms") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


def error_code(response: dict) -> str | None:
    return (response["error"] or {}).get("code")


def history(world: World) -> list[tuple]:
    return [(e["from"], e["to"], e["actor"], e["reason"]) for e in world.run()["history"]]


# ---------------------------------------------------------------------------
# One test per row of the transition table, with its effects in order
# ---------------------------------------------------------------------------
ROLLBACK_EFFECTS = [("set_kill_switch", False), ("converse",), ("notify", "{key}")]
ROWS = {
    (None, names.STATE_PLANNED): (None, "start", [("make_plan",)]),
    (names.STATE_PLANNED, names.STATE_RUNNING): (
        names.STATE_PLANNED,
        "approve",
        [
            ("set_tick", True),
            ("ensure_permanent_features",),
            ("set_kill_switch", True),
            ("ensure_experiment", RUN_ID),
            ("start_experiment", EXPERIMENT_ID),
        ],
    ),
    (names.STATE_PLANNED, names.STATE_ABORTED): (
        names.STATE_PLANNED,
        "decline",
        [("set_tick", False)],
    ),
    (names.STATE_RUNNING, names.STATE_ROLLED_BACK): (
        names.STATE_RUNNING,
        "alarm",
        ROLLBACK_EFFECTS,
    ),
    (names.STATE_RUNNING, names.STATE_RESULTS_READY): (
        names.STATE_RUNNING,
        "tick",
        [
            ("alarm_state",),
            ("refresh_results", EXPERIMENT_ID, None),
            ("get_results", EXPERIMENT_ID, "{now}"),
            ("converse",),
            ("write_report",),
            ("notify", "{key}"),
        ],
    ),
    (names.STATE_RUNNING, names.STATE_HELD): (
        names.STATE_RUNNING,
        "decline",
        [("set_tick", False)],
    ),
    (names.STATE_ROLLED_BACK, names.STATE_AWAITING_RESUME): (
        names.STATE_ROLLED_BACK,
        "tick",
        [("alarm_state",), ("probe",), ("notify", "{key}")],
    ),
    (names.STATE_ROLLED_BACK, names.STATE_HELD): (
        names.STATE_ROLLED_BACK,
        "decline",
        [("set_tick", False)],
    ),
    (names.STATE_AWAITING_RESUME, names.STATE_RUNNING): (
        names.STATE_AWAITING_RESUME,
        "approve",
        [("alarm_state",), ("probe",), ("set_kill_switch", True)],
    ),
    (names.STATE_AWAITING_RESUME, names.STATE_ROLLED_BACK): (
        names.STATE_AWAITING_RESUME,
        "approve-failing-probe",
        [("alarm_state",), ("probe",), ("notify", "{key}")],
    ),
    (names.STATE_AWAITING_RESUME, names.STATE_HELD): (
        names.STATE_AWAITING_RESUME,
        "decline",
        [("set_tick", False)],
    ),
    (names.STATE_RESULTS_READY, names.STATE_ROLLED_BACK): (
        names.STATE_RESULTS_READY,
        "alarm",
        ROLLBACK_EFFECTS,
    ),
    (names.STATE_RESULTS_READY, names.STATE_PROMOTED): (
        names.STATE_RESULTS_READY,
        "approve",
        [
            ("alarm_state",),
            ("experiment_status", EXPERIMENT_ID),
            ("set_default_model", names.VARIANT_CHEAPER),
            ("set_tick", False),
        ],
    ),
    (names.STATE_RESULTS_READY, names.STATE_HELD): (
        names.STATE_RESULTS_READY,
        "decline",
        [("set_tick", False)],
    ),
}


def test_the_rows_are_the_transition_table():
    assert set(ROWS) == {(t.source, t.target) for t in names.TRANSITIONS}


@pytest.mark.parametrize("source, target", list(ROWS), ids=[f"{s or 'none'}->{t}" for s, t in ROWS])
def test_each_row_runs_its_effects_in_order_and_commits_once(world, source, target):
    _, trigger, expected = ROWS[(source, target)]
    if source is not None:
        world.to(source)
    before = len(world.run()["history"]) if source else 0
    if trigger == "start":
        response = world.start()
    elif trigger == "alarm":
        response = world.alarm_event()
    elif trigger == "tick":
        if source == names.STATE_RUNNING:
            world.clock.advance(EMBARGO + 1)
        response = world.tick()
    elif trigger == "approve-failing-probe":
        world.probe = {"passed": False, "max_ms": 12_000.0, "reason": "slow"}
        response = world.engineer("approve")
    else:
        response = world.engineer(trigger)
    key = names.dedupe_key(before, target) if before else None  # start has no history yet
    now = names.utc_iso(world.clock())
    wanted = [
        tuple(part.format(key=key, now=now) if isinstance(part, str) else part for part in step)
        for step in expected
    ]
    assert world.cleared() == wanted
    assert response["state"] == target and response["error"] is None
    item = world.run()
    assert item["state"] == target and "pending" not in item
    row = next(t for t in names.TRANSITIONS if (t.source, t.target) == (source, target))
    assert len(item["history"]) == before + 1
    last = item["history"][-1]
    assert (last["from"], last["to"], last["actor"]) == (source, target, row.actor)
    if target in names.TERMINAL_STATES:
        assert world.pointer() is None


def test_the_launch_records_the_experiment_and_the_pointers_feature_key(world):
    world.to(names.STATE_RUNNING)
    item = world.run()
    assert item["feature_key"] == names.experiment_feature_key(RUN_ID)
    assert item["experiment_id"] == EXPERIMENT_ID and item["metric_ids"] == METRIC_IDS
    assert item["first_running_at"] == names.utc_iso(START)
    assert world.pointer() == state.Pointer(RUN_ID, names.experiment_feature_key(RUN_ID))


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
def test_the_approval_message_shows_the_injected_plan_values(world):
    message = world.start()["message"]
    plan = make_plan()
    for value in (
        plan["summary"],
        plan["hypothesis"],
        f"{plan['split'][names.VARIANT_CURRENT]} percent",
        plan["current_model"],
        plan["cheaper_model"],
        f"{plan['min_identities_per_variation']} identities",
        plan["stop_rules"]["rollback_alarm"],
        f"{plan['stop_rules']['results_embargo_seconds']} seconds",
        f"{round(plan['guardrails']['max_latency_ratio'] * 100)} percent",
        plan["primary_metric"],
        *plan["guardrail_metrics"],
        plan["recommendation_rule"]["after_incident"],
        f"{round(plan['recommendation_rule']['promote_min_helpful_lift_ci_lower'] * 100)} percent",
        f"marshal approve {RUN_ID} {names.AWAITS_LAUNCH}",
        f"marshal decline {RUN_ID}",
        f"{round(plan['guardrails']['max_cost_ratio'] * 100)} percent",
        f"{plan['split'][names.VARIANT_CHEAPER]} percent {names.VARIANT_CHEAPER}",
        "ended in",
    ):
        assert value in message, value


def test_start_while_a_run_is_active_is_run_active_and_calls_no_model(world):
    world.start()
    world.log.clear()
    world.clock.advance(60)
    response = world.start()
    assert error_code(response) == names.ERROR_RUN_ACTIVE and RUN_ID in response["error"]["message"]
    assert world.log == []


def test_a_planner_model_error_on_start_is_upstream_failed_and_creates_nothing(world):
    world.plan = planner.UpstreamFailed("the model call failed")
    response = world.start()
    assert error_code(response) == names.ERROR_UPSTREAM_FAILED
    assert world.pointer() is None
    with pytest.raises(state.NotFound):
        state.load_run(RUN_ID)


def test_a_prompt_the_planner_refuses_is_bad_request(world):
    world.plan = planner.BadRequest("no model move")
    assert error_code(world.start()) == names.ERROR_BAD_REQUEST
    assert world.pointer() is None


def test_start_decline_start_in_one_minute_is_bad_request(world):
    world.start()
    world.engineer("decline")
    world.clock.advance(20)
    response = world.start()
    assert error_code(response) == names.ERROR_BAD_REQUEST
    assert "next minute" in response["error"]["message"]
    assert world.run()["state"] == names.STATE_ABORTED


def test_a_create_collision_with_no_pointer_is_upstream_failed(world, monkeypatch):
    collision = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
            "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )

    def collide(prompt, plan):
        raise collision

    monkeypatch.setattr(state, "create_run", collide)
    assert error_code(world.start()) == names.ERROR_UPSTREAM_FAILED


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_with_no_active_run_returns_nulls(world):
    response = world.status()
    assert response == {
        "run_id": None,
        "state": None,
        "message": response["message"],
        "awaiting": None,
        "pending": None,
        "error": None,
    }
    assert response["message"].startswith(names.NO_ACTIVE_RUN_MESSAGE)


def test_status_returns_the_run_without_changing_it(world):
    world.to(names.STATE_RUNNING)
    before = world.run()
    for response in (world.status(), world.status(RUN_ID)):
        assert (response["run_id"], response["state"], response["error"]) == (
            RUN_ID,
            names.STATE_RUNNING,
            None,
        )
    assert world.run() == before and world.log == []


def test_an_unknown_run_id_is_not_found(world):
    assert error_code(world.status("r202611271599")) == names.ERROR_NOT_FOUND
    for action in ("approve", "decline", "check"):
        assert error_code(world.engineer(action, "r202611271599")) == names.ERROR_NOT_FOUND
    assert world.log == []


@pytest.mark.parametrize("action", ["status", "tick", "alarm"])
def test_a_pointer_whose_run_item_is_missing_is_a_no_op(world, action):
    put_pointer(world, "r202611271599")
    if action == "status":
        response = world.status()
    elif action == "tick":
        response = world.tick()
    else:
        response = world.alarm_event()
    assert response["error"] is None and response["run_id"] is None
    assert response["message"].startswith(names.NO_ACTIVE_RUN_MESSAGE)
    assert "r202611271599" in response["message"] and names.RESET_SCRIPT in response["message"]
    assert world.log == []


def test_engineer_actions_on_a_pointer_with_no_run_item_are_not_found(world):
    put_pointer(world, "r202611271599")
    for action in ("approve", "decline", "check"):
        assert error_code(world.engineer(action, "r202611271599")) == names.ERROR_NOT_FOUND
    assert error_code(world.start()) == names.ERROR_RUN_ACTIVE


# ---------------------------------------------------------------------------
# Legality and the pointer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "source, action",
    [
        (names.STATE_RUNNING, "approve"),
        (names.STATE_ROLLED_BACK, "approve"),
        (names.STATE_HELD, "approve"),
        (names.STATE_HELD, "decline"),
        (names.STATE_HELD, "check"),
        (names.STATE_ABORTED, "check"),
        (names.STATE_PROMOTED, "decline"),
    ],
)
def test_an_engineer_action_not_legal_in_the_state_is_illegal_with_no_effect(world, source, action):
    if source == names.STATE_HELD:
        world.to(names.STATE_RUNNING)
        world.engineer("decline")
    elif source == names.STATE_ABORTED:
        world.to(names.STATE_PLANNED)
        world.engineer("decline")
    elif source == names.STATE_PROMOTED:
        world.to(names.STATE_RESULTS_READY)
        world.engineer("approve")
    else:
        world.to(source)
    world.log.clear()
    before = world.run()
    response = world.engineer(action)
    assert error_code(response) == names.ERROR_ILLEGAL_IN_STATE
    assert world.run() == before and world.log == []


@pytest.mark.parametrize("action", ["approve", "decline", "check"])
def test_an_engineer_action_on_a_run_the_pointer_does_not_name_is_run_active(world, action):
    world.to(names.STATE_RUNNING)
    response = world.engineer(action, "r202611271529")
    assert error_code(response) == names.ERROR_RUN_ACTIVE and RUN_ID in response["error"]["message"]
    assert world.log == []


@pytest.mark.parametrize("action", ["approve", "check"])
def test_approve_or_check_on_a_planned_run_with_no_pointer_is_illegal(world, action):
    world.to(names.STATE_PLANNED)
    drop_pointer(world)
    before = world.run()
    response = world.engineer(action)
    assert error_code(response) == names.ERROR_ILLEGAL_IN_STATE
    assert "start a new one" in response["error"]["message"]
    assert f"marshal decline {RUN_ID}" in response["error"]["message"]
    assert world.run() == before and world.log == [] and world.pointer() is None


def test_status_of_a_reset_planned_run_offers_no_approve(world):
    world.to(names.STATE_PLANNED)
    drop_pointer(world)
    message = world.status(RUN_ID)["message"]
    assert "marshal approve" not in message and f"marshal decline {RUN_ID}" in message
    put_pointer(world, "r202611271599")  # a run started since
    message = world.status(RUN_ID)["message"]
    assert "marshal approve" not in message and f"marshal decline {RUN_ID}" in message


def test_decline_ends_a_planned_run_with_no_pointer(world):
    world.to(names.STATE_PLANNED)
    drop_pointer(world)
    assert world.engineer("decline")["state"] == names.STATE_ABORTED
    assert world.log == [("set_tick", False)]


def test_decline_of_a_planned_run_with_no_pointer_and_a_launch_pending_is_pending(world):
    world.to(names.STATE_PLANNED)
    state.claim(RUN_ID, names.STATE_PLANNED, names.STATE_RUNNING)
    drop_pointer(world)
    world.clock.advance(STALE + 60)  # even a stale launch: decline never takes it over
    response = world.engineer("decline")
    assert error_code(response) == names.ERROR_TRANSITION_PENDING
    assert "start a new one" in response["error"]["message"]
    assert "pending for" in response["error"]["message"]  # with its age
    assert world.run()["state"] == names.STATE_PLANNED and world.log == []


def test_a_decline_in_flight_on_a_reset_run_answers_with_its_age_and_decline(world):
    world.to(names.STATE_PLANNED)
    state.claim(RUN_ID, names.STATE_PLANNED, names.STATE_ABORTED)  # another decline
    drop_pointer(world)
    world.clock.advance(42)
    response = world.engineer("decline")
    assert error_code(response) == names.ERROR_TRANSITION_PENDING
    message = response["error"]["message"]
    assert "reset" in message and "pending for 42 s" in message
    assert f"marshal decline {RUN_ID}" in message and "marshal check" not in message
    assert world.log == []


def test_a_repeat_decline_resumes_its_own_stalled_decline_with_no_pointer(world):
    """Only a pending launch keeps decline from ending a reset run."""
    world.to(names.STATE_PLANNED)
    state.claim(RUN_ID, names.STATE_PLANNED, names.STATE_ABORTED)  # a decline that died
    drop_pointer(world)
    world.clock.advance(STALE + 1)
    assert world.engineer("decline")["state"] == names.STATE_ABORTED
    assert world.log == [("set_tick", False)]


def test_decline_of_a_planned_run_touches_nothing_in_flagsmith(world):
    world.to(names.STATE_PLANNED)
    world.engineer("decline")
    assert world.log == [("set_tick", False)]


def test_decline_from_rolled_back_leaves_the_kill_switch_off(world):
    world.to(names.STATE_ROLLED_BACK)
    assert world.engineer("decline")["state"] == names.STATE_HELD
    assert ("set_kill_switch", True) not in world.log
    assert world.log == [("set_tick", False)]


@pytest.mark.parametrize(
    "source, how",
    [
        (names.STATE_PLANNED, "decline"),
        (names.STATE_RUNNING, "decline"),
        (names.STATE_ROLLED_BACK, "decline"),
        (names.STATE_AWAITING_RESUME, "decline"),
        (names.STATE_RESULTS_READY, "decline"),
        (names.STATE_RESULTS_READY, "approve"),
    ],
)
def test_every_terminal_state_clears_the_pointer_and_disables_the_tick(world, source, how):
    world.to(source)
    response = world.engineer(how)
    assert response["state"] in names.TERMINAL_STATES
    assert world.pointer() is None
    assert world.log[-1] == ("set_tick", False)


# ---------------------------------------------------------------------------
# alarm
# ---------------------------------------------------------------------------
def test_alarm_and_tick_with_no_active_run_are_no_ops(world):
    for response in (world.alarm_event(), world.tick()):
        assert response["error"] is None and response["run_id"] is None
        assert response["message"].startswith(names.NO_ACTIVE_RUN_MESSAGE)
    assert world.log == []


@pytest.mark.parametrize(
    "source", [names.STATE_PLANNED, names.STATE_ROLLED_BACK, names.STATE_AWAITING_RESUME]
)
def test_an_alarm_with_nothing_to_do_is_a_no_op(world, source):
    world.to(source)
    before = world.run()
    response = world.alarm_event()
    assert response["error"] is None and response["state"] == source
    assert world.run() == before and world.log == []


def test_alarm_twice_rolls_back_and_notifies_once(world):
    world.to(names.STATE_RUNNING)
    world.alarm_event()
    world.clock.advance(5)
    second = world.alarm_event()
    assert second["state"] == names.STATE_ROLLED_BACK and second["error"] is None
    assert len(world.publishes()) == 1
    assert len(world.run()["incidents"]) == 1


def test_an_alarm_stamped_before_the_latest_entry_into_running_is_a_no_op(world):
    world.to(names.STATE_AWAITING_RESUME)
    world.clock.advance(30)
    assert world.engineer("approve")["state"] == names.STATE_RUNNING
    resumed_at = world.run()["history"][-1]["at"]
    world.log.clear()
    late = world.alarm_event(at=datetime.fromisoformat(resumed_at) - timedelta(seconds=1))
    assert late["state"] == names.STATE_RUNNING and late["error"] is None
    assert world.log == []
    assert world.alarm_event()["state"] == names.STATE_ROLLED_BACK  # a fresh one still acts


def test_the_rollback_note_is_the_models_with_the_facts_and_survives_a_model_failure(world):
    world.to(names.STATE_RUNNING)
    world.alarm_event()
    (first,) = world.publishes()
    assert PROSE in first["Message"] and RUN_ID in first["Message"]
    assert names.KILL_SWITCH_FEATURE in first["Message"]
    # A model failure does not stop a rollback: the note goes out with the facts alone.
    world.clock.advance(1)
    world.tick()  # probe passes: AWAITING_RESUME
    world.clock.advance(1)
    world.engineer("approve")
    world.clock.advance(1)
    world.prose = planner.UpstreamFailed("the model call failed")
    assert world.alarm_event()["state"] == names.STATE_ROLLED_BACK
    second = world.publishes()[-1]
    assert RUN_ID in second["Message"] and PROSE not in second["Message"]


# ---------------------------------------------------------------------------
# tick and check
# ---------------------------------------------------------------------------
def test_tick_in_running_checks_the_alarm_before_results(world):
    world.to(names.STATE_RUNNING)
    world.clock.advance(EMBARGO + 1)
    world.alarm = names.ALARM_STATE_ALARM
    response = world.tick()
    assert response["state"] == names.STATE_ROLLED_BACK
    assert world.log[0] == ("alarm_state",)
    assert not [step for step in world.log if step[0] in ("refresh_results", "get_results")]


def test_tick_in_running_inside_the_embargo_does_not_read_results(world):
    world.to(names.STATE_RUNNING)
    first = datetime.fromisoformat(world.run()["first_running_at"])
    world.clock.now = first + timedelta(seconds=EMBARGO - 1)
    response = world.tick()
    assert response["state"] == names.STATE_RUNNING and response["error"] is None
    assert world.log == [("alarm_state",)]
    world.clock.now = first + timedelta(seconds=EMBARGO)  # the first 600 s are over
    world.tick()
    assert ("refresh_results", EXPERIMENT_ID, None) in world.log


def test_a_tick_that_refreshes_without_moving_records_last_refresh_at(world):
    world.to(names.STATE_RUNNING)
    world.clock.advance(EMBARGO + 1)
    world.flagsmith.results = make_results(ready=False)
    assert world.tick()["state"] == names.STATE_RUNNING
    first = names.utc_iso(world.clock())
    assert world.run()["last_refresh_at"] == first
    world.clock.advance(120)  # inside the 5 minute window: the last results, no refresh
    world.log.clear()
    world.tick()
    assert ("refresh_results", EXPERIMENT_ID, first) in world.log
    assert ("get_results", EXPERIMENT_ID, first) in world.log  # still checked against it
    assert world.run()["last_refresh_at"] == first


def test_results_not_current_are_not_ready_not_a_failure(world):
    world.to(names.STATE_RUNNING)
    world.clock.advance(EMBARGO + 1)
    world.flagsmith.not_current = True
    response = world.tick()
    assert response["state"] == names.STATE_RUNNING and response["error"] is None
    assert "pending" not in world.run()


def test_a_report_prose_failure_in_the_guard_is_upstream_failed_with_no_claim(world):
    world.to(names.STATE_RUNNING)
    world.clock.advance(EMBARGO + 1)
    world.prose = planner.UpstreamFailed("the model call failed")
    response = world.tick()
    assert error_code(response) == names.ERROR_UPSTREAM_FAILED
    assert world.run()["state"] == names.STATE_RUNNING and "pending" not in world.run()
    assert ("write_report",) not in world.log


def test_the_report_carries_the_prose_written_before_the_claim(world):
    world.to(names.STATE_RESULTS_READY)
    body = world.report_body()  # what SNS sends starts with the same report
    assert PROSE in body and f"**{names.RECOMMEND_PROMOTE}**" in body
    assert world.run()["report_key"] == names.report_key(RUN_ID)
    (published,) = [p for p in world.publishes() if names.report_key(RUN_ID) in p["Message"]]
    message = published["Message"]
    assert message.startswith(body.rstrip("\n").split("\n")[0])  # the report's title
    for section in names.REPORT_SECTIONS:
        assert f"\n## {section}\n" in message, section
    assert PROSE in message
    tail = message.split("## Time taken", 1)[1]
    assert f"marshal approve {RUN_ID} {names.AWAITS_PROMOTION}" in tail
    assert f"marshal decline {RUN_ID}" in tail
    assert tail.rstrip().endswith(f"s3://{names.REPORTS_BUCKET}/{names.report_key(RUN_ID)}")


def test_tick_in_rolled_back_with_ready_results_does_not_reach_results_ready(world):
    world.to(names.STATE_ROLLED_BACK)
    world.clock.advance(EMBARGO + 1)
    world.probe = {"passed": False, "max_ms": 12_000.0, "reason": "slow"}
    response = world.tick()
    assert response["state"] == names.STATE_ROLLED_BACK
    assert not [step for step in world.log if step[0] in ("refresh_results", "get_results")]


def test_tick_in_rolled_back_with_a_failing_probe_changes_nothing(world):
    world.to(names.STATE_ROLLED_BACK)
    before = world.run()
    world.probe = {"passed": False, "max_ms": 12_000.0, "reason": "slow"}
    response = world.tick()
    assert response["state"] == names.STATE_ROLLED_BACK and response["pending"] is None
    assert world.run() == before


def test_tick_in_rolled_back_with_the_alarm_in_alarm_skips_the_probe(world):
    world.to(names.STATE_ROLLED_BACK)
    world.alarm = names.ALARM_STATE_ALARM
    assert world.tick()["state"] == names.STATE_ROLLED_BACK
    assert world.log == [("alarm_state",)]


def test_a_failed_alarm_read_in_rolled_back_counts_as_a_failing_probe(world):
    world.to(names.STATE_ROLLED_BACK)
    before = world.run()
    world.alarm = failed()
    response = world.tick()
    assert response["state"] == names.STATE_ROLLED_BACK and response["error"] is None
    assert world.run() == before and ("probe",) not in world.log


def test_a_failed_alarm_read_on_approve_in_awaiting_resume_fails_the_probe(world):
    world.to(names.STATE_AWAITING_RESUME)
    world.alarm = failed()
    assert world.engineer("approve")["state"] == names.STATE_ROLLED_BACK


@pytest.mark.parametrize(
    "source",
    [
        names.STATE_PLANNED,
        names.STATE_RUNNING,
        names.STATE_ROLLED_BACK,
        names.STATE_AWAITING_RESUME,
        names.STATE_RESULTS_READY,
    ],
)
def test_check_does_what_a_tick_would_in_every_non_terminal_state(world, source):
    world.to(source)
    if source == names.STATE_RUNNING:
        world.clock.advance(EMBARGO + 1)
    snapshot = copy.deepcopy(world.run())
    tick = world.tick()
    tick_log = world.cleared()
    tick_item = world.run()
    # Put the run back as it was, and check instead.
    world.dynamodb.put_item(TableName=names.RUNS_TABLE, Item=state._encode(snapshot))
    check = world.engineer("check")
    assert world.cleared() == tick_log
    assert (check["state"], check["error"]) == (tick["state"], tick["error"])
    assert world.run()["state"] == tick_item["state"]


def test_check_in_running_with_the_alarm_in_alarm_rolls_back_as_an_alarm_does(world):
    world.to(names.STATE_RUNNING)
    world.alarm = names.ALARM_STATE_ALARM
    key = names.dedupe_key(len(world.run()["history"]), names.STATE_ROLLED_BACK)
    assert world.engineer("check")["state"] == names.STATE_ROLLED_BACK
    assert world.log == [
        ("alarm_state",),
        ("set_kill_switch", False),
        ("converse",),
        ("notify", key),
    ]
    assert len(world.run()["incidents"]) == 1


@pytest.mark.parametrize("trigger", ["alarm", "tick", "check", "approve"])
def test_the_alarm_in_results_ready_rolls_back_and_the_new_report_says_hold_rerun(world, trigger):
    world.to(names.STATE_RESULTS_READY)
    assert f"**{names.RECOMMEND_PROMOTE}**" in world.report_body()
    world.alarm = names.ALARM_STATE_ALARM
    if trigger == "alarm":
        response = world.alarm_event()
    elif trigger == "tick":
        response = world.tick()
    else:
        response = world.engineer(trigger)
    assert response["state"] == names.STATE_ROLLED_BACK
    assert len(world.run()["incidents"]) == 1
    # Resume, and reach results again: the report now says hold, re-run.
    world.alarm = names.ALARM_STATE_OK
    world.clock.advance(1)
    assert world.tick()["state"] == names.STATE_AWAITING_RESUME
    world.clock.advance(1)
    assert world.engineer("approve")["state"] == names.STATE_RUNNING
    world.clock.advance(names.RESULTS_REFRESH_MIN_SECONDS + 1)
    assert world.tick()["state"] == names.STATE_RESULTS_READY
    assert f"**{names.RECOMMEND_HOLD_RERUN}**" in world.report_body()


def test_approve_in_awaiting_resume_with_the_alarm_in_alarm_returns_the_status(world):
    world.to(names.STATE_AWAITING_RESUME)
    before = world.run()
    world.alarm = names.ALARM_STATE_ALARM
    response = world.engineer("approve")
    assert response["state"] == names.STATE_AWAITING_RESUME and response["error"] is None
    assert world.run() == before and world.log == [("alarm_state",)]


def test_approve_with_a_failing_probe_rolls_back_without_an_incident(world):
    world.to(names.STATE_AWAITING_RESUME)
    world.probe = {"passed": False, "max_ms": 12_000.0, "reason": "slow"}
    assert world.engineer("approve")["state"] == names.STATE_ROLLED_BACK
    assert len(world.run()["incidents"]) == 1  # the first rollback's only
    assert not [step for step in world.log if step[0] == "set_kill_switch"]


def test_a_rollback_after_a_resume_sends_a_second_note_and_a_second_incident(world):
    world.to(names.STATE_AWAITING_RESUME)
    assert world.engineer("approve")["state"] == names.STATE_RUNNING
    first = world.run()["incidents"]
    assert len(first) == 1 and "resumed_at" in first[0]
    world.clock.advance(5)
    notes_before = len(world.publishes())
    assert world.alarm_event()["state"] == names.STATE_ROLLED_BACK
    assert len(world.publishes()) == notes_before + 1
    incidents = world.run()["incidents"]
    assert len(incidents) == 2 and "resumed_at" not in incidents[1]


@pytest.mark.parametrize("status", [names.EXPERIMENT_STATUS_PAUSED, "running"])
def test_approve_in_results_ready_needs_the_experiment_ended(world, status):
    world.to(names.STATE_RESULTS_READY)
    world.flagsmith.status = status
    before = world.run()
    response = world.engineer("approve")
    assert error_code(response) == names.ERROR_EXPERIMENT_NOT_ENDED
    assert response["state"] == names.STATE_RESULTS_READY and response["pending"] is None
    assert world.run() == before
    assert world.log == [("alarm_state",), ("experiment_status", EXPERIMENT_ID)]


# ---------------------------------------------------------------------------
# The hard rule: no launch, resume or promotion without the engineer's approve
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["tick", "check"])
@pytest.mark.parametrize(
    "source", [names.STATE_PLANNED, names.STATE_AWAITING_RESUME, names.STATE_RESULTS_READY]
)
def test_no_tick_or_check_launches_resumes_or_promotes(world, source, action):
    """With every guard passing (alarm OK, the probe passing, the experiment completed),
    only the engineer's approve moves these states on (CLAUDE.md's hard rule)."""
    world.to(source)
    world.alarm = names.ALARM_STATE_OK
    world.probe = {"passed": True, "max_ms": 900.0, "reason": "ok"}
    world.flagsmith.status = names.EXPERIMENT_STATUS_ENDED
    world.clock.advance(EMBARGO + names.RESULTS_REFRESH_MIN_SECONDS)
    before = world.run()
    response = world.tick() if action == "tick" else world.engineer("check")
    assert response["state"] == source and response["error"] is None
    assert world.run() == before
    for step in world.log:
        assert step[0] not in ("ensure_experiment", "start_experiment", "set_default_model")
        assert step not in (("set_kill_switch", True), ("set_tick", True)), step


@pytest.mark.parametrize(
    "source, pending",
    [
        (names.STATE_RESULTS_READY, names.STATE_PROMOTED),
        (names.STATE_RUNNING, names.STATE_RESULTS_READY),
        (names.STATE_RUNNING, names.STATE_HELD),
    ],
)
def test_an_alarm_never_takes_over_another_actions_stale_claim(world, source, pending):
    world.to(source)
    state.claim(RUN_ID, source, pending)
    world.clock.advance(STALE + 1)
    before = world.run()
    response = world.alarm_event()
    assert response["error"] is None and response["pending"] == pending
    assert world.run() == before and world.log == []


def test_an_approve_that_loses_its_claim_before_the_commit_is_pending(world):
    """Another invocation takes the stale claim over while the effects run, so the
    commit loses the race: TRANSITION_PENDING, with PROMOTED still pending."""
    world.to(names.STATE_RESULTS_READY)

    def taken_over():
        world.clock.advance(STALE + 1)
        held = state.load_run(RUN_ID)
        state.resume(RUN_ID, names.STATE_RESULTS_READY, names.STATE_PROMOTED, held.pending_at)

    world.flagsmith.on["set_default_model"] = taken_over
    response = world.engineer("approve")
    assert error_code(response) == names.ERROR_TRANSITION_PENDING
    assert response["pending"] == names.STATE_PROMOTED
    assert world.run()["state"] == names.STATE_RESULTS_READY


def test_an_incident_starts_at_the_commit_not_the_claim(world):
    world.to(names.STATE_RUNNING)
    claimed_at = names.utc_iso(world.clock())
    world.flagsmith.on["set_kill_switch"] = lambda: world.clock.advance(5)
    assert world.alarm_event()["state"] == names.STATE_ROLLED_BACK
    item = world.run()
    assert item["incidents"][-1]["started_at"] == item["history"][-1]["at"]
    assert item["incidents"][-1]["started_at"] != claimed_at


# ---------------------------------------------------------------------------
# Claims: fresh, pending, released, taken over
# ---------------------------------------------------------------------------
def test_a_tick_or_a_repeat_during_a_fresh_claim_runs_no_effects(world):
    world.to(names.STATE_PLANNED)
    state.claim(RUN_ID, names.STATE_PLANNED, names.STATE_RUNNING)  # another invocation
    world.clock.advance(STALE)  # exactly 360 s: not older, so not stale
    tick = world.tick()
    assert tick["error"] is None and tick["pending"] == names.STATE_RUNNING
    repeat = world.engineer("approve")
    assert error_code(repeat) == names.ERROR_TRANSITION_PENDING
    assert names.STATE_RUNNING in repeat["error"]["message"]
    assert f"marshal check {RUN_ID}" in repeat["error"]["message"]
    assert world.log == []


def test_decline_while_another_actions_transition_is_pending_is_pending(world):
    world.to(names.STATE_RUNNING)
    state.claim(RUN_ID, names.STATE_RUNNING, names.STATE_ROLLED_BACK)  # an alarm's, mid-way
    for wait in (1, STALE + 60):  # fresh, then stale: decline never takes it over
        world.clock.advance(wait)
        response = world.engineer("decline")
        assert error_code(response) == names.ERROR_TRANSITION_PENDING
    assert world.run()["state"] == names.STATE_RUNNING and world.log == []


def test_approve_takes_over_only_a_transition_approve_triggers(world):
    world.to(names.STATE_PLANNED)
    state.claim(RUN_ID, names.STATE_PLANNED, names.STATE_ABORTED)  # a decline that died
    world.clock.advance(STALE + 1)
    response = world.engineer("approve")
    assert error_code(response) == names.ERROR_TRANSITION_PENDING
    assert world.run()["state"] == names.STATE_PLANNED and world.log == []
    assert world.engineer("decline")["state"] == names.STATE_ABORTED  # its own action may


def test_a_repeat_approve_resumes_its_own_stalled_promotion(world):
    world.to(names.STATE_RESULTS_READY)
    state.claim(RUN_ID, names.STATE_RESULTS_READY, names.STATE_PROMOTED)  # an approve that died
    world.clock.advance(STALE + 1)
    assert world.engineer("approve")["state"] == names.STATE_PROMOTED
    assert world.log == [("set_default_model", names.VARIANT_CHEAPER), ("set_tick", False)]
    assert history(world)[-1][3] == "approve, resuming RESULTS_READY -> PROMOTED"


def test_an_effect_that_throws_is_effect_failed_and_a_later_tick_completes_it(world):
    world.to(names.STATE_RUNNING)
    world.flagsmith.fail["set_kill_switch"] = RuntimeError("Flagsmith is down")
    response = world.alarm_event()
    assert error_code(response) == names.ERROR_EFFECT_FAILED
    assert "set_kill_switch" in response["error"]["message"]
    assert f"marshal check {RUN_ID}" in response["error"]["message"]
    assert "tick" not in response["error"]["message"]
    assert response["pending"] == names.STATE_ROLLED_BACK
    assert world.run()["pending"] == names.STATE_ROLLED_BACK
    world.log.clear()
    world.clock.advance(1)  # a tick right after: the claim was released, so it resumes now
    assert world.tick()["state"] == names.STATE_ROLLED_BACK
    assert ("set_kill_switch", False) in world.log
    item = world.run()
    assert "pending" not in item and len(item["incidents"]) == 1
    assert [e["to"] for e in item["history"]].count(names.STATE_ROLLED_BACK) == 1


@pytest.mark.parametrize("finisher", ["tick", "check"])
def test_a_failed_launch_enabled_the_tick_first_and_a_tick_or_check_completes_it(world, finisher):
    world.to(names.STATE_PLANNED)
    world.flagsmith.fail["start_experiment"] = RuntimeError("the start timed out")
    response = world.engineer("approve")
    assert error_code(response) == names.ERROR_EFFECT_FAILED
    assert world.log[0] == ("set_tick", True)
    world.log.clear()
    world.clock.advance(1)
    done = world.tick() if finisher == "tick" else world.engineer("check")
    assert done["state"] == names.STATE_RUNNING and done["error"] is None
    assert ("start_experiment", EXPERIMENT_ID) in world.log
    launches = [h for h in history(world) if h[1] == names.STATE_RUNNING]
    assert len(launches) == 1 and launches[0][2] == names.ACTOR_ENGINEER
    assert world.pointer().feature_key == names.experiment_feature_key(RUN_ID)


def test_a_flagsmith_that_cannot_open_fails_the_launch_and_releases_the_claim(world):
    """main.py opens the Gateway session on first use, so even reaching a tool can fail
    after the claim: that is EFFECT_FAILED, and the claim is released."""
    world.to(names.STATE_PLANNED)
    real = world.tools.flagsmith

    class Unreachable:
        def __getattr__(self, name):
            raise ConnectionError("the Gateway cannot be reached")

    world.tools.flagsmith = Unreachable()
    response = world.engineer("approve")
    assert error_code(response) == names.ERROR_EFFECT_FAILED
    assert world.run()["pending"] == names.STATE_RUNNING
    world.tools.flagsmith = real
    world.clock.advance(1)
    assert world.tick()["state"] == names.STATE_RUNNING  # released, so resumed at once


def test_a_resumed_transition_whose_publish_went_out_does_not_publish_again(world, monkeypatch):
    world.to(names.STATE_RUNNING)
    # The note is published, then the commit's turn never comes: the invocation dies.

    def die(*args, **kwargs):
        raise KeyboardInterrupt  # not an Exception, so nothing releases the claim

    with monkeypatch.context() as patch:
        patch.setattr(state, "commit", die)
        with pytest.raises(KeyboardInterrupt):
            world.alarm_event()
    assert len(world.publishes()) == 1 and world.run()["pending"] == names.STATE_ROLLED_BACK
    world.clock.advance(STALE + 1)
    assert world.tick()["state"] == names.STATE_ROLLED_BACK
    assert len(world.publishes()) == 1  # the same dedupe key: no second note
    assert len(world.run()["incidents"]) == 1


def test_a_resumed_rollback_from_awaiting_resume_adds_no_incident(world):
    world.to(names.STATE_AWAITING_RESUME)
    world.probe = {"passed": False, "max_ms": 12_000.0, "reason": "slow"}
    real_notify = world.tools.notify

    def down(*args):
        raise failed("InternalError", "Publish")

    world.tools.notify = down
    assert error_code(world.engineer("approve")) == names.ERROR_EFFECT_FAILED
    world.tools.notify = real_notify
    world.clock.advance(1)
    assert world.tick()["state"] == names.STATE_ROLLED_BACK
    assert len(world.run()["incidents"]) == 1


def test_a_commit_that_fails_other_than_by_a_lost_race_is_effect_failed(world):
    world.to(names.STATE_PLANNED)
    world.flagsmith.on["start_experiment"] = lambda: put_pointer(world, "r202611271599")
    response = world.engineer("approve")
    assert error_code(response) == names.ERROR_EFFECT_FAILED
    assert world.run()["state"] == names.STATE_PLANNED


def test_a_resumed_results_ready_reads_the_results_and_writes_the_prose_in_the_effect(world):
    world.to(names.STATE_RUNNING)
    world.clock.advance(EMBARGO + 1)
    real_write = world.tools.write_report

    def fail_once(*args, **kwargs):
        world.tools.write_report = real_write
        raise failed("InternalError", "PutObject")

    world.tools.write_report = fail_once
    assert error_code(world.tick()) == names.ERROR_EFFECT_FAILED
    world.log.clear()
    world.clock.advance(1)
    assert world.tick()["state"] == names.STATE_RESULTS_READY
    names_called = [step[0] for step in world.log]
    assert names_called.index("get_results") < names_called.index("converse")
    assert names_called.index("converse") < names_called.index("write_report")
    assert PROSE in world.report_body()


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"action": "launch"},
        {"action": "start", "prompt": "", "actor": "engineer"},
        {"action": "start", "prompt": PROMPT},
        {"action": "start", "prompt": PROMPT, "actor": "agent"},
        {"action": "approve", "run_id": "R1", "actor": "engineer"},
        {"action": "approve", "run_id": RUN_ID},
        {"action": "status", "run_id": RUN_ID, "actor": "engineer"},
        {"action": "tick", "extra": 1},
        {
            "action": "alarm",
            "alarm_name": "other",
            "alarm_state": "ALARM",
            "at": "2026-11-27T15:42:05Z",
        },
        {
            "action": "alarm",
            "alarm_name": names.ALARM_NAME,
            "alarm_state": "OK",
            "at": "2026-11-27T15:42:05Z",
        },
        {"action": "alarm", "alarm_name": names.ALARM_NAME, "alarm_state": "ALARM", "at": "soon"},
    ],
)
def test_a_payload_that_is_not_an_invoke_payload_is_bad_request(world, payload):
    assert not contracts.validator("invoke").is_valid(payload)
    response = handlers.handle(payload, world.tools)
    contracts.validate("response", response)
    assert error_code(response) == names.ERROR_BAD_REQUEST
    assert world.log == []


def test_the_payload_check_agrees_with_invoke_json():
    at = "2026-11-27T15:42:05.989+0000"
    payloads = [
        {"action": "start", "prompt": PROMPT, "actor": "engineer"},
        {"action": "approve", "run_id": RUN_ID, "actor": "engineer"},
        {"action": "decline", "run_id": RUN_ID, "actor": "engineer"},
        {"action": "check", "run_id": RUN_ID, "actor": "engineer"},
        {"action": "status"},
        {"action": "status", "run_id": RUN_ID},
        {"action": "tick"},
        {"action": "alarm", "alarm_name": names.ALARM_NAME, "alarm_state": "ALARM", "at": at},
        {
            "action": "alarm",
            "alarm_name": names.ALARM_NAME,
            "alarm_state": "ALARM",
            "at": "2026-11-27T15:42:05Z",
        },
        {
            "action": "alarm",
            "alarm_name": names.ALARM_NAME,
            "alarm_state": "ALARM",
            "at": "2026-11-27T15:42:05+05:30",
        },
        {
            "action": "alarm",
            "alarm_name": names.ALARM_NAME,
            "alarm_state": "ALARM",
            "at": "2026-11-27 15:42:05Z",
        },
        {
            "action": "alarm",
            "alarm_name": names.ALARM_NAME,
            "alarm_state": "ALARM",
            "at": "2026-13-27T15:42:05Z",
        },
        {"action": "status", "run_id": None},
        {"action": "start", "prompt": 7, "actor": "engineer"},
        {"action": "check", "run_id": RUN_ID, "actor": "engineer", "extra": True},
        {"action": "tick", "actor": "engineer"},
        "tick",
    ]
    validator = contracts.validator("invoke")
    for payload in payloads:
        assert handlers.is_invoke_payload(payload) is validator.is_valid(payload), payload


# ---------------------------------------------------------------------------
# main.py: the AgentCore entrypoint
# ---------------------------------------------------------------------------
def _load_main():
    path = Path(__file__).resolve().parents[1] / "runtime" / "main.py"
    spec = importlib.util.spec_from_file_location("rm_agent_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_hands_each_payload_to_handle_with_the_real_tools(world, monkeypatch):
    main = _load_main()
    assert main.app.__class__.__name__ == "BedrockAgentCoreApp"
    opened = []
    monkeypatch.setattr(main, "gateway_client", lambda: opened.append(1))
    response = main.invoke({"action": "status"})
    contracts.validate("response", response)
    assert response["message"].startswith(names.NO_ACTIVE_RUN_MESSAGE)
    assert opened == []  # status opens no Gateway session


def test_main_opens_the_gateway_only_when_a_flagsmith_tool_is_used(monkeypatch):
    main = _load_main()

    class Session:
        entered = exited = 0

        def __enter__(self):
            Session.entered += 1
            return self

        def __exit__(self, *exc_info):
            Session.exited += 1
            return False

    monkeypatch.setattr(main, "gateway_client", Session)
    with main.real_tools() as tools:
        assert Session.entered == 0
        assert isinstance(tools.flagsmith.open(), flagsmith.Flagsmith)
        assert Session.entered == 1
    assert Session.exited == 1
