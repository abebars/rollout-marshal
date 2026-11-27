# The signals module (task 4.1; design.md sections 5, 7 and 8). Owner: workstream C.
# The root pins the AWS provider. archive packages rm-invoker's code during plan, so no
# build step runs outside Terraform.
terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }
}
