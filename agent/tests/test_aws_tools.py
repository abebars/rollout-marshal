"""Tests for task 2.4: the agent's AWS tools in agent/runtime/rm_agent/tools/.

moto stands in for CloudWatch, EventBridge Scheduler, SNS, DynamoDB and S3: dummy
credentials, us-east-1, the AWS config files at /dev/null, and moto's account set to the
project's, so every ARN the tools build is the one moto made. The probe's Lambda invokes go
to a fake client, because moto would need Docker to run a function.
"""

from __future__ import annotations

import copy
import io
import json
import os
import threading
import time
import unicodedata
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from rm_agent.tools import notify, probe, report, schedule, signals

import contracts
from contracts import names

RUN_ID = "r202611271530"
START = datetime(2026, 11, 27, 15, 30, 5, tzinfo=UTC)
TICK = {"action": names.ACTION_TICK}

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


class Spy:
    """A client that counts the calls of each operation and passes them on."""

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

    def count(self, operation: str) -> int:
        return sum(1 for name, _ in self.calls if name == operation)


@pytest.fixture
def aws(monkeypatch):
    """moto in account 818928662830, and each tool module pointed at its own spied client."""
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
        clients = {
            service: Spy(boto3.client(service, region_name=names.REGION))
            for service in ("cloudwatch", "scheduler", "sns", "dynamodb", "s3")
        }
        monkeypatch.setattr(signals, "_client", lambda: clients["cloudwatch"])
        monkeypatch.setattr(schedule, "_client", lambda: clients["scheduler"])
        monkeypatch.setattr(notify, "_sns", lambda: clients["sns"])
        monkeypatch.setattr(notify, "_dynamodb", lambda: clients["dynamodb"])
        monkeypatch.setattr(report, "_client", lambda: clients["s3"])
        yield clients


# ---------------------------------------------------------------------------
# signals: alarm_state
# ---------------------------------------------------------------------------
def make_alarm(cloudwatch) -> None:
    variant, value = names.ALARM_DIMENSION.split("=")
    cloudwatch.put_metric_alarm(
        AlarmName=names.ALARM_NAME,
        Namespace=names.CW_NAMESPACE,
        MetricName=names.ALARM_METRIC,
        Dimensions=[{"Name": variant, "Value": value}],
        ExtendedStatistic=names.ALARM_STATISTIC,
        Period=names.ALARM_PERIOD_SECONDS,
        EvaluationPeriods=names.ALARM_EVALUATION_PERIODS,
        DatapointsToAlarm=names.ALARM_DATAPOINTS_TO_ALARM,
        Threshold=names.ALARM_THRESHOLD_MS,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData=names.ALARM_TREAT_MISSING_DATA,
    )


@pytest.mark.parametrize("state", names.ALARM_STATES)
def test_alarm_state_and_threshold_come_from_one_describe_alarms_call(aws, state):
    make_alarm(aws["cloudwatch"])
    aws["cloudwatch"].set_alarm_state(AlarmName=names.ALARM_NAME, StateValue=state, StateReason="t")
    before = aws["cloudwatch"].count("describe_alarms")
    alarm = signals.alarm_state(names.ALARM_NAME)
    assert alarm == signals.Alarm(state, names.ALARM_THRESHOLD_MS)
    assert alarm.state in names.ALARM_STATES
    assert aws["cloudwatch"].count("describe_alarms") == before + 1


def test_a_missing_alarm_is_an_error(aws):
    with pytest.raises(LookupError, match=names.ALARM_NAME):
        signals.alarm_state(names.ALARM_NAME)


# ---------------------------------------------------------------------------
# probe: probe_cheaper
# ---------------------------------------------------------------------------
GOOD_REPLY = {
    "answer": "Open Settings, choose Security, then Reset password.",
    "input_tokens": 49,
    "output_tokens": 60,
    "model_id": names.MODEL_CHEAPER,
}


def ok(delay: float = 0.0, reply: dict | None = None):
    def behave(kwargs):
        time.sleep(delay)
        body = json.dumps(GOOD_REPLY if reply is None else reply).encode()
        return {"StatusCode": 200, "Payload": io.BytesIO(body)}

    return behave


def raises(kwargs):
    raise RuntimeError("connection reset")


