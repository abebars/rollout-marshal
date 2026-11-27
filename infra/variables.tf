variable "invoker_stub" {
  description = "When true, rm-invoker gets env STUB=1 and only logs the payload it would send. The lead sets it to false in task 5.1 (design.md section 7)."
  type        = bool
  default     = true
}

variable "incident_email" {
  description = "The address subscribed to SNS topic rm-incidents (design.md section 8). Sensitive because plans are attached to pull requests in a public repo; set in the git-ignored infra/terraform.tfvars."
  type        = string
  sensitive   = true
}
