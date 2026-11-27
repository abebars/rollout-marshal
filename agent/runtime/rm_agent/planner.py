"""The planner (tasks.md 2.3; design.md sections 3 and 6): one engineer prompt, one plan.

The model writes the plan's hypothesis and summary, and reports the model move it read
from the prompt as two model IDs from the list it is given. Code compares that move with
design.md section 3's, MODEL_CURRENT to MODEL_CHEAPER: no move, or any other, is
BAD_REQUEST before anything is written. Every other plan value is injected from
contracts.names, whatever the prompt asks (design.md section 6), so a plan whose values
came from the prompt cannot be made.

The call is the agent's own prose request (design.md section 3): MODEL_AGENT, maxTokens
AGENT_MAX_TOKENS, thinking effort AGENT_THINKING_EFFORT in additionalModelRequestFields,
and no sampling parameter. A Bedrock error, a max_tokens stop, or a reply that is not the
JSON asked for is UPSTREAM_FAILED: nothing changed.

Imports only contracts, the standard library, boto3 and botocore.
"""

from __future__ import annotations

import functools
import json
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from contracts import names

# The names the engineer uses (KICKOFF.md's sample prompt), for the move check.
MODEL_DISPLAY_NAMES = {  # names.MODEL_DISPLAY_NAMES after c3
    names.MODEL_CURRENT: "Claude Fable 5.1",
    names.MODEL_CHEAPER: "Claude Sonnet 5",
    names.MODEL_JUDGE: "Claude Haiku 5.5",
}
# tasks.md 2.3: a report-length call near this tells the lead to adjust it.
BOTOCORE_READ_TIMEOUT_SECONDS = Config().read_timeout
_MOVE = (names.MODEL_CURRENT, names.MODEL_CHEAPER)  # the only move design.md section 3 plans


class PlannerError(Exception):
    """A refusal or a failure that carries one of design.md section 7's error codes."""

    code: str

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadRequest(PlannerError):
    """The prompt names no model move, or a move other than Fable 5.1 to Sonnet 5."""

    code = names.ERROR_BAD_REQUEST


class UpstreamFailed(PlannerError):
    """The model call failed, stopped at max_tokens, or replied with something unreadable."""

    code = names.ERROR_UPSTREAM_FAILED


@functools.cache
def _client():
    """The bedrock-runtime client, made on first use, in us-east-1 (design.md section 2)."""
    return boto3.client(names.BEDROCK_RUNTIME_SERVICE, region_name=names.REGION)


def _keys(value: Any) -> set[str]:
    """Every key at any depth."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def _display(model_id: Any) -> str:
    return MODEL_DISPLAY_NAMES.get(model_id, repr(model_id))


def _system_prompt() -> str:
    models = "\n".join(f"- {model_id}: {name}" for model_id, name in MODEL_DISPLAY_NAMES.items())
    split = f"{names.TREATMENT_PERCENT}/{names.TREATMENT_PERCENT}"
    guardrails = " and ".join(names.GUARDRAIL_METRICS)
    return (
        "You plan an experiment that moves a customer support assistant from one Claude "
        "model on Amazon Bedrock to another.\n\n"
        f"The models:\n{models}\n\n"
        "The experiment is fixed, and the engineer's request cannot change it, whatever it "
        "asks:\n"
        f"- identities are split {split} between the current model and the cheaper one;\n"
        f"- {names.PRIMARY_METRIC} is the primary metric, and {guardrails} are guardrails;\n"
        "- results count once each variation has at least "
        f"{names.MIN_IDENTITIES_PER_VARIATION} identities;\n"
        f"- the latency alarm {names.ALARM_NAME} rolls the cheaper model back without "
        "approval;\n"
        "- promotion waits for the engineer to end the experiment and approve.\n\n"
        "Read the engineer's request and answer with one JSON object and nothing else:\n"
        '{"move": {"from": "<model ID>", "to": "<model ID>"}, "hypothesis": "<one sentence>", '
        '"summary": "<a short paragraph>"}\n\n'
        "- move: the model the request moves away from and the model it moves to, as model "
        "IDs from the list above. If the request names no move between two models on the "
        "list, move is null.\n"
        "- hypothesis: one testable sentence about the move, in terms of the metrics above.\n"
        "- summary: the plan in plain words for the engineer's approval, true to the fixed "
        "experiment above."
    )


def agent_request(system: str, text: str) -> dict[str, Any]:
    """A Converse request for the agent's own prose: the plan, the incident note, the
    report (design.md section 3)."""
    field, key = names.THINKING_EFFORT_FIELD.split(".")
    return {
        "modelId": names.MODEL_AGENT,
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": [{"text": text}]}],
        "inferenceConfig": {"maxTokens": names.AGENT_MAX_TOKENS},
        "additionalModelRequestFields": {field: {key: names.AGENT_THINKING_EFFORT}},
    }


def plan_request(prompt: str) -> dict[str, Any]:
    """The planner's request for one engineer prompt."""
    return agent_request(_system_prompt(), prompt)


