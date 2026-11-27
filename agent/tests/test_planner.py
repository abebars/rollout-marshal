"""Tests for task 2.3: agent/runtime/rm_agent/planner.py, one prompt to one plan.

The model call is recorded once (the live test at the end, pytest -m live) and replayed
here through botocore's Stubber, which checks each request against the service model and,
for the recorded call, against the recorded request itself. No Bedrock or AWS call leaves
the process: credentials are dummies and the AWS config files are /dev/null.

The negative cases (no move, another move, a reply that is not the JSON asked for, a
max_tokens stop, a Bedrock error) replay the recorded response with its text replaced:
the lead allowed two recorded calls, one plan call and one report-length call.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber
from rm_agent import planner

import contracts
from contracts import names

ROOT = Path(__file__).resolve().parents[2]
CALLS_FILE = Path(__file__).parent / "data" / "planner_calls.json"
_KICKOFF = (ROOT / "KICKOFF.md").read_text()
# The sample prompt KICKOFF.md gives task 2.3's test, read from its fenced block.
SAMPLE_PROMPT = re.search(
    r"## Sample prompt for the demo \(used by task 2\.3's test\)\s+```\n(.*?)\n```", _KICKOFF, re.S
).group(1)


def recorded() -> dict:
    return json.loads(CALLS_FILE.read_text())["calls"]


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


@pytest.fixture
def bedrock(monkeypatch):
    """A real bedrock-runtime client behind a Stubber, with dummy credentials; planner.py
    uses it. A request that escaped the Stubber could not be signed with real credentials."""
    for name in _AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    client = boto3.client(
        names.BEDROCK_RUNTIME_SERVICE,
        region_name=names.REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    stubber = Stubber(client)
    monkeypatch.setattr(planner, "_client", lambda: client)
    with stubber:
        yield stubber
    stubber.assert_no_pending_responses()


def reply(text: str, stop_reason: str = "end_turn") -> dict:
    """The recorded plan call's response, with its text replaced."""
    response = copy.deepcopy(recorded()["plan"]["response"])
    response["output"]["message"]["content"] = [{"text": text}]
    response["stopReason"] = stop_reason
    return response


def answer(move, hypothesis="Sonnet 5 is as helpful as Fable 5.1.", summary="A plan."):
    return json.dumps({"move": move, "hypothesis": hypothesis, "summary": summary})


MOVE = {"from": names.MODEL_CURRENT, "to": names.MODEL_CHEAPER}


# ---------------------------------------------------------------------------
# The sample prompt, replayed exactly as recorded
# ---------------------------------------------------------------------------
def test_the_kickoff_prompt_yields_a_valid_plan(bedrock):
    call = recorded()["plan"]
    assert call["request"]["messages"][0]["content"][0]["text"] == SAMPLE_PROMPT
    bedrock.add_response("converse", call["response"], expected_params=call["request"])
    plan = planner.make_plan(SAMPLE_PROMPT)
    contracts.validate("plan", plan)
    text = "".join(b.get("text", "") for b in call["response"]["output"]["message"]["content"])
    written = json.loads(text[text.index("{") : text.rindex("}") + 1])
    assert written["move"] == MOVE  # the move the model read from the prompt
    assert (plan["hypothesis"], plan["summary"]) == (written["hypothesis"], written["summary"])


