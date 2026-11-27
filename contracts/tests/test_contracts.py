"""Contract tests (tasks.md 1.1): contracts.names and the schemas match design.md.

design.md is parsed here, so a contract change that is not mirrored in
contracts/ fails this suite, and so does a name in contracts/ that design.md
does not give.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta, timezone
from functools import cache
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

import contracts
from contracts import dynamo, names

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


def design_feature_keys() -> list[str]:
    """Code spans that the prose just before them calls a feature, plus the
    bulleted list of permanent features."""
    keys: dict[str, None] = {}  # an ordered set: first appearance in design.md
    for n in range(3, 10):
        text = section(n)
        for span, before in code_spans(text):
            if "_" in span and SNAKE_KEY.match(span) and "feature" in before[-60:].lower():
                keys[span] = None
        parts = blocks(text)
        for i, block in enumerate(parts):
            if "permanent feature" in block.lower() and i + 1 < len(parts):
                for key in re.findall(r"^- `([^`]+)`", parts[i + 1], flags=re.M):
                    keys[key] = None
    return list(keys)


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
    assert tuple(design_feature_keys()) == names.FEATURE_KEYS
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
    "dev": "the integration branch",
    "--force": "scripts/reset.py's override flag",
    "TransactWriteItems": "DynamoDB API operation that carries every pointer change",
    "2#ROLLED_BACK": "example of dedupe_key(); test_dedupe_key_matches_design checks it",
    "marshal check <run_id>": "command line: CLI_NAME plus ACTION_CHECK",
    "flag.variant": "SDK attribute the exposure call sends",
    "flag.value": "SDK attribute: the variant's value, which names the model served",
    "api_key_authorization()": "helper in contracts.names; test_spike_findings_match_design",
    # contract change c1 (Gate 1)
    "--profile": "an AWS CLI option in the review-access rule",
    "--profile rm-review": "the AWS CLI option and REVIEW_PROFILE, in the review-access rule",
    "AWS_PROFILE": "an AWS CLI environment variable the review-access rule warns about",
    "run_identity()": "helper in contracts.names; test_run_identities",
    "is_run_identity()": "helper in contracts.names; test_run_identities",
    "--run-id": "load.py's option, in section 4's identity rule",
    "-user-": "the separator in a run identity, in section 4's identity rule",
    "app/": "B's root package (design.md section 12)",
    "import app": "the Python statement the entrypoint's name avoids",
    "agent/tests/": "a repo path (design.md section 12, A's agent/)",
    "codeLocation": "the AgentCore CLI's agentcore.json field",
    "contracts": "the repo's contracts package, linked into the code folder",
    "pyproject.toml": "a file name",
    "pythonpath": "the pytest setting in the root pyproject.toml",
    "uv.lock": "a file name",
    "--url": "load.py's option, in section 11's request contract",
    # contract change c2 (Gate 1)
    "build.sh": "each function's build script, which make build runs",
    "make build": "a Makefile target the lead owns",
    "cli": "the repo's cli package (design.md section 12)",
    "uv run marshal": "the command the packaged console script gives",
    "flagsmith___delete_*": "every MCP delete tool, left out of 2.2's allow-list",
    "archive_file": "Terraform's archive data source, which a module may package with",
    "load.py": "the load script, named in section 11's request contract",
    "<project>_<target>_<runtime>": (
        "the AgentCore CLI's naming pattern; see AGENT_RUNTIME_DEPLOYED_NAME"
    ),
    "flagsmith___<tool>": "the Gateway's tool-name pattern for target GATEWAY_TARGET_FLAGSMITH",
    "scripts/inject_fault.sh --fallback": "command line of INJECT_FAULT_SCRIPT's flag fallback",
    "agent/": "directory; design.md section 12 owns paths",
    "assistant_model_rspike<HHMM>": (
        "example: experiment_feature_key(scratch_run_id('spike', now)); "
        "test_spike_rules_match_design checks it"
    ),
    "CreateApiKeyCredentialProvider": "AWS API operation GATEWAY_SETUP_SCRIPT calls",
    "aws sts get-caller-identity": "the account check CLAUDE.md requires before any AWS write",
    "total_max_attempts": "botocore retry setting; the value is AGENT_CLIENT_MAX_ATTEMPTS",
    "apiKey": "request field of CreateApiKeyCredentialProvider",
    "evidence/": "directory; design.md section 12 owns paths",
    "s3://rm-reports-818928662830/runs/<run_id>/report.md": (
        "REPORTS_BUCKET + REPORT_KEY_TEMPLATE; test_report_location checks it"
    ),
    "scripts/load.py --run-id <run_id> --users N --questions M --url <api>": (
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


def test_run_identities():
    """Each run's identities are new to Flagsmith (contract change c1, Gate 1)."""
    run_id = "rlive0230"
    users = [names.run_identity(run_id, n) for n in range(1, names.IDENTITY_COUNT + 1)]
    assert users[0] == f"{run_id}-{names.IDENTITY_FIRST}"
    assert users[-1] == f"{run_id}-{names.IDENTITY_LAST}"
    assert all(names.is_run_identity(u) for u in users)
    assert len(set(users)) == names.IDENTITY_COUNT
    assert not names.is_run_identity(names.IDENTITY_FIRST)
    assert not names.is_run_identity(f"{run_id}-user-151")
    assert not names.is_run_identity(f"{run_id}-user-000")
    assert not names.is_run_identity(f"{run_id}-user-001\n")
    sec4 = re.sub(r"\s+", " ", section(4))
    assert "| Identity format | the run ID, a hyphen, then `user-001` to `user-150`" in sec4
    assert (
        "with the pointer's `run_id` before `-user-`) gets a 400 before any Flagsmith call" in sec4
    )
    with pytest.raises(ValueError):
        names.run_identity("live0230", 1)
    with pytest.raises(ValueError):
        names.run_identity(run_id, 151)
    assert names.run_identity("rsmoke0230", 1) != names.run_identity(run_id, 1)
    assert names.AGENT_PACKAGE == names.AGENT_RUNTIME_NAME
    row = re.sub(r"\s+", " ", section(7))
    assert (
        f"so the code lives in `{names.AGENT_CODE_DIR}` (the CLI's `codeLocation` "
        f"`{names.AGENT_CODE_LOCATION}`, entrypoint `{names.AGENT_ENTRYPOINT}`, so no test's "
        f"`import app` finds B's `app/`) as the package `{names.AGENT_PACKAGE}`" in row
    )
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert f'pythonpath = [".", "{names.AGENT_CODE_DIR.rstrip("/")}"]' in pyproject
    assert (
        f"EventBridge rule `{names.ALARM_RULE}` on the alarm entering ALARM. Role "
        f"`{names.INVOKER_ROLE}`." in row
    )
    assert (
        f"It sits in the `{names.TICK_SCHEDULE_GROUP}` schedule group, with execution role "
        f"`{names.SCHEDULER_ROLE}`, which may invoke only `{names.INVOKER_FUNCTION}`." in row
    )
    tasks = re.sub(
        r"\s+", " ", (ROOT / "openspec/changes/build-rollout-marshal/tasks.md").read_text()
    )
    assert "`scripts/load.py --run-id <run_id> --users N --questions M --url <api>`" in tasks
    assert "`run_identity(run_id, n)`" in tasks
    code = names.AGENT_CODE_DIR
    for path in ("rm_agent/state.py", "rm_agent/planner.py", "rm_agent/handlers.py"):
        assert f"`{code}{path}`" in tasks
    assert f"`{code}{names.AGENT_ENTRYPOINT}`" in tasks
    assert (
        f"`codeLocation` `{names.AGENT_CODE_LOCATION}`, entrypoint `{names.AGENT_ENTRYPOINT}`"
        in tasks
    )
    assert f"`{code}rm_agent/tools/flagsmith.py`" in tasks
    assert (
        f"The rule is `{names.ALARM_RULE}`, the function's role `{names.INVOKER_ROLE}`, and "
        f"`{names.TICK_SCHEDULE}` sits in the `{names.TICK_SCHEDULE_GROUP}` group with execution "
        f"role `{names.SCHEDULER_ROLE}`" in tasks
    )
    assert "`lifecycle { ignore_changes = [state] }`" in tasks
    assert "`rm-tick` (created disabled, with `lifecycle { ignore_changes = [state] }`" in tasks
    assert (
        "It adds `runtime/contracts` to `agent/conftest.py`'s `collect_ignore`, or pytest "
        "collects the contracts suite twice through the symlink." in tasks
    )
    assert (
        f"The runtime's role gets `iam:PassRole` on `{names.SCHEDULER_ROLE}`, with "
        f"`iam:PassedToService` `{names.SCHEDULER_SERVICE_PRINCIPAL}`" in tasks
    )
    assert "`<run_id>1-user-001` and a trailing newline" in tasks
    design = (ROOT / "openspec/changes/build-rollout-marshal/design.md").read_text()
    assert "app.py" not in tasks and "app.py" not in design
    sec4 = re.sub(r"\s+", " ", section(4))
    assert (
        "1. Read the active run pointer (section 6). With an active run, an `identity` that is "
        "not one of that run's (`is_run_identity()`, with the pointer's `run_id` before "
        "`-user-`) gets a 400 before any Flagsmith call, so a wrong `--run-id` cannot reuse an "
        "old run's identities (Gate 1)." in sec4
    )
    sec7 = re.sub(r"\s+", " ", section(7))
    assert (
        "reads it with GetSchedule and writes back every field it returned that UpdateSchedule "
        "takes (all but Arn, CreationDate and LastModificationDate), changing only its state. "
        f"That passes `{names.SCHEDULER_ROLE}` to `{names.SCHEDULER_SERVICE_PRINCIPAL}`, so the "
        "runtime's role needs pass-role on it (task 2.7)" in sec7
    )
    sec11 = re.sub(r"\s+", " ", section(11))
    assert (
        f"Each request is one JSON `answer_request` (`question`, `identity`) posted to `--url`, "
        f"the full URL of the HTTP API's only route, `{names.API_ROUTE_KEY}`" in sec11
    )
    assert "from the shell's AWS credentials" in sec11
    assert "A load never prints the URL. The shell's credentials need `execute-api:Invoke`" in sec11
    assert tasks.count("`load.py --run-id <run_id> --users 150") == 3
    assert "load.py --users" not in tasks
    load = re.sub(r"\s+", " ", section(11))
    assert "`scripts/load.py --run-id <run_id> --users N --questions M --url <api>`" in load
    assert (
        f"the HTTP API's only route, `{names.API_ROUTE_KEY}`, as the app module's output "
        f"`{names.APP_ASK_URL_OUTPUT}` gives it. The route uses `{names.API_AUTHORIZATION_TYPE}` "
        f"authorization, so `load.py` signs every request with SigV4 (service "
        f"`{names.API_SIGV4_SERVICE}`)" in load
    )
    spec = (ROOT / "openspec/changes/build-rollout-marshal/specs/assistant-api/spec.md").read_text()
    assert "`scripts/load.py --run-id <run_id> --users 150 --questions 10 --url <api>`" in spec
    assert "load.py --users" not in spec
    assert (
        "load.py --users"
        not in (ROOT / "openspec/changes/build-rollout-marshal/design.md").read_text()
    )


