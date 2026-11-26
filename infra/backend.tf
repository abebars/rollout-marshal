# Remote state (design.md section 2). The bucket is created by task 1.0 with
# the AWS CLI, before the first terraform init. Locking uses an S3 lock file,
# so no DynamoDB lock table is needed. Only the integration lead runs apply.
terraform {
  backend "s3" {
    bucket       = "rm-tfstate-818928662830"
    key          = "rollout-marshal/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
