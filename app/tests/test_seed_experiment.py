"""Tests for scripts/seed_experiment.py (task 3.5): an experiment without the agent.

Flagsmith is app/tests/flagsmith_fake.py and rm-runs is app/tests/aws_fakes.FakeDynamo, so
nothing leaves the machine.
"""

from __future__ import annotations

import pytest

from app.tests.aws_fakes import FakeDynamo, pointer
from app.tests.flagsmith_fake import DEV_KEY, FakeFlagsmith
from contracts import names
from scripts import bootstrap_flagsmith as bootstrap
from scripts import seed_experiment as seed

ORG_KEY = "dummyOrganisationKeyForTests"
RUN_ID = "rseedtest0507"
FEATURE_KEY = names.experiment_feature_key(RUN_ID)
KILL, DEFAULT = names.KILL_SWITCH_FEATURE, names.DEFAULT_MODEL_FEATURE


@pytest.fixture
def flagsmith():
    fake = FakeFlagsmith()
    fake.add_feature(KILL, enabled=True)
    fake.add_feature(DEFAULT, value=names.VARIANT_CURRENT)
    return fake


@pytest.fixture
def admin(flagsmith):
    return bootstrap.Admin(ORG_KEY, transport=flagsmith)


def _run(admin, dynamodb, *argv):
    lines = []
    code = seed.main(list(argv), admin=admin, dynamodb=dynamodb, out=lines.append)
    return code, "\n".join(lines)


def test_it_seeds_a_running_experiment_and_then_the_pointer(admin, flagsmith):
    dynamodb = FakeDynamo()

    code, text = _run(admin, dynamodb, RUN_ID)

    assert code == bootstrap.EXIT_OK, text
    feature = flagsmith.feature(FEATURE_KEY)
    assert feature["type"] == names.API_FEATURE_MULTIVARIATE
    assert flagsmith.state(FEATURE_KEY) == (True, names.CONTROL_VALUE)
    [option] = flagsmith.options[feature["id"]]
    assert option == {
        "id": option["id"],
        "feature": feature["id"],
        "type": names.API_OPTION_TYPE,
        "string_value": names.TREATMENT_OPTION_VALUE,
        "key": names.TREATMENT_OPTION_KEY,
        "default_percentage_allocation": names.TREATMENT_PERCENT,
    }
    [experiment] = flagsmith.experiments
    assert experiment["status"] == names.EXPERIMENT_STATUS_RUNNING
    assert experiment["experiment_rollout"] == {
        "enabled": True,
        "rollout_percentage": names.EXPERIMENT_ROLLOUT_PERCENT,
        "feature_state_value": {"type": names.API_ROLLOUT_VALUE_TYPE, "value": names.CONTROL_VALUE},
        "multivariate_feature_state_values": [
            {
                "multivariate_feature_option": option["id"],
                "percentage_allocation": names.TREATMENT_PERCENT,
            }
        ],
    }
    assert dynamodb.items[names.ACTIVE_POINTER_PK] == pointer(RUN_ID)
    assert f"experiment {experiment['id']}" in text


def test_the_experiment_carries_the_three_metrics_helpful_rate_first(admin, flagsmith):
    _run(admin, FakeDynamo(), RUN_ID)

    by_id = {str(m["id"]): m for m in flagsmith.metrics}
    [experiment] = flagsmith.experiments
    attached = [
        (by_id[m["metric"]]["name"], m["expected_direction"]) for m in experiment["metrics"]
    ]
    assert attached == [
        (metric, names.EXPECTED_DIRECTION_API[names.EXPERIMENT_EXPECTED_DIRECTIONS[metric]])
        for metric in names.METRICS
    ]
    assert attached[0][0] == names.PRIMARY_METRIC
    for metric in flagsmith.metrics:
        definition = names.METRIC_DEFINITIONS[metric["name"]]
        assert metric["aggregation"] == definition["aggregation"]
        assert metric["direction"] == names.METRIC_DIRECTION_API[definition["direction"]]
        assert metric["definition"] == {
            "version": names.API_METRIC_DEFINITION_VERSION,
            "event": definition["event"],
        }


def test_the_create_never_carries_a_status_or_rollout_decision(admin, flagsmith):
    _run(admin, FakeDynamo(), RUN_ID)

    [create] = [b for m, p, b in flagsmith.requests if m == "POST" and p.endswith("/experiments/")]
    assert not set(create) & set(names.MCP_EXPERIMENT_FORBIDDEN_FIELDS)
    [start] = [b for m, p, b in flagsmith.requests if p.endswith("/start/")]
    assert set(start) == {"name", "hypothesis"} and start["name"] == FEATURE_KEY


