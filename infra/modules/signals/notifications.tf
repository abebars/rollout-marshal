# SNS topic rm-incidents and its one email subscription (design.md section 8). The human
# confirms the subscription from the email AWS sends; until then it is pending.

resource "aws_sns_topic" "incidents" {
  name = local.names.INCIDENTS_TOPIC
}

resource "aws_sns_topic_subscription" "incident_email" {
  topic_arn = aws_sns_topic.incidents.arn
  protocol  = "email"
  endpoint  = var.incident_email
}