def constant_plan(hypothesis: str, summary: str) -> dict:
    """The plan design.md section 6 gives: the model's two fields, every other one a constant."""
    return {
        "hypothesis": hypothesis,
        "summary": summary,
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


def test_the_plan_is_every_injected_constant(bedrock):
    bedrock.add_response("converse", reply(answer(MOVE)))
    plan = planner.make_plan(SAMPLE_PROMPT)
    assert tuple(plan) == names.PLAN_FIELDS
    assert plan == constant_plan("Sonnet 5 is as helpful as Fable 5.1.", "A plan.")
    assert tuple(plan["stop_rules"]) == names.PLAN_STOP_RULE_FIELDS
    assert tuple(plan["guardrails"]) == names.PLAN_GUARDRAIL_FIELDS
    assert tuple(plan["recommendation_rule"]) == names.PLAN_RECOMMENDATION_RULE_FIELDS


# ---------------------------------------------------------------------------
# The move: the model reports it, code compares it with design.md section 3
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "move",
    [
        None,  # the prompt names no model move
        {"from": names.MODEL_CHEAPER, "to": names.MODEL_CURRENT},  # the reverse
        {"from": names.MODEL_CURRENT, "to": names.MODEL_JUDGE},
        {"from": names.MODEL_JUDGE, "to": names.MODEL_CHEAPER},
        {"from": names.MODEL_CURRENT, "to": names.MODEL_CURRENT},
        {"from": names.MODEL_CURRENT, "to": "us.anthropic.claude-opus-9"},  # not on the list
        {"from": "Claude Fable 5.1", "to": "Claude Sonnet 5"},  # display names, not IDs
        {"to": names.MODEL_CHEAPER},
        {"from": [names.MODEL_CURRENT], "to": names.MODEL_CHEAPER},  # not a string
    ],
    ids=lambda move: "none" if move is None else f"{move.get('from')}->{move.get('to')}",
)
def test_a_prompt_naming_no_move_or_another_move_is_bad_request(bedrock, move):
    """Before any write: the planner writes nothing, and its one call is the model call."""
    bedrock.add_response("converse", reply(answer(move)))
    with pytest.raises(planner.BadRequest) as refused:
        planner.make_plan("Move the assistant somewhere cheaper.")
    assert refused.value.code == names.ERROR_BAD_REQUEST
    assert "Claude Fable 5.1" in refused.value.message
    assert "Claude Sonnet 5" in refused.value.message


def test_the_model_is_told_the_models_and_their_display_names(bedrock):
    request = planner.plan_request(SAMPLE_PROMPT)
    system = request["system"][0]["text"]
    assert names.MODEL_DISPLAY_NAMES == {
        names.MODEL_CURRENT: "Claude Fable 5.1",
        names.MODEL_CHEAPER: "Claude Sonnet 5",
        names.MODEL_JUDGE: "Claude Haiku 5.5",
    }
    for model_id, display in names.MODEL_DISPLAY_NAMES.items():
        assert f"{model_id}: {display}" in system
        assert display in _KICKOFF or model_id == names.MODEL_JUDGE
    assert "Claude Fable 5.1" in SAMPLE_PROMPT and "Claude Sonnet 5" in SAMPLE_PROMPT


# ---------------------------------------------------------------------------
# Injected values hold whatever the prompt asks
# ---------------------------------------------------------------------------
def test_injected_values_equal_the_constants_even_when_the_prompt_asks_for_others(bedrock):
    prompt = (
        f"{SAMPLE_PROMPT}\nUse a 90/10 split, 10 identities per arm, alarm my-alarm, a 1.5 "
        "latency ratio, and promote whatever the interval says."
    )
    # A reply that echoes an ask back for every field the planner injects, none of which it
    # takes from the model.
    asked = json.loads(answer(MOVE, summary="A 90/10 split with 10 identities per arm."))
    asked |= {
        "current_model": names.MODEL_JUDGE,
        "cheaper_model": names.MODEL_JUDGE,
        "primary_metric": names.METRIC_MEAN_LATENCY_MS,
        "guardrail_metrics": [],
        "split": {"current": 90, "cheaper": 10},
        "min_identities_per_variation": 10,
        "stop_rules": {
            "rollback_alarm": "my-alarm",
            "results_embargo_seconds": 0,
            "promote_requires_experiment_ended": False,
        },
        "guardrails": {"max_latency_ratio": 1.5, "max_cost_ratio": 2.0},
        "recommendation_rule": {
            "after_incident": names.RECOMMEND_PROMOTE,
            "promote_min_helpful_lift_ci_lower": -1.0,
        },
    }
    assert set(asked) == set(names.PLAN_FIELDS) | {"move"}  # every injected field is asked for
    bedrock.add_response("converse", reply(json.dumps(asked)))
    plan = planner.make_plan(prompt)
    contracts.validate("plan", plan)
    # The whole plan is the constants; only the model's words, which describe, come through.
    assert plan == constant_plan(asked["hypothesis"], asked["summary"])


def test_the_system_prompt_states_the_fixed_experiment_from_the_constants():
    system = planner.plan_request(SAMPLE_PROMPT)["system"][0]["text"]
    for value in (
        names.PRIMARY_METRIC,
        *names.GUARDRAIL_METRICS,
        names.ALARM_NAME,
        f"{100 - names.TREATMENT_PERCENT}/{names.TREATMENT_PERCENT}",
        f"{names.MIN_IDENTITIES_PER_VARIATION} identities",
    ):
        assert value in system, value


