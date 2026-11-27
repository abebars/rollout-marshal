# Alarm rm-cheaper-latency-p95 and rule rm-alarm-to-invoker (design.md sections 5 and 7).

locals {
  # "Variant=cheaper": the dimension's name and value.
  alarm_dimension = split("=", local.names.ALARM_DIMENSION)
}

resource "aws_cloudwatch_metric_alarm" "cheaper_latency" {
  alarm_name        = local.names.ALARM_NAME
  alarm_description = "p95 AnswerLatencyMs for Variant=cheaper above the threshold in 2 of 2 one-minute periods. Rule rm-alarm-to-invoker wakes rm_agent when it enters ALARM."

  namespace   = local.names.CW_NAMESPACE
  metric_name = local.names.ALARM_METRIC
  dimensions = {
    (local.alarm_dimension[0]) = local.alarm_dimension[1]
  }

  extended_statistic  = local.names.ALARM_STATISTIC
  period              = local.names.ALARM_PERIOD_SECONDS
  evaluation_periods  = local.names.ALARM_EVALUATION_PERIODS
  datapoints_to_alarm = local.names.ALARM_DATAPOINTS_TO_ALARM
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alarm_threshold_ms
  treat_missing_data  = local.names.ALARM_TREAT_MISSING_DATA

  # Left unset on purpose. Unit: AWS recommends omitting it, since the wrong unit leaves the
  # alarm in INSUFFICIENT_DATA. EvaluateLowSampleCountPercentile: omitted means "evaluate"
  # (PutMetricAlarm reference), as tasks.md 4.1 expects. No alarm actions: the alarm reaches
  # rm_agent through EventBridge, which receives every state change.
}

resource "aws_cloudwatch_event_rule" "alarm" {
  name        = local.names.ALARM_RULE
  description = "rm-cheaper-latency-p95 entering ALARM invokes rm-invoker."

  # The CloudWatch "Alarm State Change" event (cloudwatch-and-eventbridge.html). rm-invoker
  # checks the same fields again.
  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [local.names.ALARM_NAME]
      state = {
        value = [local.names.ALARM_STATE_ALARM]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "alarm" {
  rule      = aws_cloudwatch_event_rule.alarm.name
  target_id = local.names.INVOKER_FUNCTION
  arn       = aws_lambda_function.invoker.arn
}

# EventBridge invokes a Lambda target through the function's resource-based policy when the
# target has no role (EventBridge User Guide, resource-based policies).
resource "aws_lambda_permission" "alarm_rule" {
  statement_id  = local.names.ALARM_RULE
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.invoker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm.arn
}