def test_contract_c2():
    """Gate 1's second contract change: the boundary rule, statuses, the promote rule."""
    prose = re.sub(r"\s+", " ", section(4))
    fields = ", ".join(f"`{f}`" for f in names.MCP_EXPERIMENT_FORBIDDEN_FIELDS[:-1])
    assert (
        f"an experiment create or update carrying {fields} or "
        f"`{names.MCP_EXPERIMENT_FORBIDDEN_FIELDS[-1]}`" in prose
    )
    forbidden = names.FLAGSMITH_ADMIN_FORBIDDEN_PATHS
    paths = ", ".join(f"`{p}`" for p in forbidden[:-1]) + f" and `{forbidden[-1]}`"
    assert f"never DELETE, PUT or PATCH, and {paths} can never be built" in prose
    assert set(names.FLAGSMITH_ADMIN_METHODS) == {"GET", "POST"}
    assert f"The MCP allow-list leaves out `{names.MCP_EXCLUDED_TOOL}`" in prose
    state = re.sub(r"\s+", " ", section(6))
    assert (
        f"Ended in Flagsmith means the experiment's status is `{names.EXPERIMENT_STATUS_ENDED}`; "
        f"`{names.EXPERIMENT_STATUS_PAUSED}` is not ended." in state
    )
    assert names.EXPERIMENT_STATUS_ENDED in names.FLAGSMITH_EXPERIMENT_STATUSES
    assert names.EXPERIMENT_STATUS_PAUSED in names.FLAGSMITH_EXPERIMENT_STATUSES
    tools = json.loads((ROOT / "evidence/1.3-mcp-tools.json").read_text())

    def status_enums(node):
        if isinstance(node, dict):
            status = node.get("status")
            if isinstance(status, dict):
                for part in [status, *status.get("allOf", [])]:
                    if "enum" in part:
                        yield tuple(part["enum"])
            for value in node.values():
                yield from status_enums(value)
        elif isinstance(node, list):
            for value in node:
                yield from status_enums(value)

    enums = set(status_enums(tools))
    assert enums == {names.FLAGSMITH_EXPERIMENT_STATUSES}, enums
    # The interval is the lift plus or minus 1.96 sd, and chance_to_win uses the same normal,
    # so ci_low >= -0.05 means at least a 97.5 percent probability (design.md section 6).
    from statistics import NormalDist

    sample = json.loads((ROOT / "evidence/1.4-results-sample.json").read_text())
    found = [
        m["inference"][names.VARIANT_CHEAPER]
        for m in sample["results"]["payload"]["metrics"]
        if names.VARIANT_CHEAPER in m.get("inference", {})
    ]
    assert found
    for inf in found:
        lift, low, high = inf["lift"], inf["ci_low"], inf["ci_high"]
        assert math.isclose((low + high) / 2, lift, abs_tol=1e-12)
        sd = (high - low) / 2 / NormalDist().inv_cdf(0.975)
        assert math.isclose(sd, lift / NormalDist().inv_cdf(inf["chance_to_win"]), rel_tol=1e-9)
    assert "promote_min_prob_not_worse" not in names.PLAN_RECOMMENDATION_RULE_FIELDS
    assert "prob_cheaper_not_worse" not in names.RESULTS_FIELDS
    assert "topK" in names.FORBIDDEN_SAMPLING_PARAMETERS
    spec = ROOT / "openspec/changes/build-rollout-marshal/specs/experiment-agent/spec.md"
    assert (
        "the lower bound of the 95 percent credible interval for the lift on `helpful_rate` is "
        "at or above -0.05, and both guardrails pass" in spec.read_text()
    )
    sec7 = re.sub(r"\s+", " ", section(7))
    assert f"`{names.AGENT_STACK}` creates it with the Bedrock, logging and Gateway grants" in sec7
    assert f"export `{names.AGENT_RUNTIME_ROLE_EXPORT}`" in sec7
    assert f"attaches inline policy `{names.AGENT_RUNTIME_TOOLS_POLICY}`" in sec7
    assert (
        f"exits {names.CLI_EXIT_OK} when its error is null, {names.CLI_EXIT_ERROR} when it "
        f"carries an error code, and {names.CLI_EXIT_USAGE} for a usage error" in sec7
    )


def test_not_a_name_entries_still_exist_in_design():
    spans = {s for n in range(2, 12) for s in single_line_spans(section(n))}
    assert set(NOT_A_NAME) <= spans, "remove NOT_A_NAME entries design.md no longer has"


def fenced_blocks(n: int) -> list[str]:
    return re.findall(r"^```[a-z]*\n(.*?)^```", section(n, keep_fences=True), flags=re.S | re.M)


@pytest.mark.parametrize("n", [5, 7])
def test_names_inside_fenced_blocks_have_constants(n):
    """The section 5 request-path diagram and the section 7 payload examples."""
    values = name_values()
    text = "\n".join(fenced_blocks(n))
    assert text, f"design.md section {n} has no fenced block"
    found = set(re.findall(r"\brm-[a-z0-9-]*[a-z0-9]\b|\bscripts/[a-z_]+\.py\b", text))
    found |= set(re.findall(r'"([a-z_]+)":', text))  # payload keys
    assert sorted(found - values) == []


def test_invoke_fields_match_design():
    keys = []
    for line in "\n".join(fenced_blocks(7)).splitlines():
        if line.startswith('{"action"'):
            keys += [k for k in re.findall(r'"([a-z_]+)":', line) if k not in keys]
    assert tuple(keys) == names.INVOKE_FIELDS
    branches = contracts.load_schema("invoke")["oneOf"]
    assert {k for b in branches for k in b["properties"]} == set(names.INVOKE_FIELDS)


def test_error_fields_match_design():
    span = next(s for s in multi_line_spans(section(7)) if s.startswith('{"run_id"'))
    error = re.search(r'"error": null \| \{(.*?)\}', span, flags=re.S).group(1)
    assert tuple(re.findall(r'"(\w+)":', error)) == names.ERROR_FIELDS
    error_schema = contracts.load_schema("response")["properties"]["error"]["oneOf"][1]
    assert tuple(error_schema["required"]) == names.ERROR_FIELDS


# Constant values that are not in design.md word for word, each with its source.
OTHER_SOURCES = {
    "%Y%m%d%H%M": "strftime form of section 4's YYYYMMDDHHMM",
    "%H%M": "strftime form of section 4's HHMM",
    "OK": "signals spec ('changes to OK or INSUFFICIENT_DATA') and tasks.md 2.4",
    "INSUFFICIENT_DATA": "signals spec and tasks.md 2.4",
    names.IDENTITY_PATTERN: "section 4: 'user-001 to user-150'",
    names.RUN_IDENTITY_PATTERN: "section 4: 'the run ID, a hyphen, then user-001 to user-150'",
    "rprobe0000": "the run part of section 6's probe identity rprobe0000-user-001",
    "contracts/schemas/run_item.json": "section 7: contracts/schemas/ plus the name run_item",
    "contracts/schemas/judge_payload.json": "section 7: contracts/schemas/ plus judge_payload",
    "contracts/schemas/answer_request.json": "section 7: contracts/schemas/ plus answer_request",
    "contracts/schemas/answer_response.json": "section 7: contracts/schemas/ plus answer_response",
    "notBreaching": "CloudWatch's value for section 5's 'missing data treated as not breaching'",
    -0.05: "section 6: 'at or above minus 5 percent', as a fraction",
    "%Y-%m-%dT%H:%M:%S.%fZ": "the one stored form of section 6's ISO 8601 UTC *_at values",
    1.1: "section 6: 'at most 110 percent', as a ratio",
    35000: "CLAUDE.md: 'Flagsmith request usage passes 35,000'",
    60000000: "CLAUDE.md: 'AnswerCostMicroUsd passes 60,000,000'",
    "type": "PERMANENT_FEATURE_DEFINITIONS key (section 4: boolean, string value)",
}


def constant_leaves():
    def walk(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, str | int | float):
            yield value
        elif isinstance(value, dict):
            for k, v in value.items():
                yield from walk(k)
                yield from walk(v)
        elif isinstance(value, list | tuple | set | frozenset):
            for v in value:
                yield from walk(v)

    for attr, value in vars(names).items():
        if attr.isupper() and not attr.startswith("_"):
            for leaf in walk(value):
                yield attr, leaf


def test_every_constant_value_comes_from_design_or_a_named_source():
    """The reverse direction: names.py invents nothing."""
    flat = re.sub(r"\s+", " ", DESIGN)
    invented = set()
    for attr, leaf in constant_leaves():
        if leaf in OTHER_SOURCES:
            continue
        if isinstance(leaf, str):
            ok = bool(re.search(rf"(?<![\w-]){re.escape(leaf)}(?![\w-])", flat))
        else:
            forms = {str(leaf), f"{leaf:,}"} if isinstance(leaf, int) else {str(leaf)}
            ok = any(re.search(rf"(?<![\w.,]){re.escape(f)}(?![\w]|[.,]\d)", flat) for f in forms)
        if not ok:
            invented.add((attr, leaf))
    assert sorted(invented, key=str) == []


def test_other_sources_are_real():
    claude = (ROOT / "CLAUDE.md").read_text()
    signals = (ROOT / "openspec/changes/build-rollout-marshal/specs/signals/spec.md").read_text()
    assert "35,000" in claude and "60,000,000" in claude
    assert "OK or INSUFFICIENT_DATA" in signals
    assert "missing data treated as not breaching" in re.sub(r"\s+", " ", section(5))
    used = {leaf for _, leaf in constant_leaves()}
    assert set(OTHER_SOURCES) <= used, "remove OTHER_SOURCES entries no constant uses"


def test_names_and_dynamo_import_without_jsonschema():
    """Lambdas import contracts.names without packaging jsonschema."""
    code = (
        "import sys; sys.modules['jsonschema'] = None; sys.modules['referencing'] = None\n"
        "import contracts, contracts.names, contracts.dynamo\n"
        "assert contracts.names.ALARM_NAME == 'rm-cheaper-latency-p95'\n"
        "try:\n    contracts.validate('invoke', {'action': 'tick'})\n"
        "except ImportError:\n    print('validate needs jsonschema, as documented')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "validate needs jsonschema" in done.stdout


def test_run_item_survives_a_dynamodb_round_trip():
    """Offline: boto3's own serializer and deserializer, no AWS call."""
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

    item = run_item(experiment_id=4321, metric_ids={"helpful_rate": 7})
    serialize, deserialize = TypeSerializer().serialize, TypeDeserializer().deserialize
    with pytest.raises(TypeError):  # boto3 refuses raw floats: the plan has 1.1 and -0.05
        serialize(item)
    stored = {k: serialize(v) for k, v in dynamo.to_dynamo(item).items()}
    raw = {k: deserialize(v) for k, v in stored.items()}
    invalid("run_item", raw)  # Decimals as boto3 returns them do not validate
    back = dynamo.from_dynamo(raw)
    assert back == item
    assert type(back["experiment_id"]) is int and type(back["metric_ids"]["helpful_rate"]) is int
    assert type(back["plan"]["guardrails"]["max_latency_ratio"]) is float
    valid("run_item", back)
    naive = json.loads(json.dumps(raw, default=int))  # every number to int: 1.1 becomes 1
    invalid("run_item", naive)


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
        f"judge {names.JUDGE_TIMEOUT_SECONDS} seconds, "
        f"`{names.INVOKER_FUNCTION}` {names.INVOKER_TIMEOUT_SECONDS} seconds, "
        f"`{names.FLAGSMITH_ADMIN_FUNCTION}` {names.FLAGSMITH_ADMIN_TIMEOUT_SECONDS} seconds"
    )


def test_agent_client_setting_matches_design():
    prose = re.sub(r"\s+", " ", section(7))
    attempts = {1: "one"}[names.AGENT_CLIENT_MAX_ATTEMPTS]
    assert (
        f"read timeout {names.AGENT_CLIENT_READ_TIMEOUT_SECONDS} seconds and {attempts} attempt "
        f"(`total_max_attempts` {names.AGENT_CLIENT_MAX_ATTEMPTS})" in prose
    )
    assert names.AGENT_CLIENT_READ_TIMEOUT_SECONDS < names.INVOKER_TIMEOUT_SECONDS
    assert f"with `STUB={names.INVOKER_STUB_ON}` it only logs the payload and never reads" in prose


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
    assert set(props["incidents"]["items"]["properties"]) == set(names.INCIDENT_FIELDS)
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
    n = len(names.RESPONSE_FIELDS)
    assert tuple(fields[:n]) == names.RESPONSE_FIELDS
    assert set(fields[n:]) == {"code", "message"}
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
    assert f"statistic {names.ALARM_STATISTIC}," in prose
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
    assert f"`, {names.FIS_INVOCATION_PERCENTAGE} percent of invocations," in prose
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
    assert f"before exceeding {names.ANSWER_HARD_STOP:,}." in prose
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
    assert f"at most once every {names.RESULTS_REFRESH_MIN_SECONDS / 60:g} minutes" in prose
    assert f"at least {names.MIN_IDENTITIES_PER_VARIATION} identities" in prose
    assert f"probe is {names.PROBE_INVOKES} direct invokes" in prose
    assert f"at most {round(names.GUARDRAIL_MAX_LATENCY_RATIO * 100)} percent" in prose
    assert (
        "the rule asks for at least a 97.5 percent probability that the lift is at least -0.05"
        in prose
    )
    assert (
        "at or above minus 5 percent" in prose and names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER == -0.05
    )
    assert (
        f"EventBridge Scheduler, every {names.TICK_INTERVAL_MINUTES} minutes, enabled by launch"
        in prose
    )
    assert f'metadata={{"{names.EXPOSURE_METADATA_KEY}": flag.experiment.id}}' in prose, (
        "exposure metadata key"
    )
    assert f"at {names.TREATMENT_PERCENT} percent" in prose
    assert f"`{names.ANSWER_THINKING_EFFORT}` through" in prose
    assert f"Answers (both arms): `maxTokens` {names.ANSWER_MAX_TOKENS}," in prose
    assert f"Judge: thinking disabled, `maxTokens` {names.JUDGE_MAX_TOKENS}." in prose
    assert (
        f"Agent prose: `maxTokens` {names.AGENT_MAX_TOKENS}, thinking effort "
        f"`{names.AGENT_THINKING_EFFORT}`" in prose
    )
    assert f"at most {names.ANSWER_MAX_WORDS} words" in prose
    assert f"than the {names.SESSION_ID_MIN_LENGTH} character minimum" in prose
    sdk = (
        f"{names.FLAGSMITH_SDK_PACKAGE}` Python package {names.FLAGSMITH_SDK_MIN_VERSION} or later"
    )
    assert sdk in prose


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
    for bad in ("spike1", ""):
        with pytest.raises(ValueError):
            names.scratch_run_id(bad, FIXED)


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
    assert tuple(names.EXPERIMENT_EXPECTED_DIRECTIONS) == names.METRICS
    expected = {
        names.METRIC_DIRECTION_HIGHER: names.EXPECTED_DIRECTION_NOT_DECREASE,
        names.METRIC_DIRECTION_LOWER: names.EXPECTED_DIRECTION_NOT_INCREASE,
    }
    for metric, definition in names.METRIC_DEFINITIONS.items():
        assert names.EXPERIMENT_EXPECTED_DIRECTIONS[metric] == expected[definition["direction"]]
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