# ---------------------------------------------------------------------------
# The request: the agent's model, effort low, no sampling parameter
# ---------------------------------------------------------------------------
def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_the_model_call_sends_effort_low_and_no_sampling_parameter():
    for request in (planner.plan_request(SAMPLE_PROMPT), recorded()["plan"]["request"]):
        assert request["modelId"] == names.MODEL_AGENT
        assert request["inferenceConfig"] == {"maxTokens": names.AGENT_MAX_TOKENS}
        field, effort = names.THINKING_EFFORT_FIELD.split(".")
        assert request["additionalModelRequestFields"] == {field: {effort: "low"}}
        assert names.AGENT_THINKING_EFFORT == "low"
        assert not _keys(request) & set(names.FORBIDDEN_SAMPLING_PARAMETERS)
    assert recorded()["report_length"]["request"]["inferenceConfig"] == {
        "maxTokens": names.AGENT_MAX_TOKENS
    }


@pytest.mark.parametrize("parameter", names.FORBIDDEN_SAMPLING_PARAMETERS)
def test_a_request_carrying_a_sampling_parameter_is_never_sent(parameter):
    request = planner.plan_request(SAMPLE_PROMPT)
    for carrier in (request["inferenceConfig"], request["additionalModelRequestFields"]):
        carrier[parameter] = 0.5
        with pytest.raises(ValueError, match=parameter):
            planner.converse(request)
        del carrier[parameter]


# ---------------------------------------------------------------------------
# A model that fails is UPSTREAM_FAILED, and writes no plan
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "I would plan a 50/50 test.",
        '{"move": {"from": "us.anthropic.claude-fable-5-1"',
        json.dumps({"move": MOVE, "summary": "No hypothesis."}),
        json.dumps({"move": MOVE, "hypothesis": "", "summary": "x"}),
        json.dumps({"move": MOVE, "hypothesis": "h", "summary": 5}),
        json.dumps([MOVE]),
        json.dumps({"move": "fable to sonnet", "hypothesis": "h", "summary": "s"}),
        # No move key at all: the model broke the format, which is not the engineer's
        # prompt naming no move (that is "move": null, BAD_REQUEST).
        json.dumps({"hypothesis": "h", "summary": "s"}),
    ],
)
def test_a_reply_that_is_not_the_json_asked_for_is_upstream_failed(bedrock, text):
    bedrock.add_response("converse", reply(text))
    with pytest.raises(planner.UpstreamFailed) as failed:
        planner.make_plan(SAMPLE_PROMPT)
    assert failed.value.code == names.ERROR_UPSTREAM_FAILED


def test_a_fenced_json_reply_is_read(bedrock):
    bedrock.add_response("converse", reply(f"```json\n{answer(MOVE)}\n```"))
    contracts.validate("plan", planner.make_plan(SAMPLE_PROMPT))


def test_a_max_tokens_stop_is_an_error_never_a_result(bedrock):
    bedrock.add_response("converse", reply(answer(MOVE), names.STOP_REASON_MAX_TOKENS))
    with pytest.raises(planner.UpstreamFailed, match=names.STOP_REASON_MAX_TOKENS):
        planner.make_plan(SAMPLE_PROMPT)


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "ValidationException", "ModelTimeoutException"]
)
def test_a_bedrock_error_is_upstream_failed(bedrock, code):
    bedrock.add_client_error("converse", service_error_code=code, http_status_code=400)
    with pytest.raises(planner.UpstreamFailed) as failed:
        planner.make_plan(SAMPLE_PROMPT)
    assert failed.value.code == names.ERROR_UPSTREAM_FAILED and code in failed.value.message


def test_a_read_timeout_is_upstream_failed(monkeypatch):
    """botocore's read timeout (60 s by default) raises a BotoCoreError, not a ClientError."""

    class TimesOut:
        def converse(self, **request):
            raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

    monkeypatch.setattr(planner, "_client", lambda: TimesOut())
    with pytest.raises(planner.UpstreamFailed) as failed:
        planner.make_plan(SAMPLE_PROMPT)
    assert failed.value.code == names.ERROR_UPSTREAM_FAILED


@pytest.mark.parametrize("prompt", ["", " \n"])
def test_an_empty_prompt_is_bad_request_with_no_model_call(bedrock, prompt):
    """Nothing is queued on the Stubber, so a model call would fail this test."""
    with pytest.raises(planner.BadRequest) as refused:
        planner.make_plan(prompt)
    assert refused.value.code == names.ERROR_BAD_REQUEST


