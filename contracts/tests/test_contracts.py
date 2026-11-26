"""Contract tests (tasks.md 1.1): contracts.names and the schemas match design.md.

design.md is parsed here, so a contract change that is not mirrored in
contracts/ fails this suite, and so does a name in contracts/ that design.md
does not give.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from functools import cache
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

import contracts
from contracts import names

ROOT = Path(__file__).resolve().parents[2]
DESIGN_PATH = ROOT / "openspec/changes/build-rollout-marshal/design.md"
DESIGN = DESIGN_PATH.read_text()
FIXED = datetime(2026, 11, 27, 15, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# design.md parsing
# ---------------------------------------------------------------------------
@cache
def sections(*, keep_fences: bool) -> dict[int, str]:
    text = DESIGN if keep_fences else re.sub(r"^```.*?^```", "", DESIGN, flags=re.S | re.M)
    parts = re.split(r"^(## \d+\. .*)$", text, flags=re.M)
    out = {}
    for i in range(1, len(parts), 2):
        out[int(re.match(r"## (\d+)\.", parts[i]).group(1))] = parts[i + 1]
    return out


def section(n: int, *, keep_fences: bool = False) -> str:
    return sections(keep_fences=keep_fences)[n]


def blocks(text: str) -> list[str]:
    return re.split(r"\n\s*\n", text)


def code_spans(text: str) -> list[tuple[str, str]]:
    """(span, prose before it in the same block) for every `code span`."""
    out = []
    for block in blocks(text):
        segments = block.split("`")
        assert len(segments) % 2 == 1, f"unbalanced backticks in design.md: {block[:80]!r}"
        for j in range(1, len(segments), 2):
            prose_before = " ".join(segments[k] for k in range(0, j, 2))
            out.append((segments[j], prose_before))
    return out


def single_line_spans(text: str) -> list[str]:
    return [span for span, _ in code_spans(text) if "\n" not in span]


def multi_line_spans(text: str) -> list[str]:
    return [span for span, _ in code_spans(text) if "\n" in span]


def table_rows(text: str, header_start: str) -> list[list[str]]:
    match = re.search(
        rf"^{re.escape(header_start)}.*\n\|[- |]+\|\n((?:\|.*\|\n?)+)", text, flags=re.M
    )
    assert match, f"design.md table not found: {header_start}"
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in match.group(1).strip().splitlines()
    ]


def ticks(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


# ---------------------------------------------------------------------------
# The 1.1 Pass check: states, actions, error codes and feature keys
# ---------------------------------------------------------------------------
def design_transitions() -> list[tuple[str | None, str, tuple[str, ...], str]]:
    rows = table_rows(section(6), "| From | To | Trigger | Actor |")
    out = []
    for source, target, trigger, actor in rows:
        src = ticks(source)
        out.append((src[0] if src else None, ticks(target)[0], tuple(ticks(trigger)), actor))
    return out


def design_states() -> set[str]:
    states = set()
    for source, target, _, _ in design_transitions():
        states.update(s for s in (source, target) if s)
    terminal = re.search(r"^Terminal states: (.*)$", section(6), flags=re.M).group(1)
    states.update(ticks(terminal))
    return states


def design_actions() -> list[str]:
    return re.findall(r'"action": "(\w+)"', section(7, keep_fences=True))


def design_error_codes() -> list[str]:
    line = re.search(r"^Error codes: (.*?)\.$", section(7), flags=re.M | re.S).group(1)
    return ticks(line)


SNAKE_KEY = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*_?(?:<run_id>)?$")


def design_feature_keys() -> set[str]:
    """Code spans that the prose just before them calls a feature, plus the
    bulleted list of permanent features."""
    keys = set()
    for n in range(3, 10):
        text = section(n)
        for span, before in code_spans(text):
            if "_" in span and SNAKE_KEY.match(span) and "feature" in before[-60:].lower():
                keys.add(span)
        parts = blocks(text)
        for i, block in enumerate(parts):
            if "permanent feature" in block.lower() and i + 1 < len(parts):
                keys.update(re.findall(r"^- `([^`]+)`", parts[i + 1], flags=re.M))
    return keys


def test_states_match_design():
    assert design_states() == set(names.STATES)
    assert len(names.STATES) == len(set(names.STATES))


def test_terminal_states_match_design():
    terminal = re.search(r"^Terminal states: (.*)$", section(6), flags=re.M).group(1)
    assert ticks(terminal) == list(names.TERMINAL_STATES)


def test_actions_match_design():
    assert design_actions() == list(names.ACTIONS)


def test_error_codes_match_design():
    assert design_error_codes() == list(names.ERROR_CODES)


def test_feature_keys_match_design():
    assert design_feature_keys() == set(names.FEATURE_KEYS)
    for key in names.FEATURE_KEYS:
        assert f"`{key}`" in DESIGN


def test_transitions_match_design():
    assert design_transitions() == [tuple(t) for t in names.TRANSITIONS]
    assert names.LEGAL_TRANSITIONS == {(t.source, t.target) for t in names.TRANSITIONS}


def test_transition_actors_and_triggers_are_known_names():
    for t in names.TRANSITIONS:
        assert t.actor in names.ACTORS
        assert set(t.triggers) <= set(names.ACTIONS)


def test_awaiting_values_match_design():
    line = re.search(r'"awaiting": (null(?: \| "\w+")+)', section(7, keep_fences=True))
    assert re.findall(r'"(\w+)"', line.group(1)) == list(names.AWAITING_VALUES)


def test_awaiting_by_state_follows_the_approve_rows():
    """Each awaiting value belongs to a state left by an approve the engineer gives."""
    approve_sources = {
        t.source
        for t in names.TRANSITIONS
        if names.ACTION_APPROVE in t.triggers and t.actor == names.ACTOR_ENGINEER
    }
    assert set(names.AWAITING_BY_STATE) == approve_sources
    assert set(names.AWAITING_BY_STATE.values()) == set(names.AWAITING_VALUES)


# ---------------------------------------------------------------------------
# Every backticked name in sections 2 to 11 has a constant (one direction)...
# ---------------------------------------------------------------------------
# Code spans that are not names of this project, each with the reason.
NOT_A_NAME = {
    "terraform apply": "a command in the apply rule",
    "terraform validate": "a command in the apply rule",
    "terraform plan": "a command in the apply rule",
    "maxTokens": "Converse API field; the value is ANSWER_MAX_TOKENS and friends",
    "stopReason": "Converse API field; the value is STOP_REASON_MAX_TOKENS",
    "additionalModelRequestFields": "Converse API field that carries THINKING_EFFORT_FIELD",
    "YYYYMMDDHHMM": "notation; see RUN_ID_TIME_FORMAT and run_id_for()",
    "HHMM": "notation; see SCRATCH_RUN_ID_TIME_FORMAT and scratch_run_id()",
    "r202611271530": "example; test_design_run_id_examples checks it",
    "rsmoke0412": "example; test_design_run_id_examples checks it",
    "value": "SDK argument that carries the event value; task 1.2 proves the signature",
    "track_exposure_event(feature, identifier, value=flag.variant, metadata="
    '{"experiment_id": flag.experiment.id})': (
        "call signature; SDK_EXPOSURE_CALL and EXPOSURE_METADATA_KEY, proven by task 1.2"
    ),
    "flag.experiment.in_experiment": "SDK attribute read before SDK_EXPOSURE_CALL",
    "InvokeAgentRuntime": "AWS API operation used by the CLI and rm-invoker",
    "cli/": "directory; design.md section 12 owns paths",
    "evidence/": "directory; design.md section 12 owns paths",
    "s3://rm-reports-818928662830/runs/<run_id>/report.md": (
        "REPORTS_BUCKET + REPORT_KEY_TEMPLATE; test_report_location checks it"
    ),
    "scripts/load.py --users N --questions M --url <api>": (
        "command line of LOAD_SCRIPT, owned by task 3.4"
    ),
}
PLACEHOLDER = re.compile(r"^<[a-z_]+>$|^\(none\)$")


def name_values() -> set[str]:
    """Every string, number and boolean reachable from names' upper-case constants."""
    values: set[str] = set()

    def walk(value):
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, bool | int | float):
            values.add(str(value))
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(k)
                walk(v)
        elif isinstance(value, list | tuple | set | frozenset):
            for v in value:
                walk(v)
        elif value is None:
            pass
        else:
            raise TypeError(f"unexpected constant type: {type(value)}")

    for attr, value in vars(names).items():
        if attr.isupper() and not attr.startswith("_"):
            walk(value)
    return values