def test_schema_files_are_exactly_the_named_ones():
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
            optional_id = "<id or omitted for the active run>"
            if optional_id in line:
                parsed = json.loads(line.replace(optional_id, fills["<id>"]))
                out.append(parsed)
                out.append({k: v for k, v in parsed.items() if k != "run_id"})
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
        "pending": None,
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
        {"pending": "DONE"},
        {"error": {"code": "TIMEOUT_PENDING", "message": "x"}},
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
                "at": "2026-11-27T15:30:05.000000Z",
                "from": None,
                "to": "PLANNED",
                "actor": "engineer",
                "reason": "start",
            }
        ],
        "notified": {},
        "created_at": "2026-11-27T15:30:05.000000Z",
        "updated_at": "2026-11-27T15:30:05.123456Z",
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
            incidents=[{"started_at": "2026-11-27T15:52:00.000000Z"}],
            notified={"4#ROLLED_BACK": "5f2c9a1e-2b6a-4bd2-9a59-3b1c0e8f7d21"},
            pending="AWAITING_RESUME",
            pending_at="2026-11-27T15:58:00.000000Z",
            first_running_at="2026-11-27T15:35:00.000000Z",
            last_refresh_at="2026-11-27T15:50:00.000000Z",
            report_key="runs/r202611271530/report.md",
        ),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"pending": "RUNNING"},
        {"state": "DONE"},
        {"pk": "RUN#rspike"},
        {"created_at": "2026-11-27T10:30:05-05:00"},
        {"created_at": "2026-11-27"},
        {"created_at": "2026-13-45T25:61:61Z"},
        {"created_at": "2026-11-27T15:30:05+0000"},
        {"created_at": "2026-11-27T15:30:05Z"},
        {"created_at": "2026-11-27T15:30:05.123456+00:00"},
        {"updated_at": "2026-11-27T15:30:05.123Z"},
        {"updated_at": "2026-11-27T15:30:05.5Z"},
        {"history": []},
        {
            "history": [
                {
                    "at": "2026-11-27T15:30:05.000000Z",
                    "from": None,
                    "to": "PLANNED",
                    "actor": "human",
                    "reason": "start",
                }
            ]
        },
        {"metric_ids": {"accuracy": 1}},
        {"incident": {"started_at": "2026-11-27T15:52:00.000000Z"}},
        {"incidents": []},
        {"incidents": [{"resumed_at": "2026-11-27T15:58:00.000000Z"}]},
        {"incidents": [{"started_at": "2026-11-27T15:52:00.000000Z", "alarm_name": "x"}]},
        {"notified": {"ROLLED_BACK": "m"}},
        {"notified": {"0#ROLLED_BACK": "m"}},
        {"notified": {"2#DONE": "m"}},
        {"notified": {"2#ROLLED_BACK": ""}},
        {"feature_key": "assistant_model_rspike"},
        {"experiment_id": "4321"},
        {"plan": plan(split={"current": 70, "cheaper": 30})},
        {"dedupe_key": "x"},
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
        ),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"prob_cheaper_not_worse": None},
        {"ready": "yes"},
        {"variations": {"current": {}}},
        {"helpful_lift_ci_lower": "-5%"},
        {"win_probability": 0.9},
        {
            "variations": {
                "current": {
                    "identities": 75,
                    "helpful_rate": 1.5,
                    "mean_latency_ms": 1.0,
                    "mean_cost_micro_usd": 1.0,
                },
                "cheaper": {
                    "identities": 75,
                    "helpful_rate": 0.9,
                    "mean_latency_ms": 1.0,
                    "mean_cost_micro_usd": 1.0,
                },
            }
        },
        {
            "variations": {
                "current": {
                    "identities": 75,
                    "helpful_rate": -0.1,
                    "mean_latency_ms": 1.0,
                    "mean_cost_micro_usd": 1.0,
                },
                "cheaper": {
                    "identities": 75,
                    "helpful_rate": 0.9,
                    "mean_latency_ms": 1.0,
                    "mean_cost_micro_usd": 1.0,
                },
            }
        },
    ],
)
def test_bad_results_are_rejected(overrides):
    invalid("results", results(**overrides))


def judge_payload(**overrides):
    base = {
        "run_id": "r202611271530",
        "identity": "r202611271530-user-042",
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
        {"identity": "rlive0230-user-151"},
        {"identity": "user-042"},
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
    assert judge["properties"]["identity"]["pattern"] == names.RUN_IDENTITY_PATTERN
    assert judge["properties"]["model_id"]["enum"] == [names.MODEL_CURRENT, names.MODEL_CHEAPER]
    run = contracts.load_schema("run_item")
    assert run["$defs"]["state"]["enum"] == list(names.STATES)
    assert run["$defs"]["history_entry"]["properties"]["actor"]["enum"] == list(names.ACTORS)
    assert run["properties"]["metric_ids"]["propertyNames"]["enum"] == list(names.METRICS)


def test_commit_may_leave_pending_at_behind():
    """design.md section 6 step 3 removes pending only."""
    valid("run_item", run_item(pending_at="2026-11-27T15:58:00.000000Z"))


@pytest.mark.parametrize(
    "at", ["2026-11-27T15:42:00Z", "2026-11-27T15:42:00.551+0000", "2026-11-27T10:42:00-05:00"]
)
def test_alarm_at_accepts_iso_8601_offsets(at):
    valid(
        "invoke",
        {"action": "alarm", "alarm_name": names.ALARM_NAME, "alarm_state": "ALARM", "at": at},
    )


@pytest.mark.parametrize("at", ["2026-13-45T25:61:61Z", "2026-11-27T15:42:00", "2026-11-27"])
def test_alarm_at_rejects_impossible_or_offsetless_times(at):
    invalid(
        "invoke",
        {"action": "alarm", "alarm_name": names.ALARM_NAME, "alarm_state": "ALARM", "at": at},
    )


def test_every_fenced_block_and_multi_line_span_has_a_check():
    """A new fenced block or multi-line span in design.md needs its own check."""
    every = sections(keep_fences=True)
    fenced = {n: len(fenced_blocks(n)) for n in every if fenced_blocks(n)}
    assert fenced == {5: 1, 7: 1}
    starts = sorted(s.split(None, 1)[0] for n in every for s in multi_line_spans(section(n)))
    assert starts == sorted(["{run_id,", '{"run_id":', '{"ready":', "V"])


def listed(items, last: str) -> str:
    """`a`, `b` or `c` (the whole tuple, so an added or dropped item fails)."""
    quoted = [f"`{i}`" for i in items]
    return f"{', '.join(quoted[:-1])} {last} {quoted[-1]}" if len(quoted) > 1 else quoted[0]


def test_rule_constants_match_their_design_sentences():
    """Booleans, maps and rule sets the value checks cannot tie to a label."""
    prose = re.sub(r"\s+", " ", DESIGN)
    on_off = "enabled" if names.KILL_SWITCH_DEFAULT_ENABLED else "disabled"
    assert f"`{names.KILL_SWITCH_FEATURE}`, default {on_off}" in prose
    assert f"`{names.KILL_SWITCH_FEATURE}` (boolean kill switch, default {on_off})" in prose
    values = listed(names.DEFAULT_MODEL_VALUES, "or")
    default = names.DEFAULT_MODEL_DEFAULT
    assert f"`{names.DEFAULT_MODEL_FEATURE}` (string value, {values}, default `{default}`)" in prose
    assert names.PERMANENT_FEATURE_DEFINITIONS == {
        names.KILL_SWITCH_FEATURE: {"type": "boolean", "default": True},
        names.DEFAULT_MODEL_FEATURE: {"type": "string", "default": names.VARIANT_CURRENT},
    }
    kwargs = ", ".join(f"`{k}={v}`" for k, v in names.FLAGSMITH_CLIENT_OPTIONS.items())
    assert f"remote evaluation, {kwargs}" in prose
    definitions = "; ".join(
        f"`{metric}` = {d['aggregation']} of `{d['event']}` ("
        + ("primary, " if metric == names.PRIMARY_METRIC else "")
        + f"{d['direction']})"
        for metric, d in names.METRIC_DEFINITIONS.items()
    )
    assert definitions in prose
    aggregations = names.METRIC_AGGREGATIONS_AVAILABLE
    assert (
        f"Available metric aggregations are {', '.join(aggregations[:-1])} and {aggregations[-1]}."
        in prose
    )
    assert "`mean_cost_micro_usd` is not higher than the current arm's" in prose
    assert names.GUARDRAIL_MAX_COST_RATIO == 1.0
    assert "**`check` is legal in every non-terminal state**" in prose
    assert "In a terminal state it returns `ILLEGAL_IN_STATE`." in prose
    assert names.CHECK_LEGAL_STATES == tuple(
        s for s in names.STATES if s not in names.TERMINAL_STATES
    )
    assert f"**An alarm in {listed(names.ALARM_NOOP_STATES, 'or')} is a no-op.**" in prose
    errs = listed(names.ACTIONS_WITH_LEGALITY_ERRORS, "and")
    assert f"Only {errs} return an error when not legal in the current state" in prose
    cli = ", ".join(f"`{a}`" for a in names.CLI_ACTIONS)
    assert f"| `{names.CLI_NAME}` CLI (`cli/`) | {cli} |" in DESIGN
    models = {role: ticks(m)[0] for role, m, _ in table_rows(section(3), "| Role |")}
    assert names.MODEL_BY_VARIANT == {
        names.VARIANT_CURRENT: models["Current arm (control)"],
        names.VARIANT_CHEAPER: models["Cheaper arm (treatment)"],
    }
    diagram = "\n".join(fenced_blocks(5))
    assert (
        f"{names.ROUTER_FUNCTION} -> (sync invoke) {names.ANSWER_CURRENT_FUNCTION} | "
        f"{names.ANSWER_CHEAPER_FUNCTION} -> Bedrock" in diagram
    )
    assert names.ANSWER_FUNCTION_BY_VARIANT == {
        v: f"rm-answer-{v}" for v in (names.VARIANT_CURRENT, names.VARIANT_CHEAPER)
    }
    assert (
        f"organisation {names.FLAGSMITH_ORGANISATION_ID}, project "
        f'"{names.FLAGSMITH_PROJECT_NAME}" (ID `{names.FLAGSMITH_PROJECT_ID}`), environment '
        f'"{names.FLAGSMITH_ENVIRONMENT_NAME}"' in prose
    )
    assert f"Free: {names.FLAGSMITH_MONTHLY_REQUEST_LIMIT:,} API requests a month" in prose
    assert f"| Timebox | {names.TASK_TIMEBOX_MINUTES} minutes per task" in DESIGN
    assert f"Timebox: {names.FIS_TIMEBOX_MINUTES / 60:g} hours." in prose
    assert f"not working after {names.GATEWAY_FALLBACK_AFTER_MINUTES} minutes of task 1.3" in prose
    assert f"environment flags (cached {names.DEFAULT_MODEL_CACHE_SECONDS} seconds)" in prose
    assert f"The router caches the pointer for {names.POINTER_CACHE_SECONDS} seconds" in prose
    assert f"API Gateway's {names.API_GATEWAY_TIMEOUT_SECONDS} second limit" in prose
    assert f"({names.QUESTIONS_COUNT} support questions" in prose
    assert f"one multivariate option with key `{names.TREATMENT_OPTION_KEY}` and value " in prose
    assert f"value `{names.TREATMENT_OPTION_VALUE}`, at {names.TREATMENT_PERCENT} percent" in prose
    assert f"the feature's own value, `{names.CONTROL_VALUE}`" in prose


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "start", "prompt": "x", "actor": "engineer", "run_id": "r202611271530"},
        {"action": "approve", "run_id": "r202611271530", "actor": "engineer", "force": True},
        {
            "action": "alarm",
            "alarm_name": "rm-cheaper-latency-p95",
            "alarm_state": "ALARM",
            "at": "2026-11-27T15:42:00Z",
            "region": "us-east-1",
        },
        {"action": "status", "run_id": "r202611271530", "verbose": True},
    ],
)
def test_extra_keys_are_rejected_on_every_payload(payload):
    invalid("invoke", payload)