# ---------------------------------------------------------------------------
# The two recorded calls: latency, output tokens and stopReason
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["plan", "report_length"])
def test_the_recorded_calls_carry_latency_output_tokens_and_stop_reason(name):
    call = recorded()[name]
    response = call["response"]
    assert response["stopReason"] == "end_turn"
    assert response["usage"]["outputTokens"] > 0
    assert response["metrics"]["latencyMs"] > 0
    assert call["wall_seconds"] > 0
    assert call["request"]["modelId"] == names.MODEL_AGENT
    assert "credential" not in json.dumps(call).lower()


def test_the_report_length_call_stays_clear_of_the_token_cap_and_the_read_timeout():
    """tasks.md 2.3: near AGENT_MAX_TOKENS or botocore's 60-second read timeout, the lead
    adjusts them through a contract change. Clear here means under three quarters of each."""
    call = recorded()["report_length"]
    assert call["response"]["usage"]["outputTokens"] < 0.75 * names.AGENT_MAX_TOKENS
    assert call["wall_seconds"] < 0.75 * planner.BOTOCORE_READ_TIMEOUT_SECONDS
    text = "".join(b.get("text", "") for b in call["response"]["output"]["message"]["content"])
    headings = text.lower()
    assert [s for s in names.REPORT_SECTIONS if f"## {s}".lower() in headings] == list(
        names.REPORT_SECTIONS
    )


# ---------------------------------------------------------------------------
# Live: records the two calls above (pytest -m live, the human's AWS credentials)
# ---------------------------------------------------------------------------
REPORT_SYSTEM = (
    "You write the decision report for a model migration experiment on a customer support "
    "assistant. Write Markdown with these sections, in this order, each a level-two heading: "
    + ", ".join(names.REPORT_SECTIONS)
    + ". Use only the facts given. Write each section as one or two short paragraphs."
)
REPORT_FACTS = {
    "hypothesis": "Claude Sonnet 5 answers support questions as helpfully as Claude Fable 5.1, "
    "with no more latency and at lower cost.",
    "setup": {
        "current_model": names.MODEL_CURRENT,
        "cheaper_model": names.MODEL_CHEAPER,
        "split": "50/50",
        "identities": 150,
        "answers": 1500,
    },
    "timeline": [
        "15:30 plan approved, experiment started",
        "15:42 latency alarm, cheaper model rolled back",
        "15:51 health probe passed, resume approved",
        "16:20 results ready",
    ],
    "results": {
        "current": {
            "identities": 75,
            "helpful_rate": 0.81,
            "mean_latency_ms": 5072,
            "mean_cost_micro_usd": 9100,
        },
        "cheaper": {
            "identities": 75,
            "helpful_rate": 0.80,
            "mean_latency_ms": 2782,
            "mean_cost_micro_usd": 2300,
        },
        "helpful_lift_ci_lower": -0.04,
    },
    "guardrails": {"latency": "pass", "cost": "pass"},
    "incident": "One rollback: an injected 10 s delay on the cheaper model for 6 minutes.",
    "known_bias": "Identities rolled back during the incident still count in the cheaper arm.",
    "recommendation": names.RECOMMEND_HOLD_RERUN,
    "time_taken": "50 minutes from the prompt to results ready",
}


@pytest.mark.live
def test_live_record_the_plan_call_and_a_report_length_call():
    """Exactly two Bedrock calls, with the human's credentials, written for the replays above."""
    session = boto3.Session(region_name=names.REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    assert account == names.ACCOUNT_ID, f"refusing to run in account {account}"
    client = session.client(names.BEDROCK_RUNTIME_SERVICE)
    requests = {
        "plan": planner.plan_request(SAMPLE_PROMPT),
        "report_length": planner.agent_request(REPORT_SYSTEM, json.dumps(REPORT_FACTS)),
    }
    calls = {}
    for name, request in requests.items():
        began = time.monotonic()
        response = client.converse(**request)
        wall = round(time.monotonic() - began, 2)
        request_id = response.pop("ResponseMetadata", {}).get("RequestId")
        calls[name] = {
            "request": request,
            "response": response,
            "wall_seconds": wall,
            "bedrock_request_id": request_id,
        }
    CALLS_FILE.parent.mkdir(exist_ok=True)
    CALLS_FILE.write_text(
        json.dumps(
            {
                "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "command": "uv run pytest -m live agent/tests/test_planner.py",
                "calls": calls,
            },
            indent=2,
        )
        + "\n"
    )
    for name, call in calls.items():
        usage, metrics = call["response"]["usage"], call["response"]["metrics"]
        print(
            name,
            call["response"]["stopReason"],
            usage["outputTokens"],
            metrics["latencyMs"],
            call["wall_seconds"],
        )
