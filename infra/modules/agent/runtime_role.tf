# rm-agent-runtime-tools (tasks.md 2.7; design.md section 7). Owner: workstream A.
#
# The runtime's role is the AgentCore CLI stack's own: AgentCore-rm-default creates it, with
# the Bedrock, logging and Gateway grants (agent/agentcore/cdk/lib/cdk-stack.ts). This file
# reads its ARN from the stack's export and attaches the inline policy the agent's tools need.
# runtime_tools_policy.json.tftpl holds the statements, one per tool call, each on its exact
# resource:
# - the state store, notify and record_refresh: GetItem, PutItem, UpdateItem and DeleteItem
#   on rm-runs (TransactWriteItems is authorised per item, by those actions);
# - signals: DescribeAlarms on the latency alarm;
# - the probe: InvokeFunction on rm-answer-cheaper;
# - the schedule tool: GetSchedule and UpdateSchedule on rm-tick, and PassRole on
#   rm-scheduler-role to the Scheduler only, since UpdateSchedule passes the target's role;
# - notify: Publish to rm-incidents;
# - report: PutObject under runs/ in the reports bucket.
#
# Nothing else is reached by boto3: Flagsmith and rm-flagsmith-admin go through the Gateway,
# and the stack already grants Bedrock and the Gateway. agent/tests/test_runtime_role.py pins
# every statement.

locals {
  runtime_role_arn  = data.aws_cloudformation_export.runtime_role.value
  runtime_role_name = element(split("/", local.runtime_role_arn), length(split("/", local.runtime_role_arn)) - 1)
  reports_prefix    = split("<run_id>", local.names.REPORT_KEY_TEMPLATE)[0] # runs/
}

# The stack's own export: the runtime's role (design.md section 7).
data "aws_cloudformation_export" "runtime_role" {
  name = local.names.AGENT_RUNTIME_ROLE_EXPORT
}

resource "aws_iam_role_policy" "runtime_tools" {
  name = local.names.AGENT_RUNTIME_TOOLS_POLICY
  role = local.runtime_role_name
  policy = templatefile("${path.module}/runtime_tools_policy.json.tftpl", {
    region              = local.names.REGION
    account_id          = local.names.ACCOUNT_ID
    runs_table          = local.names.RUNS_TABLE
    alarm_name          = local.names.ALARM_NAME
    cheaper_function    = local.names.ANSWER_CHEAPER_FUNCTION
    schedule_group      = local.names.TICK_SCHEDULE_GROUP
    tick_schedule       = local.names.TICK_SCHEDULE
    scheduler_role      = local.names.SCHEDULER_ROLE
    scheduler_principal = local.names.SCHEDULER_SERVICE_PRINCIPAL
    incidents_topic     = local.names.INCIDENTS_TOPIC
    reports_bucket      = local.names.REPORTS_BUCKET
    reports_prefix      = local.reports_prefix
  })
}