@pytest.mark.parametrize(
    "history",
    [
        [
            {
                "at": "2026-11-27T15:30:05.000000Z",
                "from": None,
                "to": "PLANNED",
                "actor": "agent",
                "reason": "start",
            }
        ],
        [
            {
                "at": "2026-11-27T15:30:05.000000Z",
                "from": "RUNNING",
                "to": "PLANNED",
                "actor": "engineer",
                "reason": "start",
            }
        ],
        [
            {
                "at": "2026-11-27T15:30:05.000000Z",
                "from": None,
                "to": "RUNNING",
                "actor": "engineer",
                "reason": "start",
            }
        ],
        [
            {
                "at": "2026-11-27T15:30:05.000000Z",
                "from": None,
                "to": "PLANNED",
                "actor": "engineer",
                "reason": "start",
            },
            {
                "at": "2026-11-27T15:31:00.000000Z",
                "from": None,
                "to": "RUNNING",
                "actor": "engineer",
                "reason": "approve",
            },
        ],
    ],
)
def test_history_starts_with_the_start_transition_only(history):
    invalid("run_item", run_item(history=history))


def test_history_first_entry_is_transitions_row_zero():
    schema = contracts.load_schema("run_item")["$defs"]["history_first"]["properties"]
    start = names.TRANSITIONS[0]
    assert (schema["from"]["const"], schema["to"]["const"], schema["actor"]["const"]) == (
        start.source,
        start.target,
        start.actor,
    )


def test_constants_holding_booleans_or_none_are_pinned():
    """The value checks skip booleans and None, so the constants holding them are listed
    here, and each is tied to design.md by its own test."""

    def holds(value, kind):
        if kind is None and value is None:
            return True
        if kind is bool and isinstance(value, bool):
            return True
        if isinstance(value, dict):
            return any(holds(k, kind) or holds(v, kind) for k, v in value.items())
        if isinstance(value, list | tuple | set | frozenset):
            return any(holds(v, kind) for v in value)
        return False

    public = {a: v for a, v in vars(names).items() if a.isupper() and not a.startswith("_")}
    assert {a for a, v in public.items() if holds(v, bool)} == {
        "KILL_SWITCH_DEFAULT_ENABLED",  # test_rule_constants_match_their_design_sentences
        "FLAGSMITH_CLIENT_OPTIONS",  # same test: `enable_events=True`
        "PERMANENT_FEATURE_DEFINITIONS",  # same test
    }
    assert {a for a, v in public.items() if holds(v, None)} == {
        "TRANSITIONS",  # test_transitions_match_design: the "(none)" row
        "LEGAL_TRANSITIONS",  # same test
    }


def test_history_with_later_transitions_validates():
    first = run_item()["history"][0]
    later = {
        "at": "2026-11-27T15:31:00.000000Z",
        "from": "PLANNED",
        "to": "RUNNING",
        "actor": "engineer",
        "reason": "approve",
    }
    valid("run_item", run_item(state="RUNNING", history=[first, later]))


def test_list_constants_match_their_design_lists():
    """Tuples whose members design.md lists in one place: the whole tuple must match."""
    prose = re.sub(r"\s+", " ", DESIGN)
    assert f"Send no {listed(names.FORBIDDEN_SAMPLING_PARAMETERS, 'or')}." in prose
    assert (
        f"dimension `{names.CW_DIMENSION_VARIANT}` = {listed(names.VARIANTS, 'or')} "
        "(the variant actually served)" in prose
    )
    rows = {r[0]: r[1] for r in table_rows(section(4), "| Thing | Name or value |")}
    assert tuple(ticks(rows["Events, one of each per answer"])) == names.EVENTS
    guardrails = re.search(r"Guardrails pass when (.*?)\.", prose).group(1)
    assert tuple(ticks(guardrails)) == names.GUARDRAIL_METRICS
    pointer = re.search(r'Pointer item: `pk = "ACTIVE"`, attributes (.*?)\.', prose).group(1)
    assert tuple(ticks(pointer)) == names.POINTER_ATTRIBUTES
    meaning = {
        ticks(attr)[0]: text
        for attr, _, text in table_rows(section(6), "| Attribute | Type | Meaning |")
        if ticks(attr)
    }
    assert tuple(ticks(meaning["incidents"])) == names.INCIDENT_FIELDS
    assert tuple(ticks(meaning["history"])) == names.HISTORY_FIELDS
    rule = re.search(
        r"Recommendation rule \(in code, not in the prompt\): (.*?) Flagsmith's", prose
    )
    recommended = re.findall(r"recommend `([^`]+)`", rule.group(1))
    assert tuple(recommended) == names.RECOMMENDATIONS  # order is the rule: first match wins
    assert (
        "An incident happened during the run (`incidents` is not empty): recommend "
        f"`{names.RECOMMEND_HOLD_RERUN}`" in prose
    )
    rule_schema = contracts.load_schema("plan")["properties"]["recommendation_rule"]
    assert rule_schema["properties"]["after_incident"]["const"] == names.RECOMMEND_HOLD_RERUN
    schemas = re.search(r"Schemas in `contracts/schemas/`: (.*?)\.", prose).group(1)
    assert tuple(ticks(schemas)) == names.SCHEMA_NAMES
    assert names.SCHEMA_FILES == {n: f"{names.SCHEMA_DIR}{n}.json" for n in names.SCHEMA_NAMES}
    tools = table_rows(section(7), "| Tool | Reaches | Through |")
    assert tuple(ticks(tool)[0] for tool, _, _ in tools if ticks(tool)) == names.AGENT_TOOLS
    actors = [actor for *_, actor in design_transitions()]
    assert set(actors) == set(names.ACTORS) and len(set(names.ACTORS)) == len(names.ACTORS)
    parts = blocks(section(4))
    bullets = next(
        parts[i + 1] for i, b in enumerate(parts) if "permanent features exist" in b.lower()
    )
    assert tuple(re.findall(r"^- `([^`]+)`", bullets, flags=re.M)) == names.PERMANENT_FEATURES
    tasks = (ROOT / "openspec/changes/build-rollout-marshal/tasks.md").read_text()
    alarm = re.search(r"`alarm_state\(name\) -> (.*?)`", tasks).group(1)
    assert tuple(re.findall(r'"(\w+)"', alarm)) == names.ALARM_STATES


# ---------------------------------------------------------------------------
# The spike decisions from the 1.1 gate (contract change a)
# ---------------------------------------------------------------------------
def test_agentcore_cli_matches_design():
    major = names.AGENTCORE_CLI_MIN_VERSION.split(".")[0]
    assert (
        f"| AgentCore CLI | npm package `{names.AGENTCORE_CLI_PACKAGE}`, command "
        f"`{names.AGENTCORE_CLI_COMMAND}`, version {names.AGENTCORE_CLI_MIN_VERSION} or later "
        f"within {major}.x." in DESIGN
    )
    minor = names.AGENTCORE_CLI_MIN_VERSION.split(".")[1]
    assert names.AGENTCORE_CLI_MIN_VERSION.split(".")[2] == "0"  # preflight checks major.minor
    preflight = (ROOT / "scripts/preflight.sh").read_text()
    assert f'AGENTCORE_CLI_MAJOR="{major}"' in preflight
    assert f'AGENTCORE_CLI_MIN_MINOR="{minor}"' in preflight


def test_spike_rules_match_design():
    prose = re.sub(r"\s+", " ", DESIGN)
    assert (
        f"agent B applies and destroys `{names.SPIKE_INFRA_DIR}`, a separate root whose "
        "local state is never committed" in prose
    )
    assert f"Every resource name starts with `{names.SPIKE_RESOURCE_PREFIX}`." in prose
    assert names.SPIKE_RESOURCE_PREFIX.startswith(names.RESOURCE_PREFIX)
    assert names.SPIKE_INFRA_DIR.startswith(names.INFRA_DIR)
    assert (
        f"every name created in Flagsmith starts with `{names.SPIKE_NAME_PREFIX}`: features, "
        "metrics and events" in prose
    )
    key = names.experiment_feature_key(names.scratch_run_id("spike", FIXED))
    assert key == "assistant_model_rspike1530"
    assert f"`{key.removesuffix('1530')}<HHMM>`" in prose
    for name in (*names.FEATURE_KEYS, *names.METRICS, *names.EVENTS):
        assert not name.startswith(names.SPIKE_NAME_PREFIX), name


def test_experiment_rollout_and_directions_match_design():
    rows = {r[0]: r[1] for r in table_rows(section(4), "| Thing | Name or value |")}
    assert rows["Experiment rollout"] == (
        f"{names.EXPERIMENT_ROLLOUT_PERCENT} percent of identities, set when the experiment "
        "is created; a wrong one is fixed by hand on the experiment page"
    )
    prose = re.sub(r"\s+", " ", DESIGN)  # section 13's copies of the same values
    assert f"50/50, rollout {names.EXPERIMENT_ROLLOUT_PERCENT} percent" in prose
    assert f"the option's {names.TREATMENT_PERCENT} percent allocation" in prose
    keys = list(names.METRIC_DEFINITIONS[names.PRIMARY_METRIC])
    assert f"definition on {', '.join(keys[:-1])} and {keys[-1]}." in prose
    assert "permanent feature is checked on type only" in prose
    assert set(next(iter(names.PERMANENT_FEATURE_DEFINITIONS.values()))) == {"type", "default"}
    values = listed(names.DEFAULT_MODEL_VALUES, "or")
    assert f"default-model feature holds a string value, {values}." in prose
    expected = "; ".join(f"`{m}`: {d}" for m, d in names.EXPERIMENT_EXPECTED_DIRECTIONS.items())
    assert rows["Expected direction per experiment metric"] == expected


def test_agentcore_pieces_match_design():
    rows = table_rows(section(7), "| Piece | Name | Notes |")
    pieces = {piece: ticks(name)[0] for piece, name, _ in rows}
    notes = {piece: note for piece, _, note in rows}
    assert pieces == {
        "Runtime": names.AGENT_RUNTIME_NAME,
        "Gateway": names.GATEWAY_NAME,
        "Gateway service role": names.GATEWAY_ROLE,
        "Credential provider": names.GATEWAY_CREDENTIAL_PROVIDER,
        "MCP server target": names.GATEWAY_TARGET_FLAGSMITH,
        "Lambda target": names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
    }
    assert names.GATEWAY_TARGETS == (pieces["MCP server target"], pieces["Lambda target"])
    gateway = notes["Gateway"]
    assert (
        f"Protocol `{names.GATEWAY_PROTOCOL}`, inbound auth `{names.GATEWAY_INBOUND_AUTH}`"
        in gateway
    )
    assert f"SigV4 through `{names.GATEWAY_SIGV4_PROXY_PACKAGE}`" in gateway
    assert f"`{names.GATEWAY_INVOKE_ACTION}` on the Gateway" in gateway
    assert f"from `{names.SECRET_FLAGSMITH_API_KEY}`" in notes["Credential provider"]
    assert notes["MCP server target"] == (
        f"`{names.FLAGSMITH_MCP_URL}`, API-key outbound auth, header "
        f"`{names.GATEWAY_API_KEY_HEADER}`, prefix `{names.GATEWAY_API_KEY_PREFIX}`; the service "
        "stores the prefix trimmed (task 1.3)"
    )
    assert notes["Lambda target"].startswith(f"Lambda `{names.FLAGSMITH_ADMIN_FUNCTION}`")
    assert names.FLAGSMITH_MCP_AUTH_HEADER_TEMPLATE == (
        f"{names.GATEWAY_API_KEY_HEADER}: {names.GATEWAY_API_KEY_PREFIX} <key>"
    )
    assert f"its ARN and `{names.AGENT_RUNTIME_ARN_PARAMETER}` never change" in notes["Runtime"]
    roles = re.sub(r"\s+", " ", section(7))
    assert f"except the Gateway service role: it is always `{names.GATEWAY_ROLE}`" in roles
    prose = re.sub(r"\s+", " ", section(7))
    assert f"`{names.GATEWAY_SETUP_SCRIPT}` gets or creates each one by name" in prose


