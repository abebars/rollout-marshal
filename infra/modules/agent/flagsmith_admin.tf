# rm-flagsmith-admin (tasks.md 2.2; design.md sections 4, 7 and 8). Owner: workstream A.
#
# The Flagsmith Admin API calls the MCP server cannot make: get-or-create the metrics,
# start an experiment, refresh and read results, read an experiment. The handler reads the
# organisation key from Secrets Manager at run time; the role may read that one secret and
# write its own logs, nothing else. The module takes no inputs: it reads
# contracts/names.json itself (infra/agent.tf).
#
# The zip holds agent/flagsmith_admin/handler.py and contracts/names.py, standard library
# only (boto3 comes with the Lambda runtime), built at plan time by the archive provider.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

locals {
  names = jsondecode(file("${path.root}/../contracts/names.json"))

  function_name = local.names.FLAGSMITH_ADMIN_FUNCTION # rm-flagsmith-admin
  role_name     = "${local.function_name}-role"        # rm-flagsmith-admin-role
  policy_name   = "${local.function_name}-policy"      # rm-flagsmith-admin-policy
  log_group     = "/aws/lambda/${local.function_name}"
  handler_dir   = "${path.root}/../agent/flagsmith_admin"
  contracts_dir = "${path.root}/../contracts"
}

data "archive_file" "flagsmith_admin" {
  type        = "zip"
  output_path = "${path.module}/build/${local.function_name}.zip"

  source {
    content  = file("${local.handler_dir}/handler.py")
    filename = "handler.py"
  }

  source {
    content  = file("${local.contracts_dir}/__init__.py")
    filename = "contracts/__init__.py"
  }

  source {
    content  = file("${local.contracts_dir}/names.py")
    filename = "contracts/names.py"
  }
}

# Metadata only (ARN and name). The function reads the value at run time, never here.
data "aws_secretsmanager_secret" "api_key" {
  name = local.names.SECRET_FLAGSMITH_API_KEY
}

# The trust policy the Lambda developer guide gives for an execution role.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "flagsmith_admin" {
  statement {
    sid       = "ReadTheOrganisationKeyOnly"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.api_key.arn]
  }

  statement {
    sid       = "WriteItsLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.flagsmith_admin.arn}:*"]
  }
}

resource "aws_iam_role" "flagsmith_admin" {
  name               = local.role_name
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "flagsmith_admin" {
  name   = local.policy_name
  role   = aws_iam_role.flagsmith_admin.id
  policy = data.aws_iam_policy_document.flagsmith_admin.json
}

resource "aws_cloudwatch_log_group" "flagsmith_admin" {
  name = local.log_group
}

resource "aws_lambda_function" "flagsmith_admin" {
  function_name    = local.function_name
  role             = aws_iam_role.flagsmith_admin.arn
  runtime          = "python${local.names.PYTHON_VERSION}"
  architectures    = [local.names.LAMBDA_ARCHITECTURE]
  handler          = "handler.handler"
  filename         = data.archive_file.flagsmith_admin.output_path
  source_code_hash = data.archive_file.flagsmith_admin.output_base64sha256
  timeout          = local.names.FLAGSMITH_ADMIN_TIMEOUT_SECONDS

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.flagsmith_admin.name
  }

  depends_on = [aws_iam_role_policy.flagsmith_admin]

  lifecycle {
    precondition {
      condition     = startswith(local.function_name, local.names.RESOURCE_PREFIX)
      error_message = "rm-flagsmith-admin's name must start with RESOURCE_PREFIX."
    }
  }
}
