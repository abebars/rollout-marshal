# State and reports (design.md sections 6 and 10).

# rm-runs: the pointer (pk ACTIVE) and one item per run (pk RUN#<run_id>).
resource "aws_dynamodb_table" "runs" {
  name         = local.names.RUNS_TABLE
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = local.names.PARTITION_KEY

  attribute {
    name = local.names.PARTITION_KEY
    type = "S"
  }
}

# The reports bucket: runs/<run_id>/report.md, written by the agent's report tool.
resource "aws_s3_bucket" "reports" {
  bucket = local.names.REPORTS_BUCKET
}

resource "aws_s3_bucket_public_access_block" "reports" {
  bucket                  = aws_s3_bucket.reports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
