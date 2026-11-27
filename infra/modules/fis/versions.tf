# The FIS module (task 4.3; design.md section 9). Owner: workstream C. The root pins the
# AWS provider.
terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}