def test_flagsmith_key_readers_match_design():
    rows = table_rows(section(8), "| Name | Holds | Read by |")
    assert ticks(rows[0][2]) == [
        names.FLAGSMITH_ADMIN_FUNCTION,
        names.GATEWAY_SETUP_SCRIPT,
        names.BOOTSTRAP_FLAGSMITH_SCRIPT,
        names.SEED_EXPERIMENT_SCRIPT,
        names.RESET_SCRIPT,
        names.INJECT_FAULT_SCRIPT,
    ]
    assert ticks(rows[1][2]) == [
        names.ROUTER_FUNCTION,
        names.JUDGE_FUNCTION,
        names.ANSWER_CHEAPER_FUNCTION,
    ]


# ---------------------------------------------------------------------------
# The workstream decisions from the 1.1 gate (contract change b)
# ---------------------------------------------------------------------------
def test_plan_table_matches_design_and_schema():
    """design.md section 6's plan table, plan.json and names agree field by field."""
    schema = contracts.load_schema("plan")
    assert tuple(schema["properties"]) == names.PLAN_FIELDS
    assert tuple(schema["required"]) == names.PLAN_FIELDS
    nested = {
        "stop_rules": names.PLAN_STOP_RULE_FIELDS,
        "guardrails": names.PLAN_GUARDRAIL_FIELDS,
        "recommendation_rule": names.PLAN_RECOMMENDATION_RULE_FIELDS,
    }
    for field, fields in nested.items():
        assert tuple(schema["properties"][field]["properties"]) == fields, field
    assert names.PLAN_MODEL_FIELDS == names.PLAN_FIELDS[:2]
    rows = table_rows(section(6), "| Field | Value |")
    model = ", ".join(f"`{f}`" for f in names.PLAN_MODEL_FIELDS)
    assert rows[0] == [model, "written by the model"]

    def text(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return f"`{value}`"
        return str(value)

    built = plan()
    expected = []
    for field in names.PLAN_FIELDS[2:]:
        value = built[field]
        if isinstance(value, list):
            cell = ", ".join(text(v) for v in value)
        elif field == "split":
            cell = ", ".join(f"`{k}` {v}" for k, v in value.items())
        elif isinstance(value, dict):
            cell = "; ".join(f"`{k}` {text(v)}" for k, v in value.items())
        else:
            cell = text(value)
        expected.append([f"`{field}`", cell])
    assert rows[1:] == expected
    prose = re.sub(r"\s+", " ", section(6))
    assert (
        "The model writes only `hypothesis` and `summary`; every other field is injected" in prose
    )
    assert "`start` returns `BAD_REQUEST` before any write when the prompt names no model" in prose


def test_answer_interface_matches_design():
    prose = re.sub(r"\s+", " ", section(5))
    req = listed(names.ANSWER_REQUEST_FIELDS, "and")
    resp = listed(names.ANSWER_RESPONSE_FIELDS, "and")
    assert f"The answer functions take {req} and return {resp} (schemas `answer_request` and" in (
        prose
    )
    for name, fields in (
        ("answer_request", names.ANSWER_REQUEST_FIELDS),
        ("answer_response", names.ANSWER_RESPONSE_FIELDS),
    ):
        schema = contracts.load_schema(name)
        assert tuple(schema["required"]) == fields
        assert tuple(schema["properties"]) == fields
    request = contracts.load_schema("answer_request")
    assert request["properties"]["identity"]["pattern"] == names.RUN_IDENTITY_PATTERN
    response = contracts.load_schema("answer_response")
    assert response["properties"]["model_id"]["enum"] == [names.MODEL_CURRENT, names.MODEL_CHEAPER]
    valid("answer_request", {"question": names.PROBE_QUESTION, "identity": names.PROBE_IDENTITY})
    good = {"answer": "Open Settings.", "input_tokens": 12, "output_tokens": 40}
    for model in (names.MODEL_CURRENT, names.MODEL_CHEAPER):
        valid("answer_response", {**good, "model_id": model})
    for bad in (
        {"question": "", "identity": names.run_identity("rlive0230", 1)},
        {"question": "x", "identity": "rlive0230-user-151"},
        {"question": "x", "identity": names.IDENTITY_FIRST},
        {"question": "x"},
        {
            "question": "x",
            "identity": names.run_identity("rlive0230", 1),
            "run_id": "r202611271530",
        },
    ):
        invalid("answer_request", bad)
    for bad in (
        {**good, "model_id": names.MODEL_JUDGE},
        {**good, "answer": "", "model_id": names.MODEL_CHEAPER},
        {**good, "input_tokens": -1, "model_id": names.MODEL_CHEAPER},
        {**good, "output_tokens": 1.5, "model_id": names.MODEL_CHEAPER},
        {**good, "model_id": names.MODEL_CHEAPER, "latency_ms": 9},
    ):
        invalid("answer_response", bad)
    probe = re.sub(r"\s+", " ", section(6))
    assert (
        f"The probe is {names.PROBE_INVOKES} direct invokes of `{names.ANSWER_CHEAPER_FUNCTION}`, "
        f"run in parallel, each asking `{names.PROBE_QUESTION}` as `{names.PROBE_IDENTITY}`."
        in probe
    )
    assert names.PROBE_IDENTITY == names.run_identity(names.PROBE_RUN_ID, 1)
    assert names.is_run_identity(names.PROBE_IDENTITY)
    assert "a reply that fails `answer_response` fails it" in probe


def test_dedupe_key_matches_design():
    prose = re.sub(r"\s+", " ", section(6))
    example = names.dedupe_key(2, names.STATE_ROLLED_BACK)
    assert example == "2#ROLLED_BACK"
    assert (
        f"A notification's dedupe key is `{names.DEDUPE_KEY_TEMPLATE}`, for example `{example}`"
        in prose
    )
    pattern = contracts.load_schema("run_item")["properties"]["notified"]["propertyNames"]
    for state in names.STATES:
        assert re.fullmatch(pattern["pattern"], names.dedupe_key(12, state))
    for bad in ((0, "RUNNING"), (-1, "RUNNING"), (True, "RUNNING"), ("2", "RUNNING"), (2, "DONE")):
        with pytest.raises(ValueError):
            names.dedupe_key(*bad)


@pytest.mark.parametrize(
    "variations, ready, ok",
    [
        ({"current": 50, "cheaper": 50}, True, True),
        ({"current": 49, "cheaper": 60}, True, False),
        ({"current": 49, "cheaper": 60}, False, True),
        ({"current": 50, "cheaper": 50}, False, False),
        ({"current": 120, "cheaper": "mean_cost_micro_usd"}, True, False),
        ({"current": 120, "cheaper": "mean_cost_micro_usd"}, False, True),
        ({"current": "helpful_rate", "cheaper": 120}, True, False),
        ({"current": "helpful_rate", "cheaper": 120}, False, True),
        ({"current": 120, "cheaper": "mean_latency_ms"}, True, False),
        ({"current": 120, "cheaper": "mean_latency_ms"}, False, True),
    ],
)
def test_ready_follows_the_rule(variations, ready, ok):
    """ready is true exactly when both arms have 50 identities and no null mean."""

    def arm(spec):
        if isinstance(spec, str):  # enough identities, this mean still null
            return {**GOOD_VARIATION, "identities": 80, spec: None}
        return {**GOOD_VARIATION, "identities": spec}

    instance = results(ready=ready, variations={k: arm(v) for k, v in variations.items()})
    (valid if ok else invalid)("results", instance)


def test_results_definitions_match_design():
    prose = re.sub(r"\s+", " ", section(7))
    assert (
        f"both variations have at least {names.MIN_IDENTITIES_PER_VARIATION} identities and "
        "non-null `helpful_rate`, `mean_latency_ms` and `mean_cost_micro_usd`" in prose
    )
    assert "as a fraction (-0.05 is minus 5 percent)" in prose
    assert "it can fall below -1, because the interval is the lift plus or minus 1.96" in prose
    valid("results", results(helpful_lift_ci_lower=-1.07))  # the judge hat's case, Gate 1
    assert "prob_cheaper_not_worse" not in prose
    complete = contracts.load_schema("results")["$defs"]["complete_variation"]["properties"]
    assert complete["identities"] == {"minimum": names.MIN_IDENTITIES_PER_VARIATION}
    assert set(complete) == set(names.VARIATION_FIELDS)


def test_state_machine_rules_match_design():
    prose = re.sub(r"\s+", " ", section(6))
    row = names.TRANSITIONS[11]
    assert (row.source, row.target, row.actor) == (
        names.STATE_RESULTS_READY,
        names.STATE_ROLLED_BACK,
        names.ACTOR_AGENT,
    )
    assert "**`tick` and `check` in `RUNNING` or `RESULTS_READY` look at the alarm first**" in prose
    assert (
        "**An alarm whose `at` is earlier than the run's latest entry into `RUNNING` is a" in prose
    )
    assert "**The tick schedule is enabled first and disabled last**" in prose
    assert "`decline` never takes over a transition another action claimed" in prose
    assert f"moving `pending_at` back by {names.PENDING_STALE_SECONDS} seconds" in prose
    assert (
        f"a `pending` older than {names.PENDING_STALE_SECONDS} seconds may be taken over" in prose
    )
    assert "A failed pointer condition means `RUN_ACTIVE`" in prose
    assert '`start` returns `BAD_REQUEST`, "start again next minute"' in prose
    assert "**An engineer action on a run the pointer does not name returns `RUN_ACTIVE`**" in prose
    assert "The commit appends an `incidents` entry with `started_at`." in prose
    assert "The commit sets `resumed_at` on the open `incidents` entry." in prose
    assert "Any transition into `HELD`: disable the tick schedule; leave everything" in prose
    assert "including the kill switch" in prose
    seven = re.sub(r"\s+", " ", section(7))
    for code in (names.ERROR_TRANSITION_PENDING, names.ERROR_UPSTREAM_FAILED):
        assert f"- `{code}`: " in section(7), code
    assert (
        "`status` with no active run returns `run_id`, `state`, `awaiting`, `pending` and "
        f'`error` all null, and a message that starts "{names.NO_ACTIVE_RUN_MESSAGE}".' in seven
    )
    valid(
        "response",
        response(
            run_id=None,
            state=None,
            awaiting=None,
            pending=None,
            message=names.NO_ACTIVE_RUN_MESSAGE,
        ),
    )
    valid("response", response(state="RUNNING", awaiting=None, pending="ROLLED_BACK"))
    resp = contracts.load_schema("response")
    assert resp["properties"]["pending"]["enum"] == [None, *names.STATES]


def test_alarm_threshold_rule_matches_design():
    prose = re.sub(r"\s+", " ", section(5))
    assert names.ALARM_THRESHOLD_FAULT_MARGIN_MS == names.FIS_DELAY_MS // 2
    assert (
        "plus half the injected delay "
        f"({names.ALARM_THRESHOLD_FAULT_MARGIN_MS} ms), rounded up to the next 1000" in prose
    )
    assert f"If that p95 is above {names.ALARM_BASELINE_STOP_MS} ms, it stops and asks" in prose
    assert (
        f"the signals module's variable `{names.ALARM_THRESHOLD_VARIABLE}`, which the root fills "
        f"from `{names.NAMES_JSON_FILE}`" in prose
    )
    assert (ROOT / names.NAMES_JSON_FILE).is_file()
    fallback = re.sub(r"\s+", " ", section(9))
    assert f"sleeps {names.FIS_DELAY_MS} ms before the Bedrock call" in fallback
    assert f"cached {names.DEFAULT_MODEL_CACHE_SECONDS} seconds" in fallback
    assert f"turns it on for {names.FIS_DURATION_MINUTES} minutes, then off" in fallback


def test_process_names_match_design():
    prose = re.sub(r"\s+", " ", DESIGN)
    assert f"| Review access | Role `{names.REVIEW_ROLE}` (AWS managed ReadOnlyAccess" in DESIGN
    assert f"through the AWS CLI profile `{names.REVIEW_PROFILE}`" in prose
    assert (
        f"the root variable `{names.INCIDENT_EMAIL_VARIABLE}`, marked sensitive" in prose
        and f"the git-ignored `{names.TFVARS_FILE}`" in prose
    )
    assert f"another load holds `{names.LOAD_LOCK_NAME}`, an OS file lock" in prose
    assert f"through the console script `{names.CLI_ENTRY_POINT}`" in DESIGN
    assert names.CLI_ENTRY_POINT.split(":")[0] == "cli." + names.CLI_NAME
    assert f"Send no {listed(names.FORBIDDEN_SAMPLING_PARAMETERS, 'or')}." in prose
    assert "topP" in names.FORBIDDEN_SAMPLING_PARAMETERS
    gitignore = (ROOT / ".gitignore").read_text()
    assert "*.tfvars" in gitignore  # TFVARS_FILE never reaches the public repo


# Rule sentences that hold no constant, pinned word for word (the PR #3 review found
# each one could be edited with the suite green). Change one only with its rule.
PINNED_RULE_SENTENCES = (
    # pointer and transactions (section 6)
    "returning the old item if that fails",
    "sets the pointer's `feature_key` on condition its `run_id` is this run",
    "plus `clear_active`, a delete of the pointer on condition it is absent or names this run",
    "(`clear_active`), on condition it is absent or names this run",
    "A transaction cancelled by a colliding write is a lost race",
    "A pointer whose run item is missing also counts as no active run for `status`, `tick` and "
    "`alarm`",
    "a `PLANNED` run with no pointer can never launch, so `approve` and `check` on it return "
    "`ILLEGAL_IN_STATE`",
    "`scripts/reset.py` refuses while the pointer names a run in a non-terminal state, unless it "
    "is given `--force`",
    # claim, takeover, commit
    "on condition `state = <from>`, `pending = <to>` and `pending_at` still holding the value read",
    "`tick` and `check` may take over any transition; any other action only one its row lists as "
    "a trigger",
    "on condition `state = <from>`, `pending = <to>` and `pending_at` holding the value this "
    "invocation wrote",
    "The health probe is the exception: an invoke error, a timeout, an invalid reply or a failed "
    "alarm read fails the probe.",
    "A resumed `RUNNING -> RESULTS_READY` reads the last results again and writes the report "
    "prose inside the effect.",
    # effects
    "`PLANNED -> RUNNING` (launch): enable the tick schedule; get-or-create the two permanent "
    "features",
    "`RUNNING -> ROLLED_BACK` and `RESULTS_READY -> ROLLED_BACK`: disable "
    "`assistant_cheaper_enabled`",
    "It adds no `incidents` entry.",
    "write the report to S3 with a plain put",
    "the alarm is read first, and an alarm in ALARM rolls the run back instead",
    "`PLANNED -> ABORTED`: disable the tick schedule (normally a no-op).",
    "or any move other than Fable 5.1 to Sonnet 5",
    # the transition table's guard cells
    "| `AWAITING_RESUME` | `RUNNING` | `approve`, when the alarm is not in ALARM and the health "
    "probe passes again | engineer |",
    "| `RESULTS_READY` | `PROMOTED` | `approve`, when the alarm is not in ALARM, only if the "
    "experiment is ended in Flagsmith | engineer |",
    # section 7, section 4 and section 9
    "Legality is judged after the `RUN_ACTIVE` check and before anything pending: an action the "
    "table does not allow from the run's state returns `ILLEGAL_IN_STATE` whatever is pending.",
    "The message names the pending target, its age and `marshal check <run_id>` (for a `PLANNED` "
    "run with no pointer: the run was reset; start a new one).",
    "After a claim, every effect failure, and a commit that fails other than by a lost race, is "
    "`EFFECT_FAILED`.",
    "still ends it, unless a launch is pending on it; such a run stays `PLANNED` and, with no "
    "pointer, affects nothing.",
    "**`check` is legal in every non-terminal state** (except on a `PLANNED` run with no pointer, "
    "above)",
    'a no-op whose message starts "no active run" and names the pointer\'s run',
    "While a branch is applied unmerged, every later apply includes it; branches are applied "
    "together only from one local merge, and only if they touch different modules",
    "that every plan and apply uses; the lead copies it into each worktree it creates.",
    "and do not invoke `rm-judge`, so no `AnswerCostMicroUsd` or `AnswerHelpful` is emitted",
    "a missing flag or a failed read means off",
    # what the spikes found (task 1.4)
    "the flag is enabled and `flag.variant` is set, record the exposure with "
    "`track_exposure_event`, as the SDK's own `get_experiment_flag` would",
    "The SDK sends the value as a string, and Flagsmith aggregates it as a number (task 1.2)",
    "a buffer of one item drops events tracked in a row (task 1.2)",
    "Development and Production use v2 feature versioning: a feature's environment state changes "
    "through a new version (create it, update its default state, publish it), never in place",
    "or sends an experiment's status or rollout decision through MCP's experiment create or "
    "update tools",
    "The experiment's create carries its whole rollout block",
    "The API has no primary-metric field, and whether the dashboard takes the first attached "
    "metric as primary is untested, so the agent attaches `helpful_rate` first and task 5.1 "
    "records which metric the dashboard shows as primary.",
    "in task 1.2 all events were in by about 2 minutes 15 seconds after the last was sent",
    "a refresh returns final results, as of the end (task 1.4)",
    "get-or-create the run's feature, options and experiment through the MCP server, and its "
    "metrics through the Admin API tool",
    "A variation's `identities` is the sum of its `new_identities` in the same response's "
    "exposures timeseries, not `n`",
    "the credential provider, because the CLI either stores the key in its local env file or takes "
    "it from a JSON-keyed secret, and `rm/flagsmith/api-key` holds a bare string; and the "
    "`flagsmith` target, which CLI 1.3.0 cannot create with API-key auth",
    "Results are read through the Admin API tool; MCP's results read returned nothing before a "
    "refresh",
    "that the Gateway adds the space is inferred, not observed",
    "**One run at a time.** `start` is refused while the pointer exists, that is, while a run is "
    "active.",
)


def test_rule_sentences_are_pinned():
    flat = re.sub(r"\s+", " ", DESIGN)
    missing = [sentence for sentence in PINNED_RULE_SENTENCES if sentence not in flat]
    assert missing == []


def test_other_files_repeat_the_rules():
    review = re.sub(r"\s+", " ", (ROOT / "docs/REVIEW.md").read_text())
    assert f"`aws --profile {names.REVIEW_PROFILE} sts get-caller-identity` shows the role" in (
        review
    )
    assert f"each with `--profile {names.REVIEW_PROFILE}`" in review
    assert f"`{names.REVIEW_ROLE}`" in review
    claude = re.sub(r"\s+", " ", (ROOT / "CLAUDE.md").read_text())
    assert (
        f"the Sum for `{names.CW_DIMENSION_VARIANT}={names.VARIANT_CURRENT}` plus the Sum for "
        f"`{names.CW_DIMENSION_VARIANT}={names.VARIANT_CHEAPER}`" in claude
    )
    assert "An empty result means unknown, not zero" in claude
    assert (
        "The count lags by more than 25 minutes (Gate 1), so read it again later, or ask the "
        "human to read the Usage page." in claude
    )
    spec = re.sub(
        r"\s+",
        " ",
        (
            ROOT / "openspec/changes/build-rollout-marshal/specs/experiment-agent/spec.md"
        ).read_text(),
    )
    assert (
        "a second `alarm` action arrives while the run is `ROLLED_BACK` or `AWAITING_RESUME`"
        in spec
    )
    assert (
        "and SHALL create the metrics and start the experiment through the Flagsmith Admin API"
        in spec
    )
    assert set(names.ALARM_NOOP_STATES) == {names.STATE_ROLLED_BACK, names.STATE_AWAITING_RESUME}
    tasks = re.sub(
        r"\s+", " ", (ROOT / "openspec/changes/build-rollout-marshal/tasks.md").read_text()
    )
    assert (
        f"`probe_cheaper(threshold_ms, n={names.PROBE_INVOKES}) -> {{passed, max_ms, reason}}`"
        in (tasks)
    )
    assert "`notify(run_id, subject, body, dedupe_key) -> message_id`" in tasks
    deployed = names.AGENT_RUNTIME_DEPLOYED_NAME
    assert f"The deployed runtime is `{deployed}`" in tasks
    assert (
        f"arn:aws:bedrock-agentcore:{names.REGION}:{names.ACCOUNT_ID}:runtime/{deployed}-*" in tasks
    )
    assert (
        "The agent creates the flag and experiment through the Flagsmith MCP server, and the "
        "metrics and the start through the Admin API." in tasks
    )
    assert "`helpful_lift_ci_lower` from `ci_low`" in tasks
    assert f"maps the results' `{names.CONTROL_LABEL}` arm to `{names.VARIANT_CURRENT}`" in tasks
    for line in (
        f"`resume` refuses a `pending` younger than {names.PENDING_STALE_SECONDS} seconds",
        "`PLANNED` run with no pointer returns `ILLEGAL_IN_STATE` and changes nothing, while "
        "`decline` ends it unless a launch is pending on it",
        "`decline` while another action's transition is pending returns `TRANSITION_PENDING`",
        "it refuses while the pointer names a run in a non-terminal state, unless given `--force`",
        "B, T, the alarm margin (B + 10000 - T) and the headroom (T - B)",
        "Lambda concurrent executions are at least 50",
    ):
        assert line in tasks, line
    for line in (
        "- **THEN** the response carries error `ILLEGAL_IN_STATE` and nothing changes, while "
        "`decline` still ends it unless a launch is pending on it",
        "has not been ended, and the alarm is not in ALARM",
        "while another action's transition is still pending",
        "`marshal start` is sent while a run is active (the pointer exists)",
    ):
        assert line in spec, line
    config = (ROOT / "openspec/config.yaml").read_text()
    assert "plus at most one follow-up pull request that holds only its post-merge lines" in config
    preflight = (ROOT / "scripts/preflight.sh").read_text()
    assert 'LAMBDA_CONCURRENCY_MIN="50"' in preflight


def test_console_script_once_the_cli_exists():
    """pyproject gains `marshal = cli.marshal:main` when task 2.6 creates cli/."""
    import tomllib

    if not (ROOT / "cli").is_dir():
        pytest.skip("cli/ arrives with task 2.6; the lead adds the console script then")
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"].get("scripts", {})
    assert scripts.get(names.CLI_NAME) == names.CLI_ENTRY_POINT
    # Gate 1: an unpackaged project installs no console script, so run it for real.
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    done = subprocess.run(
        [uv, "run", names.CLI_NAME, "--help"], cwd=ROOT, capture_output=True, text=True
    )
    assert done.returncode == names.CLI_EXIT_OK, done.stderr


# ---------------------------------------------------------------------------
# What the spikes found (task 1.4)
# ---------------------------------------------------------------------------
def test_spike_findings_match_design():
    prose = re.sub(r"\s+", " ", DESIGN)
    assert (
        f"effort `{names.ANSWER_THINKING_EFFORT}` is `{names.THINKING_EFFORT_FIELD}` on both "
        f"answer models, and thinking is disabled with `{names.THINKING_DISABLED_FIELD}` "
        f"`{names.THINKING_DISABLED_VALUE}`" in prose
    )
    assert f"exposures and results label it `{names.CONTROL_LABEL}` (task 1.2)" in prose
    assert names.CONTROL_LABEL not in names.VARIANTS
    rows = {r[0]: r[1] for r in table_rows(section(4), "| Thing | Name or value |")}
    words = {**names.METRIC_DIRECTION_API, **names.EXPECTED_DIRECTION_API}
    assert rows["API spellings (task 1.3)"] == "; ".join(f"{k}: `{v}`" for k, v in words.items())
    assert set(names.METRIC_DIRECTION_API) == {
        d["direction"] for d in names.METRIC_DEFINITIONS.values()
    }
    assert set(names.EXPECTED_DIRECTION_API) == set(names.EXPERIMENT_EXPECTED_DIRECTIONS.values())
    variant = names.FLAGSMITH_RESULT_VARIANT_FIELDS
    inference = names.FLAGSMITH_RESULT_INFERENCE_FIELDS
    assert (
        f"each variant's {listed(variant, 'and')} (the mean is sum / n) and, for `cheaper`, its "
        f"`{inference[0]}` (relative, a fraction), {listed(inference[1:], 'and')}" in prose
    )
    assert f"`helpful_lift_ci_lower` is the `helpful_rate` metric's `{inference[1]}`" in prose
    sample = json.loads((ROOT / "evidence/1.4-results-sample.json").read_text())
    metric = sample["results"]["payload"]["metrics"][0]
    assert set(metric["variants"]) == {names.VARIANT_CHEAPER, names.CONTROL_LABEL}
    for arm in metric["variants"].values():
        assert tuple(arm) == variant
    assert set(metric["inference"][names.VARIANT_CHEAPER]) == set(inference)
    points = sample["results"]["payload"]["exposures_timeseries"]["points"]
    exposures = {
        arm: sum(p[names.FLAGSMITH_EXPOSURES_FIELD].get(arm, 0) for p in points)
        for arm in (names.VARIANT_CHEAPER, names.CONTROL_LABEL)
    }
    assert exposures == {names.VARIANT_CHEAPER: 78, names.CONTROL_LABEL: 72}
    assert f"sum of its `{names.FLAGSMITH_EXPOSURES_FIELD}` in the same response" in prose
    assert names.AGENT_RUNTIME_DEPLOYED_NAME == (
        f"{names.AGENTCORE_PROJECT_NAME}_{names.AGENTCORE_TARGET_NAME}_{names.AGENT_RUNTIME_NAME}"
    )
    assert (
        f"in the AgentCore CLI project `{names.AGENTCORE_PROJECT_NAME}` with target "
        f"`{names.AGENTCORE_TARGET_NAME}`"
        in prose
        and f"so it is `{names.AGENT_RUNTIME_DEPLOYED_NAME}`, and callers use its ARN" in prose
    )
    assert "Task 1.2 confirmed that events accept this client-side key" in DESIGN
    assert names.api_key_authorization("k3y") == "Api-Key k3y"
    assert names.FLAGSMITH_MCP_AUTH_HEADER_TEMPLATE == (
        f"{names.GATEWAY_API_KEY_HEADER}: {names.api_key_authorization('<key>')}"
    )
    for bad in ("", " k3y", "k3y "):
        with pytest.raises(ValueError):
            names.api_key_authorization(bad)
    assert "with exactly one space; `api_key_authorization()`" in prose
    assert "Results lag the events" in prose and "refresh returns final results" in prose
    assert "The MCP server cannot create a metric" in prose
    assert "| `flagsmith_admin` | get-or-create metrics, start experiment," in DESIGN


# Every tuple, list, dict or set constant names the test that ties it to its
# source, so a new collection constant cannot land without one.
TIED_BY = {
    "FLAGSMITH_ADMIN_FORBIDDEN_PATHS": test_contract_c2,
    "FLAGSMITH_ADMIN_METHODS": test_contract_c2,
    "FLAGSMITH_EXPERIMENT_STATUSES": test_contract_c2,
    "MCP_EXPERIMENT_FORBIDDEN_FIELDS": test_contract_c2,
    "FORBIDDEN_SAMPLING_PARAMETERS": test_list_constants_match_their_design_lists,
    "VARIANTS": test_list_constants_match_their_design_lists,
    "EVENTS": test_list_constants_match_their_design_lists,
    "GUARDRAIL_METRICS": test_list_constants_match_their_design_lists,
    "POINTER_ATTRIBUTES": test_list_constants_match_their_design_lists,
    "INCIDENT_FIELDS": test_list_constants_match_their_design_lists,
    "HISTORY_FIELDS": test_list_constants_match_their_design_lists,
    "RECOMMENDATIONS": test_list_constants_match_their_design_lists,
    "SCHEMA_NAMES": test_list_constants_match_their_design_lists,
    "SCHEMA_FILES": test_list_constants_match_their_design_lists,
    "AGENT_TOOLS": test_list_constants_match_their_design_lists,
    "ACTORS": test_list_constants_match_their_design_lists,
    "PERMANENT_FEATURES": test_list_constants_match_their_design_lists,
    "ALARM_STATES": test_list_constants_match_their_design_lists,
    "MODEL_BY_VARIANT": test_rule_constants_match_their_design_sentences,
    "DEFAULT_MODEL_VALUES": test_rule_constants_match_their_design_sentences,
    "PERMANENT_FEATURE_DEFINITIONS": test_rule_constants_match_their_design_sentences,
    "METRIC_AGGREGATIONS_AVAILABLE": test_rule_constants_match_their_design_sentences,
    "METRIC_DEFINITIONS": test_rule_constants_match_their_design_sentences,
    "FLAGSMITH_CLIENT_OPTIONS": test_rule_constants_match_their_design_sentences,
    "ANSWER_FUNCTION_BY_VARIANT": test_rule_constants_match_their_design_sentences,
    "ACTIONS_WITH_LEGALITY_ERRORS": test_rule_constants_match_their_design_sentences,
    "CHECK_LEGAL_STATES": test_rule_constants_match_their_design_sentences,
    "ALARM_NOOP_STATES": test_rule_constants_match_their_design_sentences,
    "CLI_ACTIONS": test_rule_constants_match_their_design_sentences,
    "METRICS": test_definitions_are_consistent,
    "EXPERIMENT_EXPECTED_DIRECTIONS": test_experiment_rollout_and_directions_match_design,
    "GATEWAY_TARGETS": test_agentcore_pieces_match_design,
    "METRIC_DIRECTION_API": test_spike_findings_match_design,
    "EXPECTED_DIRECTION_API": test_spike_findings_match_design,
    "FLAGSMITH_RESULT_VARIANT_FIELDS": test_spike_findings_match_design,
    "FLAGSMITH_RESULT_INFERENCE_FIELDS": test_spike_findings_match_design,
    "FEATURE_KEYS": test_feature_keys_match_design,
    "JUDGE_PAYLOAD_FIELDS": test_judge_payload_fields_match_design,
    "CW_DIMENSION_SETS": test_cloudwatch_metrics_match_design,
    "CW_METRIC_UNITS": test_cloudwatch_metrics_match_design,
    "CW_METRIC_EMITTER": test_cloudwatch_metrics_match_design,
    "RUN_ITEM_ATTRIBUTES": test_run_item_attributes_match_design,
    "STATES": test_states_match_design,
    "TERMINAL_STATES": test_terminal_states_match_design,
    "ACTIONS": test_actions_match_design,
    "TRANSITIONS": test_transitions_match_design,
    "LEGAL_TRANSITIONS": test_transitions_match_design,
    "AWAITING_VALUES": test_awaiting_values_match_design,
    "AWAITING_BY_STATE": test_awaiting_by_state_follows_the_approve_rows,
    "ERROR_CODES": test_error_codes_match_design,
    "INVOKE_FIELDS": test_invoke_fields_match_design,
    "RESPONSE_FIELDS": test_response_fields_match_design,
    "ERROR_FIELDS": test_error_fields_match_design,
    "RESULTS_FIELDS": test_results_fields_match_design,
    "VARIATION_FIELDS": test_results_fields_match_design,
    "REPORT_SECTIONS": test_report_sections_match_design,
    "LOAD_SMOKE": test_load_plans_match_design,
    "LOAD_FAULT": test_load_plans_match_design,
    "LOAD_FULL": test_load_plans_match_design,
    "PLAN_FIELDS": test_plan_table_matches_design_and_schema,
    "PLAN_MODEL_FIELDS": test_plan_table_matches_design_and_schema,
    "PLAN_STOP_RULE_FIELDS": test_plan_table_matches_design_and_schema,
    "PLAN_GUARDRAIL_FIELDS": test_plan_table_matches_design_and_schema,
    "PLAN_RECOMMENDATION_RULE_FIELDS": test_plan_table_matches_design_and_schema,
    "ANSWER_REQUEST_FIELDS": test_answer_interface_matches_design,
    "ANSWER_RESPONSE_FIELDS": test_answer_interface_matches_design,
}


def test_every_collection_constant_names_the_test_that_ties_it():
    collections = {
        attr
        for attr, value in vars(names).items()
        if attr.isupper()
        and not attr.startswith("_")
        and isinstance(value, tuple | list | dict | set | frozenset)
    }
    assert sorted(collections - set(TIED_BY)) == [], "add each new collection to TIED_BY"
    assert sorted(set(TIED_BY) - collections) == [], "remove TIED_BY entries names.py dropped"


# ---------------------------------------------------------------------------
# Final hardening (1.1 review panel)
# ---------------------------------------------------------------------------
def test_awaiting_rule_holds_for_every_state():
    for state in [None, *names.STATES]:
        for awaiting in [None, *names.AWAITING_VALUES]:
            payload = response(state=state, awaiting=awaiting)
            check = valid if awaiting == names.awaiting_for(state) else invalid
            check("response", payload)


def test_model_id_follows_served_for_every_variant():
    for served in names.VARIANTS:
        for model in (names.MODEL_CURRENT, names.MODEL_CHEAPER):
            payload = judge_payload(served=served, model_id=model)
            check = valid if model == names.MODEL_BY_VARIANT[served] else invalid
            check("judge_payload", payload)


def test_required_lists_are_pinned():
    def every_field_required(node, path):
        if node.get("type") == "object" and "properties" in node:
            assert set(node["required"]) == set(node["properties"]), path
            for key, child in node["properties"].items():
                every_field_required(child, f"{path}.{key}")

    every_field_required(contracts.load_schema("plan"), "plan")
    run = contracts.load_schema("run_item")
    written_at_start = ["pk", "run_id", "state", "prompt", "plan", "history", "notified"]
    assert run["required"] == [*written_at_start, "created_at", "updated_at"]
    assert run["properties"]["incidents"]["items"]["required"] == ["started_at"]
    assert run["$defs"]["history_entry"]["required"] == list(names.HISTORY_FIELDS)
    invoke = contracts.load_schema("invoke")
    status_keys = set(next(p for p in design_payloads() if p["action"] == names.ACTION_STATUS))
    for payload in design_payloads():
        action = payload["action"]
        branch = next(
            b
            for b in invoke["oneOf"]
            if action
            in b["properties"]["action"].get("enum", [b["properties"]["action"].get("const")])
        )
        if action == names.ACTION_STATUS:
            status_line = next(
                line for line in "\n".join(fenced_blocks(7)).splitlines() if '"status"' in line
            )
            optional = "omitted" in status_line  # "<id or omitted for the active run>"
            assert branch["required"] == (["action"] if optional else ["action", "run_id"])
            assert set(branch["properties"]) == status_keys
        else:
            assert branch["required"] == list(payload), action
            assert set(branch["properties"]) == set(payload), action


def test_every_schema_copy_of_a_name_matches_names():
    body = names.RUN_ID_PATTERN[1:-1]
    invoke = contracts.load_schema("invoke")
    run = contracts.load_schema("run_item")
    judge = contracts.load_schema("judge_payload")
    resp = contracts.load_schema("response")
    assert run["$defs"]["run_id"]["pattern"] == names.RUN_ID_PATTERN
    assert judge["properties"]["run_id"]["pattern"] == names.RUN_ID_PATTERN
    assert resp["properties"]["run_id"]["oneOf"][1]["pattern"] == names.RUN_ID_PATTERN
    assert run["properties"]["pk"]["pattern"] == f"^{names.RUN_PK_PREFIX}{body}$"
    assert (
        run["properties"]["feature_key"]["pattern"] == f"^{names.EXPERIMENT_FEATURE_PREFIX}{body}$"
    )
    report = names.REPORT_KEY_TEMPLATE.replace(".", "\\.").replace("<run_id>", body)
    assert run["properties"]["report_key"]["pattern"] == f"^{report}$"
    assert run["$defs"]["history_entry"]["properties"]["from"]["enum"] == [None, *names.STATES]
    assert run["properties"]["pending"] == {"$ref": "#/$defs/state"}
    states = "|".join(names.STATES)
    assert run["properties"]["notified"]["propertyNames"]["pattern"] == f"^[1-9][0-9]*#({states})$"
    assert judge["properties"]["assigned"]["enum"] == list(names.VARIANTS)
    assert judge["properties"]["served"]["enum"] == list(names.VARIANTS)
    branches = {
        tuple(b["properties"]["action"].get("enum", [b["properties"]["action"].get("const")])): b
        for b in invoke["oneOf"]
    }
    engineer_actions = (names.ACTION_APPROVE, names.ACTION_DECLINE, names.ACTION_CHECK)
    assert set(branches) == {
        (names.ACTION_START,),
        engineer_actions,
        (names.ACTION_STATUS,),
        (names.ACTION_ALARM,),
        (names.ACTION_TICK,),
    }
    for key in [(names.ACTION_START,), engineer_actions]:
        assert branches[key]["properties"]["actor"] == {"const": names.ACTOR_ENGINEER}
    alarm = branches[(names.ACTION_ALARM,)]["properties"]
    assert alarm["alarm_name"] == {"const": names.ALARM_NAME}
    assert alarm["alarm_state"] == {"const": names.ALARM_STATE_ALARM}


def test_every_object_rejects_unlisted_keys():
    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object" and "propertyNames" not in node:
                assert node.get("additionalProperties") is False, path
            for key, child in node.items():
                walk(child, f"{path}.{key}")
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]")

    for name in names.SCHEMA_NAMES:
        walk(contracts.load_schema(name), name)
    shapes = {
        ("judge_payload", None): names.JUDGE_PAYLOAD_FIELDS,
        ("response", None): names.RESPONSE_FIELDS,
        ("results", None): names.RESULTS_FIELDS,
        ("answer_request", None): names.ANSWER_REQUEST_FIELDS,
        ("answer_response", None): names.ANSWER_RESPONSE_FIELDS,
    }
    for (name, _), fields in shapes.items():
        assert set(contracts.load_schema(name)["properties"]) == set(fields), name
    variation = contracts.load_schema("results")["$defs"]["variation"]["properties"]
    assert set(variation) == set(names.VARIATION_FIELDS)
    error = contracts.load_schema("response")["properties"]["error"]["oneOf"][1]["properties"]
    assert set(error) == set(names.ERROR_FIELDS)


