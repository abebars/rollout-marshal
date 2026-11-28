# The app module (task 3.5; design.md sections 4, 5, 6 and 11). Owner: workstream B.
# The root pins the AWS provider. The functions' zips come from app/build.sh, which
# `make build` runs before every plan and apply, so this module packages nothing itself.
terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}