def covered(span: str, values: set[str]) -> bool:
    if span in values or span in NOT_A_NAME:
        return True
    # Compound spans such as `pk = "ACTIVE"`, `Variant=cheaper`, `PLANNED -> RUNNING`,
    # `incident.started_at` and `flush_events()`: every part must be a name.
    parts = [p.strip().strip('"') for p in re.split(r"->|=|\.", span.removesuffix("()"))]
    parts = [p for p in parts if p and not PLACEHOLDER.match(p)]
    return bool(parts) and all(p in values for p in parts)


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
def test_every_name_in_design_section_has_a_constant(n):
    values = name_values()
    missing = sorted({s for s in single_line_spans(section(n)) if not covered(s, values)})
    assert missing == [], f"design.md section {n} names without a constant in contracts.names"


def test_not_a_name_entries_still_exist_in_design():
    spans = {s for n in range(2, 12) for s in single_line_spans(section(n))}
    assert set(NOT_A_NAME) <= spans, "remove NOT_A_NAME entries design.md no longer has"


def test_no_flagsmith_key_literal_in_design_or_contracts():
    """design.md section 8 describes the client-side key; its value stays out of the repo."""
    key_like = re.compile(r"`[A-Za-z0-9]{20,24}`")
    assert not key_like.search(section(8))
    for path in (ROOT / "contracts").rglob("*.py"):
        assert not re.search(r'"[A-Za-z0-9]{22}"', path.read_text()), path