DESIGN_TYPE = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def declared_types(text: str) -> set[str]:
    kinds = set()
    for part in (p.strip() for p in text.split("|")):
        if part == "null":
            kinds.add("null")
        elif part in DESIGN_TYPE:
            kinds.add(DESIGN_TYPE[part])
        elif part.startswith('"'):
            kinds.add("string")
        elif part.startswith("{") or part == "V":
            kinds.add("object")
    return kinds


def schema_types(node: dict, root: dict) -> set[str]:
    if "$ref" in node:
        return schema_types(root["$defs"][node["$ref"].split("/")[-1]], root)
    if "oneOf" in node:
        return set().union(*(schema_types(n, root) for n in node["oneOf"]))
    if "enum" in node:
        py = {str: "string", bool: "boolean", int: "integer", float: "number"}
        return {"null" if v is None else py[type(v)] for v in node["enum"]}
    kind = node["type"]
    return {kind} if isinstance(kind, str) else set(kind)


def design_field_types(span: str) -> dict[str, set[str]]:
    flat = re.sub(r"\s+", " ", span)
    pairs = re.findall(r'"(\w+)": (.*?)(?=, "\w+":|\}\s*$|\}\}$)', flat)
    out: dict[str, set[str]] = {}
    for field, kind in pairs:
        out.setdefault(field, declared_types(kind))  # keep the first: the top-level field
    return out


