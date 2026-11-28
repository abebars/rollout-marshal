# Template rm-delay-cheaper and the role FIS runs it with (design.md section 9).

# Only FIS experiments in this account may assume the role (the FIS guide's confused-deputy
# conditions, docs.aws.amazon.com/fis/latest/userguide/getting-started-iam-service-role.html).
resource "aws_iam_role" "experiment" {
  name        = local.names.FIS_ROLE
  description = "The role AWS FIS assumes to run rm-delay-cheaper."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "fis.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:fis:${local.region}:${local.account_id}:experiment/*" }
        }
      },
    ]
  })
}

# The guide's experiment policy for the Lambda actions, which matches the action's listed
# permissions (s3:PutObject, s3:DeleteObject, lambda:GetFunction, tag:GetResources). The writes
# are limited to the configuration folder.
resource "aws_iam_role_policy" "experiment" {
  name = local.names.FIS_ROLE_POLICY
  role = aws_iam_role.experiment.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowFisToWriteAndDeleteFaultConfigurations"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject"]
        Resource = local.config_objects_arn
      },
      {
        Sid      = "AllowFisToInspectLambdaFunctions"
        Effect   = "Allow"
        Action   = ["lambda:GetFunction"]
        Resource = "*"
      },
      {
        Sid      = "AllowFisToDoTagLookups"
        Effect   = "Allow"
        Action   = ["tag:GetResources"]
        Resource = "*"
      },
    ]
  })
}

# aws:lambda:invocation-add-delay on rm-answer-cheaper: 100 percent of invocations, 10000 ms,
# 6 minutes. Parameter and target names are from `aws fis get-action`. There is no stop
# condition: the fault ends by its duration, and no agent stops an experiment. FIS templates
# have no name field, so the Name tag carries rm-delay-cheaper and scripts/inject_fault.sh finds
# the template by it.
resource "aws_fis_experiment_template" "delay_cheaper" {
  description = "Adds ${local.names.FIS_DELAY_MS} ms to ${local.names.FIS_INVOCATION_PERCENTAGE} percent of ${var.cheaper_function_name}'s invocations for ${local.names.FIS_DURATION_MINUTES} minutes."
  role_arn    = aws_iam_role.experiment.arn

  action {
    name      = "add-delay"
    action_id = local.names.FIS_ACTION

    target {
      key   = "Functions"
      value = "cheaper-function"
    }

    parameter {
      key   = "duration"
      value = "PT${local.names.FIS_DURATION_MINUTES}M"
    }

    parameter {
      key   = "invocationPercentage"
      value = tostring(local.names.FIS_INVOCATION_PERCENTAGE)
    }

    parameter {
      key   = "startupDelayMilliseconds"
      value = tostring(local.names.FIS_DELAY_MS)
    }
  }

  target {
    name           = "cheaper-function"
    resource_type  = "aws:lambda:function"
    resource_arns  = [local.cheaper_function_arn]
    selection_mode = "ALL"
  }

  stop_condition {
    source = "none"
  }

  tags = {
    Name = local.names.FIS_TEMPLATE
  }

  depends_on = [aws_iam_role_policy.experiment]
}
