# What rm-answer-cheaper needs for the FIS Lambda actions (docs.aws.amazon.com/fis/latest/
# userguide/use-lambda-actions.html): the extension layer, read access to the configuration
# folder, and the environment variables. The app module attaches the layer and variables from
# this module's outputs; this module adds the read policy to the function's role.

locals {
  # The public parameter that holds the extension's layer ARN for this architecture
  # (actions-lambda-extension-arns.html). 1.x.x follows the latest 1.x release.
  fis_extension_parameter = "/aws/service/fis/lambda-extension/AWS-FIS-extension-${local.names.LAMBDA_ARCHITECTURE}/1.x.x"
}

# The layer is pinned in contracts/names.json (FIS_EXTENSION_LAYER_ARN, task 4.3's read), so
# no new release changes the cheaper function alone mid-project (design.md section 9). Every
# plan reads SSM's latest ARN, and the check below warns when it has moved on. Nothing else uses
# the read: the outputs carry the pin.
#
# The read is top-level, not scoped inside the check. A scoped read is planned again for every
# apply, so no plan of infra/ would ever say "No changes" (evidence/4.3-fis.md). The cost: if
# the read itself fails, the plan fails rather than warning.
data "aws_ssm_parameter" "fis_extension" {
  name = local.fis_extension_parameter
}

check "fis_extension_layer" {
  assert {
    condition     = data.aws_ssm_parameter.fis_extension.insecure_value == local.names.FIS_EXTENSION_LAYER_ARN
    error_message = "SSM's AWS FIS extension layer is now ${data.aws_ssm_parameter.fis_extension.insecure_value}, but the module pins ${local.names.FIS_EXTENSION_LAYER_ARN}. To move to the new layer, update FIS_EXTENSION_LAYER_ARN in a contract change."
  }
}

# FIS writes the active fault configuration here, and the extension polls it.
resource "aws_s3_bucket" "config" {
  bucket = local.names.FIS_CONFIG_BUCKET
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
  name = local.names.FIS_CONFIG_READ_POLICY
  role = var.cheaper_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowListingConfigLocation"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = [local.config_bucket_arn]
        Condition = { StringLike = { "s3:prefix" = ["${local.names.FIS_CONFIG_PREFIX}/*"] } }
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