def function_error(kwargs):
    payload = io.BytesIO(json.dumps({"errorMessage": "boom"}).encode())
    return {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": payload}


class FakeLambda:
    """Lambda's Invoke, one behaviour per call in order (the last one repeats)."""

    def __init__(self, *behaviours) -> None:
        self.behaviours = list(behaviours)
        self.calls: list[dict] = []
        self.started: list[float] = []
        self._lock = threading.Lock()

    def invoke(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            self.started.append(time.monotonic())
            behaviour = self.behaviours[min(len(self.calls), len(self.behaviours)) - 1]
        return behaviour(kwargs)


@pytest.fixture
def fake_lambda(monkeypatch):
    def install(*behaviours):
        fake = FakeLambda(*behaviours)
        monkeypatch.setattr(probe, "_client", lambda: fake)
        return fake

    return install


THRESHOLD_MS = 400  # the tests' threshold, small so a slow invoke costs little time


def test_the_probe_passes_when_every_invoke_is_under_the_threshold(fake_lambda):
    fake = fake_lambda(ok(0.05))
    began = time.monotonic()
    result = probe.probe_cheaper(THRESHOLD_MS)
    elapsed_ms = (time.monotonic() - began) * 1000
    assert result["passed"] is True and result["reason"]
    assert 50 <= result["max_ms"] < THRESHOLD_MS
    assert len(fake.calls) == names.PROBE_INVOKES == 5
    assert elapsed_ms < 3 * 50  # in parallel: five 50 ms invokes in well under 250 ms
    for call in fake.calls:
        assert call["FunctionName"] == names.ANSWER_CHEAPER_FUNCTION
        assert call["InvocationType"] == "RequestResponse"
        payload = json.loads(call["Payload"])
        contracts.validate("answer_request", payload)
        assert payload == {"question": names.PROBE_QUESTION, "identity": names.PROBE_IDENTITY}


@pytest.mark.parametrize(
    "behaviours, reason",
    [
        ((ok(0.0), ok(0.0), ok(0.45)), "threshold"),  # at or above the threshold
        ((ok(0.0), raises), "RuntimeError"),
        ((ok(0.0), function_error), "function error"),
        ((ok(reply={**GOOD_REPLY, "answer": ""}),), "answer_response"),
        ((ok(reply={k: v for k, v in GOOD_REPLY.items() if k != "model_id"}),), "answer_response"),
        ((ok(reply={**GOOD_REPLY, "output_tokens": -1}),), "answer_response"),
        ((ok(reply={**GOOD_REPLY, "input_tokens": True}),), "answer_response"),
        ((ok(reply={**GOOD_REPLY, "model_id": names.MODEL_JUDGE}),), "answer_response"),
        ((ok(reply={**GOOD_REPLY, "extra": 1}),), "answer_response"),
    ],
    ids=[
        "slow",
        "raises",
        "function-error",
        "empty",
        "no-model",
        "negative",
        "bool",
        "judge",
        "extra",
    ],
)
def test_the_probe_fails_on_any_bad_invoke(fake_lambda, behaviours, reason):
    fake_lambda(*behaviours)
    result = probe.probe_cheaper(THRESHOLD_MS)
    assert result["passed"] is False
    assert reason in result["reason"]


def test_the_probe_fails_without_waiting_out_an_invoke_that_times_out(fake_lambda):
    fake_lambda(ok(0.0), ok(2.0))
    began = time.monotonic()
    result = probe.probe_cheaper(THRESHOLD_MS)
    waited_ms = (time.monotonic() - began) * 1000
    assert result["passed"] is False and "timed out" in result["reason"]
    assert waited_ms < 1500  # it gave up at the threshold, not after the 2 s invoke


def test_the_probe_makes_its_client_once_in_the_calling_thread(monkeypatch):
    """Making clients from boto3's default session in several threads at once is not safe,
    so the threads share one client made before they start."""
    fake, made = FakeLambda(ok(0.0)), []

    def client():
        made.append(threading.current_thread())
        return fake

    monkeypatch.setattr(probe, "_client", client)
    assert probe.probe_cheaper(THRESHOLD_MS)["passed"] is True
    assert made == [threading.current_thread()]
    assert len(fake.calls) == names.PROBE_INVOKES


def test_the_probe_reply_check_agrees_with_answer_response():
    replies = [
        GOOD_REPLY,
        {**GOOD_REPLY, "model_id": names.MODEL_CURRENT},
        {**GOOD_REPLY, "answer": ""},
        {**GOOD_REPLY, "input_tokens": 1.5},
        {**GOOD_REPLY, "input_tokens": 49.0},  # JSON Schema counts a whole float as an integer
        {**GOOD_REPLY, "input_tokens": True},
        {**GOOD_REPLY, "extra": None},
        {k: v for k, v in GOOD_REPLY.items() if k != "answer"},
        ["not", "an", "object"],
        None,
    ]
    validator = contracts.validator("answer_response")
    for value in replies:
        assert probe.is_answer_response(value) is validator.is_valid(value), value


# ---------------------------------------------------------------------------
# schedule: set_tick
# ---------------------------------------------------------------------------
INVOKER_ARN = f"arn:aws:lambda:{names.REGION}:{names.ACCOUNT_ID}:function:{names.INVOKER_FUNCTION}"
SCHEDULER_ROLE_ARN = f"arn:aws:iam::{names.ACCOUNT_ID}:role/{names.SCHEDULER_ROLE}"


def make_schedule(scheduler, *, input_payload=TICK, role=SCHEDULER_ROLE_ARN) -> None:
    scheduler.create_schedule(
        Name=names.TICK_SCHEDULE,
        GroupName=names.TICK_SCHEDULE_GROUP,
        ScheduleExpression=f"rate({names.TICK_INTERVAL_MINUTES} minutes)",
        FlexibleTimeWindow={"Mode": "OFF"},
        Description="Rollout Marshal tick",
        State=schedule.SCHEDULE_DISABLED,
        Target={"Arn": INVOKER_ARN, "RoleArn": role, "Input": json.dumps(input_payload)},
    )


def read_schedule(scheduler) -> dict:
    got = scheduler.get_schedule(Name=names.TICK_SCHEDULE, GroupName=names.TICK_SCHEDULE_GROUP)
    got.pop("ResponseMetadata", None)
    return got


def test_the_write_back_is_every_field_get_returns_that_update_takes(aws):
    model = aws["scheduler"].meta.service_model
    returned = set(model.operation_model("GetSchedule").output_shape.members)
    assert set(schedule.update_fields(aws["scheduler"])) == returned - {
        "Arn",
        "CreationDate",
        "LastModificationDate",
    }


def test_set_tick_changes_only_the_state_and_keeps_the_tick_payload(aws):
    make_schedule(aws["scheduler"])
    original = read_schedule(aws["scheduler"])
    for enabled in (True, False, True, False):
        schedule.set_tick(enabled)
        now = read_schedule(aws["scheduler"])
        assert now["State"] == (
            schedule.SCHEDULE_ENABLED if enabled else schedule.SCHEDULE_DISABLED
        )
        assert json.loads(now["Target"]["Input"]) == TICK
        contracts.validate("invoke", json.loads(now["Target"]["Input"]))
        unchanged = {"State", "LastModificationDate"}
        assert {k: v for k, v in now.items() if k not in unchanged} == {
            k: v for k, v in original.items() if k not in unchanged
        }


def test_set_tick_to_the_state_it_already_has_is_a_no_op(aws):
    make_schedule(aws["scheduler"])
    assert schedule.set_tick(False) is False  # already disabled
    assert aws["scheduler"].count("update_schedule") == 0
    assert schedule.set_tick(True) is True
    assert schedule.set_tick(True) is False
    assert aws["scheduler"].count("update_schedule") == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"input_payload": {"action": "alarm"}},
        {"role": f"arn:aws:iam::{names.ACCOUNT_ID}:role/other"},
    ],
    ids=["not-the-tick", "another-role"],
)
def test_set_tick_refuses_a_schedule_that_is_not_the_tick(aws, changes):
    make_schedule(aws["scheduler"], **changes)
    with pytest.raises(ValueError):
        schedule.set_tick(True)
    assert aws["scheduler"].count("update_schedule") == 0


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------
def make_run_table(dynamodb, notified: dict | None = None) -> None:
    dynamodb.create_table(
        TableName=names.RUNS_TABLE,
        KeySchema=[{"AttributeName": names.PARTITION_KEY, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": names.PARTITION_KEY, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.put_item(
        TableName=names.RUNS_TABLE,
        Item={
            names.PARTITION_KEY: {"S": names.run_pk(RUN_ID)},
            "run_id": {"S": RUN_ID},
            "notified": {"M": {k: {"S": v} for k, v in (notified or {}).items()}},
        },
    )


def stored_notified(dynamodb) -> dict:
    item = dynamodb.get_item(
        TableName=names.RUNS_TABLE,
        Key={names.PARTITION_KEY: {"S": names.run_pk(RUN_ID)}},
        ConsistentRead=True,
    )["Item"]
    return {k: v["S"] for k, v in item["notified"]["M"].items()}


ROLLBACK_KEY = names.dedupe_key(2, names.STATE_ROLLED_BACK)


def test_notify_publishes_once_per_dedupe_key_and_returns_the_same_message_id(aws):
    aws["sns"].create_topic(Name=names.INCIDENTS_TOPIC)
    make_run_table(aws["dynamodb"])
    first = notify.notify(RUN_ID, "Rolled back", "The cheaper model is slow.", ROLLBACK_KEY)
    again = notify.notify(RUN_ID, "Rolled back", "The cheaper model is slow.", ROLLBACK_KEY)
    assert first == again
    assert aws["sns"].count("publish") == 1
    assert stored_notified(aws["dynamodb"]) == {ROLLBACK_KEY: first}
    (sent,) = [kwargs for name, kwargs in aws["sns"].calls if name == "publish"]
    topic = f"arn:aws:sns:{names.REGION}:{names.ACCOUNT_ID}:{names.INCIDENTS_TOPIC}"
    assert (sent["TopicArn"], sent["Message"]) == (topic, "The cheaper model is slow.")


def test_notify_records_its_key_beside_the_others_and_never_over_them(aws):
    aws["sns"].create_topic(Name=names.INCIDENTS_TOPIC)
    earlier = {names.dedupe_key(1, names.STATE_PLANNED): "message-0"}
    make_run_table(aws["dynamodb"], earlier)
    second_rollback = names.dedupe_key(6, names.STATE_ROLLED_BACK)
    one = notify.notify(RUN_ID, "Rolled back", "first", ROLLBACK_KEY)
    two = notify.notify(RUN_ID, "Rolled back again", "second", second_rollback)
    assert one != two
    assert stored_notified(aws["dynamodb"]) == {**earlier, ROLLBACK_KEY: one, second_rollback: two}


def test_a_failed_publish_records_nothing(aws):
    make_run_table(aws["dynamodb"])  # no topic: the publish fails
    with pytest.raises(Exception):  # noqa: B017 - whatever SNS raises, nothing is recorded
        notify.notify(RUN_ID, "Rolled back", "body", ROLLBACK_KEY)
    assert stored_notified(aws["dynamodb"]) == {}


def test_a_key_recorded_by_another_invocation_meanwhile_wins(aws, monkeypatch):
    """Both published (at least once); the first recorded message ID stays the run's."""
    aws["sns"].create_topic(Name=names.INCIDENTS_TOPIC)
    make_run_table(aws["dynamodb"])
    real_publish = aws["sns"].publish

    def publish_while_another_records(**kwargs):
        reply = real_publish(**kwargs)
        aws["dynamodb"].update_item(
            TableName=names.RUNS_TABLE,
            Key={names.PARTITION_KEY: {"S": names.run_pk(RUN_ID)}},
            UpdateExpression="SET #n.#k = :m",
            ExpressionAttributeNames={"#n": "notified", "#k": ROLLBACK_KEY},
            ExpressionAttributeValues={":m": {"S": "message-from-the-other"}},
        )
        return reply

    monkeypatch.setattr(aws["sns"], "publish", publish_while_another_records, raising=False)
    assert notify.notify(RUN_ID, "Rolled back", "body", ROLLBACK_KEY) == "message-from-the-other"
    assert stored_notified(aws["dynamodb"]) == {ROLLBACK_KEY: "message-from-the-other"}


@pytest.mark.parametrize("key", ["ROLLED_BACK", "0#ROLLED_BACK", "2#DONE", "2#rolled_back", ""])
def test_notify_refuses_a_key_that_is_not_a_dedupe_key(aws, key):
    with pytest.raises(ValueError):
        notify.notify(RUN_ID, "s", "b", key)
    assert aws["sns"].count("publish") == 0


def test_notify_sends_a_subject_sns_accepts(aws):
    """SNS takes a Subject of UTF-8 text with no line breaks or control characters, and less
    than 100 characters long (the Publish API reference)."""
    aws["sns"].create_topic(Name=names.INCIDENTS_TOPIC)
    make_run_table(aws["dynamodb"])
    notify.notify(RUN_ID, "Rolled back\n\tnow\x07\x00" + "x" * 300, "body", ROLLBACK_KEY)
    blank = names.dedupe_key(3, names.STATE_RESULTS_READY)
    notify.notify(RUN_ID, " \n\x07 ", "body", blank)
    first, second = [kwargs for name, kwargs in aws["sns"].calls if name == "publish"]
    assert notify.SNS_SUBJECT_MAX == 99
    assert first["Subject"] == ("Rolled back now " + "x" * 300)[:99]
    assert not [c for c in first["Subject"] if unicodedata.category(c) == "Cc"]
    assert "Subject" not in second  # nothing printable was left, so no Subject is sent
    assert set(stored_notified(aws["dynamodb"])) == {ROLLBACK_KEY, blank}


# ---------------------------------------------------------------------------
# report: guardrails_pass, recommend, write_report
# ---------------------------------------------------------------------------
def variation(identities=75, helpful=0.8, latency=1000.0, cost=2000.0) -> dict:
    return {
        "identities": identities,
        "helpful_rate": helpful,
        "mean_latency_ms": latency,
        "mean_cost_micro_usd": cost,
    }


def make_results(*, current=None, cheaper=None, ci=-0.03, ready=True) -> dict:
    return {
        "ready": ready,
        "variations": {
            names.VARIANT_CURRENT: current or variation(),
            names.VARIANT_CHEAPER: cheaper or variation(latency=900.0, cost=500.0),
        },
        "helpful_lift_ci_lower": ci,
    }


def make_plan() -> dict:
    return {
        "hypothesis": "Sonnet 5 answers support questions as helpfully as Fable 5.1, for less.",
        "summary": "Split identities 50/50 between Fable 5.1 and Sonnet 5 and compare.",
        "current_model": names.MODEL_CURRENT,
        "cheaper_model": names.MODEL_CHEAPER,
        "primary_metric": names.PRIMARY_METRIC,
        "guardrail_metrics": list(names.GUARDRAIL_METRICS),
        "split": {variant: names.TREATMENT_PERCENT for variant in names.VARIANTS},
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


def at(minutes: float) -> str:
    return names.utc_iso(START + timedelta(minutes=minutes))


def make_run(*, incidents: list | None = None) -> dict:
    history = [
        {
            "at": at(0),
            "from": None,
            "to": names.STATE_PLANNED,
            "actor": "engineer",
            "reason": "start",
        },
        {
            "at": at(3),
            "from": names.STATE_PLANNED,
            "to": names.STATE_RUNNING,
            "actor": "engineer",
            "reason": "approve",
        },
    ]
    item = {
        "pk": names.run_pk(RUN_ID),
        "run_id": RUN_ID,
        "state": names.STATE_RUNNING,
        "prompt": "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5.",
        "plan": make_plan(),
        "feature_key": names.experiment_feature_key(RUN_ID),
        "experiment_id": 4321,
        "history": history,
        "notified": {},
        "first_running_at": at(3),
        "created_at": at(0),
        "updated_at": at(3),
    }
    if incidents:
        item["incidents"] = incidents
    contracts.validate("run_item", item)
    return item


INCIDENT = [{"started_at": at(12), "resumed_at": at(21)}]


@pytest.mark.parametrize(
    "run, results, expected",
    [
        (make_run(), make_results(), names.RECOMMEND_PROMOTE),
        (make_run(incidents=INCIDENT), make_results(), names.RECOMMEND_HOLD_RERUN),
        (
            make_run(),
            make_results(ci=names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER),
            names.RECOMMEND_PROMOTE,
        ),
        (make_run(), make_results(ci=-0.0500001), names.RECOMMEND_HOLD),
        (make_run(), make_results(ci=None), names.RECOMMEND_HOLD),
        (
            make_run(),
            make_results(cheaper=variation(latency=1100.01, cost=500.0)),
            names.RECOMMEND_HOLD,
        ),
        (
            make_run(),
            make_results(cheaper=variation(latency=900.0, cost=2000.01)),
            names.RECOMMEND_HOLD,
        ),
    ],
    ids=["promote", "incident", "floor-exactly", "just-below", "no-interval", "slow", "dearer"],
)
def test_recommend(run, results, expected):
    """An incident run that would otherwise qualify still recommends hold, re-run."""
    assert report.recommend(run, results) == expected
    assert expected in names.RECOMMENDATIONS


def test_recommend_takes_a_run_object_with_an_item_too():
    class Run:
        item = make_run(incidents=INCIDENT)

    assert report.recommend(Run(), make_results()) == names.RECOMMEND_HOLD_RERUN


@pytest.mark.parametrize(
    "latency, cost, passes",
    [
        (1100.0, 2000.0, True),  # exactly 110 percent latency, the same cost
        (1100.0000001, 2000.0, False),  # just above
        (1000.0, 2000.0000001, False),  # cost just higher
        (500.0, 100.0, True),
    ],
)
def test_guardrails_pass_at_exactly_110_percent_and_not_above(latency, cost, passes):
    results = make_results(
        current=variation(latency=1000.0, cost=2000.0),
        cheaper=variation(latency=latency, cost=cost),
    )
    assert report.guardrails_pass(results) is passes


def test_guardrails_do_not_pass_on_a_missing_mean():
    results = make_results(cheaper=variation(latency=None), ready=False)
    assert report.guardrails_pass(results) is False


def _sections(markdown: str) -> list[str]:
    return [line[3:].strip() for line in markdown.splitlines() if line.startswith("## ")]


def test_the_report_has_the_nine_sections_in_order():
    markdown = report.render_report(make_run(incidents=INCIDENT), make_results(), now=START)
    assert _sections(markdown) == list(names.REPORT_SECTIONS)


def _section(markdown: str, name: str) -> str:
    return markdown.split(f"## {name}\n", 1)[1].split("\n## ", 1)[0]


def test_known_bias_names_both_biases():
    for run in (make_run(), make_run(incidents=INCIDENT)):
        bias = _section(report.render_report(run, make_results(), now=START), "Known bias")
        assert "rolled-back users still count in the cheaper arm" in bias.lower()
        assert names.KILL_SWITCH_FEATURE in bias
        assert "enrolled in" in bias and "without ever being served it" in bias


def test_the_recommendation_states_the_promote_rule_as_at_least_a_97_5_percent_probability():
    assert report.PROMOTE_PROBABILITY_PERCENT == 97.5
    for run in (make_run(), make_run(incidents=INCIDENT)):
        text = _section(report.render_report(run, make_results(), now=START), "Recommendation")
        assert "at least a 97.5 percent probability" in text
        assert report.recommend(run, make_results()) in text


def test_the_report_carries_the_runs_facts():
    run = make_run(incidents=INCIDENT)
    results = make_results()
    markdown = report.render_report(run, results, now=START + timedelta(minutes=50))
    assert run["plan"]["hypothesis"] in _section(markdown, "Hypothesis")
    setup = _section(markdown, "Setup")
    assert names.MODEL_CURRENT in setup and names.MODEL_CHEAPER in setup
    assert run["feature_key"] in setup and str(run["experiment_id"]) in setup
    assert at(12) in _section(markdown, "Incident") and at(21) in _section(markdown, "Incident")
    assert "approve" in _section(markdown, "Timeline")
    assert "-0.03" in _section(markdown, "Results")
    assert "pass" in _section(markdown, "Guardrails").lower()
    assert "50 minutes" in _section(markdown, "Time taken")
    assert "No incident" in _section(
        report.render_report(make_run(), results, now=START), "Incident"
    )


def test_the_model_prose_goes_under_its_section_after_the_facts():
    prose = {"Hypothesis": "A paragraph the model wrote.", "Recommendation": "Why, in prose."}
    markdown = report.render_report(make_run(), make_results(), prose=prose, now=START)
    for name, text in prose.items():
        assert _section(markdown, name).rstrip().endswith(text)
    with pytest.raises(ValueError):
        report.render_report(make_run(), make_results(), prose={"Appendix": "x"}, now=START)


def test_write_report_puts_the_report_at_the_runs_key_with_a_plain_put(aws, monkeypatch):
    aws["s3"].create_bucket(Bucket=names.REPORTS_BUCKET)
    monkeypatch.setattr(report, "_now", lambda: START)
    run, results = make_run(), make_results()
    key = report.write_report(run, results)
    assert key == names.report_key(RUN_ID) == f"runs/{RUN_ID}/report.md"
    body = aws["s3"].get_object(Bucket=names.REPORTS_BUCKET, Key=key)["Body"].read().decode()
    assert body == report.render_report(run, results, now=START)
    # A plain put: the latest entry into RESULTS_READY replaces the report.
    report.write_report(make_run(incidents=INCIDENT), results)
    newer = aws["s3"].get_object(Bucket=names.REPORTS_BUCKET, Key=key)["Body"].read().decode()
    assert names.RECOMMEND_HOLD_RERUN in _section(newer, "Recommendation")
    (put,) = [kwargs for name, kwargs in aws["s3"].calls if name == "put_object"][:1]
    assert set(put) == {"Bucket", "Key", "Body", "ContentType"}  # no condition on the put


def test_results_used_here_are_valid_results():
    contracts.validate("results", make_results())
    contracts.validate("results", copy.deepcopy(make_results(ci=None)))
