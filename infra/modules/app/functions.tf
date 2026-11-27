# The four functions (design.md section 5): rm-answer-current and rm-answer-cheaper (one
# handler, deployed twice), rm-router and rm-judge. Each has its own role, whose inline
# policy grants only what its function calls. Role, policy and log-group names follow
# rm-invoker's: <function>-role, <function>-policy, /aws/lambda/<function>.

# Metadata only (ARN and name): rm-router and rm-judge read the value at run time.
data "aws_secretsmanager_secret" "server_key" {
  name = local.names.SECRET_FLAGSMITH_SERVER_KEY
}

# Each geographic inference profile, with the foundation models it routes to.
data "aws_bedrock_inference_profile" "model" {
  for_each             = toset(values(local.bedrock_callers))
  inference_profile_id = each.value
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "${each.value.name}-role"
  description        = "${each.value.name}'s execution role."
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

# Created here, so no role needs logs:CreateLogGroup. No retention is set, as for the group
# Lambda would create itself.
resource "aws_cloudwatch_log_group" "fn" {
  for_each = local.functions
  name     = "/aws/lambda/${each.value.name}"
}

# The Bedrock grants of a function that calls one model, as task 1.2's spike role had them.
data "aws_iam_policy_document" "bedrock" {
  for_each = local.bedrock_callers

  statement {
    sid       = "InvokeItsInferenceProfile"
    actions   = ["bedrock:InvokeModel"]
    resources = [data.aws_bedrock_inference_profile.model[each.value].inference_profile_arn]
  }

  # Its foundation models, only when reached through the profile (Bedrock user guide,
  # geographic cross-Region inference, IAM policy requirements).
  statement {
    sid       = "InvokeItsModelsThroughTheProfile"
    actions   = ["bedrock:InvokeModel"]
    resources = [for model in data.aws_bedrock_inference_profile.model[each.value].models : model.model_arn]

    condition {
      test     = "StringEquals"
      variable = "bedrock:InferenceProfileArn"
      values   = [data.aws_bedrock_inference_profile.model[each.value].inference_profile_arn]
    }
  }

  statement {
    sid       = "InvokeInTheDefaultProject"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.default_project_arn]
  }
}

data "aws_iam_policy_document" "logs" {
  for_each = local.functions

  statement {
    sid       = "WriteItsLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.log_group_arns[each.key]}:*"]
  }
}

# rm-router: the pointer, the SDK key, and the three functions it invokes.
data "aws_iam_policy_document" "router" {
  statement {
    sid       = "ReadThePointer"
    actions   = ["dynamodb:GetItem"]
    resources = [local.runs_table_arn]
  }

  statement {
    sid       = "ReadTheSdkKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.server_key.arn]
  }

  statement {
    sid       = "InvokeTheAnswerFunctionsAndTheJudge"
    actions   = ["lambda:InvokeFunction"]
    resources = [for key in ["current", "cheaper", "judge"] : local.function_arns[key]]
  }
}

# rm-judge: the SDK key, beside its Bedrock grants.
data "aws_iam_policy_document" "judge" {
  statement {
    sid       = "ReadTheSdkKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.server_key.arn]
  }
}

data "aws_iam_policy_document" "fn" {
  for_each = local.functions
  source_policy_documents = compact([
    data.aws_iam_policy_document.logs[each.key].json,
    contains(keys(local.bedrock_callers), each.key) ? data.aws_iam_policy_document.bedrock[each.key].json : "",
    each.key == "router" ? data.aws_iam_policy_document.router.json : "",
    each.key == "judge" ? data.aws_iam_policy_document.judge.json : "",
  ])
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions
  name     = "${each.value.name}-policy"
  role     = aws_iam_role.fn[each.key].id
  policy   = data.aws_iam_policy_document.fn[each.key].json
}

resource "aws_lambda_function" "fn" {
  for_each = local.functions

  function_name    = each.value.name
  role             = aws_iam_role.fn[each.key].arn
  runtime          = "python${local.names.PYTHON_VERSION}"
  architectures    = [local.names.LAMBDA_ARCHITECTURE]
  handler          = each.value.handler
  filename         = each.value.zip
  source_code_hash = filebase64sha256(each.value.zip)
  timeout          = each.value.timeout
  memory_size      = local.memory_mb
  layers           = each.value.layers

  dynamic "environment" {
    for_each = length(each.value.env) > 0 ? [each.value.env] : []
    content {
      variables = environment.value
    }
  }

  logging_config {
    log_format            = each.value.log_format
    application_log_level = each.value.log_level
    system_log_level      = each.value.log_level
    log_group             = aws_cloudwatch_log_group.fn[each.key].name
  }

  # The zip must exist when Terraform plans: `make build` writes it first.
  depends_on = [aws_iam_role_policy.fn]
}
