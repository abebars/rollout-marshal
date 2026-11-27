# Review access (design.md section 2): role rm-review-readonly, which goldfish
# reviewers use through the human's AWS CLI profile rm-review. AWS managed
# ReadOnlyAccess, plus a deny on secret values and on the state bucket's
# objects. Owner: integration lead (design.md section 12).

locals {
  review_role_name = "rm-review-readonly"
  state_bucket     = "rm-tfstate-${local.account_id}"

  # The human's IAM Identity Center permission-set role. Its name ends in a
  # suffix that changes if the permission set is provisioned again, so the
  # trust matches it by pattern instead of naming it.
  review_trusted_role_pattern = "arn:aws:iam::${local.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_abisb_IsbAdminsPS_*"
}

data "aws_iam_policy_document" "review_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values   = [local.review_trusted_role_pattern]
    }
  }
}

resource "aws_iam_role" "review" {
  name                 = local.review_role_name
  description          = "Read-only access for goldfish reviewers (design.md section 2)"
  assume_role_policy   = data.aws_iam_policy_document.review_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "review_read_only" {
  role       = aws_iam_role.review.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "review_deny" {
  # Secret values. ReadOnlyAccess grants none of these today; the deny keeps it
  # so if the managed policy changes. AgentCore Identity holds its own copy of
  # the Flagsmith key (design.md section 8), read through the first two.
  statement {
    sid    = "DenySecretValues"
    effect = "Deny"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:BatchGetSecretValue",
      "bedrock-agentcore:GetResourceApiKey",
      "bedrock-agentcore:GetResourceOauth2Token",
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = ["*"]
  }

  # The Terraform state, which holds sensitive values such as incident_email.
  # ReadOnlyAccess grants s3:Get* on every bucket.
  statement {
    sid       = "DenyStateObjects"
    effect    = "Deny"
    actions   = ["s3:GetObject*"]
    resources = ["arn:aws:s3:::${local.state_bucket}/*"]
  }
}

resource "aws_iam_role_policy" "review_deny" {
  name   = "rm-review-deny"
  role   = aws_iam_role.review.id
  policy = data.aws_iam_policy_document.review_deny.json
}

output "review_role_arn" {
  description = "The role behind the human's rm-review profile"
  value       = aws_iam_role.review.arn
}