# ---------------------------------------------------------------------------
# ...and the tables and shapes match exactly (the other direction)
# ---------------------------------------------------------------------------
def test_models_match_design():
    rows = {role: ticks(model)[0] for role, model, _ in table_rows(section(3), "| Role |")}
    assert rows == {
        "Current arm (control)": names.MODEL_CURRENT,
        "Cheaper arm (treatment)": names.MODEL_CHEAPER,
        "Judge": names.MODEL_JUDGE,
        "The agent's own reasoning": names.MODEL_AGENT,
    }


def test_lambda_timeouts_match_design():
    line = re.search(r"Lambda timeouts: (.*?)\.", re.sub(r"\s+", " ", section(3))).group(1)
    assert line == (
        f"answer functions {names.ANSWER_TIMEOUT_SECONDS} seconds, "
        f"router {names.ROUTER_TIMEOUT_SECONDS} seconds, "
        f"judge {names.JUDGE_TIMEOUT_SECONDS} seconds"
    )


def test_run_item_attributes_match_design():
    rows = table_rows(section(6), "| Attribute | Type | Meaning |")
    parsed = {}
    for attribute, kind, _ in rows:
        for name in ticks(attribute):
            parsed[name] = kind
    assert parsed == names.RUN_ITEM_ATTRIBUTES


def test_run_item_schema_covers_every_attribute():
    schema = contracts.load_schema("run_item")
    json_type = {"S": "string", "N": "integer", "M": "object", "L": "array"}
    props = schema["properties"]
    assert set(props) == set(names.RUN_ITEM_ATTRIBUTES) | {names.PARTITION_KEY}
    for attr, kind in names.RUN_ITEM_ATTRIBUTES.items():
        resolved = props[attr]
        if "$ref" in resolved:
            ref = resolved["$ref"]
            resolved = (
                contracts.load_schema("plan")
                if ref == "plan.json"
                else schema["$defs"][ref.split("/")[-1]]
            )
        if "type" in resolved:
            assert resolved["type"] == json_type[kind], attr
        else:
            assert "enum" in resolved and kind == "S", attr
    assert set(props["incident"]["properties"]) == set(names.INCIDENT_FIELDS)
    assert set(schema["$defs"]["history_entry"]["properties"]) == set(names.HISTORY_FIELDS)


def test_judge_payload_fields_match_design():
    span = next(s for s in multi_line_spans(section(5)) if s.startswith("{run_id"))
    fields = tuple(f.strip() for f in span.strip("{}").split(","))
    assert fields == names.JUDGE_PAYLOAD_FIELDS
    schema = contracts.load_schema("judge_payload")
    assert tuple(schema["required"]) == names.JUDGE_PAYLOAD_FIELDS


def test_response_fields_match_design():
    span = next(s for s in multi_line_spans(section(7)) if s.startswith('{"run_id"'))
    fields = re.findall(r'"(\w+)":', span)
    assert tuple(fields[:5]) == names.RESPONSE_FIELDS
    assert set(fields[5:]) == {"code", "message"}
    assert tuple(contracts.load_schema("response")["required"]) == names.RESPONSE_FIELDS


