# rm-invoker: the Lambda that wakes rm_agent on the alarm and on every rm-tick run
# (design.md section 7; code in signals/invoker/, task 4.2).

locals {
  # The package: the handler, its client setting, and the two contracts modules it imports.
  # contracts.names uses only the standard library, and boto3 and botocore come from the
  # python3.13 runtime (tasks.md 4.1), so nothing else is bundled. Tests stay out.
  invoker_files = [
    "contracts/__init__.py",
    "contracts/names.py",
    "signals/__init__.py",
    "signals/invoker/__init__.py",
    "signals/invoker/agent_client.py",
    "signals/invoker/handler.py",
  ]

  # The runtime splits this at the last dot and imports signals.invoker.handler from the
  # zip root (awslambdaric bootstrap.py, _get_handler).
  invoker_handler = "signals.invoker.handler.handler"

  # SSM parameter ARNs drop the name's leading slash after "parameter/" (Service
  # Authorization Reference, Systems Manager resource types).
  runtime_arn_parameter_arn = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/${trimprefix(local.names.AGENT_RUNTIME_ARN_PARAMETER, "/")}"

  # The deployed runtime's ID is <name>-<suffix>. The trailing * also covers its
  # runtime-endpoint ARN (runtime/<id>/runtime-endpoint/<name>); InvokeAgentRuntime
  # requires both resource types (Service Authorization Reference, Bedrock AgentCore).
  agent_runtime_arns = "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:runtime/${local.names.AGENT_RUNTIME_DEPLOYED_NAME}-*"
}

data "archive_file" "invoker" {
  type        = "zip"
  output_path = "${path.module}/build/${local.names.INVOKER_FUNCTION}.zip"

  # `source` blocks write MS-DOS entries with no Unix mode; output_file_mode applies only to
  # source_dir/source_file (archive v2.8.1); after-apply step 2 proves Lambda can read them.
  dynamic "source" {
    for_each = local.invoker_files
    content {
      content  = file("${local.repo_root}/${source.value}")
      filename = source.value
    }
  }
}

data "aws_iam_policy_document" "invoker_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "invoker" {
  name               = local.names.INVOKER_ROLE
  description        = "rm-invoker's execution role: logs, the runtime ARN parameter, InvokeAgentRuntime on rm_agent."
  assume_role_policy = data.aws_iam_policy_document.invoker_trust.json
}

data "aws_iam_policy_document" "invoker" {
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.invoker.arn}:*"]
  }

  statement {
    sid       = "ReadRuntimeArn"
    actions   = ["ssm:GetParameter"]
    resources = [local.runtime_arn_parameter_arn]
  }

  statement {
    sid       = "InvokeAgent"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [local.agent_runtime_arns]
  }
}

resource "aws_iam_role_policy" "invoker" {
  name   = local.names.INVOKER_FUNCTION
  role   = aws_iam_role.invoker.id
  policy = data.aws_iam_policy_document.invoker.json
}

# Created here, so the role needs no logs:CreateLogGroup. No retention is set, as for the
# group Lambda would create itself.
resource "aws_cloudwatch_log_group" "invoker" {
  name = "/aws/lambda/${local.names.INVOKER_FUNCTION}"
}

resource "aws_lambda_function" "invoker" {
  function_name    = local.names.INVOKER_FUNCTION
  description      = "Wakes rm_agent: alarm on rm-cheaper-latency-p95 entering ALARM, tick from rm-tick."
  role             = aws_iam_role.invoker.arn
  runtime          = "python${local.names.PYTHON_VERSION}"
  architectures    = [local.names.LAMBDA_ARCHITECTURE]
  handler          = local.invoker_handler
  filename         = data.archive_file.invoker.output_path
  source_code_hash = data.archive_file.invoker.output_base64sha256
  timeout          = local.names.INVOKER_TIMEOUT_SECONDS

  environment {
    variables = {
      # "1" or "0": a bare bool would give "true", which the handler treats as live.
      (local.names.INVOKER_STUB_ENV) = var.invoker_stub ? local.names.INVOKER_STUB_ON : "0"
    }
  }

  depends_on = [aws_cloudwatch_log_group.invoker, aws_iam_role_policy.invoker]
}
