# What rm-answer-cheaper needs for the FIS Lambda actions (docs.aws.amazon.com/fis/latest/
# userguide/use-lambda-actions.html): the extension layer, read access to the configuration
# folder, and the environment variables. The app module attaches the layer and variables from
# this module's outputs; this module adds the read policy to the function's role.

locals {
  # The public parameter that holds the extension's layer ARN for this architecture
  # (actions-lambda-extension-arns.html). 1.x.x follows the latest 1.x release.
  fis_extension_parameter = "/aws/service/fis/lambda-extension/AWS-FIS-extension-${local.names.LAMBDA_ARCHITECTURE}/1.x.x"
}

data "aws_ssm_parameter" "fis_extension" {
  name = local.fis_extension_parameter
}

# FIS writes the active fault configuration here, and the extension polls it.
resource "aws_s3_bucket" "config" {
  bucket = var.config_bucket_name
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket = aws_s3_bucket.config.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# The guide's read policy for the extension, on the configuration folder only.
resource "aws_iam_role_policy" "config_read" {
  name = var.config_read_policy_name
  role = var.cheaper_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowListingConfigLocation"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = [local.config_bucket_arn]
        Condition = { StringLike = { "s3:prefix" = ["${var.config_prefix}/*"] } }
      },
      {
        Sid      = "AllowReadingObjectFromConfigLocation"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = [local.config_objects_arn]
      },
    ]
  })
}