def test_results_fields_match_design():
    spans = multi_line_spans(section(7))
    top = next(s for s in spans if s.startswith('{"ready"'))
    variation = next(s for s in spans if s.startswith("V = "))
    top_fields = [f for f in re.findall(r'"(\w+)":', top) if f not in names.VARIANTS]
    assert tuple(top_fields) == names.RESULTS_FIELDS
    assert tuple(re.findall(r'"(\w+)":', variation)) == names.VARIATION_FIELDS
    schema = contracts.load_schema("results")
    assert tuple(schema["required"]) == names.RESULTS_FIELDS
    assert tuple(schema["$defs"]["variation"]["required"]) == names.VARIATION_FIELDS
    assert set(schema["properties"]["variations"]["required"]) == set(names.VARIANTS)


def test_cloudwatch_metrics_match_design():
    rows = table_rows(section(5), "| Metric | Unit | Emitted by |")
    units = {ticks(m)[0]: u.split(" ")[0] for m, u, _ in rows}
    emitters = {ticks(m)[0]: ticks(e)[0] for m, _, e in rows}
    assert units == names.CW_METRIC_UNITS
    assert emitters == names.CW_METRIC_EMITTER
    assert f"namespace `{names.CW_NAMESPACE}`" in section(5)
    assert names.CW_DIMENSION_SETS == [[names.CW_DIMENSION_VARIANT]]


def test_alarm_matches_design():
    prose = re.sub(r"\s+", " ", section(5))
    assert f"Alarm `{names.ALARM_NAME}`: `{names.ALARM_METRIC}`, `{names.ALARM_DIMENSION}`" in prose
    assert f"statistic {names.ALARM_STATISTIC}" in prose
    assert f"period {names.ALARM_PERIOD_SECONDS} seconds" in prose
    assert (
        f"{names.ALARM_DATAPOINTS_TO_ALARM} of {names.ALARM_EVALUATION_PERIODS} datapoints" in prose
    )
    assert "missing data treated as not breaching" in prose
    assert f"Threshold default {names.ALARM_THRESHOLD_MS} ms" in prose


def test_secrets_match_design():
    rows = table_rows(section(8), "| Name | Holds | Read by |")
    secret_names = [ticks(name)[0] for name, _, _ in rows]
    assert secret_names == [names.SECRET_FLAGSMITH_API_KEY, names.SECRET_FLAGSMITH_SERVER_KEY]


def test_fis_matches_design():
    prose = re.sub(r"\s+", " ", section(9))
    assert f"template `{names.FIS_TEMPLATE}`" in prose
    assert f"action `{names.FIS_ACTION}` on `{names.FIS_TARGET_FUNCTION}`" in prose
    assert f"{names.FIS_INVOCATION_PERCENTAGE} percent of invocations" in prose
    assert f"added delay {names.FIS_DELAY_MS} ms" in prose
    assert f"duration {names.FIS_DURATION_MINUTES} minutes" in prose
    assert f"`{names.FIS_SLOW_POLL_ENV}={names.FIS_SLOW_POLL_SECONDS}`" in prose


def test_report_sections_match_design():
    prose = re.sub(r"\s+", " ", section(10))
    listed = re.search(r"sections in this order: (.*?)\.", prose).group(1)
    assert tuple(s.strip() for s in listed.split(",")) == names.REPORT_SECTIONS


def test_report_location():
    span = next(s for s in single_line_spans(section(10)) if s.startswith("s3://"))
    assert span == f"s3://{names.REPORTS_BUCKET}/{names.REPORT_KEY_TEMPLATE}"
    assert names.report_key("r202611271530") == "runs/r202611271530/report.md"


def test_load_plans_match_design():
    rows = table_rows(section(11), "| Use | Users x questions | Answers |")
    plans = [tuple(int(x) for x in size.split(" x ")) for _, size, _ in rows]
    answers = [int(a.replace(",", "")) for _, _, a in rows]
    expected = [names.LOAD_SMOKE, names.LOAD_FAULT, names.LOAD_FULL]
    assert plans == [tuple(p) for p in expected]
    assert answers == [p.users * p.questions for p in expected]
    prose = re.sub(r"\s+", " ", section(11))
    assert f"= {names.ANSWER_BUDGET:,} answers" in prose
    assert f"before exceeding {names.ANSWER_HARD_STOP:,}" in prose
    assert f"At most {names.LOAD_MAX_CONCURRENCY} concurrent requests" in prose
    assert (
        names.LOAD_SMOKE.users * names.LOAD_SMOKE.questions
        + names.LOAD_FAULT.users * names.LOAD_FAULT.questions
        + 3 * names.LOAD_FULL.users * names.LOAD_FULL.questions
        == names.ANSWER_BUDGET
    )