def converse(request: dict[str, Any]) -> str:
    """Send one Converse request and return its text.

    ValueError, and nothing sent, for a request that carries a sampling parameter at any
    depth. UpstreamFailed for a Bedrock error or a max_tokens stop.
    """
    found = sorted(_keys(request) & set(names.FORBIDDEN_SAMPLING_PARAMETERS))
    if found:
        raise ValueError(f"no sampling parameter is sent to any model: {found}")
    try:
        response = _client().converse(**request)
    except (ClientError, BotoCoreError) as error:
        raise UpstreamFailed(f"the model call failed: {error}") from error
    stop = response.get("stopReason")
    if stop == names.STOP_REASON_MAX_TOKENS:
        raise UpstreamFailed(f"the model stopped at {stop}: an error, never a result")
    content = ((response.get("output") or {}).get("message") or {}).get("content") or []
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _read(text: str) -> dict[str, Any]:
    """The JSON object the planner asked for, or UpstreamFailed."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise UpstreamFailed("the model's reply holds no JSON object")
    try:
        reply = json.loads(text[start : end + 1])
    except ValueError as error:
        raise UpstreamFailed(f"the model's reply is not JSON: {error}") from error
    if not isinstance(reply, dict) or "move" not in reply:
        raise UpstreamFailed("the model's reply is not the object asked for")
    for field in names.PLAN_MODEL_FIELDS:
        value = reply.get(field)
        if not isinstance(value, str) or not value.strip():
            raise UpstreamFailed(f"the model's reply has no {field}")
    if reply["move"] is not None and not isinstance(reply["move"], dict):
        raise UpstreamFailed(
            f"the model's reply has a move that is not an object: {reply['move']!r}"
        )
    return reply


def _plan(hypothesis: str, summary: str) -> dict[str, Any]:
    """The plan of design.md section 6: the model's two fields, every other value injected."""
    return {
        "hypothesis": hypothesis,
        "summary": summary,
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


def make_plan(prompt: str) -> dict[str, Any]:
    """One plan for the engineer's prompt (contracts/schemas/plan.json).

    BadRequest when the prompt is empty, names no model move, or names any move other than
    Fable 5.1 to Sonnet 5. UpstreamFailed when the model call fails. Writes nothing.
    """
    wanted = f"{_display(_MOVE[0])} to {_display(_MOVE[1])}"
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadRequest(f"the prompt is empty; this experiment moves {wanted}")
    reply = _read(converse(plan_request(prompt)))
    move = reply["move"]
    read = (move.get("from"), move.get("to")) if isinstance(move, dict) else None
    if read != _MOVE:
        asked = (
            "no model move"
            if read is None
            else f"a move from {_display(read[0])} to {_display(read[1])}"
        )
        raise BadRequest(f"the request names {asked}; this experiment moves only {wanted}")
    return _plan(reply["hypothesis"], reply["summary"])
