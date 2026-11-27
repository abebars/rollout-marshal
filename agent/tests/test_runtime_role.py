"""infra/modules/agent/runtime_role.tf: rm-agent-runtime-tools on the runtime's role (tasks.md
2.7; design.md section 7).

The policy's statements live in runtime_tools_policy.json.tftpl, rendered by Terraform's
templatefile(). These tests render the same template with the same names and pin every
statement, so an action or a resource can neither widen nor drop quietly. A last check ties the
statements to the boto3 clients the runtime's code makes: a client that no statement (or the
CLI stack's own grants) covers fails here, not in the deployed runtime.
"""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

from contracts import names

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "infra" / "modules" / "agent"
TEMPLATE = (MODULE / "runtime_tools_policy.json.tftpl").read_text()
ROLE_TF = (MODULE / "runtime_role.tf").read_text()
STACK_TS = (ROOT / "agent" / "agentcore" / "cdk" / "lib" / "cdk-stack.ts").read_text()

# The template's variables, bound as runtime_role.tf binds them.
BINDINGS = {
    "region": ("REGION", names.REGION),
    "account_id": ("ACCOUNT_ID", names.ACCOUNT_ID),
    "runs_table": ("RUNS_TABLE", names.RUNS_TABLE),
    "alarm_name": ("ALARM_NAME", names.ALARM_NAME),
    "cheaper_function": ("ANSWER_CHEAPER_FUNCTION", names.ANSWER_CHEAPER_FUNCTION),
    "schedule_group": ("TICK_SCHEDULE_GROUP", names.TICK_SCHEDULE_GROUP),
    "tick_schedule": ("TICK_SCHEDULE", names.TICK_SCHEDULE),
    "scheduler_role": ("SCHEDULER_ROLE", names.SCHEDULER_ROLE),
    "scheduler_principal": ("SCHEDULER_SERVICE_PRINCIPAL", names.SCHEDULER_SERVICE_PRINCIPAL),
    "incidents_topic": ("INCIDENTS_TOPIC", names.INCIDENTS_TOPIC),
    "reports_bucket": ("REPORTS_BUCKET", names.REPORTS_BUCKET),
}
REPORTS_PREFIX = names.REPORT_KEY_TEMPLATE.split("<run_id>")[0]


def code(text: str) -> str:
    """HCL without comments, every run of whitespace as one space."""
    lines = (line.split("#", 1)[0] for line in text.splitlines())
    return re.sub(r"\s+", " ", " ".join(lines))


def rendered() -> dict:
    values = {variable: value for variable, (_, value) in BINDINGS.items()}
    values["reports_prefix"] = REPORTS_PREFIX
    return json.loads(string.Template(TEMPLATE).substitute(values))


def arn(service: str, resource: str) -> str:
    return f"arn:aws:{service}:{names.REGION}:{names.ACCOUNT_ID}:{resource}"


STATEMENTS = {
    "RunsTable": {
        "Action": [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
        ],
        "Resource": arn("dynamodb", f"table/{names.RUNS_TABLE}"),
    },
    "LatencyAlarm": {
        "Action": "cloudwatch:DescribeAlarms",
        "Resource": arn("cloudwatch", f"alarm:{names.ALARM_NAME}"),
    },
    "HealthProbe": {
        "Action": "lambda:InvokeFunction",
        "Resource": arn("lambda", f"function:{names.ANSWER_CHEAPER_FUNCTION}"),
    },
    "TickSchedule": {
        "Action": ["scheduler:GetSchedule", "scheduler:UpdateSchedule"],
        "Resource": arn("scheduler", f"schedule/{names.TICK_SCHEDULE_GROUP}/{names.TICK_SCHEDULE}"),
    },
    "PassSchedulerRole": {
        "Action": "iam:PassRole",
        "Resource": f"arn:aws:iam::{names.ACCOUNT_ID}:role/{names.SCHEDULER_ROLE}",
        "Condition": {"StringEquals": {"iam:PassedToService": names.SCHEDULER_SERVICE_PRINCIPAL}},
    },
    "IncidentNotes": {
        "Action": "sns:Publish",
        "Resource": arn("sns", names.INCIDENTS_TOPIC),
    },
    "Reports": {
        "Action": "s3:PutObject",
        "Resource": f"arn:aws:s3:::{names.REPORTS_BUCKET}/{REPORTS_PREFIX}*",
    },
}