def test_numbers_in_design_rules():
    prose = re.sub(r"\s+", " ", DESIGN)
    assert f"first {names.RESULTS_EMBARGO_SECONDS} seconds after" in prose
    assert f"older than {names.PENDING_STALE_SECONDS} seconds" in prose
    assert f"at most once every {names.RESULTS_REFRESH_MIN_SECONDS // 60} minutes" in prose
    assert f"at least {names.MIN_IDENTITIES_PER_VARIATION} identities" in prose
    assert f"probe is {names.PROBE_INVOKES} direct invokes" in prose
    assert f"at most {round(names.GUARDRAIL_MAX_LATENCY_RATIO * 100)} percent" in prose
    assert f"at least {names.PROMOTE_MIN_PROB_NOT_WORSE}" in prose
    assert (
        "at or above minus 5 percent" in prose and names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER == -0.05
    )
    assert f"every {names.TICK_INTERVAL_MINUTES} minutes" in prose
    assert f"at {names.TREATMENT_PERCENT} percent" in prose
    assert f"`{names.ANSWER_THINKING_EFFORT}` through" in prose
    assert f"`maxTokens` {names.ANSWER_MAX_TOKENS}" in prose
    assert f"`maxTokens` {names.JUDGE_MAX_TOKENS}" in prose
    assert f"`maxTokens` {names.AGENT_MAX_TOKENS}" in prose
    assert f"at most {names.ANSWER_MAX_WORDS} words" in prose
    assert f"{names.SESSION_ID_MIN_LENGTH} character minimum" in prose
    assert (
        f"{names.FLAGSMITH_SDK_PACKAGE}` Python package {names.FLAGSMITH_SDK_MIN_VERSION}" in prose
    )


# ---------------------------------------------------------------------------
# Helpers and formats
# ---------------------------------------------------------------------------
def test_design_run_id_examples():
    prose = section(4)
    assert "`r202611271530`" in prose and "`rsmoke0412`" in prose
    assert names.is_run_id("r202611271530")
    assert names.is_run_id("rsmoke0412")
    assert names.run_id_for(FIXED) == "r202611271530"
    assert names.scratch_run_id("smoke", FIXED.replace(hour=4, minute=12)) == "rsmoke0412"


@pytest.mark.parametrize("bad", ["rspike", "r12", "R202611271530", "rsmoke-0412", "", "x2026"])
def test_bad_run_ids_are_rejected(bad):
    assert not names.is_run_id(bad)
    with pytest.raises(ValueError):
        names.experiment_feature_key(bad)


def test_run_id_uses_utc():
    eastern = timezone(timedelta(hours=-5))
    assert names.run_id_for(datetime(2026, 11, 27, 10, 30, tzinfo=eastern)) == "r202611271530"
    with pytest.raises(ValueError):
        names.run_id_for(datetime(2026, 11, 27, 15, 30))


def test_scratch_word_must_be_letters():
    with pytest.raises(ValueError):
        names.scratch_run_id("spike1", FIXED)


def test_templates():
    run_id = names.run_id_for(FIXED)
    assert names.experiment_feature_key(run_id) == "assistant_model_r202611271530"
    assert names.EXPERIMENT_FEATURE_KEY_TEMPLATE.replace("<run_id>", run_id) == (
        names.experiment_feature_key(run_id)
    )
    assert names.run_pk(run_id) == "RUN#r202611271530"
    assert names.RUN_PK_TEMPLATE.replace("<run_id>", run_id) == names.run_pk(run_id)


def test_session_ids():
    a, b = names.new_session_id(), names.new_session_id()
    assert a != b
    assert a.startswith(names.SESSION_ID_PREFIX)
    assert len(a) >= names.SESSION_ID_MIN_LENGTH


