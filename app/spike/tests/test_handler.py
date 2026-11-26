"""Task 1.2 spike handler: the router's and the judge's Flagsmith roles, and the handler.

The router and judge run the real Flagsmith SDK against FakeFlagsmith (fakes.py), so the
counts below are what the SDK really sent. No test reaches Flagsmith or AWS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.spike import handler
from app.spike.tests.fakes import (
    DUMMY_SDK_KEY,
    EXPERIMENT_ID,
    SPIKE_FLAG,
    FakeBedrock,
    FakeSecrets,
    default_flag,
    text_response,
)
from contracts import names

ROOT = Path(__file__).resolve().parents[3]
DESIGN = (ROOT / "openspec/changes/build-rollout-marshal/design.md").read_text()
TASKS = (ROOT / "openspec/changes/build-rollout-marshal/tasks.md").read_text()


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def test_spike_names_are_the_ones_design_md_gives_and_carry_the_spike_prefix():
    for name in (handler.SPIKE_FLAG, handler.SPIKE_EVENT, handler.SPIKE_METRIC):
        assert name.startswith(names.SPIKE_NAME_PREFIX)
        assert f"`{name}`" in DESIGN
        assert f"`{name}`" in TASKS
    assert handler.SPIKE_FLAG == SPIKE_FLAG


def test_the_secret_env_holds_a_name_and_only_the_sdk_key_name_is_accepted(monkeypatch):
    secrets = FakeSecrets(names.SECRET_FLAGSMITH_SERVER_KEY, DUMMY_SDK_KEY)
    monkeypatch.setenv(handler.SDK_KEY_SECRET_ENV, names.SECRET_FLAGSMITH_SERVER_KEY)
    assert handler.read_sdk_key(secrets) == DUMMY_SDK_KEY
    assert secrets.asked == [names.SECRET_FLAGSMITH_SERVER_KEY]

    monkeypatch.setenv(handler.SDK_KEY_SECRET_ENV, names.SECRET_FLAGSMITH_API_KEY)
    with pytest.raises(ValueError, match="only reads"):
        handler.read_sdk_key(secrets)
    assert secrets.asked == [names.SECRET_FLAGSMITH_SERVER_KEY]  # the org key was never asked for


# ---------------------------------------------------------------------------
# Invocation 1: the router's role
# ---------------------------------------------------------------------------
def test_router_makes_one_identity_request_per_identity_and_returns_every_arm(
    fake_flagsmith, local_client_factory
):
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    assert len(local_client_factory.made) == 1  # one client for the whole invocation
    identifiers = [body["identifier"] for body in fake_flagsmith.identity_bodies]
    assert identifiers == [names.identity(n) for n in range(1, names.IDENTITY_COUNT + 1)]
    assert fake_flagsmith.identity_bodies[0] == {"identifier": "user-001", "traits": []}
    assert result["identity_requests"]["count"] == names.IDENTITY_COUNT
    assert result["identity_requests"]["statuses"] == {"200": names.IDENTITY_COUNT}

    assignments = result["assignments"]
    assert len(assignments) == names.IDENTITY_COUNT
    assert assignments["user-001"] == names.VARIANT_CHEAPER
    assert assignments["user-002"] == names.VARIANT_CURRENT
    assert result["arm_counts"] == {names.VARIANT_CHEAPER: 75, names.VARIANT_CURRENT: 75}
    assert len(result["identities"]) == names.IDENTITY_COUNT


def test_router_records_value_variant_and_experiment_for_an_identity_in_each_arm(
    fake_flagsmith, local_client_factory
):
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    cheaper = result["samples"][names.VARIANT_CHEAPER]
    current = result["samples"][names.VARIANT_CURRENT]
    assert cheaper["identity"] == "user-001"
    assert (cheaper["value"], cheaper["variant"]) == ("cheaper", "cheaper")
    assert (current["value"], current["variant"]) == ("current", "control")
    for sample in (cheaper, current):
        assert sample["experiment"] == {"id": EXPERIMENT_ID, "name": "spike", "in_experiment": True}
        assert sample["enabled"] is True
    assert result["variants_by_arm"] == {
        names.VARIANT_CHEAPER: [{"variant": "cheaper", "count": 75}],
        names.VARIANT_CURRENT: [{"variant": "control", "count": 75}],
    }
    assert result["experiment_ids"] == [EXPERIMENT_ID]


def test_router_tracks_an_exposure_only_when_the_flag_says_in_experiment(
    fake_flagsmith, local_client_factory
):
    def flag_for(identifier):
        flag = default_flag(identifier)
        if identifier == "user-150":
            flag["metadata"]["experiment"]["in_experiment"] = False
        return flag

    fake_flagsmith.flag_for = flag_for
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    exposures = fake_flagsmith.events()
    assert len(exposures) == names.IDENTITY_COUNT - 1
    assert "user-150" not in {event["identifier"] for event in exposures}
    assert result["exposures_tracked"] == names.IDENTITY_COUNT - 1
    assert "user-150" not in result["assignments"]
    assert result["not_in_experiment"] == ["user-150"]


def test_router_exposures_carry_design_md_arguments(fake_flagsmith, local_client_factory):
    handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    first = fake_flagsmith.events()[0]
    assert first["event"] == "$flag_exposure"
    assert first["feature_name"] == SPIKE_FLAG
    assert first["identifier"] == "user-001"
    assert first["value"] == "cheaper"  # value=flag.variant
    assert first["metadata"] == {names.EXPOSURE_METADATA_KEY: EXPERIMENT_ID, "sdk_version": "6.7.0"}


def test_router_flushes_before_returning_and_says_what_was_delivered(
    fake_flagsmith, local_client_factory
):
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    # The handler snapshots deliveries right after flush_events() returns.
    assert len(fake_flagsmith.events()) == names.IDENTITY_COUNT
    assert result["events_delivered"] == names.IDENTITY_COUNT
    assert result["delivered_before_return"] is True
    assert result["events_refused"] is False
    assert result["event_posts"] == [
        {
            "status": 202,
            "events": names.IDENTITY_COUNT,
            "elapsed_ms": result["event_posts"][0]["elapsed_ms"],
        }
    ]


def test_router_records_a_control_arm_that_reports_no_variant(fake_flagsmith, local_client_factory):
    def flag_for(identifier):
        flag = default_flag(identifier)
        if flag["feature_state_value"] == names.VARIANT_CURRENT:
            flag["variant"] = None
        return flag

    fake_flagsmith.flag_for = flag_for
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    assert result["samples"][names.VARIANT_CURRENT]["variant"] is None
    assert result["variants_by_arm"][names.VARIANT_CURRENT] == [{"variant": None, "count": 75}]


def test_router_reports_a_refused_key(fake_flagsmith, local_client_factory):
    fake_flagsmith.events_status = 401
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    assert result["events_refused"] is True
    assert result["delivered_before_return"] is False
    assert any("refused the environment key" in r["message"] for r in result["sdk_log"])


def test_router_stops_after_one_identity_when_spike_flag_is_missing(
    fake_flagsmith, local_client_factory
):
    fake_flagsmith.flag_for = lambda identifier: None
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    assert len(fake_flagsmith.identity_bodies) == 1
    assert "spike_flag" in result["aborted"]
    assert result["assignments"] == {}


def test_router_stops_after_one_identity_when_no_experiment_is_running(
    fake_flagsmith, local_client_factory
):
    def flag_for(identifier):
        flag = default_flag(identifier)
        flag["metadata"] = {}
        return flag

    fake_flagsmith.flag_for = flag_for
    result = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)

    assert len(fake_flagsmith.identity_bodies) == 1
    assert "experiment" in result["aborted"]
    assert fake_flagsmith.events() == []


# ---------------------------------------------------------------------------
# Invocation 2: the judge's role
# ---------------------------------------------------------------------------
def assignments_by_parity() -> dict[str, str]:
    return {
        names.identity(n): names.VARIANT_CHEAPER if n % 2 else names.VARIANT_CURRENT
        for n in range(1, names.IDENTITY_COUNT + 1)
    }


def test_outcome_values_are_mostly_one_for_cheaper_and_mostly_zero_for_current():
    cheaper = [handler.outcome_value(names.VARIANT_CHEAPER, i) for i in range(10)]
    current = [handler.outcome_value(names.VARIANT_CURRENT, i) for i in range(10)]
    assert cheaper == [1, 1, 1, 1, 0, 1, 1, 1, 1, 0]
    assert current == [0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    with pytest.raises(ValueError):
        handler.outcome_value("control", 0)


def test_judge_sends_one_event_per_identity_and_makes_no_identity_request(
    fake_flagsmith, local_client_factory
):
    result = handler.run_judge(
        DUMMY_SDK_KEY, assignments_by_parity(), make_client=local_client_factory
    )

    assert fake_flagsmith.identity_bodies == []
    assert result["identity_requests"]["count"] == 0
    events = fake_flagsmith.events()
    assert len(events) == names.IDENTITY_COUNT
    assert {event["event"] for event in events} == {handler.SPIKE_EVENT}
    assert [event["identifier"] for event in events] == [
        names.identity(n) for n in range(1, names.IDENTITY_COUNT + 1)
    ]
    assert result["delivered_before_return"] is True
    assert result["events_delivered"] == names.IDENTITY_COUNT


def test_judge_values_differ_by_arm_and_the_expected_means_match_what_was_sent(
    fake_flagsmith, local_client_factory
):
    assignments = assignments_by_parity()
    result = handler.run_judge(DUMMY_SDK_KEY, assignments, make_client=local_client_factory)

    sent: dict[str, list[int]] = {names.VARIANT_CHEAPER: [], names.VARIANT_CURRENT: []}
    for event in fake_flagsmith.events():
        sent[assignments[event["identifier"]]].append(int(event["value"]))
    for arm, values in sent.items():
        expected = result["expected"][arm]
        assert expected["identities"] == len(values) == 75
        assert expected["sum"] == sum(values)
        assert expected["mean"] == pytest.approx(sum(values) / len(values))
    assert result["expected"][names.VARIANT_CHEAPER]["mean"] == pytest.approx(0.8)
    assert result["expected"][names.VARIANT_CURRENT]["mean"] == pytest.approx(0.2)


def test_judge_uses_a_new_client_not_the_routers(fake_flagsmith, local_client_factory):
    routed = handler.run_router(DUMMY_SDK_KEY, make_client=local_client_factory)
    judged = handler.run_judge(
        DUMMY_SDK_KEY, routed["assignments"], make_client=local_client_factory
    )

    first, second = local_client_factory.made
    assert first is not second
    assert routed["client"]["instance"] != judged["client"]["instance"]
    assert len(fake_flagsmith.identity_bodies) == names.IDENTITY_COUNT  # all from the router


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"user-001": "control"},
        {"user-151": names.VARIANT_CHEAPER},
        {"alice": names.VARIANT_CURRENT},
        None,
    ],
)
def test_judge_rejects_assignments_it_cannot_use(bad, local_client_factory):
    with pytest.raises(ValueError):
        handler.run_judge(DUMMY_SDK_KEY, bad, make_client=local_client_factory)
    assert local_client_factory.made == []  # rejected before any client exists


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------
@pytest.fixture
def lambda_env(monkeypatch, fake_flagsmith, local_client_factory):
    secrets = FakeSecrets(names.SECRET_FLAGSMITH_SERVER_KEY, DUMMY_SDK_KEY)
    monkeypatch.setenv(handler.SDK_KEY_SECRET_ENV, names.SECRET_FLAGSMITH_SERVER_KEY)
    monkeypatch.setattr(handler, "secrets_client", lambda: secrets)
    monkeypatch.setattr(handler, "lambda_flagsmith_client", local_client_factory)
    return secrets


def test_handler_router_then_judge_round_trip_never_returns_the_key(lambda_env, fake_flagsmith):
    routed = handler.handler({"action": "router"}, None)
    judged = handler.handler({"action": "judge", "assignments": routed["assignments"]}, None)

    for result in (routed, judged):
        assert DUMMY_SDK_KEY not in json.dumps(result)
        assert result["key_is_server_side"] is False
        assert "redaction_applied" not in result
        json.dumps(result)  # the Lambda response must be JSON
    assert routed["action"] == "router" and judged["action"] == "judge"
    assert judged["container"]["invocation"] == routed["container"]["invocation"] + 1
    assert len(fake_flagsmith.events()) == 2 * names.IDENTITY_COUNT


def test_handler_converse_does_not_read_the_secret(monkeypatch, lambda_env):
    bedrock = FakeBedrock(lambda request: text_response('{"helpful": true}'))
    monkeypatch.setattr(handler, "bedrock_client", lambda: bedrock)

    result = handler.handler({"action": "converse", "include_optional": False}, None)

    assert lambda_env.asked == []
    assert result["required_ok"] is True


def test_selfcheck_loads_the_compiled_modules_without_a_secret_or_a_request(
    lambda_env, fake_flagsmith, local_client_factory
):
    result = handler.handler({"action": "selfcheck"}, None)

    assert result["ok"] is True
    assert set(result["modules"]) == {
        "flagsmith",
        "flag_engine",
        "regex",
        "iregexp_check",
        "charset_normalizer",
    }
    assert all(entry["loaded"] for entry in result["modules"].values())
    assert result["versions"]["flagsmith"] == "6.7.0"
    assert lambda_env.asked == []  # no secret read
    assert local_client_factory.made == []  # no Flagsmith client
    assert fake_flagsmith.identity_bodies == [] and fake_flagsmith.events() == []


def test_handler_rejects_an_unknown_action(lambda_env):
    with pytest.raises(ValueError, match="action"):
        handler.handler({"action": "end_experiment"}, None)
    assert lambda_env.asked == []


def test_redact_replaces_the_key_and_says_so():
    result = {"message": f"oops {DUMMY_SDK_KEY}", "nested": [{"k": DUMMY_SDK_KEY}]}
    safe = handler.redact(result, DUMMY_SDK_KEY)
    assert DUMMY_SDK_KEY not in json.dumps(safe)
    assert safe["redaction_applied"] is True
    assert handler.redact({"fine": 1}, DUMMY_SDK_KEY) == {"fine": 1}