def test_field_types_match_design():
    spans = multi_line_spans(section(7))
    response_span = next(s for s in spans if s.startswith('{"run_id"'))
    results_span = next(s for s in spans if s.startswith('{"ready"'))
    variation_span = next(s for s in spans if s.startswith("V = ")).removeprefix("V = ")
    resp = contracts.load_schema("response")
    res = contracts.load_schema("results")
    for span, schema, props, fields in [
        (response_span, resp, resp["properties"], names.RESPONSE_FIELDS),
        (results_span, res, res["properties"], names.RESULTS_FIELDS),
        (variation_span, res, res["$defs"]["variation"]["properties"], names.VARIATION_FIELDS),
    ]:
        declared = design_field_types(span)
        for field in fields:
            assert declared[field] == schema_types(props[field], schema), field
    flat = re.sub(r"\s+", " ", response_span)
    error_span = re.search(r'"error": null \| (\{.*?\})\}$', flat).group(1)
    error_props = resp["properties"]["error"]["oneOf"][1]["properties"]
    declared = design_field_types(error_span)
    assert set(declared) == set(names.ERROR_FIELDS)
    for field in names.ERROR_FIELDS:
        assert declared[field] == schema_types(error_props[field], resp), field


def test_helpers_reject_bad_input():
    eastern = timezone(timedelta(hours=-5))
    assert (
        names.scratch_run_id("smoke", datetime(2026, 11, 27, 4, 12, tzinfo=eastern)) == "rsmoke0912"
    )
    with pytest.raises(ValueError):
        names.scratch_run_id("smoke", datetime(2026, 11, 27, 4, 12))
    for bad in ("not a run", "rspike", "r202611271530\n"):
        for helper in (names.run_pk, names.report_key, names.experiment_feature_key):
            with pytest.raises(ValueError):
                helper(bad)
    assert not names.is_identity("user-001\n")
    session = names.new_session_id()
    assert uuid.UUID(session.removeprefix(names.SESSION_ID_PREFIX)).version == 4
    assert len(session) == len(names.SESSION_ID_PREFIX) + 36