def test_identities():
    assert names.identity(1) == names.IDENTITY_FIRST
    assert names.identity(names.IDENTITY_COUNT) == names.IDENTITY_LAST
    all_ids = [names.identity(n) for n in range(1, names.IDENTITY_COUNT + 1)]
    assert all(names.is_identity(i) for i in all_ids)
    for bad in ("user-000", "user-151", "user-1", "User-001"):
        assert not names.is_identity(bad)
    for n in (0, 151):
        with pytest.raises(ValueError):
            names.identity(n)


def test_awaiting_for():
    assert names.awaiting_for(names.STATE_PLANNED) == names.AWAITS_LAUNCH
    assert names.awaiting_for(names.STATE_RUNNING) is None
    assert names.awaiting_for(None) is None


def test_definitions_are_consistent():
    assert set(names.METRIC_DEFINITIONS) == set(names.METRICS)
    assert {d["event"] for d in names.METRIC_DEFINITIONS.values()} == set(names.EVENTS)
    assert names.METRIC_AGGREGATION in names.METRIC_AGGREGATIONS_AVAILABLE
    assert set(names.PERMANENT_FEATURE_DEFINITIONS) == set(names.PERMANENT_FEATURES)
    assert names.DEFAULT_MODEL_DEFAULT in names.DEFAULT_MODEL_VALUES
    assert set(names.MODEL_BY_VARIANT) == set(names.VARIANTS)
    assert set(names.ANSWER_FUNCTION_BY_VARIANT) == set(names.VARIANTS)
    assert names.FLAGSMITH_SDK_PACKAGE == "flagsmith"


def test_names_json_is_in_sync():
    path = ROOT / "contracts/names.json"
    assert path.read_text() == names.as_json(), "run: make names-json"
    assert json.loads(path.read_text())["ALARM_NAME"] == names.ALARM_NAME


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", names.SCHEMA_NAMES)
def test_schema_is_valid_draft_2020_12(name):
    schema = contracts.load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith(f"/contracts/schemas/{name}.json")
    assert (ROOT / names.SCHEMA_FILES[name]).is_file()


def test_schema_files_are_exactly_the_named_six():
    on_disk = {p.stem for p in contracts.SCHEMA_DIR.glob("*.json")}
    assert on_disk == set(names.SCHEMA_NAMES)


def valid(name, instance):
    contracts.validate(name, instance)


def invalid(name, instance):
    with pytest.raises(ValidationError):
        contracts.validate(name, instance)


def design_payloads() -> list[dict]:
    """The section 7 example payloads with their placeholders filled in."""
    fills = {
        "<text>": "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5.",
        "<id>": "r202611271530",
        "<ISO 8601>": "2026-11-27T15:42:00Z",
    }
    out = []
    for line in section(7, keep_fences=True).splitlines():
        if line.startswith('{"action"'):
            if "<id or omitted for the active run>" in line:
                out.append({"action": "status", "run_id": "r202611271530"})
                out.append({"action": "status"})
                continue
            for k, v in fills.items():
                line = line.replace(k, v)
            out.append(json.loads(line))
    return out


def test_design_payload_examples_validate():
    payloads = design_payloads()
    assert {p["action"] for p in payloads} == set(names.ACTIONS)
    for payload in payloads:
        valid("invoke", payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "promote", "run_id": "r202611271530", "actor": "engineer"},
        {"action": "start", "actor": "engineer"},
        {"action": "start", "prompt": "", "actor": "engineer"},
        {"action": "start", "prompt": "x"},
        {"action": "approve", "run_id": "r202611271530", "actor": "agent"},
        {"action": "approve", "run_id": "rspike", "actor": "engineer"},
        {"action": "decline", "actor": "engineer"},
        {"action": "check", "run_id": "r202611271530"},
        {"action": "status", "actor": "engineer"},
        {"action": "status", "run_id": "nope"},
        {"action": "tick", "run_id": "r202611271530"},
        {
            "action": "alarm",
            "alarm_name": "rm-cheaper-latency-p95",
            "alarm_state": "OK",
            "at": "2026-11-27T15:42:00Z",
        },
        {
            "action": "alarm",
            "alarm_name": "other",
            "alarm_state": "ALARM",
            "at": "2026-11-27T15:42:00Z",
        },
        {
            "action": "alarm",
            "alarm_name": "rm-cheaper-latency-p95",
            "alarm_state": "ALARM",
            "at": "yesterday",
        },
        {"action": "alarm", "alarm_name": "rm-cheaper-latency-p95", "alarm_state": "ALARM"},
    ],
)
def test_bad_payloads_are_rejected(payload):
    invalid("invoke", payload)


