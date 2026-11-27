# Schedule rm-tick and its execution role rm-scheduler-role (design.md section 7).

locals {
  tick_schedule_group_arn = "arn:aws:scheduler:${local.region}:${local.account_id}:schedule-group/${local.names.TICK_SCHEDULE_GROUP}"
}

# Confused-deputy guard from the EventBridge Scheduler User Guide: only schedules in this
# account's default group may assume the role.
data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = [local.names.SCHEDULER_SERVICE_PRINCIPAL]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.tick_schedule_group_arn]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = local.names.SCHEDULER_ROLE
  description        = "rm-tick's execution role: it may invoke only rm-invoker."
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "InvokeOnlyTheInvoker"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.invoker.arn, "${aws_lambda_function.invoker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = local.names.TICK_SCHEDULE
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# Every 2 minutes, from a run's launch to its terminal state, rm-tick sends the tick payload to
# rm-invoker. The agent's schedule tool writes back every field GetSchedule returned, changing
# only its state (design.md section 7), so the schedule carries only the fields it needs: no
# description.
resource "aws_scheduler_schedule" "tick" {
  name       = local.names.TICK_SCHEDULE
  group_name = local.names.TICK_SCHEDULE_GROUP

  schedule_expression = "rate(${local.names.TICK_INTERVAL_MINUTES} minutes)"
  state               = "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.invoker.arn
    role_arn = aws_iam_role.scheduler.arn
    # The function receives this input as its event; rm-invoker passes exactly this on.
    input = jsonencode({ action = local.names.ACTION_TICK })
  }

  # Created disabled. Afterwards the agent's schedule tool owns the state: launch enables it,
  # and a terminal state disables it. A later apply leaves that state alone.
  lifecycle {
    ignore_changes = [state]
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