def test_utc_iso_writes_one_sortable_form():
    eastern = timezone(timedelta(hours=-5))
    forms = [
        datetime(2026, 11, 27, 10, 30, 5, tzinfo=eastern),
        "2026-11-27T15:30:05Z",
        "2026-11-27T15:30:05.000+0000",
        "2026-11-27T15:30:05+00:00",
    ]
    assert {names.utc_iso(f) for f in forms} == {"2026-11-27T15:30:05.000000Z"}
    later = names.utc_iso("2026-11-27T15:30:05.5Z")
    assert names.utc_iso(forms[1]) < later
    assert re.fullmatch(
        contracts.load_schema("run_item")["$defs"]["utc"]["pattern"], names.utc_iso()
    )
    with pytest.raises(ValueError):
        names.utc_iso("2026-11-27T15:30:05")
    stored = names.utc_iso("2019-10-02T17:04:40.989+0000")  # an alarm's at, as CloudWatch sends it
    valid(
        "run_item",
        run_item(state="ROLLED_BACK", incidents=[{"started_at": stored}]),
    )


def test_names_json_content():
    data = json.loads((ROOT / "contracts/names.json").read_text())
    public = {a for a in vars(names) if a.isupper() and not a.startswith("_")}
    assert set(data) == public
    assert data["ALARM_THRESHOLD_MS"] == 10000
    assert data["TRANSITIONS"][0] == {
        "source": None,
        "target": "PLANNED",
        "triggers": ["start"],
        "actor": "engineer",
    }
    assert data["LOAD_FULL"] == {"users": 150, "questions": 10}
    assert data["RECOMMENDATIONS"] == list(names.RECOMMENDATIONS)


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "tick", "actor": "engineer"},
        {
            "action": "alarm",
            "alarm_name": "rm-cheaper-latency-p95",
            "alarm_state": "ALARM",
            "at": "2026-11-27T15:42:00Z",
            "actor": "agent",
        },
    ],
)
def test_actor_is_rejected_where_design_has_none(payload):
    invalid("invoke", payload)


def test_alarm_at_accepts_microseconds():
    valid(
        "invoke",
        {
            "action": "alarm",
            "alarm_name": names.ALARM_NAME,
            "alarm_state": "ALARM",
            "at": "2026-11-27T15:42:00.123456+00:00",
        },
    )


def test_dynamo_helpers_convert_every_level():
    from decimal import Decimal

    nested = {"a": [0.1, {"b": [0.3, 2]}], "c": 3, "d": True, "e": "x", "f": None}
    stored = dynamo.to_dynamo(nested)
    assert stored == {
        "a": [Decimal("0.1"), {"b": [Decimal("0.3"), 2]}],
        "c": 3,
        "d": True,
        "e": "x",
        "f": None,
    }
    assert type(stored["a"][0]) is Decimal and type(stored["a"][1]["b"][0]) is Decimal
    assert type(stored["d"]) is bool
    back = dynamo.from_dynamo(
        {
            "a": [Decimal("0.1"), {"b": [Decimal("0.3"), Decimal("2")]}],
            "c": Decimal("3"),
            "g": Decimal("1.0"),
            "s": {Decimal("4")},
        }
    )
    assert back == {"a": [0.1, {"b": [0.3, 2]}], "c": 3, "g": 1, "s": {4}}
    assert all(type(v) is int for v in back["s"])  # Decimal("4") == 4, so check the type
    assert type(back["a"][0]) is float and type(back["a"][1]["b"][0]) is float
    assert type(back["a"][1]["b"][1]) is int and type(back["g"]) is int  # whole numbers are int


# ---------------------------------------------------------------------------
# Test gaps the 1.1 review panel found (each one survived a mutation)
# ---------------------------------------------------------------------------
def test_plan_schema_pins_every_injected_value():
    """Every plan field but the model's prose is a const equal to the value built from names."""

    def consts(node, path):
        out = {}
        for key, child in node["properties"].items():
            if key in ("hypothesis", "summary"):
                continue
            if "const" in child:
                out[key] = child["const"]
            else:
                assert child.get("type") == "object", f"{path}.{key}: neither a const nor an object"
                out[key] = consts(child, f"{path}.{key}")
        return out

    expected = {k: v for k, v in plan().items() if k not in ("hypothesis", "summary")}
    assert consts(contracts.load_schema("plan"), "plan") == expected


def test_every_at_field_is_the_one_stored_form():
    run = contracts.load_schema("run_item")
    utc = {"$ref": "#/$defs/utc"}
    top = [k for k in names.RUN_ITEM_ATTRIBUTES if k.endswith("_at")]
    assert top == ["pending_at", "last_refresh_at", "first_running_at", "created_at", "updated_at"]
    for key in top:
        assert run["properties"][key] == utc, key
    for key in names.INCIDENT_FIELDS:
        assert run["properties"]["incidents"]["items"]["properties"][key] == utc, key
    assert run["$defs"]["history_entry"]["properties"]["at"] == utc


def test_both_variations_use_the_variation_shape():
    props = contracts.load_schema("results")["properties"]["variations"]["properties"]
    assert props == {v: {"$ref": "#/$defs/variation"} for v in names.VARIANTS}


def test_every_node_with_properties_is_a_closed_object():
    """Keyed on properties or required, not on a type line that a loosening edit can delete."""

    def walk(node, path):
        if isinstance(node, dict):
            exempt = re.search(r"\.(if|then|else)(\.|$)", path) or path.rsplit(".", 1)[-1] in (
                "history_first",  # allOf of the closed history_entry
                "history_later",
                "complete_variation",  # results.json's ready predicate, used only inside if
            )
            if ("properties" in node or "required" in node) and not exempt:
                assert node.get("type") == "object", path
                assert node.get("additionalProperties") is False, path
            for key, child in node.items():
                walk(child, f"{path}.{key}")
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]")

    for name in names.SCHEMA_NAMES:
        walk(contracts.load_schema(name), name)


GOOD_VARIATION = {
    "identities": 75,
    "helpful_rate": 0.9,
    "mean_latency_ms": 4200.5,
    "mean_cost_micro_usd": 8100.0,
}


@pytest.mark.parametrize(
    "schema, instance",
    [
        ("run_item", lambda: run_item(metric_ids={"helpful_rate": "7"})),
        ("run_item", lambda: run_item(metric_ids={"helpful_rate": -1})),
        ("run_item", lambda: run_item(experiment_id=-1)),
        ("run_item", lambda: run_item(prompt="")),
        ("plan", lambda: plan(summary="")),
        ("results", lambda: results(prob_cheaper_not_worse=None)),
        (
            "results",
            lambda: results(
                variations={
                    "current": GOOD_VARIATION,
                    "cheaper": {**GOOD_VARIATION, "identities": -3},
                }
            ),
        ),
        (
            "results",
            lambda: results(
                variations={
                    "current": GOOD_VARIATION,
                    "cheaper": {**GOOD_VARIATION, "mean_cost_micro_usd": -1.0},
                }
            ),
        ),
        ("judge_payload", lambda: judge_payload(input_tokens=180.5)),
        ("judge_payload", lambda: judge_payload(output_tokens="95")),
        ("judge_payload", lambda: judge_payload(latency_ms=-5)),
        ("judge_payload", lambda: judge_payload(question="")),
    ],
)
def test_schema_bounds_and_types_are_pinned(schema, instance):
    invalid(schema, instance())