def response(**overrides):
    base = {
        "run_id": "r202611271530",
        "state": "PLANNED",
        "message": "Plan ready. Approve to launch.",
        "awaiting": "launch",
        "error": None,
    }
    return {**base, **overrides}


def test_responses_validate():
    for state in names.STATES:
        valid("response", response(state=state, awaiting=names.awaiting_for(state)))
    valid("response", response(run_id=None, state=None, awaiting=None, message="No active run."))
    for code in names.ERROR_CODES:
        valid("response", response(error={"code": code, "message": "why"}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"awaiting": None},
        {"state": "RUNNING", "awaiting": "launch"},
        {"state": "AWAITING_RESUME", "awaiting": "promotion"},
        {"state": None, "awaiting": "launch"},
        {"state": "DONE"},
        {"error": {"code": "TIMEOUT", "message": "x"}},
        {"error": {"code": "RUN_ACTIVE", "message": ""}},
        {"error": {"code": "RUN_ACTIVE"}},
        {"run_id": "rspike"},
        {"extra": 1},
    ],
)
def test_bad_responses_are_rejected(overrides):
    invalid("response", response(**overrides))


def test_response_state_enum_matches_names():
    schema = contracts.load_schema("response")
    assert schema["properties"]["state"]["enum"] == [None, *names.STATES]
    error = schema["properties"]["error"]["oneOf"][1]
    assert error["properties"]["code"]["enum"] == list(names.ERROR_CODES)
    assert schema["properties"]["awaiting"]["enum"] == [None, *names.AWAITING_VALUES]


def plan(**overrides):
    base = {
        "hypothesis": "Sonnet 5 answers support questions as helpfully as Fable 5.1, "
        "at lower cost and similar latency.",
        "summary": "Split traffic 50/50 between Fable 5.1 and Sonnet 5 and compare.",
        "current_model": names.MODEL_CURRENT,
        "cheaper_model": names.MODEL_CHEAPER,
        "primary_metric": names.PRIMARY_METRIC,
        "guardrail_metrics": list(names.GUARDRAIL_METRICS),
        "split": {v: names.TREATMENT_PERCENT for v in names.VARIANTS},
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
            "promote_min_prob_not_worse": names.PROMOTE_MIN_PROB_NOT_WORSE,
            "promote_min_helpful_lift_ci_lower": names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER,
        },
    }
    return {**base, **overrides}


def test_plan_built_from_names_validates():
    valid("plan", plan())


@pytest.mark.parametrize(
    "overrides",
    [
        {"split": {"current": 90, "cheaper": 10}},
        {"min_identities_per_variation": 10},
        {"cheaper_model": names.MODEL_JUDGE},
        {"primary_metric": "mean_latency_ms"},
        {"guardrails": {"max_latency_ratio": 1.5, "max_cost_ratio": 1.0}},
        {
            "stop_rules": {
                "rollback_alarm": "other",
                "results_embargo_seconds": 600,
                "promote_requires_experiment_ended": True,
            }
        },
        {
            "recommendation_rule": {
                "after_incident": "promote",
                "promote_min_prob_not_worse": 0.95,
                "promote_min_helpful_lift_ci_lower": -0.05,
            }
        },
        {"hypothesis": ""},
        {"rollout": 100},
    ],
)
def test_plans_with_values_not_from_names_are_rejected(overrides):
    invalid("plan", plan(**overrides))


def run_item(**overrides):
    base = {
        "pk": "RUN#r202611271530",
        "run_id": "r202611271530",
        "state": "PLANNED",
        "prompt": "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5.",
        "plan": plan(),
        "history": [
            {
                "at": "2026-11-27T15:30:05Z",
                "from": None,
                "to": "PLANNED",
                "actor": "engineer",
                "reason": "start",
            }
        ],
        "created_at": "2026-11-27T15:30:05Z",
        "updated_at": "2026-11-27T15:30:05.123456+00:00",
    }
    return {**base, **overrides}