def test_every_statement_is_pinned_exactly():
    policy = rendered()
    assert policy["Version"] == "2012-10-17"
    found = {statement["Sid"]: statement for statement in policy["Statement"]}
    assert list(found) == list(STATEMENTS)  # nothing added, nothing dropped, in this order
    for sid, want in STATEMENTS.items():
        assert found[sid] == {"Sid": sid, "Effect": "Allow", **want}, sid


def test_no_statement_is_wider_than_its_resource():
    for statement in rendered()["Statement"]:
        actions = (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        assert all(":" in action and "*" not in action for action in actions), statement
        assert statement["Resource"] != "*" and "Deny" != statement["Effect"], statement
    assert REPORTS_PREFIX == "runs/"  # only the run reports' folder


def test_runtime_role_tf_binds_the_template_to_the_names():
    tf = code(ROLE_TF)
    export = 'data "aws_cloudformation_export" "runtime_role" {'
    assert f"{export} name = local.names.AGENT_RUNTIME_ROLE_EXPORT }}" in tf
    policy = 'resource "aws_iam_role_policy" "runtime_tools" {'
    assert f"{policy} name = local.names.AGENT_RUNTIME_TOOLS_POLICY" in tf
    assert "role = local.runtime_role_name" in tf
    assert 'templatefile("${path.module}/runtime_tools_policy.json.tftpl", {' in tf
    for variable, (constant, _) in BINDINGS.items():
        assert f"{variable} = local.names.{constant}" in tf, variable
    assert "reports_prefix = local.reports_prefix" in tf
    assert 'reports_prefix = split("<run_id>", local.names.REPORT_KEY_TEMPLATE)[0]' in tf
    assert set(re.findall(r"\$\{(\w+)\}", TEMPLATE)) == set(BINDINGS) | {"reports_prefix"}
    assert len(re.findall(r'resource "', tf)) == 1  # the policy, and nothing else


def test_the_export_and_policy_names_are_the_contracts():
    assert names.AGENT_RUNTIME_ROLE_EXPORT == "AgentCore-rm-default-rm-agent-RoleArn"
    assert names.AGENT_RUNTIME_TOOLS_POLICY == "rm-agent-runtime-tools"
    assert names.AGENT_RUNTIME_ROLE_EXPORT.startswith(names.AGENT_STACK)


# What each boto3 service the runtime reaches is granted by: this policy's statement, or the
# CLI stack's own grants (cdk-stack.ts, and the AgentCoreApplication construct's Bedrock
# grant, read from the deployed role in task 2.7's evidence).
COVERED_BY = {
    "dynamodb": "RunsTable",
    "cloudwatch": "LatencyAlarm",
    "lambda": "HealthProbe",
    "scheduler": "TickSchedule",
    "sns": "IncidentNotes",
    "s3": "Reports",
    names.BEDROCK_RUNTIME_SERVICE: "the stack: bedrock:InvokeModel",
    "bedrock-agentcore-control": "the stack: FindRmGateway and UseRmGateway",
}


def _services_in_runtime_code() -> set[str]:
    services = set()
    for path in [ROOT / "agent/runtime/main.py", *(ROOT / "agent/runtime/rm_agent").rglob("*.py")]:
        for match in re.finditer(
            r'boto3\.client\(\s*(?:"([a-z0-9-]+)"|names\.([A-Z_]+))', path.read_text()
        ):
            services.add(match.group(1) or getattr(names, match.group(2)))
    return services


def test_every_service_the_runtime_reaches_is_granted():
    assert _services_in_runtime_code() == set(COVERED_BY)
    sids = {statement["Sid"] for statement in rendered()["Statement"]}
    for service, grant in COVERED_BY.items():
        assert grant in sids or grant.startswith("the stack"), service
    # The stack's own statements that the Gateway lookup and calls rely on.
    for sid in ("UseRmGateway", "FindRmGateway"):
        assert f"sid: '{sid}'" in STACK_TS
