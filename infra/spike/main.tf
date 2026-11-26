# Task 1.2 spike root (design.md section 2). Owner: workstream B.
#
# A separate root with LOCAL state, applied and destroyed by B for task 1.2 only. There is
# no backend block, so state stays in this directory, and .gitignore keeps *.tfstate out of
# git. B runs the account check first and destroys everything before the pull request is
# reviewed. Build the zip first: app/spike/build.sh.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.74"
    }
  }
}

locals {
  # The guard, written as infra/main.tf writes it.
  account_id = "818928662830"
  region     = "us-east-1"

  # Every other name and value comes from the contracts (make names-json).
  names = jsondecode(file("${path.module}/../../contracts/names.json"))

  function_name = "${local.names.SPIKE_RESOURCE_PREFIX}sdk" # rm-spike-sdk
  role_name     = "${local.function_name}-role"             # rm-spike-sdk-role
  policy_name   = "${local.function_name}-policy"           # rm-spike-sdk-policy
  log_group     = local.function_name                       # rm-spike-sdk
  zip_path      = "${path.module}/../../app/spike/build/rm-spike-sdk.zip"

  models = toset([local.names.MODEL_CURRENT, local.names.MODEL_CHEAPER, local.names.MODEL_JUDGE])

  # Converse runs in the account's default Bedrock project unless a request names one, and
  # needs bedrock:InvokeModel on that project too (Bedrock API reference, Converse).
  default_project_arn = "arn:aws:bedrock:${local.region}:${local.account_id}:project/default"
}

provider "aws" {
  region = local.region

  # Terraform refuses to plan or apply against any other account.
  allowed_account_ids = [local.account_id]

  default_tags {
    tags = {
      project = "rollout-marshal"
    }
  }
}

# Metadata only (ARN and name). The value is read by the function at run time, never here.
data "aws_secretsmanager_secret" "sdk_key" {
  name = local.names.SECRET_FLAGSMITH_SERVER_KEY
}

# Each geographic inference profile, with the foundation models it routes to.
data "aws_bedrock_inference_profile" "model" {
  for_each             = local.models
  inference_profile_id = each.value
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

data "aws_iam_policy_document" "spike" {
  statement {
    sid       = "ReadTheSdkKeyOnly"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.sdk_key.arn]
  }

  statement {
    sid       = "InvokeTheThreeInferenceProfiles"
    actions   = ["bedrock:InvokeModel"]
    resources = [for profile in data.aws_bedrock_inference_profile.model : profile.inference_profile_arn]
  }

  # One statement per profile: its foundation models, only when reached through it
  # (Bedrock user guide, Geographic cross-Region inference, IAM policy requirements).
  dynamic "statement" {
    for_each = data.aws_bedrock_inference_profile.model

    content {
      sid       = "InvokeModelsVia${replace(title(replace(statement.key, "/[.-]/", " ")), " ", "")}"
      actions   = ["bedrock:InvokeModel"]
      resources = [for model in statement.value.models : model.model_arn]

      condition {
        test     = "StringEquals"
        variable = "bedrock:InferenceProfileArn"
        values   = [statement.value.inference_profile_arn]
      }
    }
  }

  statement {
    sid       = "InvokeInTheDefaultProject"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.default_project_arn]
  }

  statement {
    sid       = "WriteItsLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.spike.arn}:*"]
  }
}

resource "aws_iam_role" "spike" {
  name               = local.role_name
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "spike" {
  name   = local.policy_name
  role   = aws_iam_role.spike.id
  policy = data.aws_iam_policy_document.spike.json
}

resource "aws_cloudwatch_log_group" "spike" {
  name              = local.log_group
  retention_in_days = 1
}

resource "aws_lambda_function" "spike" {
  function_name    = local.function_name
  role             = aws_iam_role.spike.arn
  runtime          = "python${local.names.PYTHON_VERSION}"
  architectures    = [local.names.LAMBDA_ARCHITECTURE]
  handler          = "app.spike.handler.handler"
  filename         = local.zip_path
  source_code_hash = filebase64sha256(local.zip_path)
  timeout          = 300 # 150 identity requests in a row, then a flush
  memory_size      = 256

  environment {
    variables = {
      # The secret's NAME. The handler reads the value at run time and refuses any other name.
      FLAGSMITH_KEY_SECRET_NAME = local.names.SECRET_FLAGSMITH_SERVER_KEY
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.spike.name
  }

  depends_on = [aws_iam_role_policy.spike]

  lifecycle {
    precondition {
      condition     = local.account_id == local.names.ACCOUNT_ID && local.region == local.names.REGION
      error_message = "The provider guard no longer matches contracts/names.json."
    }

    precondition {
      condition = alltrue([
        for name in [local.function_name, local.role_name, local.policy_name, local.log_group] :
        startswith(name, local.names.SPIKE_RESOURCE_PREFIX)
      ])
      error_message = "Every spike resource name must start with SPIKE_RESOURCE_PREFIX."
    }
  }
}

output "function_name" {
  value = aws_lambda_function.spike.function_name
}

output "role_name" {
  value = aws_iam_role.spike.name
}