def test_run_items_validate():
    valid("run_item", run_item())
    valid(
        "run_item",
        run_item(
            state="ROLLED_BACK",
            feature_key="assistant_model_r202611271530",
            experiment_id=4321,
            metric_ids={"helpful_rate": 1, "mean_latency_ms": 2, "mean_cost_micro_usd": 3},
            incident={"started_at": "2026-11-27T15:52:00Z", "alarm_name": names.ALARM_NAME},
            pending="AWAITING_RESUME",
            pending_at="2026-11-27T15:58:00Z",
            first_running_at="2026-11-27T15:35:00Z",
            last_refresh_at="2026-11-27T15:50:00Z",
            report_key="runs/r202611271530/report.md",
        ),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"pending": "RUNNING"},
        {"pending_at": "2026-11-27T15:58:00Z"},
        {"state": "DONE"},
        {"pk": "RUN#rspike"},
        {"created_at": "2026-11-27T10:30:05-05:00"},
        {"created_at": "2026-11-27"},
        {"history": []},
        {
            "history": [
                {
                    "at": "2026-11-27T15:30:05Z",
                    "from": None,
                    "to": "PLANNED",
                    "actor": "human",
                    "reason": "start",
                }
            ]
        },
        {"metric_ids": {"accuracy": 1}},
        {"incident": {"started_at": "2026-11-27T15:52:00Z"}},
        {"feature_key": "assistant_model_rspike"},
        {"experiment_id": "4321"},
        {"plan": plan(split={"current": 70, "cheaper": 30})},
    ],
)
def test_bad_run_items_are_rejected(overrides):
    invalid("run_item", run_item(**overrides))


def results(**overrides):
    variation = {
        "identities": 75,
        "helpful_rate": 0.9,
        "mean_latency_ms": 4200.5,
        "mean_cost_micro_usd": 8100.0,
    }
    base = {
        "ready": True,
        "variations": {"current": dict(variation), "cheaper": dict(variation)},
        "prob_cheaper_not_worse": None,
        "helpful_lift_ci_lower": -0.03,
    }
    return {**base, **overrides}


def test_results_validate():
    valid("results", results())
    empty = {
        "identities": 0,
        "helpful_rate": None,
        "mean_latency_ms": None,
        "mean_cost_micro_usd": None,
    }
    valid(
        "results",
        results(
            ready=False,
            variations={"current": empty, "cheaper": empty},
            helpful_lift_ci_lower=None,
            prob_cheaper_not_worse=None,
        ),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"prob_cheaper_not_worse": 1.5},
        {"ready": "yes"},
        {"variations": {"current": {}}},
        {"helpful_lift_ci_lower": "-5%"},
        {"win_probability": 0.9},
    ],
)
def test_bad_results_are_rejected(overrides):
    invalid("results", results(**overrides))


def judge_payload(**overrides):
    base = {
        "run_id": "r202611271530",
        "identity": "user-042",
        "assigned": "cheaper",
        "served": "cheaper",
        "model_id": names.MODEL_CHEAPER,
        "question": "How do I reset my password?",
        "answer": "Open Settings, choose Security, then Reset password.",
        "input_tokens": 180,
        "output_tokens": 95,
        "latency_ms": 3120.4,
    }
    return {**base, **overrides}


def test_judge_payloads_validate():
    valid("judge_payload", judge_payload())
    # Kill switch off: assigned cheaper, served current by the current model.
    valid("judge_payload", judge_payload(served="current", model_id=names.MODEL_CURRENT))


@pytest.mark.parametrize(
    "overrides",
    [
        {"identity": "user-151"},
        {"assigned": "control"},
        {"served": "current"},
        {"model_id": names.MODEL_JUDGE},
        {"run_id": None},
        {"input_tokens": -1},
        {"latency_ms": "3120"},
        {"verdict": "helpful"},
    ],
)
def test_bad_judge_payloads_are_rejected(overrides):
    invalid("judge_payload", judge_payload(**overrides))


def test_schema_constants_match_names():
    invoke = contracts.load_schema("invoke")
    assert invoke["$defs"]["run_id"]["pattern"] == names.RUN_ID_PATTERN
    judge = contracts.load_schema("judge_payload")
    assert judge["properties"]["identity"]["pattern"] == names.IDENTITY_PATTERN
    assert judge["properties"]["model_id"]["enum"] == [names.MODEL_CURRENT, names.MODEL_CHEAPER]
    run = contracts.load_schema("run_item")
    assert run["$defs"]["state"]["enum"] == list(names.STATES)
    assert run["$defs"]["history_entry"]["properties"]["actor"]["enum"] == list(names.ACTORS)
    assert run["properties"]["metric_ids"]["propertyNames"]["enum"] == list(names.METRICS)
