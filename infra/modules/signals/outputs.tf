# For the agent's runtime role (task 2.7): it publishes to the topic, reads and updates the
# schedule, passes the scheduler role, and reads the alarm.

output "incidents_topic_arn" {
  value = aws_sns_topic.incidents.arn
}

output "tick_schedule_arn" {
  value = aws_scheduler_schedule.tick.arn
}

output "scheduler_role_arn" {
  value = aws_iam_role.scheduler.arn
}

output "alarm_arn" {
  value = aws_cloudwatch_metric_alarm.cheaper_latency.arn
}

output "invoker_function_arn" {
  value = aws_lambda_function.invoker.arn
}
