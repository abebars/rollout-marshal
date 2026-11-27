# Terraform root for Rollout Marshal. Owner: integration lead (design.md
# section 12). Module blocks for infra/modules/{app,agent,signals,fis} are
# added here by the lead as each module's task merges. Workstream agents run
# validate and plan only; the lead runs apply.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.74"
    }
  }
}

locals {
  account_id = "818928662830"
  region     = "us-east-1"

  # Every shared name and value (design.md section 1); a recalibrated
  # threshold arrives here as a contract change.
  names = jsondecode(file("${path.module}/../contracts/names.json"))
}

provider "aws" {
  region = local.region

  # Terraform refuses to plan or apply against any other account.
  allowed_account_ids = [local.account_id]

  default_tags {
    tags = {
      project = "rollout-marshal"
    }
  }
}