def test_the_pointer_is_written_last(admin, flagsmith):
    dynamodb = FakeDynamo()
    written_before_start = []
    real_put = dynamodb.put_item

    def put_item(**params):
        written_before_start.append([e["status"] for e in flagsmith.experiments])
        return real_put(**params)

    dynamodb.put_item = put_item
    _run(admin, dynamodb, RUN_ID)

    assert written_before_start == [[names.EXPERIMENT_STATUS_RUNNING]]
    [(_, params)] = [c for c in dynamodb.calls if c[0] == "put_item"]
    assert params["ConditionExpression"] == names.POINTER_CREATE_CONDITION


def test_it_refuses_when_a_pointer_exists_and_touches_nothing(admin, flagsmith):
    dynamodb = FakeDynamo(pointer("rother0101"))

    code, text = _run(admin, dynamodb, RUN_ID)

    assert code == bootstrap.EXIT_REFUSED
    assert "rother0101" in text
    assert flagsmith.requests == []
    assert dynamodb.writes() == []


def test_it_enables_the_kill_switch_as_launch_does(admin, flagsmith):
    flagsmith.feature(KILL)["environment_feature_state"]["enabled"] = False

    _run(admin, FakeDynamo(), RUN_ID)

    assert flagsmith.state(KILL)[0] is True
    published = [p for m, p, _ in flagsmith.requests if p.endswith("/publish/")]
    assert len(published) == 1


def test_a_kill_switch_already_on_is_left_alone(admin, flagsmith):
    _run(admin, FakeDynamo(), RUN_ID)
    assert not [p for m, p, _ in flagsmith.requests if "/versions/" in p]


def test_it_refuses_an_existing_feature_key(admin, flagsmith):
    """An ended experiment cannot restart, so each run needs a new feature (section 4)."""
    flagsmith.add_feature(FEATURE_KEY, names.API_FEATURE_MULTIVARIATE)
    dynamodb = FakeDynamo()

    code, text = _run(admin, dynamodb, RUN_ID)

    assert code == bootstrap.EXIT_REFUSED and FEATURE_KEY in text
    assert flagsmith.experiments == [] and dynamodb.writes() == []


def test_it_refuses_without_the_permanent_features(admin, flagsmith):
    flagsmith.features.clear()
    code, text = _run(admin, FakeDynamo(), RUN_ID)
    assert code == bootstrap.EXIT_REFUSED and "bootstrap_flagsmith.py" in text
    assert flagsmith.writes() == []


def test_matching_metrics_are_reused_and_a_mismatch_stops(admin, flagsmith):
    for metric in names.METRICS:
        definition = names.METRIC_DEFINITIONS[metric]
        flagsmith.metrics.append(
            {
                "id": 900 + len(flagsmith.metrics),
                "name": metric,
                "aggregation": definition["aggregation"],
                "direction": names.METRIC_DIRECTION_API[definition["direction"]],
                "definition": {"version": 1, "event": definition["event"]},
            }
        )
    assert _run(admin, FakeDynamo(), RUN_ID)[0] == bootstrap.EXIT_OK
    assert len(flagsmith.metrics) == len(names.METRICS)  # none created

    other = FakeFlagsmith()
    other.add_feature(KILL)
    other.add_feature(DEFAULT, value=names.VARIANT_CURRENT)
    other.metrics.append(
        {"id": 990, "name": names.METRIC_HELPFUL_RATE, "aggregation": "sum", "direction": "up",
         "definition": {"version": 1, "event": names.EVENT_HELPFUL}}
    )  # fmt: skip
    code, text = _run(bootstrap.Admin(ORG_KEY, transport=other), FakeDynamo(), "rseedtest0508")
    assert code == bootstrap.EXIT_FAILED and names.METRIC_HELPFUL_RATE in text
    assert other.experiments == []


def test_a_pointer_that_appears_meanwhile_is_reported_with_the_experiment(admin, flagsmith):
    dynamodb = FakeDynamo()
    dynamodb.race = pointer("rother0101")

    code, text = _run(admin, dynamodb, RUN_ID)

    assert code == bootstrap.EXIT_FAILED
    [experiment] = flagsmith.experiments
    assert f"experiment {experiment['id']}" in text and "dashboard" in text
    assert dynamodb.items[names.ACTIVE_POINTER_PK]["run_id"]["S"] == "rother0101"


@pytest.mark.parametrize("run_id", ["seed0507", "rSEED0507", f"{RUN_ID}\n", "r12"])
def test_a_bad_run_id_is_refused_before_any_call(admin, flagsmith, run_id):
    dynamodb = FakeDynamo()
    code, _ = _run(admin, dynamodb, run_id)
    assert code == bootstrap.EXIT_REFUSED
    assert flagsmith.requests == [] and dynamodb.calls == []


def test_nothing_printed_holds_a_key(admin, flagsmith):
    _, text = _run(admin, FakeDynamo(), RUN_ID)
    assert ORG_KEY not in text and DEV_KEY not in text
